"""多管道通知外掛模組（支援同時啟用 ntfy / Discord Webhook / Telegram Bot）。"""

from __future__ import annotations

import json
import urllib.request
from typing import Any


class Notifier:
    """獨立管理三種手機推播管道，可單選或同時多管齊下。"""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}

    @property
    def enabled_providers(self) -> list[str]:
        active: list[str] = []
        for name in ("ntfy", "discord", "telegram"):
            cfg = self.settings.get(name, {})
            if isinstance(cfg, dict) and cfg.get("enabled"):
                active.append(name)
        return active

    @property
    def enabled(self) -> bool:
        return bool(self.enabled_providers)

    def _send_ntfy(self, cfg: dict[str, Any], title: str, message: str) -> tuple[bool, str]:
        endpoint = str(cfg.get("endpoint", "")).strip()
        if not endpoint:
            return False, "ntfy：缺少 endpoint 網址"
        req = urllib.request.Request(
            endpoint,
            data=message.encode("utf-8"),
            headers={"Title": title.encode("utf-8")},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            return True, f"ntfy：已發送至 {endpoint}"

    def _send_discord(self, cfg: dict[str, Any], title: str, message: str) -> tuple[bool, str]:
        webhook = str(cfg.get("webhook_url") or cfg.get("endpoint") or "").strip()
        if not webhook:
            return False, "discord：缺少 webhook_url"
        body = json.dumps({"content": f"**{title}**\n{message}"}).encode("utf-8")
        req = urllib.request.Request(
            webhook,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "aniGamerPlus-Manager/1.3"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            return True, "discord：已發送至 Webhook"

    def _send_telegram(self, cfg: dict[str, Any], title: str, message: str) -> tuple[bool, str]:
        token = str(cfg.get("token", "")).strip()
        chat_id = str(cfg.get("chat_id", "")).strip()
        if not token or not chat_id:
            return False, "telegram：缺少 token 或 chat_id"
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps({"chat_id": chat_id, "text": f"{title}\n{message}"}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            return True, "telegram：已發送至指定對話"

    def send(self, title: str, message: str) -> list[tuple[str, bool, str]]:
        """對所有已啟用的管道逐一發送通知，回傳每個管道的結果。"""
        active = self.enabled_providers
        if not active:
            return [("none", False, "所有推播管道目前皆為關閉狀態（可在 manager_config.json 的 notifications 區塊分別開啟）。")]

        results: list[tuple[str, bool, str]] = []
        dispatch = {
            "ntfy": self._send_ntfy,
            "discord": self._send_discord,
            "telegram": self._send_telegram,
        }
        for name in active:
            cfg = self.settings.get(name, {})
            try:
                ok, msg = dispatch[name](cfg, title, message)
                results.append((name, ok, msg))
            except Exception as exc:
                results.append((name, False, f"{name}：發送失敗 ({exc})"))
        return results
