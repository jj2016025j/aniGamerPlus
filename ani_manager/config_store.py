"""系統偏好、排序設定與推播金鑰模組（manager_config.json）。"""

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

# 追番清單排序選項：(sort_key, 中文名稱, 預設是否降冪)
WATCHLIST_SORT_OPTIONS: list[tuple[str, str, bool]] = [
    ("added_at", "加入順序", False),
    ("tracking", "追蹤優先", True),
    ("missing", "待下載集數", True),
    ("latest_upload", "最新上架 / 完結時間", True),
    ("total_episodes", "總集數", True),
    ("popularity", "熱門程度（觀看數）", True),
    ("name", "作品名稱", False),
]
VALID_SORT_KEYS = {k for k, _, _ in WATCHLIST_SORT_OPTIONS}
SORT_LABELS = {k: label for k, label, _ in WATCHLIST_SORT_OPTIONS}


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
    watchlist_file: str = "watchlist.json"
    watchlist_sort_by: str = "added_at"
    watchlist_sort_desc: bool = False
    default_resolution: str = "1080"
    cooldown_seconds: int = 10
    dedup_mode: str = "file_only"  # "file_only" | "file_and_history"
    upgrade_lower_resolution: bool = False
    check_on_startup: bool = True
    clear_screen_on_menu: bool = False
    auto_check_audio: bool = True
    auto_fix_audio: bool = False
    notifications: dict[str, Any] = field(default_factory=default_notifications)


def default_config() -> ManagerConfig:
    return ManagerConfig()


def extract_sn_from_input(raw_input: str) -> str | None:
    """從純 SN 或動畫瘋網址抽出 SN；少於 4 碼回傳 None 防止誤輸入。"""
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
    """支援單筆（如 3）或多筆（如 5,6 或 5 6）。"""
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
    """讀寫純淨設定檔 manager_config.json（僅保存系統偏好、排序規則與推播金鑰）。"""

    def __init__(self, config_path: Path, sample_path: Path | None = None) -> None:
        self.config_path = config_path
        self.sample_path = sample_path

    def load_with_legacy_watchlist(self) -> tuple[ManagerConfig, list[dict[str, Any]] | None]:
        if not self.config_path.is_file():
            if self.sample_path and self.sample_path.is_file():
                try:
                    with self.sample_path.open("r", encoding="utf-8") as f:
                        raw = json.load(f)
                    cfg = self._parse_raw(raw)[0]
                    self.save(cfg)
                    return cfg, None
                except Exception:
                    pass
            cfg = default_config()
            self.save(cfg)
            return cfg, None

        with self.config_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        cfg, needs_save = self._parse_raw(raw)
        legacy_watchlist = raw.get("watchlist") if isinstance(raw.get("watchlist"), list) else None
        if legacy_watchlist is not None:
            needs_save = True

        if needs_save:
            self.save(cfg)
        return cfg, legacy_watchlist

    def load(self) -> ManagerConfig:
        cfg, _ = self.load_with_legacy_watchlist()
        return cfg

    def _parse_raw(self, raw: dict[str, Any]) -> tuple[ManagerConfig, bool]:
        needs_save = False
        dedup_mode = str(raw.get("dedup_mode", "file_only"))
        if dedup_mode not in ("file_only", "file_and_history"):
            dedup_mode = "file_only"
            needs_save = True

        sort_by = str(raw.get("watchlist_sort_by", "added_at"))
        if sort_by not in VALID_SORT_KEYS:
            sort_by = "added_at"
            needs_save = True

        notifications, notif_save = _migrate_notifications(raw)
        for required_key in (
            "check_on_startup",
            "clear_screen_on_menu",
            "watchlist_file",
            "watchlist_sort_by",
            "watchlist_sort_desc",
        ):
            if required_key not in raw:
                needs_save = True
        if notif_save:
            needs_save = True

        cfg = ManagerConfig(
            anigamer_dir=str(raw.get("anigamer_dir", "")),
            bangumi_dir=str(raw.get("bangumi_dir", "bangumi")),
            watchlist_file=str(raw.get("watchlist_file", "watchlist.json") or "watchlist.json"),
            watchlist_sort_by=sort_by,
            watchlist_sort_desc=bool(raw.get("watchlist_sort_desc", False)),
            default_resolution=validate_resolution(str(raw.get("default_resolution", "1080"))) or "1080",
            cooldown_seconds=int(raw.get("cooldown_seconds", 10)),
            dedup_mode=dedup_mode,
            upgrade_lower_resolution=bool(raw.get("upgrade_lower_resolution", False)),
            check_on_startup=bool(raw.get("check_on_startup", True)),
            clear_screen_on_menu=bool(raw.get("clear_screen_on_menu", False)),
            auto_check_audio=bool(raw.get("auto_check_audio", True)),
            auto_fix_audio=bool(raw.get("auto_fix_audio", False)),
            notifications=notifications,
        )
        return cfg, needs_save

    def save(self, config: ManagerConfig) -> None:
        payload = asdict(config)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
