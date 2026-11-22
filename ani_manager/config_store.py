"""設定檔、追番清單管理與嚴格輸入驗證模組（manager_config.json）。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ALLOWED_RESOLUTIONS = ("1080", "720", "540", "360")
WEEKDAY_MAP = {
    "1": "週一",
    "2": "週二",
    "3": "週三",
    "4": "週四",
    "5": "週五",
    "6": "週六",
    "7": "週日",
}
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


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


def default_notifications() -> dict[str, Any]:
    """支援四種推播管道獨立開關（ntfy / Discord / Telegram / LINE）。"""
    return {
        "ntfy": {"enabled": False, "endpoint": ""},
        "discord": {"enabled": False, "webhook_url": ""},
        "telegram": {"enabled": False, "token": "", "chat_id": ""},
        "line": {"enabled": False, "channel_access_token": "", "user_id": ""},
    }


@dataclass
class ManagerConfig:
    anigamer_dir: str = ""
    bangumi_dir: str = "bangumi"
    default_resolution: str = "1080"
    cooldown_seconds: int = 10
    dedup_mode: str = "file_only"  # "file_only" | "file_and_history"
    upgrade_lower_resolution: bool = False
    check_on_startup: bool = True
    clear_screen_on_menu: bool = True
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
                schedule_note="每週日 01:00",
                total_episodes=12,
                online_episodes=None,
                status="連載中",
                enabled=True,
            )
        ]
    )


def extract_sn_from_input(raw_input: str) -> str | None:
    """從純 SN 或動畫瘋網址抽出 SN；若明顯太短（少於 4 碼）回傳 None 防止誤輸入。"""
    text = raw_input.strip()
    if text.isdigit():
        return text if len(text) >= 4 else None
    match = re.search(r"[?&]sn=(\d{4,})", text)
    if match:
        return match.group(1)
    return None


def validate_resolution(raw: str) -> str | None:
    cleaned = raw.strip().upper().replace("P", "")
    return cleaned if cleaned in ALLOWED_RESOLUTIONS else None


def validate_total_episodes(raw: str) -> tuple[bool, int | None, str]:
    text = raw.strip()
    if not text or text == "-":
        return True, None, ""
    if not text.isdigit():
        return False, None, "總集數必須是正整數（或直接按 Enter 留空）。"
    num = int(text)
    if num < 1 or num > 2000:
        return False, None, "總集數必須介於 1 ~ 2000 之間。"
    return True, num, ""


def validate_hhmm(raw: str) -> str | None:
    """將 1:00 / 01:00 / 0100 正規化為 HH:MM，無效時間回傳 None。"""
    text = raw.strip().replace("：", ":")
    if re.fullmatch(r"\d{4}", text):
        text = f"{text[:2]}:{text[2:]}"
    elif re.fullmatch(r"\d:\d{2}", text):
        text = "0" + text
    return text if HHMM_RE.match(text) else None


def parse_multi_targets(raw: str) -> list[str]:
    """支援單選（如 3）或多選（如 5,6 或 5 6）。"""
    return [tok.strip() for tok in re.split(r"[,，\s]+", raw.strip()) if tok.strip()]


def _migrate_notifications(raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    base = default_notifications()
    needs_save = False

    if isinstance(raw.get("notifications"), dict):
        src = raw["notifications"]
        for key in ("ntfy", "discord", "telegram", "line"):
            if isinstance(src.get(key), dict):
                base[key].update(src[key])
            else:
                needs_save = True
        return base, needs_save

    legacy = raw.get("notification")
    needs_save = True
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

    return base, needs_save


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

        needs_save = False
        watchlist: list[AnimeWatchItem] = []
        for item in raw.get("watchlist", []):
            if not item.get("sn"):
                continue
            if "online_episodes" not in item or "status" not in item:
                needs_save = True
            sched = str(item.get("schedule_note", "") or "")
            status = str(item.get("status", "") or "")
            if not status and "已完結" in sched:
                status = "已完結"
            elif not status and sched.startswith("每週"):
                status = "連載中"
            watchlist.append(
                AnimeWatchItem(
                    name=item.get("name", f"SN-{item.get('sn', '')}"),
                    sn=str(item.get("sn", "")).strip(),
                    resolution=validate_resolution(str(item.get("resolution", raw.get("default_resolution", "1080")))) or "1080",
                    schedule_note=sched,
                    total_episodes=item.get("total_episodes"),
                    online_episodes=item.get("online_episodes"),
                    status=status,
                    enabled=bool(item.get("enabled", True)),
                )
            )

        dedup_mode = str(raw.get("dedup_mode", "file_only"))
        if dedup_mode not in ("file_only", "file_and_history"):
            dedup_mode = "file_only"

        notifications, notif_save = _migrate_notifications(raw)
        if notif_save or "check_on_startup" not in raw or "clear_screen_on_menu" not in raw:
            needs_save = True

        cfg = ManagerConfig(
            anigamer_dir=str(raw.get("anigamer_dir", "")),
            bangumi_dir=raw.get("bangumi_dir", "bangumi"),
            default_resolution=validate_resolution(str(raw.get("default_resolution", "1080"))) or "1080",
            cooldown_seconds=int(raw.get("cooldown_seconds", 10)),
            dedup_mode=dedup_mode,
            upgrade_lower_resolution=bool(raw.get("upgrade_lower_resolution", False)),
            check_on_startup=bool(raw.get("check_on_startup", True)),
            clear_screen_on_menu=bool(raw.get("clear_screen_on_menu", True)),
            auto_check_audio=bool(raw.get("auto_check_audio", True)),
            auto_fix_audio=bool(raw.get("auto_fix_audio", False)),
            notifications=notifications,
            watchlist=watchlist,
        )
        if needs_save:
            self.save(cfg)
        return cfg

    def save(self, config: ManagerConfig) -> None:
        payload = asdict(config)
        with self.config_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def add_or_update_anime(self, config: ManagerConfig, item: AnimeWatchItem) -> tuple[ManagerConfig, bool]:
        for idx, existing in enumerate(config.watchlist):
            if existing.sn == item.sn or existing.name == item.name:
                config.watchlist[idx] = item
                self.save(config)
                return config, False
        config.watchlist.append(item)
        self.save(config)
        return config, True

    def find_by_target(self, config: ManagerConfig, target: str) -> AnimeWatchItem | None:
        t = target.strip()
        for idx, item in enumerate(config.watchlist):
            if item.sn == t or str(idx + 1) == t:
                return item
        return None

    def remove_many(self, config: ManagerConfig, targets: list[str]) -> list[AnimeWatchItem]:
        indices: set[int] = set()
        for t in targets:
            for idx, item in enumerate(config.watchlist):
                if item.sn == t or str(idx + 1) == t:
                    indices.add(idx)
        removed: list[AnimeWatchItem] = []
        for idx in sorted(indices, reverse=True):
            removed.append(config.watchlist.pop(idx))
        if removed:
            self.save(config)
        return list(reversed(removed))

    def toggle_many(self, config: ManagerConfig, targets: list[str]) -> list[AnimeWatchItem]:
        toggled: list[AnimeWatchItem] = []
        seen: set[int] = set()
        for t in targets:
            for idx, item in enumerate(config.watchlist):
                if idx not in seen and (item.sn == t or str(idx + 1) == t):
                    seen.add(idx)
                    item.enabled = not item.enabled
                    toggled.append(item)
        if toggled:
            self.save(config)
        return toggled
