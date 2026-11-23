"""私密設定與追番資料單檔加密備份／跨機還原模組（.anibak）。"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config_store import ManagerConfig
from .env_detector import EnvironmentInfo
from .watchlist_store import AnimeWatchItem, WatchlistStore, parse_watch_items

MAGIC_HEADER = b"ANIBAK01"
MODE_PLAIN = 0
MODE_AES_GCM = 1
MODE_STDLIB_CTR = 2
PBKDF2_ITERATIONS = 200_000


@dataclass
class BackupInspectInfo:
    valid: bool
    encrypted: bool = False
    cipher_name: str = ""
    reason: str = ""


@dataclass
class BackupExportResult:
    success: bool
    output_path: Path | None = None
    included_files: list[str] = field(default_factory=list)
    encrypted: bool = False
    cipher_name: str = ""
    size_bytes: int = 0
    message: str = ""


@dataclass
class BackupImportResult:
    success: bool
    mode: str = ""  # "full" | "merge_watchlist"
    restored_files: list[str] = field(default_factory=list)
    merged_count: int = 0
    path_adjustments: list[str] = field(default_factory=list)
    snapshot_dir: Path | None = None
    source_os: str = ""
    exported_at: str = ""
    message: str = ""


def clean_dragged_path(raw_input: str) -> Path:
    """清理使用者直接把檔案拖曳進終端機時產生的引號與反斜線跳脫空白。"""
    s = raw_input.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1]
    if os.name != "nt":
        s = re.sub(r"\\([ \t'\"()&])", r"\1", s)
    return Path(s).expanduser().resolve()


def _detect_aes_backend() -> str:
    try:
        from Crypto.Cipher import AES  # type: ignore  # noqa: F401
        return "pycryptodome"
    except ImportError:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore  # noqa: F401
        return "cryptography"
    except ImportError:
        pass
    return "stdlib"


def _derive_key(password: str, salt: bytes, length: int = 32) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, dklen=length)


def _aes_gcm_encrypt(plaintext: bytes, key: bytes, nonce: bytes) -> tuple[bytes, bytes]:
    """支援 PyCryptodome 與 cryptography 雙引擎互通之標準 AES-256-GCM 加密。"""
    backend = _detect_aes_backend()
    if backend == "pycryptodome":
        from Crypto.Cipher import AES  # type: ignore

        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        return ciphertext, tag
    if backend == "cryptography":
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore

        ct_tag = AESGCM(key).encrypt(nonce, plaintext, None)
        return ct_tag[:-16], ct_tag[-16:]
    raise RuntimeError("No AES-GCM backend available")


def _aes_gcm_decrypt(ciphertext: bytes, tag: bytes, key: bytes, nonce: bytes) -> bytes:
    backend = _detect_aes_backend()
    if backend == "pycryptodome":
        from Crypto.Cipher import AES  # type: ignore

        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag)
    if backend == "cryptography":
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore

        return AESGCM(key).decrypt(nonce, ciphertext + tag, None)
    raise RuntimeError("此環境缺少 AES 解密套件（請安裝 pycryptodome 或 cryptography）。")


def _stdlib_ctr_xor(data: bytes, key: bytes, nonce: bytes) -> bytes:
    out = bytearray(len(data))
    counter = 0
    pos = 0
    total = len(data)
    while pos < total:
        block = hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        chunk = data[pos : pos + 32]
        for i, b in enumerate(chunk):
            out[pos + i] = b ^ block[i]
        pos += len(chunk)
        counter += 1
    return bytes(out)


def encrypt_payload(zip_bytes: bytes, password: str) -> tuple[bytes, str]:
    """將 ZIP 位元組封裝為 .anibak 格式（空密碼為明文；有密碼優先採用 AES-256-GCM）。"""
    if not password:
        return MAGIC_HEADER + bytes([MODE_PLAIN]) + zip_bytes, "未加密"

    backend = _detect_aes_backend()
    salt = secrets.token_bytes(16)
    if backend in ("pycryptodome", "cryptography"):
        key = _derive_key(password, salt, 32)
        nonce = secrets.token_bytes(12)
        ct, tag = _aes_gcm_encrypt(zip_bytes, key, nonce)
        blob = MAGIC_HEADER + bytes([MODE_AES_GCM]) + salt + nonce + tag + ct
        return blob, "AES-256-GCM"

    # 純標準庫備援：PBKDF2 + SHA256-CTR + Encrypt-then-MAC (HMAC-SHA256)
    key_mat = _derive_key(password, salt, 64)
    enc_key, mac_key = key_mat[:32], key_mat[32:]
    nonce = secrets.token_bytes(16)
    ct = _stdlib_ctr_xor(zip_bytes, enc_key, nonce)
    mac = hmac.new(mac_key, MAGIC_HEADER + bytes([MODE_STDLIB_CTR]) + salt + nonce + ct, hashlib.sha256).digest()
    blob = MAGIC_HEADER + bytes([MODE_STDLIB_CTR]) + salt + nonce + mac + ct
    return blob, "SHA256-CTR+HMAC"


def inspect_backup_file(file_path: Path) -> BackupInspectInfo:
    if not file_path.is_file():
        return BackupInspectInfo(valid=False, reason=f"找不到檔案：{file_path}")
    try:
        header = file_path.read_bytes()[:9]
    except Exception as exc:
        return BackupInspectInfo(valid=False, reason=f"無法讀取檔案：{exc}")

    if len(header) < 9 or header[:8] != MAGIC_HEADER:
        return BackupInspectInfo(valid=False, reason="檔案格式不符（不是有效的 .anibak 備份檔）。")

    mode = header[8]
    if mode == MODE_PLAIN:
        return BackupInspectInfo(valid=True, encrypted=False, cipher_name="未加密")
    if mode == MODE_AES_GCM:
        return BackupInspectInfo(valid=True, encrypted=True, cipher_name="AES-256-GCM")
    if mode == MODE_STDLIB_CTR:
        return BackupInspectInfo(valid=True, encrypted=True, cipher_name="SHA256-CTR+HMAC")
    return BackupInspectInfo(valid=False, reason=f"不支援的備份加密模式 ({mode})。")


def decrypt_payload(blob: bytes, password: str = "") -> bytes:
    if len(blob) < 9 or blob[:8] != MAGIC_HEADER:
        raise ValueError("不是有效的 .anibak 備份檔。")

    mode = blob[8]
    body = blob[9:]

    if mode == MODE_PLAIN:
        return body

    if not password:
        raise ValueError("此備份檔已加密，請輸入密碼。")

    if mode == MODE_AES_GCM:
        if len(body) < 16 + 12 + 16:
            raise ValueError("備份檔內容已損毀。")
        salt, nonce, tag, ct = body[:16], body[16:28], body[28:44], body[44:]
        key = _derive_key(password, salt, 32)
        try:
            return _aes_gcm_decrypt(ct, tag, key, nonce)
        except Exception as exc:
            raise ValueError("密碼錯誤或備份檔已損毀。") from exc

    if mode == MODE_STDLIB_CTR:
        if len(body) < 16 + 16 + 32:
            raise ValueError("備份檔內容已損毀。")
        salt, nonce, mac, ct = body[:16], body[16:32], body[32:64], body[64:]
        key_mat = _derive_key(password, salt, 64)
        enc_key, mac_key = key_mat[:32], key_mat[32:]
        expected = hmac.new(mac_key, MAGIC_HEADER + bytes([MODE_STDLIB_CTR]) + salt + nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            raise ValueError("密碼錯誤或備份檔已損毀。")
        return _stdlib_ctr_xor(ct, enc_key, nonce)

    raise ValueError(f"不支援的備份模式：{mode}")


def _is_foreign_os_path(path_str: str, current_raw_os: str) -> bool:
    """偵測路徑字串是否屬於不同作業系統的絕對路徑（例如把 macOS /Users/... 匯入 Windows）。"""
    p = path_str.strip()
    if not p:
        return False
    is_win_abs = bool(re.match(r"^[A-Za-z]:[\\/]|^\\\\", p))
    is_posix_abs = p.startswith("/")
    if current_raw_os == "Windows" and is_posix_abs:
        return True
    if current_raw_os != "Windows" and is_win_abs:
        return True
    # 若為同系 OS 的絕對路徑但該目錄在此機不存在，也視情況校正
    if is_win_abs or is_posix_abs:
        try:
            if not Path(p).exists():
                return True
        except Exception:
            return True
    return False


def export_backup(
    env: EnvironmentInfo,
    config: ManagerConfig,
    watchlist_path: Path,
    include_upstream_config: bool = True,
    include_history: bool = False,
    password: str = "",
) -> BackupExportResult:
    """打包核心私密檔與選配檔至 exports/*.anibak。"""
    candidates: list[tuple[str, Path]] = [
        ("manager_config.json", env.root_dir / "manager_config.json"),
        ("watchlist.json", watchlist_path),
        ("cookie.txt", env.cookie_path),
    ]
    if include_upstream_config:
        candidates.append(("upstream_config.json", env.anigamer_dir / "config.json"))
    if include_history:
        candidates.append(("download_history.json", env.root_dir / "download_history.json"))

    included: list[str] = []
    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc_name, src_path in candidates:
            if src_path.is_file():
                zf.write(src_path, arcname=arc_name)
                included.append(arc_name)

        if not included:
            return BackupExportResult(success=False, message="找不到任何可匯出的設定或資料檔案。")

        manifest = {
            "format_version": 1,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_os": env.raw_os,
            "included_files": included,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    encrypted_blob, cipher_name = encrypt_payload(mem_zip.getvalue(), password)

    exports_dir = env.root_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "enc" if password else "plain"
    out_path = exports_dir / f"anigamer_backup_{stamp}_{suffix}.anibak"
    out_path.write_bytes(encrypted_blob)

    return BackupExportResult(
        success=True,
        output_path=out_path,
        included_files=included,
        encrypted=bool(password),
        cipher_name=cipher_name,
        size_bytes=len(encrypted_blob),
        message="匯出完成",
    )


def _create_pre_import_snapshot(env: EnvironmentInfo, watchlist_path: Path) -> Path | None:
    targets = [
        ("manager_config.json", env.root_dir / "manager_config.json"),
        ("watchlist.json", watchlist_path),
        ("cookie.txt", env.cookie_path),
        ("upstream_config.json", env.anigamer_dir / "config.json"),
        ("download_history.json", env.root_dir / "download_history.json"),
    ]
    existing = [(name, p) for name, p in targets if p.is_file()]
    if not existing:
        return None
    snap_dir = env.root_dir / "exports" / f".pre_import_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    snap_dir.mkdir(parents=True, exist_ok=True)
    for name, p in existing:
        shutil.copy2(p, snap_dir / name)
    return snap_dir


def import_backup(
    file_path: Path,
    env: EnvironmentInfo,
    watchlist_path: Path,
    watchlist_store: WatchlistStore,
    current_watchlist: list[AnimeWatchItem],
    password: str = "",
    mode: str = "full",  # "full" | "merge_watchlist"
) -> BackupImportResult:
    """從 .anibak 還原設定（支援完整覆蓋還原＋跨系統路徑自動校正，或僅合併追番清單）。"""
    try:
        blob = file_path.read_bytes()
        zip_bytes = decrypt_payload(blob, password)
    except Exception as exc:
        return BackupImportResult(success=False, mode=mode, message=str(exc))

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    except zipfile.BadZipFile:
        return BackupImportResult(success=False, mode=mode, message="解密後的備份結構異常，無法解壓。")

    with zf:
        names = set(zf.namelist())
        manifest = {}
        if "manifest.json" in names:
            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            except Exception:
                manifest = {}

        source_os = str(manifest.get("source_os", "未知"))
        exported_at = str(manifest.get("exported_at", "未知"))

        if mode == "merge_watchlist":
            if "watchlist.json" not in names:
                return BackupImportResult(success=False, mode=mode, message="此備份檔中不含 watchlist.json。")
            raw_wl = json.loads(zf.read("watchlist.json").decode("utf-8"))
            raw_items = raw_wl.get("items", []) if isinstance(raw_wl, dict) else (raw_wl if isinstance(raw_wl, list) else [])
            incoming = parse_watch_items(raw_items)
            existing_sns = {w.sn for w in current_watchlist}
            added = [w for w in incoming if w.sn not in existing_sns]
            if added:
                current_watchlist.extend(added)
                watchlist_store.save(current_watchlist)
            return BackupImportResult(
                success=True,
                mode=mode,
                restored_files=["watchlist.json (合併)"],
                merged_count=len(added),
                source_os=source_os,
                exported_at=exported_at,
                message=f"已合併 {len(added)} 部新作品至追番清單（略過 {len(incoming) - len(added)} 部已存在作品）。",
            )

        # mode == "full"：先留本機快照，再覆蓋還原
        snap_dir = _create_pre_import_snapshot(env, watchlist_path)
        restored: list[str] = []
        path_adjustments: list[str] = []

        if "manager_config.json" in names:
            cfg_data = json.loads(zf.read("manager_config.json").decode("utf-8"))
            if isinstance(cfg_data, dict):
                if _is_foreign_os_path(str(cfg_data.get("anigamer_dir", "")), env.raw_os):
                    old_val = cfg_data["anigamer_dir"]
                    cfg_data["anigamer_dir"] = ""
                    path_adjustments.append(f"anigamer_dir 已由跨機路徑 ({old_val}) 重置為自動偵測")
                if _is_foreign_os_path(str(cfg_data.get("bangumi_dir", "")), env.raw_os):
                    old_val = cfg_data["bangumi_dir"]
                    cfg_data["bangumi_dir"] = "bangumi"
                    path_adjustments.append(f"bangumi_dir 已由跨機路徑 ({old_val}) 重置為預設相對目錄 bangumi")
            (env.root_dir / "manager_config.json").write_text(
                json.dumps(cfg_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            restored.append("manager_config.json")

        if "watchlist.json" in names:
            watchlist_path.parent.mkdir(parents=True, exist_ok=True)
            watchlist_path.write_bytes(zf.read("watchlist.json"))
            restored.append(watchlist_path.name)

        if "cookie.txt" in names:
            env.cookie_path.parent.mkdir(parents=True, exist_ok=True)
            env.cookie_path.write_bytes(zf.read("cookie.txt"))
            restored.append("cookie.txt")

        if "upstream_config.json" in names:
            target_upstream = env.anigamer_dir / "config.json"
            target_upstream.parent.mkdir(parents=True, exist_ok=True)
            target_upstream.write_bytes(zf.read("upstream_config.json"))
            restored.append("config.json (原專案)")

        if "download_history.json" in names:
            (env.root_dir / "download_history.json").write_bytes(zf.read("download_history.json"))
            restored.append("download_history.json")

        return BackupImportResult(
            success=True,
            mode=mode,
            restored_files=restored,
            path_adjustments=path_adjustments,
            snapshot_dir=snap_dir,
            source_os=source_os,
            exported_at=exported_at,
            message="完整還原完成",
        )
