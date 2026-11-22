"""設定檔與追番清單管理模組（manager_config.json）。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AnimeWatchItem:
    name: str
    sn: str
    resolution: str = "1080"
    schedule_note: str = ""
    total_episodes: int | None = None
    enabled: bool = True


def default_notifications() -> dict[str, Any]:
    """支援三種推播管道獨立開關，可單選也可同時啟用多個。"""
    return {
        "ntfy": {
            "enabled": False,
            "endpoint": "",
        },
        "discord": {
            "enabled": False,
            "webhook_url": "",
        },
        "telegram": {
            "enabled": False,
            "token": "",
            "chat_id": "",
        },
    }


@dataclass
class ManagerConfig:
    anigamer_dir: str = ""  # 留空自動偵測（同目錄或 vendor/aniGamerPlus）
    bangumi_dir: str = "bangumi"
    default_resolution: str = "1080"  # 預設優先嘗試 1080P，非 VIP 時 aniGamerPlus 自動降級 720P
    cooldown_seconds: int = 10
    dedup_mode: str = "file_only"  # "file_only" (刪檔就重抓) | "file_and_history" (比對檔案+歷史+DB)
    upgrade_lower_resolution: bool = False
    auto_check_audio: bool = True
    auto_fix_audio: bool = False
    notifications: dict[str, Any] = field(default_factory=default_notifications)
    watchlist: list[AnimeWatchItem] = field(default_factory=list)


def default_config() -> ManagerConfig:
    return ManagerConfig(
        watchlist=[
            AnimeWatchItem(
                name="學生會也有洞！",
                sn="51813",
                resolution="1080",
                schedule_note="每週日 01:00 更新（動畫瘋）",
                total_episodes=12,
                enabled=True,
            )
        ]
    )


def extract_sn_from_input(raw_input: str) -> str | None:
    """同時支援輸入純 SN 數字或完整動畫瘋網址（https://ani.gamer.com.tw/animeVideo.php?sn=51813）。"""
    text = raw_input.strip()
    if text.isdigit():
        return text
    match = re.search(r"[?&]sn=(\d+)", text)
    if match:
        return match.group(1)
    return None


def _migrate_notifications(raw: dict[str, Any]) -> dict[str, Any]:
    """相容舊版單一 notification 欄位，平滑升級為多管道 notifications 結構。"""
    base = default_notifications()

    if isinstance(raw.get("notifications"), dict):
        src = raw["notifications"]
        for key in ("ntfy", "discord", "telegram"):
            if isinstance(src.get(key), dict):
                base[key].update(src[key])
        return base

    legacy = raw.get("notification")
    if isinstance(legacy, dict) and legacy.get("enabled"):
        provider = str(legacy.get("provider", "none")).lower()
        if provider == "ntfy":
            base["ntfy"]["enabled"] = True
            base["ntfy"]["endpoint"] = legacy.get("endpoint", "")
        elif provider == "discord":
            base["discord"]["enabled"] = True
            base["discord"]["webhook_url"] = legacy.get("endpoint", "")
        elif provider == "telegram":
            base["telegram"]["enabled"] = True
            base["telegram"]["token"] = legacy.get("token", "")
            base["telegram"]["chat_id"] = legacy.get("chat_id", "")

    return base


class ConfigStore:
    """讀寫 manager_config.json，不干擾 aniGamerPlus 原生 config.json。"""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path

    def load(self) -> ManagerConfig:
        if not self.config_path.is_file():
            cfg = default_config()
            self.save(cfg)
            return cfg

        with self.config_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        watchlist = [
            AnimeWatchItem(
                name=item.get("name", f"SN-{item.get('sn', '')}"),
                sn=str(item.get("sn", "")).strip(),
                resolution=str(item.get("resolution", raw.get("default_resolution", "1080"))),
                schedule_note=item.get("schedule_note", ""),
                total_episodes=item.get("total_episodes"),
                enabled=bool(item.get("enabled", True)),
            )
            for item in raw.get("watchlist", [])
            if item.get("sn")
        ]

        dedup_mode = str(raw.get("dedup_mode", "file_only"))
        if dedup_mode not in ("file_only", "file_and_history"):
            dedup_mode = "file_only"

        cfg = ManagerConfig(
            anigamer_dir=str(raw.get("anigamer_dir", "")),
            bangumi_dir=raw.get("bangumi_dir", "bangumi"),
            default_resolution=str(raw.get("default_resolution", "1080")),
            cooldown_seconds=int(raw.get("cooldown_seconds", 10)),
            dedup_mode=dedup_mode,
            upgrade_lower_resolution=bool(raw.get("upgrade_lower_resolution", False)),
            auto_check_audio=bool(raw.get("auto_check_audio", True)),
            auto_fix_audio=bool(raw.get("auto_fix_audio", False)),
            notifications=_migrate_notifications(raw),
            watchlist=watchlist,
        )
        return cfg

    def save(self, config: ManagerConfig) -> None:
        payload = asdict(config)
        with self.config_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def add_or_update_anime(self, config: ManagerConfig, item: AnimeWatchItem) -> ManagerConfig:
        for idx, existing in enumerate(config.watchlist):
            if existing.sn == item.sn or existing.name == item.name:
                config.watchlist[idx] = item
                self.save(config)
                return config
        config.watchlist.append(item)
        self.save(config)
        return config

    def remove_anime(self, config: ManagerConfig, sn_or_index: str) -> bool:
        target = sn_or_index.strip()
        for idx, item in enumerate(config.watchlist):
            if item.sn == target or str(idx + 1) == target:
                config.watchlist.pop(idx)
                self.save(config)
                return True
        return False

    def toggle_anime(self, config: ManagerConfig, sn_or_index: str) -> AnimeWatchItem | None:
        target = sn_or_index.strip()
        for idx, item in enumerate(config.watchlist):
            if item.sn == target or str(idx + 1) == target:
                item.enabled = not item.enabled
                self.save(config)
                return item
        return None
