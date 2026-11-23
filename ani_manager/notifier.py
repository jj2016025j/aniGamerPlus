"""四管道手機推播模組（ntfy / Discord / Telegram / LINE）與行動友善排版報告產生器。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator

from .episode_fetcher import EpisodeInfo, format_episode_ranges

PROVIDER_LABELS = {
    "ntfy": "ntfy",
    "discord": "Discord",
    "telegram": "Telegram",
    "line": "LINE",
}


@dataclass
class DownloadedRecord:
    title: str
    episode: EpisodeInfo
    resolution_label: str = ""


@dataclass
class DigestEntry:
    title: str
    progress: str  # e.g. "8/12"
    schedule: str = ""  # e.g. "每週日 01:00"
    missing_text: str = ""  # e.g. "第 8 集" or "第 1~5 集"
    missing_count: int = 0


def format_grouped_downloads(records: list[DownloadedRecord]) -> list[tuple[str, str]]:
    """依作品與畫質標籤分組並壓縮區間，回傳 [(作品名, 明細行), ...]。"""
    if not records:
        return []

    groups: dict[tuple[str, str], list[EpisodeInfo]] = {}
    order: list[tuple[str, str]] = []
    for rec in records:
        key = (rec.title, rec.resolution_label)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(rec.episode)

    rows: list[tuple[str, str]] = []
    for title, res_label in order:
        eps = groups[(title, res_label)]
        range_str = format_episode_ranges(eps, compact=False)
        suffix = f"｜{res_label}" if res_label else ""
        rows.append((title, f"{range_str}{suffix}"))
    return rows


def format_check_digest(
    new_entries: list[DigestEntry],
    up_to_date_entries: list[DigestEntry],
    paused_names: list[str],
    errors: list[str] | None = None,
) -> tuple[str, str]:
    """產生結構化、適合手機螢幕閱讀的「追番進度簡報」。"""
    now_str = datetime.now().strftime("%m/%d %H:%M")
    title = f"動畫瘋追番簡報 ({now_str})"
    sections: list[str] = []

    total_new_eps = sum(e.missing_count for e in new_entries)
    if new_entries:
        sections.append(f"摘要：{len(new_entries)} 部有新集數（共 {total_new_eps} 集可下載）")
    else:
        sections.append("摘要：追蹤中作品皆已下載，尚無新集數")

    if new_entries:
        block = [f"[待下載新集數] ({len(new_entries)} 部)"]
        for idx, e in enumerate(new_entries, 1):
            meta_parts = [f"待補 {e.missing_text}", f"進度 {e.progress}"]
            if e.schedule:
                meta_parts.append(e.schedule)
            block.append(f"{idx}. {e.title}")
            block.append("   " + "｜".join(meta_parts))
        sections.append("\n".join(block))

    if up_to_date_entries:
        block = [f"[已下載] ({len(up_to_date_entries)} 部)"]
        for idx, e in enumerate(up_to_date_entries, 1):
            meta_parts = [f"進度 {e.progress}"]
            if e.schedule:
                meta_parts.append(e.schedule)
            block.append(f"{idx}. {e.title}")
            block.append("   " + "｜".join(meta_parts))
        sections.append("\n".join(block))

    if paused_names:
        block = [f"[略過清單] ({len(paused_names)} 部)"]
        block.extend(f"• {name}" for name in paused_names)
        sections.append("\n".join(block))

    if errors:
        block = [f"[檢查異常] ({len(errors)} 項)"]
        block.extend(f"• {err}" for err in errors)
        sections.append("\n".join(block))

    return title, "\n\n".join(sections)


def format_download_report(
    downloaded: list[DownloadedRecord],
    failed: list[str],
    remaining: list[str],
    interrupted: bool = False,
) -> tuple[str, str]:
    """產生結構化、行動友善的「下載完成報告」或「下載中斷結算」。"""
    now_str = datetime.now().strftime("%m/%d %H:%M")
    heading = "動畫瘋下載中斷結算" if interrupted else "動畫瘋下載完成報告"
    title = f"{heading} ({now_str})"
    sections: list[str] = []

    if downloaded:
        grouped = format_grouped_downloads(downloaded)
        block = [f"[本次已下載] ({len(downloaded)} 集)"]
        for idx, (anime_title, detail) in enumerate(grouped, 1):
            block.append(f"{idx}. {anime_title}")
            block.append(f"   {detail}")
        sections.append("\n".join(block))

    if failed:
        block = [f"[失敗 / 中斷] ({len(failed)} 項)"]
        block.extend(f"• {item}" for item in failed)
        sections.append("\n".join(block))

    if remaining:
        block = [f"[尚有未下載] ({len(remaining)} 部)"]
        block.extend(f"• {item}" for item in remaining)
        sections.append("\n".join(block))

    if not sections:
        sections.append("本次檢查未執行任何新下載。")

    return title, "\n\n".join(sections)


def format_anime_added_notice(
    name: str,
    sn: str,
    online_count: int,
    total_episodes: int | None,
    schedule_note: str,
    resolution: str,
) -> tuple[str, str]:
    """產生「新增追番通知」內容。"""
    title = f"已新增追番：{name}"
    progress = f"{online_count}/{total_episodes}" if total_episodes else f"{online_count}/?"
    sched_str = schedule_note or "未設定"
    msg = (
        f"作品：{name}\n"
        f"基準 SN：{sn}｜畫質：{resolution}P\n"
        f"線上進度：{progress} 集｜狀態：{sched_str}"
    )
    return title, msg


class Notifier:
    """獨立管理四種手機推播管道，支援逐管道即時回報進度與精簡輸出。"""

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
            return False, "缺少 endpoint 網址"
        req = urllib.request.Request(
            endpoint,
            data=message.encode("utf-8"),
            headers={"Title": title.encode("utf-8")},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            return True, "已送出"

    def _send_discord(self, cfg: dict[str, Any], title: str, message: str) -> tuple[bool, str]:
        webhook = str(cfg.get("webhook_url") or cfg.get("endpoint") or "").strip()
        if not webhook:
            return False, "缺少 webhook_url"
        body = json.dumps({"content": f"**{title}**\n{message}"}).encode("utf-8")
        req = urllib.request.Request(
            webhook,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "aniGamerPlus-Manager/1.6.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            return True, "已送出"

    def _send_telegram(self, cfg: dict[str, Any], title: str, message: str) -> tuple[bool, str]:
        token = str(cfg.get("token", "")).strip()
        chat_id = str(cfg.get("chat_id", "")).strip()
        if not token or not chat_id:
            return False, "缺少 token 或 chat_id"
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps({"chat_id": chat_id, "text": f"{title}\n\n{message}"}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            return True, "已送出"

    def _send_line(self, cfg: dict[str, Any], title: str, message: str) -> tuple[bool, str]:
        token = str(cfg.get("channel_access_token") or cfg.get("token") or "").strip()
        user_id = str(cfg.get("user_id") or cfg.get("to") or "").strip()
        if not token or not user_id:
            return False, "缺少 channel_access_token 或 user_id"
        if not user_id.startswith(("U", "C", "R")) or len(user_id) < 20:
            return False, "user_id 格式不正確（須為大寫 U 開頭約 33 碼）"

        url = "https://api.line.me/v2/bot/message/push"
        text_payload = f"【{title}】\n\n{message}"[:4900]
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
                return True, "已送出"
        except urllib.error.HTTPError as exc:
            hints = {
                401: "Token 無效或過期（請由 Messaging API 分頁重新 Issue）",
                400: "user_id 錯誤或尚未加官方帳號好友",
                429: "本月免費額度已用完",
            }
            return False, f"HTTP {exc.code}（{hints.get(exc.code, exc.reason)}）"

    def send_iter(self, title: str, message: str) -> Iterator[tuple[str, bool, str]]:
        """逐一發送並即時 yield 每個管道的結果 (provider_label, ok, detail)。"""
        active = self.enabled_providers
        if not active:
            yield ("系統", False, "所有推播管道皆為關閉狀態")
            return

        dispatch: dict[str, Callable[[dict[str, Any], str, str], tuple[bool, str]]] = {
            "ntfy": self._send_ntfy,
            "discord": self._send_discord,
            "telegram": self._send_telegram,
            "line": self._send_line,
        }
        for name in active:
            label = PROVIDER_LABELS.get(name, name)
            cfg = self.settings.get(name, {})
            try:
                ok, detail = dispatch[name](cfg, title, message)
                yield (label, ok, detail)
            except Exception as exc:
                yield (label, False, str(exc))

    def send(self, title: str, message: str) -> list[tuple[str, bool, str]]:
        return list(self.send_iter(title, message))
