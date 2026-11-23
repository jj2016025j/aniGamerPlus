"""跨平台環境自動偵測模組（支援原始碼執行、PyInstaller .exe/.app 封裝與獨立倉庫模式）。"""

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EnvironmentInfo:
    raw_os: str
    os_name: str
    is_frozen: bool
    root_dir: Path
    bundle_dir: Path
    anigamer_dir: Path
    anigamer_script: Path | None
    anigamer_is_exe: bool
    python_executable: Path
    bangumi_dir: Path
    db_path: Path
    cookie_path: Path
    has_cookie: bool
    has_ffmpeg: bool
    has_ffprobe: bool


def configure_console_encoding() -> None:
    """修正 Windows 終端機輸出日文或特殊符號時可能發生的編碼錯誤。"""
    if sys.platform == "win32":
        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            if stream and hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass


def resolve_runtime_roots(explicit_root: Path | None = None) -> tuple[bool, Path, Path]:
    """
    解析執行根目錄 (root_dir) 與內嵌資源目錄 (bundle_dir)：
    - 原始碼模式：root_dir = 專案根目錄；bundle_dir = 專案根目錄
    - PyInstaller 打包模式：
      * Windows: root_dir = AniManager.exe 所在資料夾
      * macOS .app: 若執行檔位於 Foo.app/Contents/MacOS/Foo，root_dir 指向 Foo.app 所在的外部資料夾，方便與 aniGamerPlus 同層放置
    """
    is_frozen = bool(getattr(sys, "frozen", False))
    if is_frozen:
        exe_path = Path(sys.executable).resolve()
        bundle_dir = Path(getattr(sys, "_MEIPASS", exe_path.parent)).resolve()
        if explicit_root is not None:
            return True, explicit_root.resolve(), bundle_dir
        exe_dir = exe_path.parent
        # macOS .app bundle: .../AniManager.app/Contents/MacOS
        if exe_dir.name == "MacOS" and exe_dir.parent.name == "Contents" and exe_dir.parent.parent.suffix == ".app":
            return True, exe_dir.parent.parent.parent.resolve(), bundle_dir
        return True, exe_dir.resolve(), bundle_dir

    pkg_root = Path(__file__).resolve().parent.parent
    root_dir = (explicit_root or pkg_root).resolve()
    return False, root_dir, pkg_root


def resolve_anigamer_location(root_dir: Path, custom_anigamer_dir: str = "") -> tuple[Path, Path | None, bool]:
    """
    自動尋找 aniGamerPlus 核心（支援 aniGamerPlus.py 與官方 Windows 版 aniGamerPlus.exe）。
    回傳 (anigamer_dir, core_path, is_exe)。
    """
    candidates: list[Path] = []
    if custom_anigamer_dir.strip():
        p = Path(custom_anigamer_dir).expanduser()
        candidates.append(p if p.is_absolute() else (root_dir / p).resolve())

    candidates.extend(
        [
            root_dir,
            root_dir / "vendor" / "aniGamerPlus",
            root_dir / "aniGamerPlus",
            root_dir.parent / "aniGamerPlus",
        ]
    )

    core_names = ("aniGamerPlus.exe", "aniGamerPlus.py") if platform.system() == "Windows" else ("aniGamerPlus.py", "aniGamerPlus.exe")

    for folder in candidates:
        if not folder.is_dir():
            continue
        for cname in core_names:
            target = folder / cname
            if target.is_file():
                return folder.resolve(), target.resolve(), cname.endswith(".exe")

    fallback_dir = candidates[0] if custom_anigamer_dir.strip() else root_dir
    return fallback_dir.resolve(), None, False


def resolve_python_executable(root_dir: Path, anigamer_dir: Path, is_frozen: bool) -> Path:
    """優先尋找 .venv Python；若本身已是打包執行檔，仍優先尋找外部 .venv 供執行 aniGamerPlus.py。"""
    search_roots = [anigamer_dir, root_dir]
    rel_candidates = [
        Path(".venv/Scripts/python.exe"),
        Path(".venv/bin/python"),
        Path("venv/Scripts/python.exe"),
        Path("venv/bin/python"),
    ]
    for base in search_roots:
        for rel in rel_candidates:
            candidate = base / rel
            if candidate.is_file():
                return candidate
    if not is_frozen:
        return Path(sys.executable)
    for py_cmd in ("python3", "python"):
        found = shutil.which(py_cmd)
        if found:
            return Path(found)
    return Path(sys.executable)


def detect_environment(
    root_dir: Path | None = None,
    custom_bangumi_dir: str | None = None,
    custom_anigamer_dir: str = "",
) -> EnvironmentInfo:
    is_frozen, resolved_root, bundle_dir = resolve_runtime_roots(root_dir)
    raw_os = platform.system()
    os_map = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}
    os_name = os_map.get(raw_os, raw_os)

    anigamer_dir, anigamer_script, anigamer_is_exe = resolve_anigamer_location(resolved_root, custom_anigamer_dir)

    if custom_bangumi_dir:
        bangumi_dir = Path(custom_bangumi_dir).expanduser()
        if not bangumi_dir.is_absolute():
            bangumi_dir = (anigamer_dir / bangumi_dir).resolve()
    else:
        bangumi_dir = anigamer_dir / "bangumi"

    db_path = anigamer_dir / "aniGamer.db"
    cookie_path = anigamer_dir / "cookie.txt"
    has_cookie = cookie_path.is_file() and cookie_path.stat().st_size > 10

    has_ffmpeg = shutil.which("ffmpeg") is not None or (anigamer_dir / "ffmpeg.exe").is_file() or (resolved_root / "ffmpeg.exe").is_file()
    has_ffprobe = shutil.which("ffprobe") is not None or (anigamer_dir / "ffprobe.exe").is_file() or (resolved_root / "ffprobe.exe").is_file()

    return EnvironmentInfo(
        raw_os=raw_os,
        os_name=os_name,
        is_frozen=is_frozen,
        root_dir=resolved_root,
        bundle_dir=bundle_dir,
        anigamer_dir=anigamer_dir,
        anigamer_script=anigamer_script,
        anigamer_is_exe=anigamer_is_exe,
        python_executable=resolve_python_executable(resolved_root, anigamer_dir, is_frozen),
        bangumi_dir=bangumi_dir,
        db_path=db_path,
        cookie_path=cookie_path,
        has_cookie=has_cookie,
        has_ffmpeg=has_ffmpeg,
        has_ffprobe=has_ffprobe,
    )
