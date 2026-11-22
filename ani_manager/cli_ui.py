"""終端互動介面與命令列控制器（優先使用 rich，未安裝時自動降級為純文字介面）。"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .audio_tools import remux_audio_for_quicktime, scan_directory_audio
from .config_store import AnimeWatchItem, ConfigStore, ManagerConfig, extract_sn_from_input
from .cookie_manager import (
    CookieValidationResult,
    capture_cookie_via_playwright,
    inspect_cookie_file,
    open_cookie_in_editor,
    read_clipboard,
    validate_and_sanitize_cookie,
    verify_cookie_online,
    write_cookie_file,
)
from .downloader import AniGamerDownloader
from .env_detector import EnvironmentInfo, detect_environment
from .episode_fetcher import EpisodeFetchError, EpisodeInfo, fetch_anime_episodes
from .history_tracker import HistoryTracker
from .notifier import Notifier

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table

    HAS_RICH = True
    console = Console()
except ImportError:
    HAS_RICH = False
    console = None


def _print(msg: str = "") -> None:
    if HAS_RICH and console:
        console.print(msg)
    else:
        print(re.sub(r"\[/?[a-zA-Z0-9_ ]+\]", "", msg))


def _ask(label: str, default: str = "") -> str:
    if HAS_RICH:
        return Prompt.ask(label, default=default) if default else Prompt.ask(label)
    suffix = f" [{default}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    return val or default


def _confirm(label: str, default: bool = False) -> bool:
    if HAS_RICH:
        return Confirm.ask(label, default=default)
    hint = "Y/n" if default else "y/N"
    val = input(f"{label} ({hint}): ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


def _open_folder(path: Path, raw_os: str) -> None:
    try:
        if raw_os == "Windows":
            os.startfile(str(path))
        elif raw_os == "Darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        pass


class AnimeManagerApp:
    """串接各獨立模組的主控制器。"""

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = (root_dir or Path.cwd()).resolve()
        self.config_store = ConfigStore(self.root_dir / "manager_config.json")
        self.config: ManagerConfig = self.config_store.load()
        self.env: EnvironmentInfo = detect_environment(
            self.root_dir,
            self.config.bangumi_dir,
            self.config.anigamer_dir,
        )
        self.tracker = HistoryTracker(
            history_path=self.root_dir / "download_history.json",
            db_path=self.env.db_path,
            bangumi_dir=self.env.bangumi_dir,
        )
        self.downloader = AniGamerDownloader(self.env)
        self.notifier = Notifier(self.config.notifications)

    def refresh_env(self) -> None:
        self.config = self.config_store.load()
        self.env = detect_environment(
            self.root_dir,
            self.config.bangumi_dir,
            self.config.anigamer_dir,
        )
        self.tracker = HistoryTracker(
            history_path=self.root_dir / "download_history.json",
            db_path=self.env.db_path,
            bangumi_dir=self.env.bangumi_dir,
        )
        self.downloader = AniGamerDownloader(self.env)
        self.notifier = Notifier(self.config.notifications)

    def _format_cookie_summary(self) -> str:
        """純本地讀取 cookie.txt 狀態，不對巴哈伺服器發請求（避免高頻刷新觸發風控）。"""
        if not self.env.has_cookie:
            return "[yellow]未設定（預設免費 720P + 廣告）[/yellow]"
        c = inspect_cookie_file(self.env.cookie_path)
        if not c.valid:
            return "[red]格式異常（請進選單 5 重新整理）[/red]"
        if c.tier == "logged_in":
            return f"[green]已登入 ({c.baha_id_masked}｜支援 VIP 自動優先 1080P)[/green]"
        if c.tier == "partial_login":
            return f"[yellow]部分憑證 ({c.baha_id_masked}｜缺 HttpOnly，視為訪客)[/yellow]"
        return "[cyan]訪客 Cookie（免費 720P + 廣告）[/cyan]"

    def _print_cookie_result(self, result: CookieValidationResult, run_online_check: bool = True) -> None:
        """顯示 Cookie 驗證與刷新後的結果（遮罩敏感值）。"""
        if not result.valid:
            _print(f"[bold red]Cookie 檢查未通過：[/bold red]{result.reason}")
            return

        tier_label = {
            "logged_in": "[bold green]完整登入憑證[/bold green]",
            "partial_login": "[bold yellow]不完整登入憑證[/bold yellow]",
            "guest": "[bold cyan]巴哈一般訪客[/bold cyan]",
        }.get(result.tier, result.tier)
        keys_str = ", ".join(result.matched_auth_keys) if result.matched_auth_keys else "無登入欄位"
        _print(
            f"狀態：{tier_label} ｜ "
            f"帳號：[cyan]{result.baha_id_masked}[/cyan] ｜ "
            f"欄位數：{result.pair_count} ｜ "
            f"憑證：[green]{keys_str}[/green] ｜ "
            f"長度：{len(result.cleaned_cookie)} 字元"
        )
        for w in result.warnings:
            _print(f"  [yellow]提醒：{w}[/yellow]")

        if run_online_check:
            _print("[dim]正在連線動畫瘋確認目前權限...[/dim]")
            online = verify_cookie_online(result.cleaned_cookie)
            color = {"vip": "green", "logged_in": "green", "guest": "cyan", "blocked": "yellow", "error": "yellow"}.get(
                online.status, "white"
            )
            _print(f"  [{color}]{online.detail}[/{color}]")

    def show_banner(self) -> None:
        core_ok = "[green]已偵測[/green]" if self.env.anigamer_script else "[red]找不到 aniGamerPlus.py[/red]"
        ff_ok = "[green]已就緒[/green]" if self.env.has_ffmpeg else "[red]未安裝[/red]"
        notify_str = (
            f"[green]{', '.join(self.notifier.enabled_providers)}[/green]"
            if self.notifier.enabled
            else "[dim]關閉[/dim]"
        )
        dedup_str = (
            "[cyan]僅依實體檔（刪影片即重抓）[/cyan]"
            if self.config.dedup_mode == "file_only"
            else "[magenta]檔案 + 歷史 + DB（歸檔防重抓）[/magenta]"
        )

        enabled_items = [w for w in self.config.watchlist if w.enabled]
        if enabled_items:
            watch_lines = "\n".join(
                f"  • [bold]{w.name}[/bold] (SN: {w.sn}｜目標 {w.resolution}P"
                + (f"｜{w.schedule_note}" if w.schedule_note else "")
                + ")"
                for w in enabled_items
            )
        else:
            watch_lines = "  [yellow]（目前無啟用中的追番項目，請按 3 新增或切換動畫）[/yellow]"

        body = (
            f"[bold]系統：[/bold]{self.env.os_name}   "
            f"[bold]核心：[/bold]{core_ok}   "
            f"[bold]FFmpeg：[/bold]{ff_ok}   "
            f"[bold]推播：[/bold]{notify_str}\n"
            f"[bold]Cookie 狀態：[/bold]{self._format_cookie_summary()}\n"
            f"[bold]畫質策略：[/bold]預設優先 {self.config.default_resolution}P（非 VIP 自動降級 720P）   "
            f"[bold]去重模式：[/bold]{dedup_str}\n"
            f"[bold]下載目錄：[/bold]{self.env.bangumi_dir}\n"
            f"[bold]目前預設下載作品 ({len(enabled_items)} 部)：[/bold]\n{watch_lines}"
        )
        if HAS_RICH and console:
            console.print(Panel(body, title="aniGamerPlus 模組化增量管理工具 v1.2", border_style="cyan"))
        else:
            print("=" * 68)
            _print(body)
            print("=" * 68)

    def action_check_or_update(
        self,
        download: bool = True,
        single_sn: str | None = None,
        force_single_episode: bool = False,
    ) -> None:
        """檢查所有（或指定）追番作品，自動過濾已下載集數，只抓缺漏或新更新集數。"""
        if force_single_episode and single_sn:
            ep = EpisodeInfo(sn=single_sn, episode_label=f"sn={single_sn}", anime_title=f"SN-{single_sn}")
            _print(f"\n[bold blue]強制單集測試下載（忽略去重紀錄）：sn={single_sn}[/bold blue]")
            res = self.downloader.download_episode(ep, resolution=self.config.default_resolution)
            if res.success:
                _print(f"[green]單集下載完成：sn={single_sn}[/green]")
            else:
                _print(f"[red]單集下載異常：{res.message}[/red]")
            return

        items = self.config.watchlist
        if single_sn:
            items = [
                next(
                    (w for w in items if w.sn == single_sn),
                    AnimeWatchItem(name=f"SN-{single_sn}", sn=single_sn, resolution=self.config.default_resolution),
                )
            ]
        else:
            items = [w for w in items if w.enabled]

        if not items:
            _print("[yellow]追番清單目前沒有啟用中的作品，請進選單 3 新增或啟用。[/yellow]")
            return

        downloaded_summary: list[str] = []

        for item in items:
            _print(f"\n[bold cyan]>>> 正在檢查：{item.name} (SN: {item.sn})[/bold cyan]")
            if item.schedule_note:
                _print(f"[dim]更新排程備註：{item.schedule_note}[/dim]")

            try:
                title, online_eps = fetch_anime_episodes(
                    base_sn=item.sn,
                    cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                    fallback_title=item.name,
                )
            except EpisodeFetchError as exc:
                _print(f"[red]線上集數解析失敗：{exc}[/red]")
                if download and _confirm("是否改用 aniGamerPlus 原生指令直接抓「最新一集 (-m latest)」？", default=False):
                    self.downloader.download_latest_direct(item.sn, item.resolution)
                continue

            diff = self.tracker.compute_diff(
                online_episodes=online_eps,
                target_resolution=item.resolution,
                upgrade_lower_resolution=self.config.upgrade_lower_resolution,
                anime_name_hint=item.name,
                dedup_mode=self.config.dedup_mode,
            )

            total_str = f" / 預定全 {item.total_episodes} 話" if item.total_episodes else ""
            _print(
                f"  線上已上架：[bold]{len(online_eps)}[/bold] 集{total_str} ｜ "
                f"已下載：[green]{len(diff.already_downloaded)}[/green] 集 ｜ "
                f"待下載：[yellow]{len(diff.missing_episodes)}[/yellow] 集"
            )
            if diff.upgradable_episodes:
                _print(f"  可升級至 {item.resolution}P：[magenta]{len(diff.upgradable_episodes)}[/magenta] 集")

            queue: list[EpisodeInfo] = list(diff.missing_episodes)
            if diff.upgradable_episodes:
                queue.extend(ep for ep, _ in diff.upgradable_episodes)

            if not queue:
                _print("  [green]已是最新狀態，沒有需要下載的集數。[/green]")
                continue

            ep_labels = ", ".join(f"[{ep.episode_label}] (sn={ep.sn})" for ep in queue)
            _print(f"  待下載清單：{ep_labels}")

            if not download:
                continue

            def _on_start(idx: int, total: int, ep: EpisodeInfo) -> None:
                _print(f"\n[bold blue][{idx}/{total}] 開始下載：{title} [{ep.episode_label}] (sn={ep.sn})[/bold blue]")

            def _on_complete(idx: int, total: int, ep: EpisodeInfo, res) -> None:
                if res.success:
                    self.tracker.mark_downloaded(ep, item.resolution)
                    downloaded_summary.append(f"{title} [{ep.episode_label}]")
                    _print(f"[green]完成：{title} [{ep.episode_label}][/green]")
                else:
                    _print(f"[red]下載異常 ({res.message})：{title} [{ep.episode_label}][/red]")

            self.downloader.download_batch(
                episodes=queue,
                resolution=item.resolution,
                cooldown_seconds=self.config.cooldown_seconds,
                on_start=_on_start,
                on_complete=_on_complete,
            )

        if downloaded_summary:
            summary_text = "本次新下載：" + "、".join(downloaded_summary)
            _print(f"\n[bold green]{summary_text}[/bold green]")
            if self.notifier.enabled:
                for _, ok, msg in self.notifier.send("動畫瘋下載完成", summary_text):
                    _print(f"[dim]推播：{msg}[/dim]" if ok else f"[yellow]推播異常：{msg}[/yellow]")

    def action_ad_hoc_download(self) -> None:
        """不改追番清單，臨時貼一部動畫網址或 SN 直接下載（可選單集強制測試或全集增量）。"""
        raw = _ask("\n請貼上動畫瘋網址或 SN（留空取消）", default="")
        if not raw:
            return
        sn = extract_sn_from_input(raw)
        if not sn:
            _print("[red]無法從輸入中解析出 SN，請貼上含 ?sn=數字 的網址或純數字。[/red]")
            return

        _print("  [cyan]1[/cyan]. 檢查該作品全部集數並下載缺漏（會過濾已下載）")
        _print("  [cyan]2[/cyan]. 僅強制下載這個 SN 單集（忽略歷史紀錄，適合 Cookie 測試）")
        mode = _ask("請選擇模式", default="1")
        if mode == "2":
            self.action_check_or_update(download=True, single_sn=sn, force_single_episode=True)
        else:
            self.action_check_or_update(download=True, single_sn=sn, force_single_episode=False)

    def action_manage_watchlist(self) -> None:
        while True:
            _print("\n[bold]目前追番清單與設定：[/bold]")
            if HAS_RICH and console:
                table = Table(show_header=True, header_style="bold magenta")
                table.add_column("#", width=4)
                table.add_column("狀態", width=6)
                table.add_column("作品名稱", style="cyan")
                table.add_column("基準 SN")
                table.add_column("目標畫質")
                table.add_column("總集數")
                table.add_column("更新備註")
                for idx, item in enumerate(self.config.watchlist, 1):
                    st = "[green]啟用[/green]" if item.enabled else "[dim]暫停[/dim]"
                    table.add_row(
                        str(idx),
                        st,
                        item.name,
                        item.sn,
                        f"{item.resolution}P",
                        str(item.total_episodes or "-"),
                        item.schedule_note or "-",
                    )
                console.print(table)
            else:
                for idx, item in enumerate(self.config.watchlist, 1):
                    st = "啟用" if item.enabled else "暫停"
                    print(f"  {idx}. [{st}] {item.name} | SN={item.sn} | {item.resolution}P | {item.schedule_note}")

            dedup_desc = "僅依實體檔（刪影片即重抓）" if self.config.dedup_mode == "file_only" else "檔案+歷史+DB（歸檔防重抓）"
            up_desc = "開啟" if self.config.upgrade_lower_resolution else "關閉"
            _print(f"[dim]目前去重模式：{dedup_desc} ｜ 720P 自動升級 1080P：{up_desc}[/dim]")
            _print("  [cyan]1[/cyan]. 新增 / 修改動畫（可直接貼動畫瘋網址或 SN，自動抓標題）")
            _print("  [cyan]2[/cyan]. 啟用 / 暫停某部動畫")
            _print("  [cyan]3[/cyan]. 刪除動畫")
            _print("  [cyan]4[/cyan]. 切換去重模式（刪影片即重抓 vs 歸檔防重抓）")
            _print("  [cyan]5[/cyan]. 切換「720P 自動升級 1080P」")
            _print("  [cyan]0[/cyan]. 返回主選單")
            sub = _ask("請選擇操作", default="0")

            if sub == "1":
                raw_sn = _ask("請貼上動畫瘋網址或任一集 SN")
                sn = extract_sn_from_input(raw_sn)
                if not sn:
                    _print("[red]無法解析 SN，請輸入純數字或含 ?sn= 的網址。[/red]")
                    continue
                auto_title = ""
                try:
                    _print("[dim]正在自動查詢作品名稱與集數...[/dim]")
                    auto_title, eps = fetch_anime_episodes(
                        base_sn=sn,
                        cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                    )
                    _print(f"[green]已偵測作品：{auto_title}（目前線上 {len(eps)} 集）[/green]")
                except EpisodeFetchError:
                    auto_title = f"SN-{sn}"
                name = _ask("作品名稱（直接按 Enter 套用偵測名稱）", default=auto_title)
                res = _ask("目標畫質 (1080 / 720 / 540)", default=self.config.default_resolution)
                total_raw = _ask("預計總集數（不知道可留空）", default="")
                note = _ask("更新時間備註（例如：每週日 01:00）", default="")
                item = AnimeWatchItem(
                    name=name,
                    sn=sn,
                    resolution=res,
                    schedule_note=note,
                    total_episodes=int(total_raw) if total_raw.isdigit() else None,
                    enabled=True,
                )
                self.config = self.config_store.add_or_update_anime(self.config, item)
                _print(f"[green]已儲存追番項目：{item.name}[/green]")
            elif sub == "2":
                target = _ask("輸入要切換啟用/暫停的編號或 SN")
                toggled = self.config_store.toggle_anime(self.config, target)
                if toggled:
                    _print(f"[green]{toggled.name} 已切換為：{'啟用' if toggled.enabled else '暫停'}[/green]")
                else:
                    _print("[red]找不到該項目。[/red]")
            elif sub == "3":
                target = _ask("輸入要刪除的編號或 SN")
                if self.config_store.remove_anime(self.config, target):
                    _print("[green]已刪除。[/green]")
                else:
                    _print("[red]找不到該項目。[/red]")
            elif sub == "4":
                self.config.dedup_mode = "file_and_history" if self.config.dedup_mode == "file_only" else "file_only"
                self.config_store.save(self.config)
                _print(f"[green]去重模式已切換為：{self.config.dedup_mode}[/green]")
            elif sub == "5":
                self.config.upgrade_lower_resolution = not self.config.upgrade_lower_resolution
                self.config_store.save(self.config)
                state = "開啟（已有 720P 且設定 1080P 會自動重抓）" if self.config.upgrade_lower_resolution else "關閉（有檔就跳過）"
                _print(f"[green]自動畫質升級已切換為：{state}[/green]")
            else:
                break

    def action_audio_check_and_fix(self, auto_fix: bool = False) -> None:
        _print(f"\n[cyan]正在掃描影片音軌：{self.env.bangumi_dir}[/cyan]")
        inspections = scan_directory_audio(self.env.bangumi_dir)
        if not inspections:
            _print("[yellow]下載目錄中找不到任何影片檔。[/yellow]")
            return

        need_fix: list[Path] = []
        for ins in inspections:
            if ins.error:
                status = f"[red]錯誤：{ins.error}[/red]"
            elif not ins.has_audio:
                status = "[bold red]完全無音軌[/bold red]"
            elif ins.quicktime_friendly:
                status = f"[green]正常 ({ins.audio_codec}, {ins.channels}ch)[/green]"
            else:
                status = f"[yellow]有音軌但可能不相容 QuickTime ({ins.audio_codec})[/yellow]"
                need_fix.append(ins.file_path)
            _print(f"  - {ins.file_path.name} -> {status}")

        all_with_audio = [ins.file_path for ins in inspections if ins.has_audio]
        if not all_with_audio:
            _print("[red]沒有可修復的音軌（檔案本身缺少音軌時無法靠轉檔生出聲音）。[/red]")
            return

        targets = need_fix
        if not targets and not auto_fix:
            if _confirm("全部檔案皆含 AAC 音軌；若用 macOS QuickTime 播放仍無聲，是否要全部無損重洗音軌標頭？", default=False):
                targets = all_with_audio
        elif targets and not auto_fix:
            if not _confirm(f"共有 {len(targets)} 個檔案建議轉成標準 AAC MP4，是否立即修復？", default=True):
                return

        for file_path in targets:
            _print(f"  正在修復：{file_path.name} ...")
            ok, msg = remux_audio_for_quicktime(file_path, replace_original=True)
            if ok:
                _print(f"    [green]修復完成：{msg}[/green]")
            else:
                _print(f"    [red]修復失敗：{msg}[/red]")

    def action_setup_cookie(self) -> None:
        """Cookie 管理選單：1/2 寫入後自動刷新驗證；3 手動開檔；4 放最後做純刷新重讀。"""
        while True:
            _print("\n[bold]巴哈姆特 Cookie 管理 (cookie.txt)[/bold]")
            _print(f"檔案路徑：[cyan]{self.env.cookie_path}[/cyan]")
            _print("  [cyan]1[/cyan]. 從剪貼簿讀取 -> 防呆過濾 -> 寫入 cookie.txt 並自動刷新驗證（推薦）")
            _print("  [cyan]2[/cyan]. Playwright 半自動腳本抓取（彈出獨立瀏覽器登入 -> 自動存檔並刷新驗證）")
            _print("  [cyan]3[/cyan]. 用文字編輯器開啟 cookie.txt 手動修改（存檔後請選 4 刷新）")
            _print("  [cyan]4[/cyan]. 純刷新重新讀取現有 cookie.txt 並線上驗證權限")
            _print("  [cyan]0[/cyan]. 返回主選單")

            sub = _ask("請選擇操作", default="1")

            if sub == "1":
                ok, content = read_clipboard(self.env.raw_os)
                if not ok:
                    _print(f"[red]{content}[/red]")
                    continue
                check = validate_and_sanitize_cookie(content)
                if not check.valid:
                    _print(f"[bold red]攔截！未修改現有 cookie.txt：[/bold red]{check.reason}")
                    continue
                write_cookie_file(self.env.cookie_path, check.cleaned_cookie)
                self.refresh_env()
                _print("[green]已寫入 cookie.txt 並完成刷新：[/green]")
                self._print_cookie_result(check, run_online_check=True)

            elif sub == "2":
                _print("[cyan]正在啟動獨立無痕瀏覽器，請在彈出視窗登入巴哈姆特（登入完成後會自動寫入、刷新並關閉視窗）...[/cyan]")
                ok, msg, check = capture_cookie_via_playwright(self.env.cookie_path)
                if not ok:
                    _print(f"[yellow]{msg}[/yellow]")
                    continue
                self.refresh_env()
                _print(f"[green]{msg}[/green]")
                if check:
                    self._print_cookie_result(check, run_online_check=True)

            elif sub == "3":
                if open_cookie_in_editor(self.env.cookie_path, self.env.raw_os):
                    _print(f"[green]已用編輯器開啟：{self.env.cookie_path}[/green]")
                    _print("[dim]手動貼上並存檔後，按「4」即可重新讀取並驗證。[/dim]")
                else:
                    _print(f"[yellow]請手動編輯：{self.env.cookie_path}[/yellow]")

            elif sub == "4":
                self.refresh_env()
                _print("[cyan]已重新讀取 cookie.txt：[/cyan]")
                check = inspect_cookie_file(self.env.cookie_path)
                self._print_cookie_result(check, run_online_check=True)

            else:
                break

    def action_test_notification(self) -> None:
        self.refresh_env()
        results = self.notifier.send("aniGamerPlus 測試通知", "這是一則來自 aniGamerPlus 模組化管理工具 v1.2 的多管道測試訊息。")
        for _, ok, msg in results:
            if ok:
                _print(f"[green]成功：{msg}[/green]")
            else:
                _print(f"[yellow]{msg}[/yellow]")

    def run_interactive(self) -> None:
        while True:
            self.show_banner()
            _print("\n[bold]主選單：[/bold]")
            _print("  [cyan]1[/cyan]. 檢查並下載追番清單的新集數（自動過濾已下載）")
            _print("  [cyan]2[/cyan]. 僅檢查追番清單更新狀態（不下載）")
            _print("  [cyan]3[/cyan]. 管理追番清單 / 切換動畫與去重模式")
            _print("  [cyan]4[/cyan]. 臨時指定其他動畫網址或 SN 下載（含單集強制測試）")
            _print("  [cyan]5[/cyan]. Cookie 管理（剪貼簿寫入 / 腳本抓取 / 編輯 / 純刷新）")
            _print("  [cyan]6[/cyan]. 檢查影片音軌 / 修復 QuickTime 無聲問題")
            _print("  [cyan]7[/cyan]. 開啟下載資料夾 (bangumi)")
            _print("  [cyan]8[/cyan]. 測試手機推播通知（支援同時多管道）")
            _print("  [cyan]0[/cyan]. 離開")

            choice = _ask("\n請選擇功能", default="1")
            if choice == "1":
                self.action_check_or_update(download=True)
            elif choice == "2":
                self.action_check_or_update(download=False)
            elif choice == "3":
                self.action_manage_watchlist()
            elif choice == "4":
                self.action_ad_hoc_download()
            elif choice == "5":
                self.action_setup_cookie()
            elif choice == "6":
                self.action_audio_check_and_fix()
            elif choice == "7":
                self.env.bangumi_dir.mkdir(parents=True, exist_ok=True)
                _open_folder(self.env.bangumi_dir, self.env.raw_os)
            elif choice == "8":
                self.action_test_notification()
            elif choice == "0":
                _print("[dim]已離開。[/dim]")
                break
