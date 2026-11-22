"""終端互動介面與命令列控制器 v1.3（含明確分隔停頓、q 離開、全功能 Ctrl+C 中斷、畫質升級前後雙檢查）。"""

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
    check_vip_upgrade_eligibility,
    inspect_cookie_file,
    open_cookie_in_editor,
    read_clipboard,
    validate_and_sanitize_cookie,
    verify_cookie_online,
    write_cookie_file,
)
from .downloader import AniGamerDownloader, DownloadResult
from .env_detector import EnvironmentInfo, detect_environment
from .episode_fetcher import EpisodeFetchError, EpisodeInfo, fetch_anime_episodes
from .history_tracker import HistoryTracker, parse_resolution_int
from .notifier import Notifier
from .terminal_utils import flush_stdin

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.rule import Rule
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


def _section_divider(title: str = "") -> None:
    """在操作結果與下一個畫面之間加上明確視覺留白與分隔線。"""
    print()
    if HAS_RICH and console:
        console.print(Rule(title=title, style="dim cyan"))
    else:
        line = f"--- {title} ---" if title else "-" * 68
        print(line)
    print()


def _ask(label: str, default: str = "") -> str:
    """提示輸入前先清空殘留按鍵緩衝，避免誤觸下一個選單。"""
    flush_stdin()
    if HAS_RICH:
        return Prompt.ask(label, default=default).strip() if default else Prompt.ask(label).strip()
    suffix = f" [{default}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    return val or default


def _confirm(label: str, default: bool = False) -> bool:
    flush_stdin()
    if HAS_RICH:
        return Confirm.ask(label, default=default)
    hint = "Y/n" if default else "y/N"
    val = input(f"{label} ({hint}): ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


def _pause(prompt: str = "按 Enter 返回選單...") -> None:
    """動作完成後停頓讓使用者看清結果，並清除等待期間殘留輸入。"""
    print()
    flush_stdin()
    try:
        if HAS_RICH and console:
            console.print(f"[dim]{prompt}[/dim]", end="")
        else:
            print(prompt, end="", flush=True)
        input()
    except (KeyboardInterrupt, EOFError):
        print()


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
        """純本地讀取 cookie.txt 狀態，不於首頁循環發送網路請求。"""
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
            _print("[dim]正在連線動畫瘋確認目前權限（按 Ctrl+C 可略過）...[/dim]")
            try:
                online = verify_cookie_online(result.cleaned_cookie)
                color = {"vip": "green", "logged_in": "green", "guest": "cyan", "blocked": "yellow", "error": "yellow"}.get(
                    online.status, "white"
                )
                _print(f"  [{color}]{online.detail}[/{color}]")
            except KeyboardInterrupt:
                _print("  [yellow]已手動略過線上檢查。[/yellow]")

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
        upgrade_str = (
            "[bold green]開啟[/bold green]（具 VIP 前置驗證＋降級熔斷）"
            if self.config.upgrade_lower_resolution
            else "[dim]關閉[/dim]（已有 720P 即跳過）"
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
            f"[bold]畫質策略：[/bold]預設優先 {self.config.default_resolution}P（非 VIP 自動降級 720P）\n"
            f"[bold]720P 自動升級 1080P：[/bold]{upgrade_str}\n"
            f"[bold]去重模式：[/bold]{dedup_str}\n"
            f"[bold]下載目錄：[/bold]{self.env.bangumi_dir}\n"
            f"[bold]目前預設下載作品 ({len(enabled_items)} 部)：[/bold]\n{watch_lines}"
        )
        if HAS_RICH and console:
            console.print(Panel(body, title="aniGamerPlus 模組化增量管理工具 v1.3.1", border_style="cyan"))
        else:
            print("=" * 72)
            _print(body)
            print("=" * 72)

    def action_check_or_update(
        self,
        download: bool = True,
        single_sn: str | None = None,
        force_single_episode: bool = False,
    ) -> None:
        """檢查所有（或指定）追番作品，自動過濾已下載集數，並對畫質升級執行前後雙重防護。"""
        if force_single_episode and single_sn:
            ep = EpisodeInfo(sn=single_sn, episode_label=f"sn={single_sn}", anime_title=f"SN-{single_sn}")
            _print(f"\n[bold blue]強制單集測試下載（忽略去重紀錄，按 Ctrl+C 可隨時中斷）：sn={single_sn}[/bold blue]")
            res = self.downloader.download_episode(ep, resolution=self.config.default_resolution)
            if res.interrupted:
                _print("[yellow]已手動中斷單集下載。[/yellow]")
            elif res.success:
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
        vip_eligibility_cache: dict[int, tuple[bool, bool, str]] = {}

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
            except KeyboardInterrupt:
                _print("[yellow]已手動中斷檢查流程。[/yellow]")
                return
            except EpisodeFetchError as exc:
                _print(f"[red]線上集數解析失敗：{exc}[/red]")
                if download and _confirm("是否改用 aniGamerPlus 原生指令直接抓「最新一集 (-m latest)」？", default=False):
                    _, interrupted = self.downloader.download_latest_direct(item.sn, item.resolution)
                    if interrupted:
                        _print("[yellow]已手動中斷下載。[/yellow]")
                        return
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
                f"待下載新集數：[yellow]{len(diff.missing_episodes)}[/yellow] 集"
            )

            # ===== 階段 1：處理新集數／缺漏集數 =====
            missing_queue: list[EpisodeInfo] = list(diff.missing_episodes)
            if missing_queue:
                ep_labels = ", ".join(f"[{ep.episode_label}] (sn={ep.sn})" for ep in missing_queue)
                _print(f"  待下載新集數：{ep_labels}")

            # ===== 階段 2：畫質升級前置檢查（防止非 VIP 重複抓 720P） =====
            upgrade_queue: list[EpisodeInfo] = []
            target_res_int = parse_resolution_int(item.resolution)

            if diff.upgradable_episodes:
                up_labels = ", ".join(f"[{ep.episode_label}](現有{cur}P)" for ep, cur in diff.upgradable_episodes)
                _print(f"  偵測到低於 {target_res_int}P 的既有集數 ({len(diff.upgradable_episodes)} 集)：{up_labels}")

                if target_res_int not in vip_eligibility_cache:
                    _print("[dim]  正在執行 720P->1080P 升級前置檢查（確認是否具 VIP 權限）...[/dim]")
                    elig = check_vip_upgrade_eligibility(self.env.cookie_path, target_res_int)
                    vip_eligibility_cache[target_res_int] = (elig.allowed, elig.certain, elig.reason)

                allowed, _, reason = vip_eligibility_cache[target_res_int]
                if allowed:
                    _print(f"  [green]{reason}[/green]")
                    upgrade_queue = [ep for ep, _ in diff.upgradable_episodes]
                else:
                    _print(f"  [bold yellow]{reason}[/bold yellow]")

            if not missing_queue and not upgrade_queue:
                _print("  [green]已是最新狀態，沒有需要下載的集數。[/green]")
                continue

            if not download:
                continue

            # 1) 下載新集數
            if missing_queue:
                _print("[dim]（下載或冷卻期間隨時按 Ctrl+C 可安全中斷並返回選單）[/dim]")

                def _on_start_new(idx: int, total: int, ep: EpisodeInfo) -> None:
                    _print(f"\n[bold blue][新集數 {idx}/{total}] 開始下載：{title} [{ep.episode_label}] (sn={ep.sn})[/bold blue]")

                def _on_complete_new(idx: int, total: int, ep: EpisodeInfo, res: DownloadResult) -> None:
                    if res.interrupted:
                        _print(f"[yellow]已手動中斷：{title} [{ep.episode_label}][/yellow]")
                    elif res.success:
                        actual_res, fpath = self.tracker.find_highest_local_resolution(ep, anime_name_hint=item.name)
                        self.tracker.mark_downloaded(ep, actual_res or item.resolution, fpath)
                        downloaded_summary.append(f"{title} [{ep.episode_label}]")
                        res_tag = f" ({actual_res}P)" if actual_res else ""
                        _print(f"[green]完成：{title} [{ep.episode_label}]{res_tag}[/green]")
                    else:
                        _print(f"[red]下載異常 ({res.message})：{title} [{ep.episode_label}][/red]")

                _, aborted = self.downloader.download_batch(
                    episodes=missing_queue,
                    resolution=item.resolution,
                    cooldown_seconds=self.config.cooldown_seconds,
                    on_start=_on_start_new,
                    on_complete=_on_complete_new,
                )
                if aborted:
                    _print("[yellow]已中斷本次下載批次。[/yellow]")
                    break

            # 2) 下載畫質升級集數（附帶後置解析度熔斷檢查）
            if upgrade_queue:
                _print(
                    f"\n[magenta]開始畫質升級任務（目標 {target_res_int}P；若實測仍降級將立即熔斷中止剩餘集數）[/magenta]"
                )
                circuit_broken = False

                def _on_start_up(idx: int, total: int, ep: EpisodeInfo) -> None:
                    _print(f"\n[bold magenta][升級 {idx}/{total}] 嘗試升級：{title} [{ep.episode_label}] (sn={ep.sn})[/bold magenta]")

                def _on_complete_up(idx: int, total: int, ep: EpisodeInfo, res: DownloadResult) -> None:
                    if res.interrupted:
                        _print(f"[yellow]已手動中斷升級：{title} [{ep.episode_label}][/yellow]")
                    elif not res.success:
                        _print(f"[red]升級下載異常 ({res.message})：{title} [{ep.episode_label}][/red]")

                def _post_check_upgrade(ep: EpisodeInfo, res: DownloadResult) -> bool:
                    nonlocal circuit_broken
                    if not res.success:
                        return False
                    actual_res, fpath = self.tracker.find_highest_local_resolution(ep, anime_name_hint=item.name)
                    if actual_res >= target_res_int - 100:
                        self.tracker.mark_downloaded(ep, actual_res, fpath)
                        downloaded_summary.append(f"{title} [{ep.episode_label}] ({actual_res}P升級)")
                        _print(f"[green]後置檢查通過：{title} [{ep.episode_label}] 已成功取得 {actual_res}P！[/green]")
                        return False

                    # 未達目標畫質（原腳本仍降級抓 720P）-> 立即熔斷！
                    circuit_broken = True
                    _print(
                        f"[bold red]後置檢查觸發熔斷：[/bold red]{title} [{ep.episode_label}] 實際最高僅為 "
                        f"{actual_res or '未知'}P（未達目標 {target_res_int}P）。\n"
                        "[yellow]研判目前帳號無 VIP 權限或片源無該畫質，已立刻中止剩餘集數的升級任務，避免重複抓 720P！[/yellow]"
                    )
                    return True

                _, aborted = self.downloader.download_batch(
                    episodes=upgrade_queue,
                    resolution=item.resolution,
                    cooldown_seconds=self.config.cooldown_seconds,
                    on_start=_on_start_up,
                    on_complete=_on_complete_up,
                    should_abort=_post_check_upgrade,
                )
                if aborted and not circuit_broken:
                    _print("[yellow]已中斷本次升級批次。[/yellow]")
                    break

        if downloaded_summary:
            summary_text = "本次新下載：" + "、".join(downloaded_summary)
            _print(f"\n[bold green]{summary_text}[/bold green]")
            if self.notifier.enabled:
                for _, ok, msg in self.notifier.send("動畫瘋下載完成", summary_text):
                    _print(f"[dim]推播：{msg}[/dim]" if ok else f"[yellow]推播異常：{msg}[/yellow]")

    def action_ad_hoc_download(self) -> None:
        raw = _ask("\n請貼上動畫瘋網址或 SN（輸入 q 取消）", default="q")
        if not raw or raw.lower() == "q":
            return
        sn = extract_sn_from_input(raw)
        if not sn:
            _print("[red]無法從輸入中解析出 SN，請貼上含 ?sn=數字 的網址或純數字。[/red]")
            return

        _print("  [cyan]1[/cyan]. 檢查該作品全部集數並下載缺漏（會過濾已下載）")
        _print("  [cyan]2[/cyan]. 僅強制下載這個 SN 單集（忽略歷史紀錄，適合 Cookie 測試）")
        _print("  [cyan]q[/cyan]. 取消返回")
        mode = _ask("請選擇模式", default="1").lower()
        if mode == "q":
            return
        if mode == "2":
            self.action_check_or_update(download=True, single_sn=sn, force_single_episode=True)
        elif mode == "1":
            self.action_check_or_update(download=True, single_sn=sn, force_single_episode=False)

    def action_manage_watchlist(self) -> None:
        while True:
            _section_divider("追番清單與設定")
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

            dedup_desc = (
                "[cyan]僅依實體檔（刪影片即重抓）[/cyan]"
                if self.config.dedup_mode == "file_only"
                else "[magenta]檔案 + 歷史 + DB（歸檔防重抓）[/magenta]"
            )
            up_desc = (
                "[bold green]開啟[/bold green]（執行前會驗證 VIP，降級自動熔斷）"
                if self.config.upgrade_lower_resolution
                else "[dim]關閉[/dim]（已有檔案直接跳過）"
            )
            _print(f"\n[bold]目前設定狀態：[/bold]")
            _print(f"  • 去重模式：{dedup_desc}")
            _print(f"  • 720P 自動升級 1080P：{up_desc}\n")

            _print("  [cyan]1[/cyan]. 新增 / 修改動畫（可貼動畫瘋網址或 SN，自動抓標題）")
            _print("  [cyan]2[/cyan]. 啟用 / 暫停某部動畫")
            _print("  [cyan]3[/cyan]. 刪除動畫")
            _print("  [cyan]4[/cyan]. 切換去重模式（刪影片即重抓 <-> 歸檔防重抓）")
            _print("  [cyan]5[/cyan]. 切換「720P 自動升級 1080P」開關")
            _print("  [cyan]q[/cyan]. 返回主選單")

            try:
                sub = _ask("\n請選擇操作", default="q").lower()
            except KeyboardInterrupt:
                _print("\n[yellow]已返回主選單。[/yellow]")
                return

            if sub == "q":
                break
            elif sub == "1":
                raw_sn = _ask("請貼上動畫瘋網址或任一集 SN（輸入 q 取消）", default="q")
                if raw_sn.lower() == "q":
                    continue
                sn = extract_sn_from_input(raw_sn)
                if not sn:
                    _print("[red]無法解析 SN，請輸入純數字或含 ?sn= 的網址。[/red]")
                    _pause()
                    continue
                auto_title = ""
                try:
                    _print("[dim]正在自動查詢作品名稱與集數（按 Ctrl+C 可略過）...[/dim]")
                    auto_title, eps = fetch_anime_episodes(
                        base_sn=sn,
                        cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                    )
                    _print(f"[green]已偵測作品：{auto_title}（目前線上 {len(eps)} 集）[/green]")
                except (EpisodeFetchError, KeyboardInterrupt):
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
                _pause()
            elif sub == "2":
                target = _ask("輸入要切換啟用/暫停的編號或 SN（q 取消）", default="q")
                if target.lower() == "q":
                    continue
                toggled = self.config_store.toggle_anime(self.config, target)
                if toggled:
                    _print(f"[green]{toggled.name} 已切換為：{'啟用' if toggled.enabled else '暫停'}[/green]")
                else:
                    _print("[red]找不到該項目。[/red]")
                _pause()
            elif sub == "3":
                target = _ask("輸入要刪除的編號或 SN（q 取消）", default="q")
                if target.lower() == "q":
                    continue
                if self.config_store.remove_anime(self.config, target):
                    _print("[green]已刪除。[/green]")
                else:
                    _print("[red]找不到該項目。[/red]")
                _pause()
            elif sub == "4":
                self.config.dedup_mode = "file_and_history" if self.config.dedup_mode == "file_only" else "file_only"
                self.config_store.save(self.config)
                label = "僅依實體檔（刪影片即重抓）" if self.config.dedup_mode == "file_only" else "檔案 + 歷史 + DB（歸檔防重抓）"
                _print(f"[green]去重模式已切換為：{label}[/green]")
                _pause()
            elif sub == "5":
                self.config.upgrade_lower_resolution = not self.config.upgrade_lower_resolution
                self.config_store.save(self.config)
                state = "開啟（執行前自動檢查 VIP，若仍抓到 720P 會立即熔斷）" if self.config.upgrade_lower_resolution else "關閉（有檔即跳過）"
                _print(f"[green]「720P 自動升級 1080P」已切換為：{state}[/green]")
                _pause()
            else:
                _print("[yellow]無效選項，請重新輸入。[/yellow]")

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
            _print(f"  正在修復：{file_path.name}（按 Ctrl+C 可中斷）...")
            try:
                ok, msg = remux_audio_for_quicktime(file_path, replace_original=True)
            except KeyboardInterrupt:
                _print("  [yellow]已手動中斷音軌修復作業。[/yellow]")
                return
            if ok:
                _print(f"    [green]修復完成：{msg}[/green]")
            else:
                _print(f"    [red]修復失敗：{msg}[/red]")

    def action_setup_cookie(self) -> None:
        while True:
            _section_divider("巴哈姆特 Cookie 管理")
            _print(f"檔案路徑：[cyan]{self.env.cookie_path}[/cyan]")
            _print(f"目前狀態：{self._format_cookie_summary()}\n")
            _print("  [cyan]1[/cyan]. 從剪貼簿讀取 -> 防呆過濾 -> 寫入 cookie.txt 並自動刷新驗證（推薦）")
            _print("  [cyan]2[/cyan]. Playwright 半自動腳本抓取（改用正式 Chrome；關視窗或按 Ctrl+C 立刻中止）")
            _print("  [cyan]3[/cyan]. 用文字編輯器開啟 cookie.txt 手動修改（存檔後選 4 刷新）")
            _print("  [cyan]4[/cyan]. 純刷新重新讀取現有 cookie.txt 並線上驗證權限")
            _print("  [cyan]q[/cyan]. 返回主選單")

            try:
                sub = _ask("\n請選擇操作", default="q").lower()
            except KeyboardInterrupt:
                _print("\n[yellow]已返回主選單。[/yellow]")
                return

            if sub == "q":
                break
            elif sub == "1":
                ok, content = read_clipboard(self.env.raw_os)
                if not ok:
                    _print(f"[red]{content}[/red]")
                else:
                    check = validate_and_sanitize_cookie(content)
                    if not check.valid:
                        _print(f"[bold red]攔截！未修改現有 cookie.txt：[/bold red]{check.reason}")
                    else:
                        write_cookie_file(self.env.cookie_path, check.cleaned_cookie)
                        self.refresh_env()
                        _print("[green]已寫入 cookie.txt 並完成刷新：[/green]")
                        self._print_cookie_result(check, run_online_check=True)
                _pause()

            elif sub == "2":
                _print("[cyan]正在啟動瀏覽器前往巴哈登入頁（直接關閉視窗或於終端按 Ctrl+C 可隨時中止）...[/cyan]")
                ok, msg, check = capture_cookie_via_playwright(self.env.cookie_path)
                flush_stdin()
                if not ok:
                    _print(f"[yellow]{msg}[/yellow]")
                else:
                    self.refresh_env()
                    _print(f"[green]{msg}[/green]")
                    if check:
                        self._print_cookie_result(check, run_online_check=True)
                _pause()

            elif sub == "3":
                if open_cookie_in_editor(self.env.cookie_path, self.env.raw_os):
                    _print(f"[green]已用編輯器開啟：{self.env.cookie_path}[/green]")
                    _print("[dim]手動貼上並存檔後，按「4」即可重新讀取並驗證。[/dim]")
                else:
                    _print(f"[yellow]請手動編輯：{self.env.cookie_path}[/yellow]")
                _pause()

            elif sub == "4":
                self.refresh_env()
                _print("[cyan]已重新讀取 cookie.txt：[/cyan]")
                check = inspect_cookie_file(self.env.cookie_path)
                self._print_cookie_result(check, run_online_check=True)
                _pause()
            else:
                _print("[yellow]無效選項，請重新輸入。[/yellow]")

    def action_test_notification(self) -> None:
        self.refresh_env()
        results = self.notifier.send("aniGamerPlus 測試通知", "這是一則來自 aniGamerPlus 模組化管理工具 v1.3.1 的多管道測試訊息。")
        for _, ok, msg in results:
            if ok:
                _print(f"[green]成功：{msg}[/green]")
            else:
                _print(f"[yellow]{msg}[/yellow]")

    def run_interactive(self) -> None:
        first_render = True
        while True:
            if not first_render:
                _section_divider("主畫面")
            first_render = False

            self.show_banner()
            _print("\n[bold]主選單：[/bold]")
            _print("  [cyan]1[/cyan]. 檢查並下載追番清單的新集數（自動過濾已下載）")
            _print("  [cyan]2[/cyan]. 僅檢查追番清單更新狀態（不下載）")
            _print("  [cyan]3[/cyan]. 管理追番清單 / 切換動畫、去重模式與畫質升級設定")
            _print("  [cyan]4[/cyan]. 臨時指定其他動畫網址或 SN 下載（含單集強制測試）")
            _print("  [cyan]5[/cyan]. Cookie 管理（剪貼簿寫入 / 腳本抓取 / 編輯 / 純刷新）")
            _print("  [cyan]6[/cyan]. 檢查影片音軌 / 修復 QuickTime 無聲問題")
            _print("  [cyan]7[/cyan]. 開啟下載資料夾 (bangumi)")
            _print("  [cyan]8[/cyan]. 測試手機推播通知（支援同時多管道）")
            _print("  [cyan]q[/cyan]. 離開程式")

            try:
                choice = _ask("\n請選擇功能", default="1").lower()
            except KeyboardInterrupt:
                _print("\n[dim]已中斷並離開程式。[/dim]")
                break

            if choice in ("q", "quit", "exit"):
                _print("[dim]已離開。[/dim]")
                break

            try:
                if choice == "1":
                    self.action_check_or_update(download=True)
                    _pause()
                elif choice == "2":
                    self.action_check_or_update(download=False)
                    _pause()
                elif choice == "3":
                    self.action_manage_watchlist()
                elif choice == "4":
                    self.action_ad_hoc_download()
                    _pause()
                elif choice == "5":
                    self.action_setup_cookie()
                elif choice == "6":
                    self.action_audio_check_and_fix()
                    _pause()
                elif choice == "7":
                    self.env.bangumi_dir.mkdir(parents=True, exist_ok=True)
                    _open_folder(self.env.bangumi_dir, self.env.raw_os)
                    _print(f"[green]已開啟資料夾：{self.env.bangumi_dir}[/green]")
                    _pause()
                elif choice == "8":
                    self.action_test_notification()
                    _pause()
                else:
                    _print("[yellow]無效選項，請輸入 1~8 或 q。[/yellow]")
                    _pause()
            except KeyboardInterrupt:
                flush_stdin()
                _print("\n[bold yellow]已手動中斷目前任務，安全返回主選單。[/bold yellow]")
                _pause()
