"""通知外掛介面（預設關閉，日後要接手機推播時無需改動核心邏輯）。"""

from __future__ import annotations

import json
import urllib.request
from typing import Any


class Notifier:
    """輕量通知模組：預留 ntfy / Discord Webhook / Telegram Bot 擴充點。"""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}
        self.enabled = bool(self.settings.get("enabled", False))
        self.provider = str(self.settings.get("provider", "none")).lower()

    def send(self, title: str, message: str) -> bool:
        if not self.enabled or self.provider == "none":
            return False

        try:
            if self.provider == "ntfy":
                endpoint = str(self.settings.get("endpoint", "")).strip()
                if not endpoint:
                    return False
                req = urllib.request.Request(
                    endpoint,
                    data=message.encode("utf-8"),
                    headers={"Title": title.encode("utf-8")},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10):
                    return True

            if self.provider == "discord":
                endpoint = str(self.settings.get("endpoint", "")).strip()
                if not endpoint:
                    return False
                body = json.dumps({"content": f"**{title}**\n{message}"}).encode("utf-8")
                req = urllib.request.Request(
                    endpoint,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10):
                    return True

            if self.provider == "telegram":
                token = str(self.settings.get("token", "")).strip()
                chat_id = str(self.settings.get("chat_id", "")).strip()
                if not token or not chat_id:
                    return False
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                body = json.dumps({"chat_id": chat_id, "text": f"{title}\n{message}"}).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10):
                    return True
        except Exception:
            return False

        return False
