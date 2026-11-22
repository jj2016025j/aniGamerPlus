"""動畫瘋線上集數與 SN 解析模組。"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
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


def load_cookie_header(cookie_path: Path | None) -> str:
    if not cookie_path or not cookie_path.is_file():
        return ""
    raw = cookie_path.read_text(encoding="utf-8", errors="ignore").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    return raw.splitlines()[0].strip() if raw else ""


def http_get_page(url: str, cookie_header: str = "", timeout: int = 15) -> tuple[str, str]:
    """回傳 (final_url, html_text)，自動跟隨重導向（例如 animeRef.php -> animeVideo.php）。"""
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
