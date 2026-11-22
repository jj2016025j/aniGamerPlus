"""多管道通知外掛模組（支援同時啟用 ntfy / Discord Webhook / Telegram Bot / LINE Messaging API）。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class Notifier:
    """獨立管理四種手機推播管道，可單選或同時多管齊下。"""

    PROVIDERS = ("ntfy", "discord", "telegram", "line")

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}

    @property
    def enabled_providers(self) -> list[str]:
        active: list[str] = []
        for name in self.PROVIDERS:
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
            headers={"Content-Type": "application/json", "User-Agent": "aniGamerPlus-Manager/1.3.1"},
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

    def _send_line(self, cfg: dict[str, Any], title: str, message: str) -> tuple[bool, str]:
        token = str(cfg.get("channel_access_token") or cfg.get("token") or "").strip()
        user_id = str(cfg.get("user_id") or cfg.get("to") or "").strip()
        if not token or not user_id:
            return False, "line：缺少 channel_access_token 或 user_id"
        if not user_id.startswith(("U", "C", "R")) or len(user_id) < 20:
            return (
                False,
                "line：user_id 格式不正確（必須是 LINE Developers 後台 Basic settings 最下方的 Your user ID，U 開頭約 33 碼，不是加好友用的 LINE ID）",
            )

        url = "https://api.line.me/v2/bot/message/push"
        text_payload = f"【{title}】\n{message}"[:4900]
        body = json.dumps(
            {
                "to": user_id,
                "messages": [{"type": "text", "text": text_payload}],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                return True, "line：已透過 Messaging API 推播至 LINE"
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                err_body = exc.read().decode("utf-8", errors="ignore")
                err_json = json.loads(err_body)
                detail = err_json.get("message", err_body)
            except Exception:
                detail = str(exc.reason)

            hints = {
                401: "Channel access token 無效或已過期，請重新檢查（不含多餘空白）。",
                400: "user_id 錯誤或尚未將此官方帳號加為好友。",
                429: "本月免費推播額度（200 則）已用完。",
            }
            hint = hints.get(exc.code, detail)
            return False, f"line：HTTP {exc.code} 失敗（{hint}）"

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
            "line": self._send_line,
        }
        for name in active:
            cfg = self.settings.get(name, {})
            try:
                ok, msg = dispatch[name](cfg, title, message)
                results.append((name, ok, msg))
            except Exception as exc:
                results.append((name, False, f"{name}：發送失敗 ({exc})"))
        return results
