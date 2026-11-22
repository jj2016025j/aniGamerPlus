"""巴哈姆特 VIP Cookie 驗證、剪貼簿寫入、重讀與 Playwright 自動抓取模組。"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

COOKIE_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

# 巴哈姆特登入憑證關鍵欄位（BAHARUNE / BAHAENUR 多為 HttpOnly）
BAHA_AUTH_KEYS = {"BAHAID", "BAHARUNE", "BAHAENUR"}
BAHA_KNOWN_KEYS = {
    "BAHAID",
    "BAHARUNE",
    "BAHAENUR",
    "BAHALV",
    "BAHANICK",
    "MB_BAHANICK",
    "ANIME_SIGN",
    "NOLOGINUSER",
    "CKBAHAAD",
}
NON_ASCII_STRIP_KEYS = {"BAHANICK", "MB_BAHANICK"}


@dataclass
class CookieValidationResult:
    valid: bool
    is_logged_in: bool
    cleaned_cookie: str = ""
    baha_id_masked: str = ""
    pair_count: int = 0
    matched_auth_keys: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class CookieOnlineStatus:
    checked: bool
    status: str  # "vip" | "logged_in" | "guest" | "blocked" | "error"
    detail: str


def mask_account(value: str) -> str:
    """遮罩帳號 ID，避免終端畫面暴露完整帳號。"""
    v = value.strip()
    if len(v) <= 2:
        return "*" * len(v)
    if len(v) <= 5:
        return v[0] + "*" * (len(v) - 2) + v[-1]
    return v[:2] + "*" * (len(v) - 4) + v[-2:]


def read_clipboard(raw_os: str) -> tuple[bool, str]:
    """跨平台讀取系統剪貼簿，避開終端機單行 1024 bytes 輸入上限。"""
    cmds: list[list[str]] = []
    if raw_os == "Darwin":
        cmds.append(["pbpaste"])
    elif raw_os == "Windows":
        cmds.append(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"])
    else:
        for tool, args in (
            ("wl-paste", ["wl-paste", "--no-newline"]),
            ("xclip", ["xclip", "-selection", "clipboard", "-o"]),
            ("xsel", ["xsel", "--clipboard", "--output"]),
        ):
            if shutil.which(tool):
                cmds.append(args)

    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
            if proc.returncode == 0:
                return True, proc.stdout
        except Exception:
            continue

    return False, "無法自動讀取系統剪貼簿。"


def open_cookie_in_editor(cookie_path: Path, raw_os: str) -> bool:
    """使用系統文字編輯器開啟 cookie.txt。"""
    try:
        if not cookie_path.exists():
            cookie_path.write_text("", encoding="utf-8")
        if raw_os == "Darwin":
            subprocess.run(["open", "-e", str(cookie_path)], check=False)
        elif raw_os == "Windows":
            subprocess.Popen(["notepad.exe", str(cookie_path)])
        else:
            subprocess.run(["xdg-open", str(cookie_path)], check=False)
        return True
    except Exception:
        return False


def validate_and_sanitize_cookie(raw_text: str) -> CookieValidationResult:
    """嚴格驗證並清洗 Cookie 字串，避免複製到網址、程式碼或空字串時誤覆蓋舊 cookie.txt。"""
    if not raw_text or not raw_text.strip():
        return CookieValidationResult(valid=False, is_logged_in=False, reason="內容為空（尚未複製內容或檔案為空）。")

    stripped = raw_text.strip()

    # 排除明顯不是 Cookie 的剪貼簿內容（網址、JSON、HTML、指令）
    lower_start = stripped.lower()
    if lower_start.startswith(("http://", "https://", "{", "[", "<", "git ", "python", "pip ", "brew ")):
        return CookieValidationResult(
            valid=False,
            is_logged_in=False,
            reason="內容看起來像是網址、程式碼或指令，而不是 Cookie 字串，已拒絕寫入。",
        )

    # 整理換行與開頭的 cookie: 前綴
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) > 1:
        # 若不小心多選到其他 HTTP Header 行，只嘗試抽出 Cookie 行
        cookie_line = next((ln for ln in lines if ln.lower().startswith("cookie:")), None)
        joined = cookie_line if cookie_line else "; ".join(lines)
    else:
        joined = lines[0]

    if joined.lower().startswith("cookie:"):
        joined = joined.split(":", 1)[1].strip()

    if len(joined) < 25 or "=" not in joined:
        return CookieValidationResult(
            valid=False,
            is_logged_in=False,
            reason="字串長度過短或缺少 key=value 結構，不是有效的 Cookie。",
        )

    raw_parts = [part.strip() for part in joined.split(";") if part.strip()]
    valid_pairs: list[tuple[str, str]] = []
    warnings: list[str] = []

    for part in raw_parts:
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        key = k.strip()
        val = v.strip()
        if not key or not COOKIE_KEY_RE.match(key):
            continue
        # 修正 aniGamerPlus 已知坑：中文暱稱含非 ASCII 字元可能導致 HTTP 標頭編碼異常
        if key.upper() in NON_ASCII_STRIP_KEYS and not val.isascii():
            warnings.append(f"已自動移除含非 ASCII 字元的 {key}（避免 aniGamerPlus 標頭編碼失敗）")
            continue
        if not val.isascii():
            warnings.append(f"已略過含非 ASCII 字元的欄位：{key}")
            continue
        valid_pairs.append((key, val))

    if len(valid_pairs) < 2:
        return CookieValidationResult(
            valid=False,
            is_logged_in=False,
            reason="解析出的有效 Cookie 欄位少於 2 組，疑似複製到其他文字，已拒絕寫入。",
        )

    key_map = {k.upper(): v for k, v in valid_pairs}
    matched_known = [k for k in key_map if k in BAHA_KNOWN_KEYS]
    if not matched_known:
        return CookieValidationResult(
            valid=False,
            is_logged_in=False,
            reason="未偵測到任何巴哈姆特相關欄位（如 BAHAID / BAHARUNE / BAHAENUR），疑似複製到其他網站或一般文字，已保留原檔案不覆蓋。",
        )

    matched_auth = sorted(k for k in BAHA_AUTH_KEYS if k in key_map and key_map[k])
    has_id = "BAHAID" in matched_auth
    has_token = ("BAHARUNE" in matched_auth) or ("BAHAENUR" in matched_auth)
    is_logged_in = has_id and has_token

    if not is_logged_in:
        if has_id and not has_token:
            return CookieValidationResult(
                valid=False,
                is_logged_in=False,
                matched_auth_keys=matched_auth,
                reason="雖偵測到 BAHAID，但缺少 HttpOnly 驗證欄位（BAHARUNE / BAHAENUR）。請確認是從 DevTools -> Network -> Request Headers 複製，而非 Console 的 document.cookie。",
            )
        return CookieValidationResult(
            valid=False,
            is_logged_in=False,
            matched_auth_keys=matched_auth,
            reason="這是「未登入訪客」的巴哈 Cookie（缺少 BAHAID 與登入憑證），已拒絕寫入以避免覆蓋原設定。",
        )

    cleaned = "; ".join(f"{k}={v}" for k, v in valid_pairs)
    baha_id_masked = mask_account(key_map.get("BAHAID", ""))

    return CookieValidationResult(
        valid=True,
        is_logged_in=True,
        cleaned_cookie=cleaned,
        baha_id_masked=baha_id_masked,
        pair_count=len(valid_pairs),
        matched_auth_keys=matched_auth,
        warnings=warnings,
        reason="驗證通過",
    )


def inspect_cookie_file(cookie_path: Path) -> CookieValidationResult:
    """重新讀取並驗證現有 cookie.txt。"""
    if not cookie_path.is_file():
        return CookieValidationResult(valid=False, is_logged_in=False, reason=f"找不到檔案：{cookie_path}")
    raw = cookie_path.read_text(encoding="utf-8", errors="ignore")
    return validate_and_sanitize_cookie(raw)


def verify_cookie_online(cookie_str: str, timeout: int = 10) -> CookieOnlineStatus:
    """輕量發送請求至動畫瘋首頁，確認 Cookie 是否被伺服器承認為登入 / VIP 狀態。"""
    url = "https://ani.gamer.com.tw/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Cookie": cookie_str,
        "Referer": "https://ani.gamer.com.tw/",
    }
    try:
        try:
            import requests  # type: ignore

            resp = requests.get(url, headers=headers, timeout=timeout)
            status_code = resp.status_code
            body = resp.text
        except ImportError:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status_code = getattr(r, "status", 200)
                body = r.read().decode("utf-8", errors="replace")

        if status_code == 403:
            return CookieOnlineStatus(
                checked=False,
                status="blocked",
                detail="線上驗證遭 Cloudflare 攔截 (HTTP 403)，但本地 Cookie 格式正確，將於實際下載時由 aniGamerPlus 驗證。",
            )
        if status_code != 200:
            return CookieOnlineStatus(checked=False, status="error", detail=f"伺服器回應 HTTP {status_code}")

        # 檢查動畫瘋頁面的登入與付費狀態特徵
        has_logout = ("logout" in body.lower()) or ("登出" in body)
        is_vip_hint = bool(
            re.search(r"vip\s*[:=]\s*(?:true|1)|is_vip[\"']?\s*[:=]\s*(?:true|1)|付費會員|動畫瘋\s*VIP", body, re.IGNORECASE)
        )
        if is_vip_hint and has_logout:
            return CookieOnlineStatus(checked=True, status="vip", detail="線上驗證成功：已登入且偵測到 VIP 狀態。")
        if has_logout:
            return CookieOnlineStatus(
                checked=True,
                status="logged_in",
                detail="線上驗證成功：巴哈帳號已登入（首頁未直接顯示 VIP 標記，實際畫質權限將於下載時確認）。",
            )
        return CookieOnlineStatus(
            checked=True,
            status="guest",
            detail="線上回應顯示為未登入狀態（Cookie 可能已過期或剛在瀏覽器按了登出）。",
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return CookieOnlineStatus(
                checked=False,
                status="blocked",
                detail="線上驗證遭 Cloudflare 攔截 (HTTP 403)，但本地 Cookie 格式正確，將於實際下載時由 aniGamerPlus 驗證。",
            )
        return CookieOnlineStatus(checked=False, status="error", detail=f"HTTP 錯誤 ({exc.code})")
    except Exception as exc:
        return CookieOnlineStatus(checked=False, status="error", detail=f"線上檢查失敗：{exc}")


def write_cookie_file(cookie_path: Path, cleaned_cookie: str) -> None:
    cookie_path.write_text(cleaned_cookie.strip() + "\n", encoding="utf-8")


def capture_cookie_via_playwright(cookie_path: Path, timeout_seconds: int = 180) -> tuple[bool, str, CookieValidationResult | None]:
    """
    半自動腳本抓取：開啟獨立無痕視窗讓使用者登入巴哈姆特，
    偵測登入完成後自動擷取含 HttpOnly 的完整 Cookie 並關閉視窗（不按登出，保留 Session）。
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return (
            False,
            "目前環境尚未安裝 Playwright。若要啟用自動抓 Cookie 功能，請在終端執行：\n"
            "  pip install playwright\n"
            "  playwright install chromium",
            None,
        )

    captured_str = ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(locale="zh-TW")
            page = context.new_page()
            page.goto("https://user.gamer.com.tw/login.php", wait_until="domcontentloaded")

            deadline = time.time() + timeout_seconds
            logged_in = False
            while time.time() < deadline:
                cookies = context.cookies(["https://ani.gamer.com.tw", "https://gamer.com.tw", "https://user.gamer.com.tw"])
                names = {c["name"].upper() for c in cookies if c.get("value")}
                if "BAHAID" in names and ("BAHARUNE" in names or "BAHAENUR" in names):
                    logged_in = True
                    break
                time.sleep(1.5)

            if not logged_in:
                browser.close()
                return False, f"超過 {timeout_seconds} 秒未偵測到完成登入，已自動取消。", None

            # 順便造訪動畫瘋首頁，補齊動畫瘋網域專屬 Cookie（如 ANIME_SIGN）
            page.goto("https://ani.gamer.com.tw/", wait_until="domcontentloaded")
            time.sleep(2.0)

            all_cookies = context.cookies(["https://ani.gamer.com.tw", "https://gamer.com.tw"])
            # 依名稱去重並組裝成標準 Cookie 標頭
            dedup: dict[str, str] = {}
            for c in all_cookies:
                name = str(c.get("name", "")).strip()
                val = str(c.get("value", "")).strip()
                if name and val:
                    dedup[name] = val
            captured_str = "; ".join(f"{k}={v}" for k, v in dedup.items())

            # 直接關掉獨立瀏覽器（不按登出），保留伺服器端 Session 有效
            browser.close()
    except Exception as exc:
        return False, f"Playwright 抓取過程發生錯誤：{exc}", None

    validation = validate_and_sanitize_cookie(captured_str)
    if not validation.valid:
        return False, f"擷取到的 Cookie 未通過驗證：{validation.reason}", validation

    write_cookie_file(cookie_path, validation.cleaned_cookie)
    return True, "已自動擷取並寫入 cookie.txt。", validation
