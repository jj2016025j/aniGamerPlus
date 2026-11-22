"""四管道手機推播模組（ntfy / Discord / Telegram / LINE）與結構化分組報告產生器。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .episode_fetcher import EpisodeInfo, format_episode_ranges


@dataclass
class DownloadedRecord:
    title: str
    episode: EpisodeInfo
    resolution_label: str = ""  # 例如 "720P" / "1080P" / "1080P升級"


def format_grouped_downloads(records: list[DownloadedRecord]) -> list[str]:
    """
    依作品與畫質標籤分組，並將連續集數壓縮為區間，避免長片名重複洗版：
    - 單集：學生會也有洞！ 第 8 集 (720P)
    - 連續多集：被放逐的作弊賦予魔術師享受隨心所欲的第二人生。 第 1~5 集 (720P)
    """
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

    lines: list[str] = []
    for title, res_label in order:
        eps = groups[(title, res_label)]
        range_str = format_episode_ranges(eps, compact=False)
        suffix = f" ({res_label})" if res_label else ""
        lines.append(f"《{title}》{range_str}{suffix}")
    return lines


def format_check_digest(
    new_updates: list[str],
    up_to_date: list[str],
    paused_names: list[str],
    errors: list[str] | None = None,
) -> tuple[str, str]:
    """產生「追番檢查／上線簡報」通知內容。"""
    now_str = datetime.now().strftime("%m/%d %H:%M")
    title = f"動畫瘋追番簡報 ({now_str})"
    lines: list[str] = []

    if new_updates:
        lines.append("【有新集數可下載】")
        lines.extend(f"• {row}" for row in new_updates)
    else:
        lines.append("【目前沒有未下載的新集數】")

    if up_to_date:
        lines.append("")
        lines.append("【已追平最新進度】")
        lines.extend(f"• {row}" for row in up_to_date)

    if paused_names:
        lines.append("")
        lines.append(f"【暫停追蹤 ({len(paused_names)} 部)】" + "、".join(paused_names))

    if errors:
        lines.append("")
        lines.append("【檢查異常】")
        lines.extend(f"• {err}" for err in errors)

    return title, "\n".join(lines)


def format_download_report(
    downloaded: list[DownloadedRecord],
    failed: list[str],
    remaining: list[str],
    interrupted: bool = False,
) -> tuple[str, str]:
    """產生「下載完成報告」或「下載中斷結算」通知內容（依作品分組＋區間壓縮）。"""
    now_str = datetime.now().strftime("%m/%d %H:%M")
    heading = "動畫瘋下載中斷結算" if interrupted else "動畫瘋下載完成報告"
    title = f"{heading} ({now_str})"
    lines: list[str] = []

    if downloaded:
        grouped_lines = format_grouped_downloads(downloaded)
        lines.append(f"【本次已下載 ({len(downloaded)} 集)】")
        lines.extend(f"• {row}" for row in grouped_lines)

    if failed:
        if lines:
            lines.append("")
        lines.append(f"【下載失敗／中斷 ({len(failed)} 集)】")
        lines.extend(f"• {item}" for item in failed)

    if remaining:
        if lines:
            lines.append("")
        lines.append("【尚有未下載集數】")
        lines.extend(f"• {item}" for item in remaining)

    if not lines:
        lines.append("本次檢查未執行任何新下載。")

    return title, "\n".join(lines)


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
    total_str = f" / 預計全 {total_episodes} 話" if total_episodes else ""
    sched_str = schedule_note or "未設定（已完結或不定時）"
    msg = (
        f"• 作品：{name} (SN: {sn})\n"
        f"• 目前線上可下載：{online_count} 集{total_str}\n"
        f"• 目標畫質：{resolution}P\n"
        f"• 更新時間：{sched_str}"
    )
    return title, msg


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
            headers={"Content-Type": "application/json", "User-Agent": "aniGamerPlus-Manager/1.4.2"},
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
                "line：user_id 格式不正確（必須是 LINE Developers 後台 Basic settings 最下方的 Your user ID，大寫 U 開頭約 33 碼）",
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
                401: "Channel access token 無效或已過期（請從 Messaging API 分頁最下方重新 Issue，勿填頻道秘密）。",
                400: "user_id 錯誤或尚未將此官方帳號加為好友。",
                429: "本月免費推播額度（200 則）已用完。",
            }
            hint = hints.get(exc.code, detail)
            return False, f"line：HTTP {exc.code} 失敗（{hint}）"

    def send(self, title: str, message: str) -> list[tuple[str, bool, str]]:
        active = self.enabled_providers
        if not active:
            return [("none", False, "所有推播管道目前皆為關閉狀態（可在 manager_config.json 的 notifications 區塊開啟）。")]

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
