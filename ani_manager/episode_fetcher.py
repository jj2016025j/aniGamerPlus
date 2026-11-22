"""動畫瘋線上集數、上架時間解析與集數區間壓縮模組。"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ANIGAMER_VIDEO_URL = "https://ani.gamer.com.tw/animeVideo.php?sn={sn}"
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


@dataclass
class EpisodeInfo:
    sn: str
    episode_label: str
    anime_title: str = ""


class EpisodeFetchError(RuntimeError):
    """抓取動畫瘋集數清單失敗時拋出。"""


def format_episode_ranges(episodes: list[EpisodeInfo], compact: bool = False) -> str:
    """
    將連續集數壓縮成易讀區間，避免一集一集條列：
    - [1,2,3,4,5,6,7] -> "第 1~7 集" (compact: "#1~7")
    - [1,2,3, 5, 7,8] -> "第 1~3、5、7~8 集" (compact: "#1~3, 5, 7~8")
    - [8] -> "第 8 集" (compact: "#8")
    """
    if not episodes:
        return "無"

    nums: list[int] = []
    specials: list[str] = []
    seen_num: set[int] = set()
    seen_sp: set[str] = set()

    for ep in episodes:
        lbl = str(ep.episode_label).strip()
        if lbl.isdigit():
            n = int(lbl)
            if n not in seen_num:
                seen_num.add(n)
                nums.append(n)
        else:
            if lbl and lbl not in seen_sp:
                seen_sp.add(lbl)
                specials.append(lbl)

    nums.sort()
    range_parts: list[str] = []
    if nums:
        start = prev = nums[0]
        for n in nums[1:]:
            if n == prev + 1:
                prev = n
            else:
                range_parts.append(f"{start}~{prev}" if prev > start else f"{start}")
                start = prev = n
        range_parts.append(f"{start}~{prev}" if prev > start else f"{start}")

    if compact:
        all_parts = ([f"#{range_parts[0]}"] + range_parts[1:] if range_parts else []) + specials
        return ", ".join(all_parts)

    sep = "、"
    chunks: list[str] = []
    if range_parts:
        chunks.append(f"第 {sep.join(range_parts)} 集")
    if specials:
        chunks.extend(specials)
    return sep.join(chunks)


def load_cookie_header(cookie_path: Path | None) -> str:
    if not cookie_path or not cookie_path.is_file():
        return ""
    raw = cookie_path.read_text(encoding="utf-8", errors="ignore").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    return raw.splitlines()[0].strip() if raw else ""


def http_get_page(url: str, cookie_header: str = "", timeout: int = 15) -> tuple[str, str]:
    """回傳 (final_url, html_text)，自動跟隨重導向。"""
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": "https://ani.gamer.com.tw/",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    try:
        import requests  # type: ignore

        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code == 403:
            raise EpisodeFetchError("連線被動畫瘋 / Cloudflare 擋下 (HTTP 403)，請稍後再試或更新 cookie.txt。")
        if resp.status_code == 404:
            raise EpisodeFetchError("動畫瘋找不到此頁面 (HTTP 404)，請確認 SN 是否正確。")
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return str(resp.url), resp.text
    except ImportError:
        pass
    except (EpisodeFetchError, KeyboardInterrupt):
        raise
    except Exception as exc:
        raise EpisodeFetchError(f"抓取網頁失敗：{exc}") from exc

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return str(resp.geturl()), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise EpisodeFetchError("連線被動畫瘋 / Cloudflare 擋下 (HTTP 403)，請稍後再試或更新 cookie.txt。") from exc
        if exc.code == 404:
            raise EpisodeFetchError("動畫瘋找不到此頁面 (HTTP 404)，請確認 SN 是否正確。") from exc
        raise EpisodeFetchError(f"HTTP 錯誤 ({exc.code})：{exc.reason}") from exc
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        raise EpisodeFetchError(f"網路請求失敗：{exc}") from exc


def parse_upload_datetime_from_html(page_html: str) -> datetime | None:
    """從動畫瘋播放頁 HTML 解析該集的「上架時間：YYYY/MM/DD HH:MM」。"""
    m = re.search(r"上架時間[：:\s]*(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", page_html)
    if not m:
        return None
    yr, mo, dy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hr = int(m.group(4)) if m.group(4) else 0
    mn = int(m.group(5)) if m.group(5) else 0
    try:
        return datetime(yr, mo, dy, hr, mn)
    except ValueError:
        return None


def parse_episodes_from_html(page_html: str, fallback_title: str = "") -> tuple[str, list[EpisodeInfo]]:
    """從動畫瘋頁面 HTML 解析番劇名稱與各集 SN。"""
    title = fallback_title
    title_match = re.search(r"<title>(.*?)</title>", page_html, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        raw_title = html.unescape(title_match.group(1)).strip()
        cleaned = re.sub(r"\s*\[\d+(?:\.\d+)?\]\s*線上看.*$", "", raw_title).strip()
        cleaned = re.sub(r"\s*-\s*巴哈姆特動畫瘋.*$", "", cleaned).strip()
        if cleaned and cleaned != "巴哈姆特動畫瘋":
            title = cleaned

    season_match = re.search(
        r'<section[^>]*class="[^"]*season[^"]*"[^>]*>(.*?)</section>',
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    target_block = season_match.group(1) if season_match else page_html

    link_pattern = re.compile(
        r'<a[^>]+href=["\'][^"\']*\?sn=(\d+)["\'][^>]*>(.*?)</a>',
        flags=re.IGNORECASE | re.DOTALL,
    )

    episodes: list[EpisodeInfo] = []
    seen_sn: set[str] = set()

    for sn, inner_html in link_pattern.findall(target_block):
        if sn in seen_sn:
            continue
        label = html.unescape(re.sub(r"<[^>]+>", "", inner_html)).strip()
        if not label:
            continue
        if not season_match and not re.fullmatch(r"\d+(?:\.\d+)?|[A-Za-z0-9.\-]{1,8}", label):
            continue
        seen_sn.add(sn)
        episodes.append(EpisodeInfo(sn=sn, episode_label=label, anime_title=title))

    return title, episodes


def fetch_anime_episodes(base_sn: str, cookie_path: Path | None = None, fallback_title: str = "") -> tuple[str, list[EpisodeInfo]]:
    """依任一集 SN 抓取整部作品目前上架的所有集數清單。"""
    url = ANIGAMER_VIDEO_URL.format(sn=str(base_sn).strip())
    cookie_header = load_cookie_header(cookie_path)
    _, page_html = http_get_page(url, cookie_header=cookie_header)
    title, episodes = parse_episodes_from_html(page_html, fallback_title=fallback_title)
    if not episodes:
        raise EpisodeFetchError("找不到此 SN 對應的動畫集數（SN 可能無效、已下架，或需登入驗證）。")
    return title, episodes


def fetch_episode_upload_time(sn: str, cookie_path: Path | None = None) -> datetime | None:
    """抓取指定單集播放頁的上架時間（精準取得每週更新星期＋時間，並可判斷是否已完結）。"""
    try:
        url = ANIGAMER_VIDEO_URL.format(sn=str(sn).strip())
        cookie_header = load_cookie_header(cookie_path)
        _, page_html = http_get_page(url, cookie_header=cookie_header, timeout=12)
        return parse_upload_datetime_from_html(page_html)
    except Exception:
        return None
