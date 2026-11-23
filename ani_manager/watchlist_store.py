"""獨立追番清單儲存、排序與管理模組（watchlist.json）。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config_store import validate_resolution


@dataclass
class AnimeWatchItem:
    name: str
    sn: str
    resolution: str = "1080"
    schedule_note: str = ""
    total_episodes: int | None = None
    online_episodes: int | None = None
    status: str = ""  # "連載中" | "已完結" | ""
    enabled: bool = True
    added_at: str = ""  # YYYY-MM-DD HH:MM:SS
    latest_upload_at: str = ""  # YYYY/MM/DD HH:MM
    views: int | None = None  # 動畫瘋觀看次數（熱門度排序用）


def now_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_views(views: int | None) -> str:
    if views is None or views <= 0:
        return "-"
    if views >= 10000:
        return f"{views / 10000:.1f}萬"
    return f"{views:,}"


def default_watchlist() -> list[AnimeWatchItem]:
    return [
        AnimeWatchItem(
            name="學生會也有洞！",
            sn="51813",
            resolution="1080",
            schedule_note="每週日 01:00",
            total_episodes=12,
            online_episodes=None,
            status="連載中",
            enabled=True,
            added_at=now_timestamp(),
        )
    ]


def parse_watch_items(raw_items: list[dict[str, Any]], default_resolution: str = "1080") -> list[AnimeWatchItem]:
    items: list[AnimeWatchItem] = []
    seen_sns: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        sn = str(item.get("sn", "")).strip()
        if not sn or sn in seen_sns:
            continue
        seen_sns.add(sn)
        sched = str(item.get("schedule_note", "") or "")
        status = str(item.get("status", "") or "")
        if not status and "已完結" in sched:
            status = "已完結"
        elif not status and sched.startswith("每週"):
            status = "連載中"
        raw_views = item.get("views")
        views = int(raw_views) if isinstance(raw_views, (int, float)) and raw_views > 0 else None
        items.append(
            AnimeWatchItem(
                name=str(item.get("name", f"SN-{sn}")),
                sn=sn,
                resolution=validate_resolution(str(item.get("resolution", default_resolution))) or default_resolution,
                schedule_note=sched,
                total_episodes=item.get("total_episodes"),
                online_episodes=item.get("online_episodes"),
                status=status,
                enabled=bool(item.get("enabled", True)),
                added_at=str(item.get("added_at", "") or ""),
                latest_upload_at=str(item.get("latest_upload_at", "") or ""),
                views=views,
            )
        )
    return items


def sort_watchlist(
    items: list[AnimeWatchItem],
    sort_by: str = "added_at",
    desc: bool = False,
    missing_counts: dict[str, int] | None = None,
) -> list[AnimeWatchItem]:
    """依使用者設定的持久化條件排序追番清單（無值項目自動穩定排後）。"""
    missing_map = missing_counts or {}
    indexed = list(enumerate(items))

    def _key(pair: tuple[int, AnimeWatchItem]):
        idx, it = pair
        if sort_by == "added_at":
            return (it.added_at or f"0000-{idx:05d}", idx)
        if sort_by == "tracking":
            return (1 if it.enabled else 0, -idx if desc else idx)
        if sort_by == "missing":
            return (missing_map.get(it.sn, -1), 1 if it.enabled else 0, -idx if desc else idx)
        if sort_by == "latest_upload":
            return (it.latest_upload_at or "", -idx if desc else idx)
        if sort_by == "total_episodes":
            return (it.total_episodes or it.online_episodes or 0, -idx if desc else idx)
        if sort_by == "popularity":
            return (it.views or 0, -idx if desc else idx)
        if sort_by == "name":
            return (it.name.lower(), idx)
        return (idx,)

    return [it for _, it in sorted(indexed, key=_key, reverse=desc)]


def resolve_targets_from_displayed(
    displayed_items: list[AnimeWatchItem],
    targets: list[str],
) -> list[AnimeWatchItem]:
    """依畫面目前排序後的編號 (#1, #2...) 或 SN，解析成對應的作品物件清單（去重保序）。"""
    matched: list[AnimeWatchItem] = []
    seen_sns: set[str] = set()
    for t in targets:
        tok = t.strip()
        if not tok:
            continue
        found = None
        if tok.isdigit():
            idx = int(tok)
            if 1 <= idx <= len(displayed_items):
                found = displayed_items[idx - 1]
        if found is None:
            found = next((it for it in displayed_items if it.sn == tok), None)
        if found and found.sn not in seen_sns:
            seen_sns.add(found.sn)
            matched.append(found)
    return matched


class WatchlistStore:
    """專責管理 watchlist.json 讀寫與增刪改查。"""

    def __init__(self, watchlist_path: Path, sample_path: Path | None = None) -> None:
        self.watchlist_path = watchlist_path
        self.sample_path = sample_path

    def _read_raw_list(self, path: Path) -> list[dict[str, Any]]:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("items", "watchlist"):
                if isinstance(raw.get(key), list):
                    return raw[key]
        return []

    def load(
        self,
        default_resolution: str = "1080",
        legacy_items: list[dict[str, Any]] | None = None,
    ) -> tuple[list[AnimeWatchItem], bool]:
        migrated = False
        if self.watchlist_path.is_file():
            raw_list = self._read_raw_list(self.watchlist_path)
            items = parse_watch_items(raw_list, default_resolution)
            needs_resave = any(
                isinstance(r, dict) and ("added_at" not in r or "latest_upload_at" not in r or "views" not in r)
                for r in raw_list
            )
            if legacy_items:
                existing_sns = {w.sn for w in items}
                extra = [w for w in parse_watch_items(legacy_items, default_resolution) if w.sn not in existing_sns]
                if extra:
                    items.extend(extra)
                    needs_resave = True
                migrated = True
            if needs_resave:
                self.save(items)
            return items, migrated

        if legacy_items is not None:
            items = parse_watch_items(legacy_items, default_resolution)
            self.save(items)
            return items, True

        if self.sample_path and self.sample_path.is_file():
            items = parse_watch_items(self._read_raw_list(self.sample_path), default_resolution)
            if items:
                self.save(items)
                return items, False

        items = default_watchlist()
        self.save(items)
        return items, False

    def save(self, items: list[AnimeWatchItem]) -> None:
        payload = {
            "version": 2,
            "items": [asdict(item) for item in items],
        }
        self.watchlist_path.parent.mkdir(parents=True, exist_ok=True)
        with self.watchlist_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def add_or_update(self, items: list[AnimeWatchItem], item: AnimeWatchItem) -> tuple[list[AnimeWatchItem], bool]:
        for idx, existing in enumerate(items):
            if existing.sn == item.sn or existing.name == item.name:
                if not item.added_at:
                    item.added_at = existing.added_at or now_timestamp()
                items[idx] = item
                self.save(items)
                return items, False
        if not item.added_at:
            item.added_at = now_timestamp()
        items.append(item)
        self.save(items)
        return items, True

    def remove_by_sns(self, items: list[AnimeWatchItem], target_sns: set[str]) -> list[AnimeWatchItem]:
        removed = [it for it in items if it.sn in target_sns]
        if removed:
            items[:] = [it for it in items if it.sn not in target_sns]
            self.save(items)
        return removed

    def toggle_by_sns(self, items: list[AnimeWatchItem], target_sns: set[str]) -> list[AnimeWatchItem]:
        toggled: list[AnimeWatchItem] = []
        for it in items:
            if it.sn in target_sns:
                it.enabled = not it.enabled
                toggled.append(it)
        if toggled:
            self.save(items)
        return toggled
