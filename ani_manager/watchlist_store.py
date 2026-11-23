"""獨立追番清單儲存與操作模組（watchlist.json，無敏感金鑰，可獨立備份或跨機同步）。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
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
            )
        )
    return items


class WatchlistStore:
    """專責管理 watchlist.json 讀寫與增刪改查，與 manager_config.json 與 download_history.json 完全分離。"""

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
        """
        載入追番清單；回傳 (items, migrated)。
        - 若存在舊版 manager_config.json 遷移來的 legacy_items：
          * watchlist.json 不存在時，直接完整寫入 watchlist.json。
          * watchlist.json 已存在時，僅補入尚未存在的 SN，不覆蓋現有設定。
        """
        migrated = False
        if self.watchlist_path.is_file():
            items = parse_watch_items(self._read_raw_list(self.watchlist_path), default_resolution)
            if legacy_items:
                existing_sns = {w.sn for w in items}
                extra = [w for w in parse_watch_items(legacy_items, default_resolution) if w.sn not in existing_sns]
                if extra:
                    items.extend(extra)
                    self.save(items)
                migrated = True
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
            "version": 1,
            "items": [asdict(item) for item in items],
        }
        self.watchlist_path.parent.mkdir(parents=True, exist_ok=True)
        with self.watchlist_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def add_or_update(self, items: list[AnimeWatchItem], item: AnimeWatchItem) -> tuple[list[AnimeWatchItem], bool]:
        for idx, existing in enumerate(items):
            if existing.sn == item.sn or existing.name == item.name:
                items[idx] = item
                self.save(items)
                return items, False
        items.append(item)
        self.save(items)
        return items, True

    def find_by_target(self, items: list[AnimeWatchItem], target: str) -> AnimeWatchItem | None:
        t = target.strip()
        for idx, item in enumerate(items):
            if item.sn == t or str(idx + 1) == t:
                return item
        return None

    def remove_many(self, items: list[AnimeWatchItem], targets: list[str]) -> list[AnimeWatchItem]:
        indices: set[int] = set()
        for t in targets:
            for idx, item in enumerate(items):
                if item.sn == t or str(idx + 1) == t:
                    indices.add(idx)
        removed: list[AnimeWatchItem] = []
        for idx in sorted(indices, reverse=True):
            removed.append(items.pop(idx))
        if removed:
            self.save(items)
        return list(reversed(removed))

    def toggle_many(self, items: list[AnimeWatchItem], targets: list[str]) -> list[AnimeWatchItem]:
        toggled: list[AnimeWatchItem] = []
        seen: set[int] = set()
        for t in targets:
            for idx, item in enumerate(items):
                if idx not in seen and (item.sn == t or str(idx + 1) == t):
                    seen.add(idx)
                    item.enabled = not item.enabled
                    toggled.append(item)
        if toggled:
            self.save(items)
        return toggled
