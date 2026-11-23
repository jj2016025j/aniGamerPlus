"""動畫瘋片名搜尋、最新集上架時間推算更新／完結狀態、與 Bangumi 總集數查詢模組。"""

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

from .episode_fetcher import (
    EpisodeFetchError,
    EpisodeInfo,
    fetch_anime_episodes,
    fetch_episode_page_info,
    fetch_episode_upload_time,
    http_get_page,
    load_cookie_header,
    parse_episodes_from_html,
)

ANIGAMER_HOME_URL = "https://ani.gamer.com.tw/"
ANIGAMER_SEARCH_URL = "https://ani.gamer.com.tw/search.php?keyword={keyword}"
ANIGAMER_REF_URL = "https://ani.gamer.com.tw/animeRef.php?sn={ref_sn}"
BANGUMI_V0_SEARCH_URL = "https://api.bgm.tv/v0/search/subjects"
BANGUMI_LEGACY_SEARCH_URL = "https://api.bgm.tv/search/subject/{keyword}?type=2&responseGroup=small"
BANGUMI_SUBJECT_URL = "https://api.bgm.tv/v0/subjects/{subject_id}"
BANGUMI_UA = "aniGamerPlus-Manager/1.6.0 (personal-watchlist-tool)"

WEEKDAY_NAMES = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

# 常見動漫標題繁轉簡 1-to-1 對照表（提升 Bangumi 簡中資料庫命中率）
_TC_PAIRS = [
    ("學", "学"), ("會", "会"), ("員", "员"), ("國", "国"), ("體", "体"), ("語", "语"),
    ("戰", "战"), ("機", "机"), ("龍", "龙"), ("館", "馆"), ("師", "师"), ("術", "术"),
    ("賦", "赋"), ("騎", "骑"), ("劍", "剑"), ("轉", "转"), ("憑", "凭"), ("識", "识"),
    ("無", "无"), ("雙", "双"), ("聖", "圣"), ("戀", "恋"), ("愛", "爱"), ("惡", "恶"),
    ("靈", "灵"), ("異", "异"), ("變", "变"), ("開", "开"), ("關", "关"), ("門", "门"),
    ("時", "时"), ("間", "间"), ("點", "点"), ("進", "进"), ("後", "后"), ("從", "从"),
    ("來", "来"), ("與", "与"), ("為", "为"), ("這", "这"), ("個", "个"), ("們", "们"),
    ("麼", "么"), ("書", "书"), ("畫", "画"), ("樂", "乐"), ("動", "动"), ("劇", "剧"),
    ("編", "编"), ("導", "导"), ("總", "总"), ("話", "话"), ("數", "数"), ("結", "结"),
    ("實", "实"), ("遊", "游"), ("戲", "戏"), ("團", "团"), ("長", "长"), ("風", "风"),
    ("雲", "云"), ("電", "电"), ("氣", "气"), ("車", "车"), ("飛", "飞"), ("鐵", "铁"),
    ("銀", "银"), ("銅", "铜"), ("鏡", "镜"), ("夢", "梦"), ("殺", "杀"), ("報", "报"),
    ("復", "复"), ("歸", "归"), ("還", "还"), ("離", "离"), ("別", "别"), ("棄", "弃"),
    ("絕", "绝"), ("強", "强"), ("勝", "胜"), ("敗", "败"), ("負", "负"), ("極", "极"),
    ("獸", "兽"), ("領", "领"), ("貴", "贵"), ("險", "险"), ("傭", "佣"), ("園", "园"),
    ("輩", "辈"), ("親", "亲"), ("偵", "侦"), ("醫", "医"), ("藥", "药"), ("鍊", "链"),
    ("煉", "炼"), ("鍛", "锻"), ("築", "筑"), ("農", "农"), ("釣", "钓"), ("魚", "鱼"),
    ("飯", "饭"), ("鋪", "铺"), ("鎮", "镇"), ("莊", "庄"), ("島", "岛"), ("嶼", "屿"),
    ("脈", "脉"), ("陽", "阳"), ("節", "节"), ("霧", "雾"), ("紅", "红"), ("藍", "蓝"),
    ("綠", "绿"), ("黃", "黄"), ("蒼", "苍"), ("緋", "绯"), ("蓮", "莲"), ("櫻", "樱"),
    ("蘭", "兰"), ("楓", "枫"), ("蘋", "苹"), ("萬", "万"), ("歲", "岁"), ("歷", "历"),
    ("傳", "传"), ("說", "说"), ("記", "记"), ("錄", "录"), ("願", "愿"), ("隨", "随"),
    ("選", "选"), ("擇", "择"), ("獨", "独"), ("級", "级"), ("職", "职"), ("業", "业"),
    ("賢", "贤"), ("僕", "仆"), ("護", "护"), ("衛", "卫"), ("隊", "队"), ("軍", "军"),
    ("藉", "借"), ("掛", "挂"), ("裡", "里"), ("裏", "里"), ("邊", "边"), ("頭", "头"),
    ("發", "发"), ("現", "现"), ("覺", "觉"), ("稱", "称"), ("號", "号"), ("裝", "装"),
    ("備", "备"), ("買", "买"), ("賣", "卖"), ("錢", "钱"), ("幣", "币"), ("貧", "贫"),
    ("窮", "穷"), ("宮", "宫"), ("謎", "谜"), ("題", "题"), ("華", "华"), ("麗", "丽"),
    ("優", "优"), ("溫", "温"), ("殘", "残"), ("毀", "毁"), ("滅", "灭"), ("創", "创"),
    ("運", "运"), ("跡", "迹"), ("蹟", "迹"), ("約", "约"), ("絆", "绊"), ("終", "终"),
    ("曉", "晓"), ("輝", "辉"), ("閃", "闪"), ("響", "响"),
]
TC_TO_SC_MAP = str.maketrans({tc: sc for tc, sc in _TC_PAIRS})


def to_simplified(text: str) -> str:
    return text.translate(TC_TO_SC_MAP)


@dataclass
class SearchCandidate:
    title: str
    sn: str
    is_ref_sn: bool
    year_text: str = ""
    ep_count_text: str = ""


@dataclass
class EnrichedMetadata:
    title: str
    base_sn: str
    online_count: int
    status: str  # "連載中" | "已完結" | ""
    schedule_note: str
    total_episodes: int | None
    latest_upload_at: str = ""
    views: int | None = None
    schedule_source: str = ""
    total_source: str = ""


def _norm(text: str) -> str:
    cleaned = re.sub(r"[\s！!？?：:、，,。.\-—_「」『』（）()\[\]～~・·]+", "", text or "")
    return to_simplified(cleaned).lower()


def parse_search_results_html(page_html: str) -> list[SearchCandidate]:
    """解析動畫瘋 search.php 搜尋結果清單。"""
    results: list[SearchCandidate] = []
    seen: set[tuple[str, str]] = set()

    card_pattern = re.compile(
        r'<a[^>]+href=["\']([^"\']*(?:animeRef|animeVideo)\.php\?sn=(\d+))["\'][^>]*class=["\'][^"\']*theme-list-main[^"\']*["\'][^>]*>(.*?)</a>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    matches = card_pattern.findall(page_html)

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
    """將搜尋結果解析成基準播放 SN（第 1 集 SN）與集數列表。"""
    cookie_header = load_cookie_header(cookie_path)
    if candidate.is_ref_sn:
        url = ANIGAMER_REF_URL.format(ref_sn=candidate.sn)
        final_url, page_html = http_get_page(url, cookie_header=cookie_header)
        m = re.search(r"[?&]sn=(\d+)", final_url)
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
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", mmdd.strip())
    if not m:
        return ""
    month, day = int(m.group(1)), int(m.group(2))
    now = datetime.now()
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
    """從動畫瘋首頁解析本季新番更新週表，支援 day-list 與 newanime-date-area 結構。"""
    schedule_map: dict[str, str] = {}

    day_sections = re.split(r"(?=(?:週|星期)[一二三四五六日])", page_html)
    for sec in day_sections:
        day_m = re.match(r"((?:週|星期)[一二三四五六日])", sec)
        if not day_m:
            continue
        day_label = day_m.group(1).replace("星期", "週")
        block_cards = re.findall(
            r'<a[^>]+href=["\'][^"\']*anime(?:Video|Ref)\.php\?sn=\d+["\'][^>]*>(.*?)</a>',
            sec[:30000],
            flags=re.IGNORECASE | re.DOTALL,
        )
        for card in block_cards:
            name_m = re.search(r'class=["\'][^"\']*(?:anime-name|theme-name)[^"\']*["\'][^>]*>(.*?)</', card, flags=re.IGNORECASE | re.DOTALL)
            time_m = re.search(r"(\d{2}:\d{2})", card)
            if name_m and time_m:
                title = html.unescape(re.sub(r"<[^>]+>", "", name_m.group(1))).strip()
                if title:
                    schedule_map[_norm(title)] = f"每{day_label} {time_m.group(1)}"

    blocks = re.split(r'class=["\'][^"\']*newanime-date-area[^"\']*["\']', page_html, flags=re.IGNORECASE)
    for blk in blocks[1:]:
        chunk = blk[:2500]
        name_m = re.search(r'class=["\'][^"\']*anime-name[^"\']*["\'][^>]*>(.*?)</', chunk, flags=re.IGNORECASE | re.DOTALL)
        date_m = re.search(r"(\d{1,2}/\d{1,2})", chunk)
        time_m = re.search(r"(\d{2}:\d{2})", chunk)
        if name_m and date_m and time_m:
            title = html.unescape(re.sub(r"<[^>]+>", "", name_m.group(1))).strip()
            wd = _weekday_from_mmdd(date_m.group(1))
            if title and wd and _norm(title) not in schedule_map:
                schedule_map[_norm(title)] = f"每{wd} {time_m.group(1)}"

    return schedule_map


class ScheduleCache:
    """快取本次執行期間抓到的動畫瘋首頁週表。"""

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
        for k, val in self._data.items():
            if len(k) >= 4 and (k in target_norm or target_norm in k):
                return val
        return ""


_schedule_cache = ScheduleCache()


def _http_request_json(url: str, payload: dict | None = None, timeout: int = 7) -> dict | None:
    headers = {"User-Agent": BANGUMI_UA, "Accept": "application/json"}
    data_bytes = None
    method = "GET"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _pick_best_bangumi_eps(items: list, target_norm: str, min_episodes: int = 1) -> int | None:
    best_item = None
    best_score = 0.0

    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        names = [str(item.get("name_cn") or ""), str(item.get("name") or "")]
        score = max((SequenceMatcher(None, target_norm, _norm(n)).ratio() for n in names if n), default=0.0)
        if any(len(target_norm) >= 4 and (target_norm in _norm(n) or _norm(n) in target_norm) for n in names if n):
            score = max(score, 0.85)
        if score > best_score:
            best_score = score
            best_item = item

    if not best_item or best_score < 0.48:
        return None

    eps = best_item.get("total_episodes") or best_item.get("eps") or best_item.get("eps_count")
    if isinstance(eps, int) and min_episodes <= eps <= 2000:
        return eps

    subj_id = best_item.get("id")
    if subj_id:
        detail = _http_request_json(BANGUMI_SUBJECT_URL.format(subject_id=subj_id), timeout=5)
        if isinstance(detail, dict):
            total = detail.get("total_episodes") or detail.get("eps")
            if isinstance(total, int) and min_episodes <= total <= 2000:
                return total

    return None


def lookup_total_episodes_bangumi(title: str, min_episodes: int = 1) -> int | None:
    """
    透過免費公開 Bangumi API 查詢預定總集數：
    1. 先轉簡體並去除尾端標點。
    2. 優先使用 v0 POST 搜尋，失敗再退回舊版 GET；長片名若查無再自動縮短前 10 字重試。
    3. 強制校驗：回傳總集數不得小於動畫瘋現已上架集數（min_episodes）。
    """
    clean_title = re.sub(r"\s*\[.*?\]\s*", "", title).strip().rstrip("。！!？?")
    if not clean_title:
        return None

    sc_title = to_simplified(clean_title)
    target_norm = _norm(sc_title)

    queries = [sc_title]
    if len(sc_title) > 10:
        queries.append(sc_title[:10])

    for q in queries:
        v0 = _http_request_json(
            BANGUMI_V0_SEARCH_URL,
            payload={"keyword": q, "filter": {"type": [2]}},
            timeout=6,
        )
        if isinstance(v0, dict) and isinstance(v0.get("data"), list) and v0["data"]:
            eps = _pick_best_bangumi_eps(v0["data"], target_norm, min_episodes=min_episodes)
            if eps:
                return eps

        encoded = urllib.parse.quote(q)
        legacy = _http_request_json(BANGUMI_LEGACY_SEARCH_URL.format(keyword=encoded), timeout=6)
        if isinstance(legacy, dict) and isinstance(legacy.get("list"), list) and legacy["list"]:
            eps = _pick_best_bangumi_eps(legacy["list"], target_norm, min_episodes=min_episodes)
            if eps:
                return eps

    return None


def infer_status_and_schedule(
    latest_dt: datetime | None,
    homepage_schedule: str,
    online_count: int,
    bangumi_total: int | None,
    now: datetime | None = None,
) -> tuple[str, str, str]:
    """
    結合「最新集上架時間」、「動畫瘋首頁週表」與「總集數」判斷連載狀態與更新時間。
    回傳 (status, schedule_note, source)。
    """
    ref_now = now or datetime.now()

    if homepage_schedule:
        return "連載中", homepage_schedule, "動畫瘋本季週表"

    if latest_dt is not None:
        days_ago = (ref_now - latest_dt).days
        if (bangumi_total and online_count >= bangumi_total) or days_ago >= 21:
            return "已完結", f"已完結 ({latest_dt.strftime('%Y/%m')})", "最新集上架時間"
        wd = WEEKDAY_NAMES[latest_dt.weekday()]
        hhmm = latest_dt.strftime("%H:%M")
        return "連載中", f"每{wd} {hhmm}", "最新集上架時間"

    if bangumi_total and online_count >= bangumi_total:
        return "已完結", "已完結", "總集數比對"

    return "", "", ""


def enrich_anime_metadata(
    base_sn: str,
    fallback_title: str = "",
    cookie_path: Path | None = None,
    query_external_total: bool = True,
) -> tuple[EnrichedMetadata, list[EpisodeInfo]]:
    """一次整合：動畫瘋標題＋上架集數＋最新集上架時間（推算每週時間／已完結）＋ Bangumi 預定總集數。"""
    title, episodes = fetch_anime_episodes(base_sn, cookie_path=cookie_path, fallback_title=fallback_title)
    canonical_sn = episodes[0].sn if episodes else base_sn
    latest_sn = episodes[-1].sn if episodes else base_sn
    online_count = len(episodes)

    latest_dt, views = fetch_episode_page_info(latest_sn, cookie_path=cookie_path)
    homepage_sched = _schedule_cache.get_schedule_for_title(title, cookie_path=cookie_path)

    total_eps = None
    total_source = ""
    if query_external_total:
        total_eps = lookup_total_episodes_bangumi(title, min_episodes=online_count)
        if total_eps:
            total_source = "Bangumi 資料庫"

    status, schedule_note, sched_source = infer_status_and_schedule(
        latest_dt=latest_dt,
        homepage_schedule=homepage_sched,
        online_count=online_count,
        bangumi_total=total_eps,
    )

    if status == "已完結" and not total_eps and online_count > 0:
        total_eps = online_count
        total_source = "動畫瘋(已完結)"

    meta = EnrichedMetadata(
        title=title,
        base_sn=canonical_sn,
        online_count=online_count,
        status=status,
        schedule_note=schedule_note,
        total_episodes=total_eps,
        latest_upload_at=latest_dt.strftime("%Y/%m/%d %H:%M") if latest_dt else "",
        views=views,
        schedule_source=sched_source,
        total_source=total_source,
    )
    return meta, episodes
