"""設定檔與追番清單管理模組（manager_config.json）。"""

from __future__ import annotations

import json
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


@dataclass
class ManagerConfig:
    bangumi_dir: str = "bangumi"
    default_resolution: str = "1080"
    cooldown_seconds: int = 10
    upgrade_lower_resolution: bool = False
    auto_check_audio: bool = True
    auto_fix_audio: bool = False
    notification: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": False,
            "provider": "none",  # 支援: "ntfy" | "discord" | "telegram"
            "endpoint": "",
            "token": "",
            "chat_id": "",
        }
    )
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

        return ManagerConfig(
            bangumi_dir=raw.get("bangumi_dir", "bangumi"),
            default_resolution=str(raw.get("default_resolution", "1080")),
            cooldown_seconds=int(raw.get("cooldown_seconds", 10)),
            upgrade_lower_resolution=bool(raw.get("upgrade_lower_resolution", False)),
            auto_check_audio=bool(raw.get("auto_check_audio", True)),
            auto_fix_audio=bool(raw.get("auto_fix_audio", False)),
            notification=raw.get("notification", default_config().notification),
            watchlist=watchlist,
        )

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
