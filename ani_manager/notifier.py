"""通知外掛介面（預設關閉，支援 ntfy / Discord Webhook / Telegram Bot）。"""

from __future__ import annotations

import json
import urllib.request
from typing import Any


class Notifier:
    """輕量通知模組：下載完成時推播到手機或通訊軟體。"""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}
        self.enabled = bool(self.settings.get("enabled", False))
        self.provider = str(self.settings.get("provider", "none")).lower()

    def send(self, title: str, message: str) -> tuple[bool, str]:
        if not self.enabled or self.provider == "none":
            return False, "通知功能目前為關閉狀態（請在 manager_config.json 設定 enabled: true 與 provider）。"

        try:
            if self.provider == "ntfy":
                endpoint = str(self.settings.get("endpoint", "")).strip()
                if not endpoint:
                    return False, "缺少 ntfy endpoint 網址。"
                req = urllib.request.Request(
                    endpoint,
                    data=message.encode("utf-8"),
                    headers={"Title": title.encode("utf-8")},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10):
                    return True, f"已透過 ntfy 發送至 {endpoint}"

            if self.provider == "discord":
                endpoint = str(self.settings.get("endpoint", "")).strip()
                if not endpoint:
                    return False, "缺少 Discord Webhook 網址。"
                body = json.dumps({"content": f"**{title}**\n{message}"}).encode("utf-8")
                req = urllib.request.Request(
                    endpoint,
                    data=body,
                    headers={"Content-Type": "application/json", "User-Agent": "aniGamerPlus-Manager/1.1"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10):
                    return True, "已透過 Discord Webhook 發送。"

            if self.provider == "telegram":
                token = str(self.settings.get("token", "")).strip()
                chat_id = str(self.settings.get("chat_id", "")).strip()
                if not token or not chat_id:
                    return False, "缺少 Telegram token 或 chat_id。"
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                body = json.dumps({"chat_id": chat_id, "text": f"{title}\n{message}"}).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10):
                    return True, "已透過 Telegram Bot 發送。"
        except Exception as exc:
            return False, f"發送通知失敗：{exc}"

        return False, f"不支援的通知類型：{self.provider}"
