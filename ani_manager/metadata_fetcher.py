"""動畫瘋片名搜尋、本季新番更新週表解析與外部 Bangumi 總集數查詢模組。"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from .episode_fetcher import EpisodeFetchError, EpisodeInfo, fetch_anime_episodes, http_get_page, load_cookie_header

ANIGAMER_HOME_URL = "https://ani.gamer.com.tw/"
ANIGAMER_SEARCH_URL = "https://ani.gamer.com.tw/search.php?keyword={keyword}"
ANIGAMER_REF_URL = "https://ani.gamer.com.tw/animeRef.php?sn={ref_sn}"
BANGUMI_SEARCH_URL = "https://api.bgm.tv/search/subject/{keyword}?type=2&responseGroup=small"
BANGUMI_SUBJECT_URL = "https://api.bgm.tv/v0/subjects/{subject_id}"
BANGUMI_UA = "aniGamerPlus-Manager/1.4 (personal-watchlist-tool)"

WEEKDAY_NAMES = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


@dataclass
class SearchCandidate:
    title: str
    sn: str
    is_ref_sn: bool  # True=來自 animeRef.php?sn=... (選中後需轉換為實際播放 SN)
    year_text: str = ""
    ep_count_text: str = ""


@dataclass
class EnrichedMetadata:
    title: str
    base_sn: str
    online_count: int
    schedule_note: str
    total_episodes: int | None
    schedule_source: str = ""
    total_source: str = ""


def _norm(text: str) -> str:
    cleaned = re.sub(r"[\s！!？?：:、，,。.\-—_「」『』（）()\[\]～~]+", "", text or "")
    return cleaned.lower()


def parse_search_results_html(page_html: str) -> list[SearchCandidate]:
    """解析動畫瘋 search.php 搜尋結果清單。"""
    results: list[SearchCandidate] = []
    seen: set[tuple[str, str]] = set()

    # 匹配動畫瘋標準卡片：<a href="animeRef.php?sn=..." class="theme-list-main"> ... </a>
    card_pattern = re.compile(
        r'<a[^>]+href=["\']([^"\']*(?:animeRef|animeVideo)\.php\?sn=(\d+))["\'][^>]*class=["\'][^"\']*theme-list-main[^"\']*["\'][^>]*>(.*?)</a>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    matches = card_pattern.findall(page_html)

    # 備用順序（class 在 href 之前）
    if not matches:
        alt_pattern = re.compile(
            r'<a[^>]+class=["\'][^"\']*theme-list-main[^"\']*["\'][^>]*href=["\']([^"\']*(?:animeRef|animeVideo)\.php\?sn=(\d+))["\'][^>]*>(.*?)</a>',
            flags=re.IGNORECASE | re.DOTALL,
        )
        matches = alt_pattern.findall(page_html)

    for href, sn, body in matches:
        name_m = re.search(r'class=["\'][^"\']*theme-name[^"\']*["\'][^>]*>(.*?)</', body, flags=re.IGNORECASE | re.DOTALL)
        if not name_m:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", name_m.group(1))).strip()
        if not title:
            continue

        year_m = re.search(r'class=["\'][^"\']*theme-time[^"\']*["\'][^>]*>(.*?)</', body, flags=re.IGNORECASE | re.DOTALL)
        year_text = html.unescape(re.sub(r"<[^>]+>", "", year_m.group(1))).strip() if year_m else ""
        year_text = year_text.replace("年份：", "").strip()

        ep_m = re.search(r'class=["\'][^"\']*theme-number[^"\']*["\'][^>]*>(.*?)</', body, flags=re.IGNORECASE | re.DOTALL)
        ep_text = html.unescape(re.sub(r"<[^>]+>", "", ep_m.group(1))).strip() if ep_m else ""

        is_ref = "animeref.php" in href.lower()
        key = (sn, title)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            SearchCandidate(
                title=title,
                sn=sn,
                is_ref_sn=is_ref,
                year_text=year_text,
                ep_count_text=ep_text,
            )
        )

    return results


def search_anigamer(keyword: str, cookie_path: Path | None = None, limit: int = 12) -> list[SearchCandidate]:
    """依片名關鍵字搜尋巴哈姆特動畫瘋。"""
    encoded = urllib.parse.quote(keyword.strip())
    url = ANIGAMER_SEARCH_URL.format(keyword=encoded)
    cookie_header = load_cookie_header(cookie_path)
    _, page_html = http_get_page(url, cookie_header=cookie_header)
    return parse_search_results_html(page_html)[:limit]


def resolve_candidate_video_sn(candidate: SearchCandidate, cookie_path: Path | None = None) -> tuple[str, str, list[EpisodeInfo]]:
    """將搜尋結果（可能是 animeRef.php）解析成實際可下載的基準 episode SN（預設取第 1 集 SN）與集數列表。"""
    cookie_header = load_cookie_header(cookie_path)
    if candidate.is_ref_sn:
        url = ANIGAMER_REF_URL.format(ref_sn=candidate.sn)
        final_url, page_html = http_get_page(url, cookie_header=cookie_header)
        m = re.search(r"[?&]sn=(\d+)", final_url)
        from .episode_fetcher import parse_episodes_from_html

        title, episodes = parse_episodes_from_html(page_html, fallback_title=candidate.title)
        if episodes:
            return title, episodes[0].sn, episodes
        if m:
            video_sn = m.group(1)
            title, episodes = fetch_anime_episodes(video_sn, cookie_path=cookie_path, fallback_title=candidate.title)
            return title, episodes[0].sn, episodes
        raise EpisodeFetchError("無法從作品頁解析出播放集數 SN。")

    title, episodes = fetch_anime_episodes(candidate.sn, cookie_path=cookie_path, fallback_title=candidate.title)
    return title, episodes[0].sn, episodes


def _weekday_from_mmdd(mmdd: str) -> str:
    """從動畫瘋卡片上的 MM/DD 日期推算對應星期（週一~週日）。"""
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", mmdd.strip())
    if not m:
        return ""
    month, day = int(m.group(1)), int(m.group(2))
    now = datetime.now()
    # 處理跨年邊界（例如 12 月看到 1 月或 1 月看到 12 月）
    best_dt = None
    best_diff = 9999
    for yr in (now.year - 1, now.year, now.year + 1):
        try:
            dt = datetime(yr, month, day)
            diff = abs((dt - now).days)
            if diff < best_diff:
                best_diff = diff
                best_dt = dt
        except ValueError:
            continue
    return WEEKDAY_NAMES[best_dt.weekday()] if best_dt else ""


def parse_seasonal_schedule_html(page_html: str) -> dict[str, str]:
    """
    從動畫瘋首頁解析本季新番更新時間表。
    回傳 {normalized_title: "每週X HH:MM"} 對應表。
    """
    schedule_map: dict[str, str] = {}

    # 策略 1：依週一~週日區塊分組解析（動畫瘋「本季新番」週表常見結構）
    day_sections = re.split(r"(?=(?:週|星期)[一二三四五六日])", page_html)
    for sec in day_sections:
        day_m = re.match(r"((?:週|星期)[一二三四五六日])", sec)
        if not day_m:
            continue
        day_label = day_m.group(1).replace("星期", "週")
        # 抓取該區塊內各動畫名稱與更新時間 HH:MM
        block_cards = re.findall(
            r'<a[^>]+href=["\'][^"\']*anime(?:Video|Ref)\.php\?sn=\d+["\'][^>]*>(.*?)</a>',
            sec[:25000],
            flags=re.IGNORECASE | re.DOTALL,
        )
        for card in block_cards:
            name_m = re.search(r'class=["\'][^"\']*(?:anime-name|theme-name)[^"\']*["\'][^>]*>(.*?)</', card, flags=re.IGNORECASE | re.DOTALL)
            time_m = re.search(r"(\d{2}:\d{2})", card)
            if name_m and time_m:
                title = html.unescape(re.sub(r"<[^>]+>", "", name_m.group(1))).strip()
                if title:
                    schedule_map[_norm(title)] = f"每{day_label} {time_m.group(1)}"

    # 策略 2：從「本季新番／最新上架」卡片中的日期 MM/DD + 時間 HH:MM 推算星期
    card_blocks = re.findall(
        r'<div[^>]+class=["\'][^"\']*anime-card-block[^"\']*["\'][^>]*>(.*?)</a>\s*</div>',
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for block in card_blocks:
        name_m = re.search(r'class=["\'][^"\']*anime-name[^"\']*["\'][^>]*>(.*?)</', block, flags=re.IGNORECASE | re.DOTALL)
        date_m = re.search(r"(\d{1,2}/\d{1,2})", block)
        time_m = re.search(r"(\d{2}:\d{2})", block)
        if name_m and date_m and time_m:
            title = html.unescape(re.sub(r"<[^>]+>", "", name_m.group(1))).strip()
            wd = _weekday_from_mmdd(date_m.group(1))
            if title and wd and _norm(title) not in schedule_map:
                schedule_map[_norm(title)] = f"每{wd} {time_m.group(1)}"

    return schedule_map


class ScheduleCache:
    """快取本次執行期間抓到的動畫瘋首頁週表，避免重複請求首頁。"""

    def __init__(self, ttl_seconds: int = 600) -> None:
        self.ttl = ttl_seconds
        self._data: dict[str, str] | None = None
        self._fetched_at: float = 0.0

    def get_schedule_for_title(self, title: str, cookie_path: Path | None = None) -> str:
        now = time.monotonic()
        if self._data is None or (now - self._fetched_at) > self.ttl:
            try:
                cookie_header = load_cookie_header(cookie_path)
                _, page_html = http_get_page(ANIGAMER_HOME_URL, cookie_header=cookie_header, timeout=12)
                self._data = parse_seasonal_schedule_html(page_html)
                self._fetched_at = now
            except Exception:
                self._data = self._data or {}

        target_norm = _norm(title)
        if not target_norm or not self._data:
            return ""
        if target_norm in self._data:
            return self._data[target_norm]
        # 容錯前綴／包含比對（處理副標差異）
        for k, val in self._data.items():
            if len(k) >= 4 and (k in target_norm or target_norm in k):
                return val
        return ""


_schedule_cache = ScheduleCache()


def _http_get_json(url: str, timeout: int = 6) -> dict | None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": BANGUMI_UA,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def lookup_total_episodes_bangumi(title: str) -> int | None:
    """透過免費公開 Bangumi API (api.bgm.tv) 查詢預定總集數（失敗或查無回傳 None）。"""
    clean_title = re.sub(r"\s*\[.*?\]\s*", "", title).strip()
    if not clean_title:
        return None

    encoded = urllib.parse.quote(clean_title)
    data = _http_get_json(BANGUMI_SEARCH_URL.format(keyword=encoded), timeout=6)
    if not isinstance(data, dict):
        return None
    items = data.get("list")
    if not isinstance(items, list) or not items:
        return None

    target_norm = _norm(clean_title)
    best_item = None
    best_score = 0.0

    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        names = [str(item.get("name_cn") or ""), str(item.get("name") or "")]
        score = max(
            (SequenceMatcher(None, target_norm, _norm(n)).ratio() for n in names if n),
            default=0.0,
        )
        # 若其中一個名稱完全包含目標前 5 字，額外加權
        if any(len(target_norm) >= 4 and (target_norm in _norm(n) or _norm(n) in target_norm) for n in names if n):
            score = max(score, 0.85)
        if score > best_score:
            best_score = score
            best_item = item

    if not best_item or best_score < 0.45:
        return None

    eps = best_item.get("eps") or best_item.get("eps_count")
    if isinstance(eps, int) and 1 <= eps <= 2000:
        return eps

    # 若 small 結果未含 eps，向 /v0/subjects/{id} 補查一次
    subj_id = best_item.get("id")
    if subj_id:
        detail = _http_get_json(BANGUMI_SUBJECT_URL.format(subject_id=subj_id), timeout=5)
        if isinstance(detail, dict):
            total = detail.get("total_episodes") or detail.get("eps")
            if isinstance(total, int) and 1 <= total <= 2000:
                return total

    return None


def enrich_anime_metadata(
    base_sn: str,
    fallback_title: str = "",
    cookie_path: Path | None = None,
    query_external_total: bool = True,
) -> tuple[EnrichedMetadata, list[EpisodeInfo]]:
    """一次整合：動畫瘋標題與上架集數 + 本季週表更新時間 + Bangumi 預定總集數。"""
    title, episodes = fetch_anime_episodes(base_sn, cookie_path=cookie_path, fallback_title=fallback_title)
    canonical_sn = episodes[0].sn if episodes else base_sn

    schedule = _schedule_cache.get_schedule_for_title(title, cookie_path=cookie_path)
    schedule_source = "動畫瘋本季週表" if schedule else ""

    total_eps = None
    total_source = ""
    if query_external_total:
        total_eps = lookup_total_episodes_bangumi(title)
        if total_eps:
            total_source = "Bangumi 資料庫"

    meta = EnrichedMetadata(
        title=title,
        base_sn=canonical_sn,
        online_count=len(episodes),
        schedule_note=schedule,
        total_episodes=total_eps,
        schedule_source=schedule_source,
        total_source=total_source,
    )
    return meta, episodes
