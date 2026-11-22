"""巴哈姆特 Cookie 防呆驗證、剪貼簿寫入、VIP 前置檢查與可即時中斷的 Playwright 抓取模組。"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .terminal_utils import interruptible_sleep

COOKIE_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

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
    "PHPSESSID",
}
NON_ASCII_STRIP_KEYS = {"BAHANICK", "MB_BAHANICK"}


@dataclass
class CookieValidationResult:
    valid: bool
    tier: str  # "logged_in" | "partial_login" | "guest" | "invalid"
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


@dataclass
class UpgradeEligibility:
    allowed: bool
    certain: bool  # True=明確可升級或明確不可升級; False=被 Cloudflare 攔截無法預判，須靠後置熔斷檢查
    reason: str


def mask_account(value: str) -> str:
    v = value.strip()
    if not v:
        return "訪客"
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
    """
    驗證並清洗 Cookie：
    - 嚴格攔截非 Cookie 雜訊（空值、網址、程式碼、其他網站 Cookie），避免誤覆蓋舊檔。
    - 允許「已登入帳號（含非 VIP）」與「巴哈一般訪客 Cookie」通過，並清楚標示狀態等級。
    """
    if not raw_text or not raw_text.strip():
        return CookieValidationResult(valid=False, tier="invalid", reason="內容為空（尚未複製內容或檔案為空）。")

    stripped = raw_text.strip()
    lower_start = stripped.lower()
    if lower_start.startswith(("http://", "https://", "{", "[", "<", "git ", "python", "pip ", "uv ", "brew ")):
        return CookieValidationResult(
            valid=False,
            tier="invalid",
            reason="內容看起來像是網址、程式碼或指令，而不是 Cookie 字串，已拒絕寫入。",
        )

    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) > 1:
        cookie_line = next((ln for ln in lines if ln.lower().startswith("cookie:")), None)
        joined = cookie_line if cookie_line else "; ".join(lines)
    else:
        joined = lines[0]

    if joined.lower().startswith("cookie:"):
        joined = joined.split(":", 1)[1].strip()

    if len(joined) < 15 or "=" not in joined:
        return CookieValidationResult(
            valid=False,
            tier="invalid",
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
        if key.upper() in NON_ASCII_STRIP_KEYS and not val.isascii():
            warnings.append(f"已自動移除含非 ASCII 字元的 {key}（避免 aniGamerPlus 標頭編碼失敗）")
            continue
        if not val.isascii():
            warnings.append(f"已略過含非 ASCII 字元的欄位：{key}")
            continue
        valid_pairs.append((key, val))

    if len(valid_pairs) < 1:
        return CookieValidationResult(
            valid=False,
            tier="invalid",
            reason="未解析出有效的 Cookie 欄位，疑似複製到一般文字，已拒絕寫入。",
        )

    key_map = {k.upper(): v for k, v in valid_pairs}
    matched_known = [k for k in key_map if k in BAHA_KNOWN_KEYS or k.startswith("BAHA") or k.startswith("CKBAHA")]
    if not matched_known:
        return CookieValidationResult(
            valid=False,
            tier="invalid",
            reason="未偵測到任何巴哈姆特相關欄位（如 BAHAID / BAHARUNE / nologinuser / ckBahaAd），疑似複製到其他網站或一般文字，已保留原檔不覆蓋。",
        )

    matched_auth = sorted(k for k in BAHA_AUTH_KEYS if k in key_map and key_map[k])
    has_id = "BAHAID" in matched_auth
    has_token = ("BAHARUNE" in matched_auth) or ("BAHAENUR" in matched_auth)
    cleaned = "; ".join(f"{k}={v}" for k, v in valid_pairs)

    if has_id and has_token:
        return CookieValidationResult(
            valid=True,
            tier="logged_in",
            cleaned_cookie=cleaned,
            baha_id_masked=mask_account(key_map.get("BAHAID", "")),
            pair_count=len(valid_pairs),
            matched_auth_keys=matched_auth,
            warnings=warnings,
            reason="已登入巴哈帳號 Cookie（驗證通過，日後升級 VIP 可直接優先抓 1080P）",
        )

    if has_id and not has_token:
        warnings.append("偵測到 BAHAID 但缺少 HttpOnly 憑證 (BAHARUNE/BAHAENUR)；若要走 VIP 驗證請改從 Network -> Request Headers 複製。")
        return CookieValidationResult(
            valid=True,
            tier="partial_login",
            cleaned_cookie=cleaned,
            baha_id_masked=mask_account(key_map.get("BAHAID", "")),
            pair_count=len(valid_pairs),
            matched_auth_keys=matched_auth,
            warnings=warnings,
            reason="不完整登入 Cookie（缺少 HttpOnly 欄位，實際下載將視為訪客）",
        )

    warnings.append("此為巴哈一般訪客 Cookie（未登入帳號）；下載時將自動走免費 720P + 廣告流程。")
    return CookieValidationResult(
        valid=True,
        tier="guest",
        cleaned_cookie=cleaned,
        baha_id_masked="訪客",
        pair_count=len(valid_pairs),
        matched_auth_keys=matched_auth,
        warnings=warnings,
        reason="巴哈一般訪客 Cookie（已接受寫入）",
    )


def inspect_cookie_file(cookie_path: Path) -> CookieValidationResult:
    """純本地讀取並驗證現有 cookie.txt（不對伺服器發送請求）。"""
    if not cookie_path.is_file():
        return CookieValidationResult(valid=False, tier="invalid", reason=f"找不到檔案：{cookie_path}")
    raw = cookie_path.read_text(encoding="utf-8", errors="ignore")
    return validate_and_sanitize_cookie(raw)


def verify_cookie_online(cookie_str: str, timeout: int = 10) -> CookieOnlineStatus:
    """發送單次輕量請求確認動畫瘋端登入與 VIP 權限狀態。"""
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
                detail="線上檢查遭 Cloudflare 攔截 (HTTP 403)，無法預先判定 VIP 狀態。",
            )
        if status_code != 200:
            return CookieOnlineStatus(checked=False, status="error", detail=f"伺服器回應 HTTP {status_code}")

        has_logout = ("logout" in body.lower()) or ("登出" in body)
        is_vip_hint = bool(
            re.search(
                r"(?:vip|is_vip|isVip)[\"']?\s*[:=]\s*(?:true|1)|付費會員專區|VIP\s*到期日|動畫瘋\s*VIP\s*會員",
                body,
                re.IGNORECASE,
            )
        )
        if is_vip_hint and has_logout:
            return CookieOnlineStatus(checked=True, status="vip", detail="線上驗證成功：已登入且偵測到 VIP 權限（優先 1080P、免廣告）。")
        if has_logout:
            return CookieOnlineStatus(
                checked=True,
                status="logged_in",
                detail="線上驗證結果：已登入一般巴哈帳號（未偵測到 VIP 權限，非 VIP 將自動降級 720P）。",
            )
        return CookieOnlineStatus(
            checked=True,
            status="guest",
            detail="線上狀態：訪客 / 未登入（僅能下載 720P + 廣告）。",
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return CookieOnlineStatus(
                checked=False,
                status="blocked",
                detail="線上檢查遭 Cloudflare 攔截 (HTTP 403)，無法預先判定 VIP 狀態。",
            )
        return CookieOnlineStatus(checked=False, status="error", detail=f"HTTP 錯誤 ({exc.code})")
    except Exception as exc:
        return CookieOnlineStatus(checked=False, status="error", detail=f"線上檢查失敗：{exc}")


def check_vip_upgrade_eligibility(cookie_path: Path, target_resolution: int) -> UpgradeEligibility:
    """
    【前置檢查】執行 720P -> 1080P 畫質升級前先檢查目前帳號是否具備 VIP 權限，
    防止非 VIP 帳號把既有的 720P 檔案重複丟進去重新下載一次 720P。
    """
    if target_resolution <= 720:
        return UpgradeEligibility(allowed=True, certain=True, reason="目標畫質為 720P 以下，無需 VIP 權限。")

    if not cookie_path.is_file():
        return UpgradeEligibility(
            allowed=False,
            certain=True,
            reason="尚未設定 cookie.txt（非 VIP 訪客），無法取得 1080P；已跳過升級任務以避免重複下載 720P。",
        )

    local = inspect_cookie_file(cookie_path)
    if not local.valid or local.tier != "logged_in":
        return UpgradeEligibility(
            allowed=False,
            certain=True,
            reason=f"目前 cookie.txt 為「{local.tier}」狀態（非完整登入帳號），無法取得 1080P；已跳過升級任務。",
        )

    online = verify_cookie_online(local.cleaned_cookie)
    if online.status == "vip":
        return UpgradeEligibility(allowed=True, certain=True, reason="前置驗證通過：偵測到 VIP 權限，允許升級 1080P。")
    if online.status in ("logged_in", "guest"):
        return UpgradeEligibility(
            allowed=False,
            certain=True,
            reason=f"前置檢查攔截：{online.detail} 已跳過 720P->1080P 升級，避免原腳本降級重複下載 720P。",
        )

    # 若遭 Cloudflare 403 無法預判，允許先試抓第 1 集，由下載後置熔斷機制把關
    return UpgradeEligibility(
        allowed=True,
        certain=False,
        reason=f"{online.detail} 將先試抓第 1 集並啟用「後置解析度熔斷檢查」（若仍抓到 720P 會立刻中止剩餘升級）。",
    )


def write_cookie_file(cookie_path: Path, cleaned_cookie: str) -> None:
    cookie_path.write_text(cleaned_cookie.strip() + "\n", encoding="utf-8")


def capture_cookie_via_playwright(cookie_path: Path, timeout_seconds: int = 180) -> tuple[bool, str, CookieValidationResult | None]:
    """
    半自動腳本抓取（改進版）：
    1. 優先呼叫本機正式版 Chrome（channel='chrome'）並隱藏 webdriver 特徵，降低被巴哈／Cloudflare 阻擋機率。
    2. 輪詢時偵測「視窗被手動關閉」就立刻退出，不再乾等 180 秒。
    3. 隨時支援 Ctrl+C 秒退並確實關閉瀏覽器行程。
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return (
            False,
            "目前環境尚未安裝 Playwright。若你的 .venv 是用 uv 建立的，請執行：\n"
            "  uv pip install playwright\n"
            "  .venv/bin/playwright install chromium",
            None,
        )

    captured_str = ""
    browser = None
    try:
        with sync_playwright() as p:
            launch_args = ["--disable-blink-features=AutomationControlled"]
            # 優先嘗試本機正式版 Chrome，失敗再降級用內建 Chromium
            try:
                browser = p.chromium.launch(headless=False, channel="chrome", args=launch_args)
            except Exception:
                browser = p.chromium.launch(headless=False, args=launch_args)

            context = browser.new_context(
                locale="zh-TW",
                viewport={"width": 1200, "height": 820},
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = context.new_page()
            page.goto("https://user.gamer.com.tw/login.php", wait_until="domcontentloaded")

            deadline = time.monotonic() + timeout_seconds
            logged_in = False
            while time.monotonic() < deadline:
                # 只要使用者把視窗關掉，立刻中止，不等超時
                if not browser.is_connected() or page.is_closed() or not context.pages:
                    return False, "偵測到瀏覽器視窗已關閉，已立刻取消抓取。", None

                try:
                    cookies = context.cookies(["https://ani.gamer.com.tw", "https://gamer.com.tw", "https://user.gamer.com.tw"])
                except Exception:
                    return False, "瀏覽器連線已中斷，已取消抓取。", None

                names = {c["name"].upper() for c in cookies if c.get("value")}
                if "BAHAID" in names and ("BAHARUNE" in names or "BAHAENUR" in names):
                    logged_in = True
                    break

                interruptible_sleep(1.0, step=0.2)

            if not logged_in:
                return False, f"超過 {timeout_seconds} 秒未偵測到完成登入，已自動取消。", None

            try:
                page.goto("https://ani.gamer.com.tw/", wait_until="domcontentloaded")
                interruptible_sleep(2.0, step=0.2)
                all_cookies = context.cookies(["https://ani.gamer.com.tw", "https://gamer.com.tw"])
            except Exception:
                all_cookies = context.cookies(["https://ani.gamer.com.tw", "https://gamer.com.tw"])

            dedup: dict[str, str] = {}
            for c in all_cookies:
                name = str(c.get("name", "")).strip()
                val = str(c.get("value", "")).strip()
                if name and val:
                    dedup[name] = val
            captured_str = "; ".join(f"{k}={v}" for k, v in dedup.items())
    except KeyboardInterrupt:
        return False, "使用者按下 Ctrl+C，已立刻中斷瀏覽器抓取並關閉視窗。", None
    except Exception as exc:
        return (
            False,
            f"Playwright 抓取未完成：{exc}\n"
            "（若巴哈／Cloudflare 擋下自動化瀏覽器登入，請改用選項 1：用平常的瀏覽器複製 Cookie 後一鍵寫入）",
            None,
        )
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    validation = validate_and_sanitize_cookie(captured_str)
    if not validation.valid:
        return False, f"擷取到的 Cookie 未通過驗證：{validation.reason}", validation

    write_cookie_file(cookie_path, validation.cleaned_cookie)
    return True, "已自動擷取、寫入 cookie.txt 並完成重新載入。", validation
