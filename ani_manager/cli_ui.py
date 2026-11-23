"""終端互動介面與命令列控制器 v1.5.0（表格化檢查與 SN 顯示、區間壓縮、精準補齊狀態與集數）。"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .audio_tools import remux_audio_for_quicktime, scan_directory_audio
from .config_store import (
    ALLOWED_RESOLUTIONS,
    WEEKDAY_MAP,
    ConfigStore,
    ManagerConfig,
    extract_sn_from_input,
    parse_multi_targets,
    validate_hhmm,
    validate_resolution,
    validate_total_episodes,
)
from .watchlist_store import AnimeWatchItem, WatchlistStore
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
from .episode_fetcher import EpisodeFetchError, EpisodeInfo, fetch_anime_episodes, format_episode_ranges
from .history_tracker import EpisodeDiffResult, HistoryTracker, parse_resolution_int
from .metadata_fetcher import (
    enrich_anime_metadata,
    resolve_candidate_video_sn,
    search_anigamer,
)
from .notifier import (
    DownloadedRecord,
    Notifier,
    format_anime_added_notice,
    format_check_digest,
    format_download_report,
)
from .terminal_utils import clear_screen, flush_stdin

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


@dataclass
class AnimeScanSnapshot:
    item: AnimeWatchItem
    title: str
    online_count: int
    diff: EpisodeDiffResult | None = None
    error: str = ""


def _print(msg: str = "") -> None:
    if HAS_RICH and console:
        console.print(msg)
    else:
        print(re.sub(r"\[/?[a-zA-Z0-9_ ]+\]", "", msg))


def _section_divider(title: str = "") -> None:
    print()
    if HAS_RICH and console:
        console.print(Rule(title=title, style="dim cyan"))
    else:
        print(f"--- {title} ---" if title else "-" * 68)
    print()


def _ask(label: str, default: str = "") -> str:
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


def _pause(prompt: str = "按 Enter 繼續...") -> None:
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


def _format_size_mb(num_bytes: int) -> str:
    if num_bytes <= 0:
        return ""
    mb = num_bytes / (1024 * 1024)
    return f"{mb:.1f} MB" if mb >= 0.1 else f"{num_bytes / 1024:.0f} KB"


def _format_ep_ratio(online: int | None, total: int | None) -> str:
    on_s = str(online) if online is not None else "?"
    tot_s = str(total) if total is not None else "?"
    if online is None and total is None:
        return "-"
    return f"{on_s}/{tot_s}"


def _format_status_schedule(item: AnimeWatchItem) -> str:
    if item.status == "已完結":
        return item.schedule_note if "已完結" in item.schedule_note else "已完結"
    if item.schedule_note:
        return item.schedule_note
    return item.status or "-"


class AnimeManagerApp:
    """串接各獨立模組的主控制器。"""

    def _resolve_watchlist_path(self, cfg: ManagerConfig) -> Path:
        p = Path(cfg.watchlist_file).expanduser()
        return p if p.is_absolute() else (self.root_dir / p).resolve()

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = (root_dir or Path.cwd()).resolve()
        self.config_store = ConfigStore(
            self.root_dir / "manager_config.json",
            sample_path=self.root_dir / "manager_config-sample.json",
        )
        self.config, legacy_watchlist = self.config_store.load_with_legacy_watchlist()
        self.watchlist_store = WatchlistStore(
            self._resolve_watchlist_path(self.config),
            sample_path=self.root_dir / "watchlist-sample.json",
        )
        self.watchlist, self.migrated_watchlist = self.watchlist_store.load(
            default_resolution=self.config.default_resolution,
            legacy_items=legacy_watchlist,
        )
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
        self.last_snapshots: list[AnimeScanSnapshot] = []

    def refresh_env(self) -> None:
        self.config, legacy_watchlist = self.config_store.load_with_legacy_watchlist()
        self.watchlist_store = WatchlistStore(
            self._resolve_watchlist_path(self.config),
            sample_path=self.root_dir / "watchlist-sample.json",
        )
        self.watchlist, _ = self.watchlist_store.load(
            default_resolution=self.config.default_resolution,
            legacy_items=legacy_watchlist,
        )
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

    def _clear_if_enabled(self) -> None:
        if self.config.clear_screen_on_menu:
            clear_screen()

    def _format_cookie_badge(self) -> str:
        if not self.env.has_cookie:
            return "[yellow]未設定 (訪客 720P)[/yellow]"
        c = inspect_cookie_file(self.env.cookie_path)
        if not c.valid:
            return "[red]格式異常[/red]"
        if c.tier == "logged_in":
            return f"[green]已登入 ({c.baha_id_masked})[/green]"
        if c.tier == "partial_login":
            return f"[yellow]部分憑證 ({c.baha_id_masked})[/yellow]"
        return "[cyan]訪客 Cookie[/cyan]"

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
            f"狀態：{tier_label} ｜ 帳號：[cyan]{result.baha_id_masked}[/cyan] ｜ "
            f"憑證：[green]{keys_str}[/green] ｜ 長度：{len(result.cleaned_cookie)} 字元"
        )
        for w in result.warnings:
            _print(f"  [yellow]提醒：{w}[/yellow]")

        if run_online_check:
            _print("[dim]正在連線動畫瘋確認權限（按 Ctrl+C 可略過）...[/dim]")
            try:
                online = verify_cookie_online(result.cleaned_cookie)
                color = {"vip": "green", "logged_in": "green", "guest": "cyan", "blocked": "yellow", "error": "yellow"}.get(
                    online.status, "white"
                )
                _print(f"  [{color}]{online.detail}[/{color}]")
            except KeyboardInterrupt:
                _print("  [yellow]已略過線上檢查。[/yellow]")

    def _scan_watchlist_snapshots(self, items: list[AnimeWatchItem] | None = None, quiet: bool = False) -> list[AnimeScanSnapshot]:
        targets = items if items is not None else [w for w in self.watchlist if w.enabled]
        snapshots: list[AnimeScanSnapshot] = []
        changed_config = False

        for item in targets:
            if not quiet:
                _print(f"[dim]  掃描中：{item.name} (SN: {item.sn})...[/dim]")
            try:
                title, online_eps = fetch_anime_episodes(
                    base_sn=item.sn,
                    cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                    fallback_title=item.name,
                )
                if item.online_episodes != len(online_eps):
                    item.online_episodes = len(online_eps)
                    changed_config = True
                if item.total_episodes is not None and item.total_episodes < len(online_eps):
                    item.total_episodes = None
                    changed_config = True

                diff = self.tracker.compute_diff(
                    online_episodes=online_eps,
                    target_resolution=item.resolution,
                    upgrade_lower_resolution=self.config.upgrade_lower_resolution,
                    anime_name_hint=item.name,
                    dedup_mode=self.config.dedup_mode,
                )
                snapshots.append(
                    AnimeScanSnapshot(
                        item=item,
                        title=title,
                        online_count=len(online_eps),
                        diff=diff,
                    )
                )
            except KeyboardInterrupt:
                raise
            except EpisodeFetchError as exc:
                snapshots.append(
                    AnimeScanSnapshot(
                        item=item,
                        title=item.name,
                        online_count=item.online_episodes or 0,
                        error=str(exc),
                    )
                )

        if changed_config:
            self.watchlist_store.save(self.watchlist)
        if items is None:
            self.last_snapshots = snapshots
        return snapshots

    def _build_digest_from_snapshots(self, snapshots: list[AnimeScanSnapshot]) -> tuple[str, str]:
        """將掃描快照轉成簡潔區間式手機推播簡報（不逐集展開洗版）。"""
        new_updates: list[str] = []
        up_to_date: list[str] = []
        errors: list[str] = []

        for snap in snapshots:
            if snap.error or not snap.diff:
                errors.append(f"{snap.item.name}：{snap.error or '無法解析'}")
                continue
            ratio = _format_ep_ratio(snap.online_count, snap.item.total_episodes)
            sched_str = f"｜{_format_status_schedule(snap.item)}" if _format_status_schedule(snap.item) != "-" else ""
            if snap.diff.missing_episodes:
                range_str = format_episode_ranges(snap.diff.missing_episodes, compact=False)
                cnt = len(snap.diff.missing_episodes)
                cnt_suffix = f"（共 {cnt} 集）" if cnt > 1 else ""
                new_updates.append(
                    f"《{snap.title}》可下載 {range_str}{cnt_suffix}（線上 {ratio} 集{sched_str}）"
                )
            else:
                up_to_date.append(
                    f"《{snap.title}》已追平 {snap.diff.total_local_count}/{snap.online_count} 集（總進度 {ratio}{sched_str}）"
                )

        paused = [w.name for w in self.watchlist if not w.enabled]
        return format_check_digest(new_updates, up_to_date, paused, errors)

    def _print_startup_briefing(self) -> None:
        enabled_items = [w for w in self.watchlist if w.enabled]
        if not enabled_items:
            return
        _print(f"[cyan]正在執行上線更新快報（共 {len(enabled_items)} 部啟用中作品，按 Ctrl+C 可略過）...[/cyan]")
        try:
            snaps = self._scan_watchlist_snapshots(enabled_items, quiet=True)
            self.last_snapshots = snaps
        except KeyboardInterrupt:
            _print("[yellow]已略過上線快報。[/yellow]")

    def show_banner(self) -> None:
        """清爽分區儀表板：系統狀態條 + 啟用中追番區間快報。"""
        core_tag = "[green]OK[/green]" if self.env.anigamer_script else "[red]缺核心[/red]"
        ff_tag = "[green]OK[/green]" if self.env.has_ffmpeg else "[red]未裝[/red]"
        notify_tag = f"[green]{'/'.join(self.notifier.enabled_providers)}[/green]" if self.notifier.enabled else "[dim]關[/dim]"
        dedup_tag = "[cyan]依實體檔[/cyan]" if self.config.dedup_mode == "file_only" else "[magenta]檔案+歷史[/magenta]"
        up_tag = "[green]開(具防呆)[/green]" if self.config.upgrade_lower_resolution else "[dim]關[/dim]"

        status_line = (
            f"[bold]環境：[/bold]{self.env.os_name} (核心:{core_tag} / FFmpeg:{ff_tag})   "
            f"[bold]Cookie：[/bold]{self._format_cookie_badge()}\n"
            f"[bold]設定：[/bold]優先 {self.config.default_resolution}P ｜ "
            f"720P升級:{up_tag} ｜ 去重:{dedup_tag} ｜ 推播:{notify_tag}"
        )

        snap_by_sn = {s.item.sn: s for s in self.last_snapshots}
        enabled_items = [w for w in self.watchlist if w.enabled]
        paused_count = len(self.watchlist) - len(enabled_items)

        brief_lines: list[str] = []
        if enabled_items:
            for w in enabled_items:
                snap = snap_by_sn.get(w.sn)
                sched_txt = _format_status_schedule(w)
                sched = f" [{sched_txt}]" if sched_txt != "-" else ""
                if snap and snap.diff:
                    if snap.diff.missing_episodes:
                        r_txt = format_episode_ranges(snap.diff.missing_episodes, compact=True)
                        state_txt = f"[bold yellow]待下載 {len(snap.diff.missing_episodes)} 集 ({r_txt})[/bold yellow]"
                    else:
                        state_txt = f"[green]已追平 ({snap.diff.total_local_count}/{snap.online_count}集)[/green]"
                elif snap and snap.error:
                    state_txt = "[red]檢查失敗[/red]"
                else:
                    ratio = _format_ep_ratio(w.online_episodes, w.total_episodes)
                    state_txt = f"[dim]進度 {ratio}｜{w.resolution}P[/dim]"
                brief_lines.append(f"  • [bold]{w.name}[/bold]{sched} → {state_txt}")
        else:
            brief_lines.append("  [yellow]（目前沒有啟用中的動畫，請按 3 新增或啟用）[/yellow]")

        header_title = f"啟用中追番 ({len(enabled_items)} 部" + (f"｜暫停 {paused_count} 部" if paused_count else "") + ")"
        body = f"{status_line}\n\n[bold]{header_title}：[/bold]\n" + "\n".join(brief_lines)

        if HAS_RICH and console:
            console.print(Panel(body, title="aniGamerPlus 增量下載管理器 v1.5.0", border_style="cyan", padding=(0, 2)))
        else:
            print("=" * 72)
            _print(body)
            print("=" * 72)

    def _render_check_tables(self, snapshots: list[AnimeScanSnapshot], show_sn_table: bool = True) -> None:
        """以表格呈現檢查總覽（區間壓縮）與縱向 SN 明細表，取代凌亂的橫向條列。"""
        if HAS_RICH and console:
            summary = Table(title="追番更新檢查總覽", show_header=True, header_style="bold cyan")
            summary.add_column("作品名稱", style="bold", max_width=26)
            summary.add_column("狀態 / 更新", max_width=16)
            summary.add_column("線上/總集", justify="center", width=9)
            summary.add_column("本地已有", justify="center", width=10)
            summary.add_column("待下載集數", style="yellow")

            for snap in snapshots:
                sched = _format_status_schedule(snap.item)
                if snap.error or not snap.diff:
                    summary.add_row(snap.title, sched, "-", "-", f"[red]失敗：{snap.error}[/red]")
                    continue
                ratio = _format_ep_ratio(snap.online_count, snap.item.total_episodes)
                local_str = f"{snap.diff.total_local_count} 集"
                if snap.diff.upgradable_episodes:
                    local_str += f" [dim]({len(snap.diff.upgradable_episodes)}低)[/dim]"
                if snap.diff.missing_episodes:
                    r_str = format_episode_ranges(snap.diff.missing_episodes, compact=False)
                    miss_str = f"{r_str} ({len(snap.diff.missing_episodes)}集)"
                else:
                    miss_str = "[green]已追平[/green]"
                summary.add_row(snap.title, sched, ratio, local_str, miss_str)
            console.print(summary)
        else:
            print("\n=== 追番更新檢查總覽 ===")
            for snap in snapshots:
                if snap.error or not snap.diff:
                    print(f"  {snap.title} | 錯誤：{snap.error}")
                    continue
                ratio = _format_ep_ratio(snap.online_count, snap.item.total_episodes)
                miss = format_episode_ranges(snap.diff.missing_episodes) if snap.diff.missing_episodes else "已追平"
                print(f"  {snap.title} | 線上/總集:{ratio} | 本地:{snap.diff.total_local_count} | 待下載:{miss}")

        if not show_sn_table:
            return

        pending_rows: list[tuple[str, str, str]] = []
        for snap in snapshots:
            if snap.diff and snap.diff.missing_episodes:
                for idx, ep in enumerate(snap.diff.missing_episodes):
                    pending_rows.append((snap.title if idx == 0 else "", f"第 {ep.episode_label} 集", ep.sn))

        if not pending_rows:
            return

        print()
        if HAS_RICH and console:
            sn_tbl = Table(title="待下載集數 SN 明細", show_header=True, header_style="bold magenta")
            sn_tbl.add_column("作品名稱", style="cyan", max_width=28)
            sn_tbl.add_column("集數", justify="right", width=10)
            sn_tbl.add_column("SN", justify="center", width=10)
            for name_col, ep_col, sn_col in pending_rows:
                sn_tbl.add_row(name_col, ep_col, sn_col)
            console.print(sn_tbl)
        else:
            print("--- 待下載集數 SN 明細 ---")
            for name_col, ep_col, sn_col in pending_rows:
                prefix = f"{name_col}\n" if name_col else ""
                print(f"{prefix}    {ep_col:<8}  SN={sn_col}")

    def _prompt_schedule_picker(self, auto_schedule: str = "") -> tuple[str, str]:
        """結構化更新時間與狀態選擇器，回傳 (status, schedule_note)。"""
        _print("\n[bold]設定連載狀態與更新時間：[/bold]")
        if auto_schedule:
            _print(f"  [green]0[/green]. 使用自動偵測結果：[bold]{auto_schedule}[/bold]（推薦）")
        _print("  [cyan]1[/cyan]. 連載中（選星期 + 時段）")
        _print("  [cyan]2[/cyan]. 標記為「已完結」")
        _print("  [cyan]3[/cyan]. 留空 / 不定時更新")

        default_opt = "0" if auto_schedule else "1"
        while True:
            mode = _ask("請選擇方式", default=default_opt)
            if mode == "0" and auto_schedule:
                st = "已完結" if "已完結" in auto_schedule else "連載中"
                return st, auto_schedule
            if mode == "2":
                return "已完結", "已完結"
            if mode == "3":
                return "", ""
            if mode == "1":
                _print("  星期：[cyan]1[/cyan]週一  [cyan]2[/cyan]週二  [cyan]3[/cyan]週三  [cyan]4[/cyan]週四  [cyan]5[/cyan]週五  [cyan]6[/cyan]週六  [cyan]7[/cyan]週日")
                while True:
                    d = _ask("請選星期 (1~7)", default="7")
                    if d in WEEKDAY_MAP:
                        day_str = WEEKDAY_MAP[d]
                        break
                    _print("[red]請輸入 1 到 7 的數字。[/red]")

                presets = {"1": "00:00", "2": "00:30", "3": "01:00", "4": "01:30", "5": "22:00", "6": "23:00", "7": "23:30"}
                _print("  時段：[cyan]1[/cyan]00:00  [cyan]2[/cyan]00:30  [cyan]3[/cyan]01:00  [cyan]4[/cyan]01:30  [cyan]5[/cyan]22:00  [cyan]6[/cyan]23:00  [cyan]7[/cyan]23:30  [cyan]8[/cyan]自訂(HH:MM)")
                while True:
                    t_opt = _ask("請選時段 (1~8)", default="3")
                    if t_opt in presets:
                        return "連載中", f"每{day_str} {presets[t_opt]}"
                    if t_opt == "8":
                        raw_t = _ask("請輸入 24 小時制時間 (例如 01:00 或 23:30)")
                        valid_t = validate_hhmm(raw_t)
                        if valid_t:
                            return "連載中", f"每{day_str} {valid_t}"
                        _print("[red]時間格式錯誤，請輸入 00:00 ~ 23:59。[/red]")
                    else:
                        _print("[red]請輸入 1 到 8。[/red]")
            _print("[red]無效選項，請重新選擇。[/red]")

    def _prompt_resolution_picker(self, default_res: str) -> str:
        while True:
            raw = _ask(f"目標畫質 ({'/'.join(ALLOWED_RESOLUTIONS)})", default=default_res)
            valid = validate_resolution(raw)
            if valid:
                return valid
            _print(f"[red]無效畫質！僅支援：{', '.join(ALLOWED_RESOLUTIONS)}[/red]")

    def _prompt_total_episodes(self, auto_total: int | None = None, min_episodes: int = 1) -> int | None:
        default_str = str(auto_total) if auto_total else ""
        hint = f"（已自動取得 {auto_total} 話，按 Enter 套用；輸入 - 清空）" if auto_total else "（不知道請按 Enter 留空）"
        while True:
            raw = _ask(f"預計總集數{hint}", default=default_str)
            ok, val, err = validate_total_episodes(raw)
            if not ok:
                _print(f"[red]{err}[/red]")
                continue
            if val is not None and val < min_episodes:
                _print(f"[red]總集數 ({val}) 不能小於動畫瘋目前線上已上架集數 ({min_episodes})。[/red]")
                continue
            return val

    def action_check_or_update(
        self,
        download: bool = True,
        single_sn: str | None = None,
        force_single_episode: bool = False,
        push_check_notify: bool = False,
        interactive: bool = True,
    ) -> None:
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

        items = self.watchlist
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

        _print(f"\n[cyan]正在掃描 {len(items)} 部作品的最新線上進度...[/cyan]")
        snapshots = self._scan_watchlist_snapshots(items, quiet=False)
        if not single_sn:
            self.last_snapshots = snapshots

        # 先用乾淨表格呈現所有作品檢查結果與 SN 明細
        print()
        self._render_check_tables(snapshots, show_sn_table=True)

        # 檢查 VIP 升級前置狀態提示
        vip_eligibility_cache: dict[int, tuple[bool, bool, str]] = {}
        upgrade_plans: dict[str, list[EpisodeInfo]] = {}

        for snap in snapshots:
            if snap.error or not snap.diff or not snap.diff.upgradable_episodes:
                continue
            target_res_int = parse_resolution_int(snap.item.resolution)
            if target_res_int not in vip_eligibility_cache:
                _print("\n[dim]正在執行 720P->1080P 升級前置 VIP 檢查...[/dim]")
                elig = check_vip_upgrade_eligibility(self.env.cookie_path, target_res_int)
                vip_eligibility_cache[target_res_int] = (elig.allowed, elig.certain, elig.reason)
            allowed, _, reason = vip_eligibility_cache[target_res_int]
            if allowed:
                _print(f"[green]《{snap.title}》{reason}[/green]")
                upgrade_plans[snap.item.sn] = [ep for ep, _ in snap.diff.upgradable_episodes]
            else:
                _print(f"[yellow]《{snap.title}》{reason}[/yellow]")

        if not download:
            if snapshots and self.notifier.enabled:
                should_push = push_check_notify or _confirm("\n是否將這份追番更新簡報推播到手機？", default=False)
                if should_push:
                    n_title, n_body = self._build_digest_from_snapshots(snapshots)
                    for _, ok, msg in self.notifier.send(n_title, n_body):
                        _print(f"[green]推播：{msg}[/green]" if ok else f"[yellow]推播異常：{msg}[/yellow]")
            return

        # ===== 開始執行下載 =====
        downloaded_ok: list[DownloadedRecord] = []
        downloaded_fail: list[str] = []
        remaining_after: list[str] = []
        was_interrupted = False

        # 記錄每部作品完成進度，供中途退出時一次彙整所有剩餘（包含後面尚未輪到的作品）
        completed_by_sn: dict[str, set[str]] = {}

        for snap_idx, snap in enumerate(snapshots):
            if snap.error or not snap.diff:
                if _confirm(f"\n《{snap.title}》線上解析失敗，是否改用原生 -m latest 抓最新一集？", default=False):
                    _, interrupted = self.downloader.download_latest_direct(snap.item.sn, snap.item.resolution)
                    if interrupted:
                        was_interrupted = True
                        _print("[yellow]已乾淨中斷下載。[/yellow]")
                        break
                continue

            item = snap.item
            title = snap.title
            missing_queue = list(snap.diff.missing_episodes)
            upgrade_queue = upgrade_plans.get(item.sn, [])
            completed_sns = completed_by_sn.setdefault(item.sn, set())

            if not missing_queue and not upgrade_queue:
                continue

            item_aborted = False
            if missing_queue:
                r_str = format_episode_ranges(missing_queue)
                _print(f"\n[bold blue]開始下載《{title}》：{r_str}（共 {len(missing_queue)} 集；按 Ctrl+C 可乾淨中斷）[/bold blue]")

                def _on_start_new(idx: int, total: int, ep: EpisodeInfo) -> None:
                    _print(f"\n[bold blue][{idx}/{total}] {title} 第 {ep.episode_label} 集 (sn={ep.sn})[/bold blue]")

                def _on_complete_new(idx: int, total: int, ep: EpisodeInfo, res: DownloadResult) -> None:
                    if res.interrupted:
                        downloaded_fail.append(f"《{title}》第 {ep.episode_label} 集（手動中斷）")
                        clean_note = ""
                        if res.cleaned_temp_count > 0:
                            sz = _format_size_mb(res.cleaned_temp_bytes)
                            clean_note = f"；已自動清理 temp/ 殘檔 {res.cleaned_temp_count} 項" + (f" ({sz})" if sz else "")
                        _print(f"\n[yellow]已乾淨中斷：《{title}》第 {ep.episode_label} 集{clean_note}[/yellow]")
                    elif res.success:
                        completed_sns.add(ep.sn)
                        actual_res, fpath = self.tracker.find_highest_local_resolution(ep, anime_name_hint=item.name)
                        self.tracker.mark_downloaded(ep, actual_res or item.resolution, fpath)
                        res_tag = f"{actual_res}P" if actual_res else f"{item.resolution}P"
                        downloaded_ok.append(DownloadedRecord(title=title, episode=ep, resolution_label=res_tag))
                        _print(f"[green]完成：《{title}》第 {ep.episode_label} 集 ({res_tag})[/green]")
                    else:
                        downloaded_fail.append(f"《{title}》第 {ep.episode_label} 集（{res.message}）")
                        _print(f"[red]下載異常 ({res.message})：《{title}》第 {ep.episode_label} 集[/red]")

                def _on_cooldown_new(secs: int, next_ep: EpisodeInfo) -> None:
                    _print(f"[dim]冷卻 {secs} 秒後開始第 {next_ep.episode_label} 集，此時按 Ctrl+C 可無損中止...[/dim]")

                _, aborted, interrupted_now = self.downloader.download_batch(
                    episodes=missing_queue,
                    resolution=item.resolution,
                    cooldown_seconds=self.config.cooldown_seconds,
                    on_start=_on_start_new,
                    on_complete=_on_complete_new,
                    on_cooldown=_on_cooldown_new,
                )
                if interrupted_now:
                    was_interrupted = True
                if aborted:
                    item_aborted = True
                    _print("[yellow]已中止本次下載批次。[/yellow]")
                    break

            if upgrade_queue and not item_aborted:
                target_res_int = parse_resolution_int(item.resolution)
                _print(f"\n[magenta]開始《{title}》畫質升級（目標 {target_res_int}P；未達標自動熔斷）[/magenta]")
                circuit_broken = False

                def _on_start_up(idx: int, total: int, ep: EpisodeInfo) -> None:
                    _print(f"\n[bold magenta][升級 {idx}/{total}] {title} 第 {ep.episode_label} 集 (sn={ep.sn})[/bold magenta]")

                def _on_complete_up(idx: int, total: int, ep: EpisodeInfo, res: DownloadResult) -> None:
                    if res.interrupted:
                        clean_note = ""
                        if res.cleaned_temp_count > 0:
                            sz = _format_size_mb(res.cleaned_temp_bytes)
                            clean_note = f"；已自動清理 temp/ 殘檔 {res.cleaned_temp_count} 項" + (f" ({sz})" if sz else "")
                        _print(f"\n[yellow]已乾淨中斷升級：《{title}》第 {ep.episode_label} 集{clean_note}[/yellow]")
                    elif not res.success:
                        downloaded_fail.append(f"《{title}》第 {ep.episode_label} 集升級失敗（{res.message}）")

                def _on_cooldown_up(secs: int, next_ep: EpisodeInfo) -> None:
                    _print(f"[dim]冷卻 {secs} 秒後嘗試升級第 {next_ep.episode_label} 集，此時按 Ctrl+C 可無損中止...[/dim]")

                def _post_check_upgrade(ep: EpisodeInfo, res: DownloadResult) -> bool:
                    nonlocal circuit_broken
                    if not res.success:
                        return False
                    actual_res, fpath = self.tracker.find_highest_local_resolution(ep, anime_name_hint=item.name)
                    if actual_res >= target_res_int - 100:
                        self.tracker.mark_downloaded(ep, actual_res, fpath)
                        downloaded_ok.append(DownloadedRecord(title=title, episode=ep, resolution_label=f"{actual_res}P升級"))
                        _print(f"[green]後置檢查通過：《{title}》第 {ep.episode_label} 集已升級 {actual_res}P！[/green]")
                        return False
                    circuit_broken = True
                    _print(
                        f"[bold red]後置檢查熔斷：[/bold red]《{title}》第 {ep.episode_label} 集實際僅 {actual_res or '未知'}P，"
                        "已中止剩餘升級任務避免重複抓 720P。"
                    )
                    return True

                _, aborted, interrupted_now = self.downloader.download_batch(
                    episodes=upgrade_queue,
                    resolution=item.resolution,
                    cooldown_seconds=self.config.cooldown_seconds,
                    on_start=_on_start_up,
                    on_complete=_on_complete_up,
                    on_cooldown=_on_cooldown_up,
                    should_abort=_post_check_upgrade,
                )
                if interrupted_now:
                    was_interrupted = True
                if aborted and not circuit_broken:
                    _print("[yellow]已中止本次升級批次。[/yellow]")
                    break

        # 彙整所有作品（含後續尚未輪到的作品）剩餘未下載集數，統一壓縮為區間
        for snap in snapshots:
            if snap.error or not snap.diff or not snap.diff.missing_episodes:
                continue
            done_sns = completed_by_sn.get(snap.item.sn, set())
            leftover = [e for e in snap.diff.missing_episodes if e.sn not in done_sns]
            if leftover:
                cnt = len(leftover)
                r_txt = format_episode_ranges(leftover, compact=False)
                remaining_after.append(f"《{snap.title}》剩餘 {r_txt}（共 {cnt} 集）")

        if downloaded_ok or downloaded_fail or (was_interrupted and remaining_after):
            n_title, n_body = format_download_report(
                downloaded_ok,
                downloaded_fail,
                remaining_after,
                interrupted=was_interrupted,
            )
            title_color = "bold yellow" if was_interrupted else "bold green"
            _print(f"\n[{title_color}]{n_title}[/{title_color}]\n{n_body}")

            if self.notifier.enabled:
                # 若為手動中斷且處於互動模式，改為先詢問再推播，避免人在電腦前還被四個管道轟炸
                should_push = True
                if was_interrupted:
                    should_push = _confirm("\n本次為手動中斷，是否仍將中斷結算推播到手機？", default=False) if interactive else False
                if should_push:
                    for _, ok, msg in self.notifier.send(n_title, n_body):
                        _print(f"[dim]推播：{msg}[/dim]" if ok else f"[yellow]推播異常：{msg}[/yellow]")

    def action_ad_hoc_download(self) -> None:
        raw = _ask("\n請貼上動畫瘋網址或 SN（輸入 q 取消）", default="q")
        if not raw or raw.lower() == "q":
            return
        sn = extract_sn_from_input(raw)
        if not sn:
            _print("[red]無效 SN（需為 4 碼以上數字或含 ?sn= 的動畫瘋網址）。[/red]")
            return

        _print("  [cyan]1[/cyan]. 檢查全集並只下載缺漏集數")
        _print("  [cyan]2[/cyan]. 僅強制下載這個 SN 單集（忽略紀錄，Cookie 測試用）")
        _print("  [cyan]q[/cyan]. 取消")
        mode = _ask("請選擇模式", default="1").lower()
        if mode == "2":
            self.action_check_or_update(download=True, single_sn=sn, force_single_episode=True)
        elif mode == "1":
            self.action_check_or_update(download=True, single_sn=sn, force_single_episode=False)

    def _add_anime_flow(self) -> None:
        _print("\n[bold cyan]新增／修改追番作品[/bold cyan]")
        _print("[dim]可直接輸入「片名關鍵字」搜尋動畫瘋，或貼上動畫瘋網址 / 4碼以上 SN（輸入 q 取消）[/dim]")
        raw = _ask("片名關鍵字 / 網址 / SN", default="q")
        if not raw or raw.lower() == "q":
            return

        sn = extract_sn_from_input(raw)
        if sn:
            _print(f"[dim]正在驗證 SN={sn} 並抓取作品資訊（標題、最新上架時間、連載狀態、總集數）...[/dim]")
            try:
                meta, episodes = enrich_anime_metadata(
                    base_sn=sn,
                    cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                )
            except EpisodeFetchError as exc:
                _print(f"[bold red]SN 驗證失敗，已拒絕新增：[/bold red]{exc}")
                return
        else:
            if raw.isdigit():
                _print("[red]無效輸入：動畫瘋 SN 至少為 4 碼數字；若要搜片名請輸入動畫名稱。[/red]")
                return

            _print(f"[dim]正在動畫瘋搜尋「{raw}」...[/dim]")
            try:
                candidates = search_anigamer(raw, cookie_path=self.env.cookie_path if self.env.has_cookie else None)
            except EpisodeFetchError as exc:
                _print(f"[red]搜尋失敗：{exc}[/red]")
                return

            if not candidates:
                _print(f"[yellow]動畫瘋找不到與「{raw}」相符的作品，請換個關鍵字或改貼網址。[/yellow]")
                return

            if HAS_RICH and console:
                tbl = Table(title=f"動畫瘋搜尋結果：{raw}", show_header=True, header_style="bold cyan")
                tbl.add_column("#", width=4)
                tbl.add_column("作品名稱", style="bold")
                tbl.add_column("年份", width=10)
                tbl.add_column("上架集數", width=10)
                for idx, c in enumerate(candidates, 1):
                    tbl.add_row(str(idx), c.title, c.year_text or "-", c.ep_count_text or "-")
                console.print(tbl)
            else:
                for idx, c in enumerate(candidates, 1):
                    print(f"  {idx}. {c.title} ({c.year_text} / {c.ep_count_text})")

            pick = _ask(f"請輸入要加入的編號 (1~{len(candidates)}，q 取消)", default="q").lower()
            if pick == "q" or not pick.isdigit() or not (1 <= int(pick) <= len(candidates)):
                _print("[dim]已取消新增。[/dim]")
                return

            chosen = candidates[int(pick) - 1]
            _print(f"[dim]正在解析《{chosen.title}》的基準 SN、上架時間、完結狀態與預定總集數...[/dim]")
            try:
                resolved_title, resolved_sn, _ = resolve_candidate_video_sn(
                    chosen,
                    cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                )
                meta, episodes = enrich_anime_metadata(
                    base_sn=resolved_sn,
                    fallback_title=resolved_title,
                    cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                )
            except EpisodeFetchError as exc:
                _print(f"[red]解析該作品失敗：{exc}[/red]")
                return

        _print(
            f"\n[green]已取得作品資訊：[/green][bold]{meta.title}[/bold] "
            f"(基準 SN: {meta.base_sn}｜線上 {meta.online_count} 集"
            + (f"｜狀態：{meta.schedule_note}" if meta.schedule_note else "")
            + (f"｜預計全 {meta.total_episodes} 話 ({meta.total_source})" if meta.total_episodes else "")
            + (f"｜最新上架：{meta.latest_upload_at}" if meta.latest_upload_at else "")
            + ")"
        )

        while True:
            name = _ask("作品名稱（按 Enter 使用官方標題）", default=meta.title)
            if len(name) >= 2 and not name.isdigit():
                break
            _print("[red]作品名稱過短或不能為純數字。[/red]")

        res = self._prompt_resolution_picker(self.config.default_resolution)
        total_eps = self._prompt_total_episodes(meta.total_episodes, min_episodes=meta.online_count)
        status, schedule_note = self._prompt_schedule_picker(meta.schedule_note)

        item = AnimeWatchItem(
            name=name,
            sn=meta.base_sn,
            resolution=res,
            schedule_note=schedule_note,
            total_episodes=total_eps,
            online_episodes=meta.online_count,
            status=status,
            enabled=True,
        )
        self.watchlist, is_new = self.watchlist_store.add_or_update(self.watchlist, item)
        action_word = "新增" if is_new else "更新"
        _print(f"\n[bold green]已{action_word}追番項目：{item.name}[/bold green] (SN: {item.sn}｜{_format_ep_ratio(item.online_episodes, item.total_episodes)}｜{_format_status_schedule(item)})")

        if is_new and self.notifier.enabled and _confirm("是否順便推播「新增追番通知」到手機？", default=False):
            n_title, n_body = format_anime_added_notice(
                name=item.name,
                sn=item.sn,
                online_count=len(episodes),
                total_episodes=item.total_episodes,
                schedule_note=item.schedule_note,
                resolution=item.resolution,
            )
            for _, ok, msg in self.notifier.send(n_title, n_body):
                _print(f"[dim]推播：{msg}[/dim]" if ok else f"[yellow]推播異常：{msg}[/yellow]")

    def _sync_existing_metadata_flow(self) -> None:
        """批次同步既有作品的線上集數、連載／已完結狀態、每週更新時間與預定總集數。"""
        if not self.watchlist:
            _print("[yellow]追番清單是空的。[/yellow]")
            return

        target_raw = _ask("要同步哪幾部？（輸入 all 全部同步，或輸入編號如 2,3，q 取消）", default="all").lower()
        if target_raw == "q":
            return

        if target_raw == "all":
            targets = list(self.watchlist)
        else:
            tokens = parse_multi_targets(target_raw)
            targets = [item for t in tokens if (item := self.watchlist_store.find_by_target(self.watchlist, t))]
            if not targets:
                _print("[red]找不到指定的項目。[/red]")
                return

        overwrite = _confirm("若原本已有填寫時間／總集數，是否強制覆蓋？（選 N 則保留手動值並修正異常值）", default=False)
        updated_count = 0

        for item in targets:
            _print(f"\n[cyan]同步中：{item.name} (SN: {item.sn})...[/cyan]")
            try:
                meta, _ = enrich_anime_metadata(
                    base_sn=item.sn,
                    fallback_title=item.name,
                    cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                    query_external_total=True,
                )
            except KeyboardInterrupt:
                _print("[yellow]已手動中斷同步。[/yellow]")
                break
            except EpisodeFetchError as exc:
                _print(f"  [red]無法連線驗證此 SN：{exc}[/red]")
                continue

            changes: list[str] = []

            # 1. 總是更新「線上已上架集數」
            if item.online_episodes != meta.online_count:
                item.online_episodes = meta.online_count
                changes.append(f"線上集數→{meta.online_count}")

            # 2. 更新標題（若為預設 SN-xxx 或強制覆蓋）
            if meta.title and (overwrite or item.name.startswith("SN-") or item.name.isdigit()):
                item.name = meta.title
                changes.append(f"名稱→{meta.title}")

            # 3. 更新連載／已完結狀態與時間
            if meta.status and (overwrite or not item.status):
                item.status = meta.status
                changes.append(f"狀態→{meta.status}")
            if meta.schedule_note and (overwrite or not item.schedule_note):
                item.schedule_note = meta.schedule_note
                changes.append(f"更新時間→{meta.schedule_note} ({meta.schedule_source})")

            # 4. 更新總集數（若原本總集數 < 線上集數，視為過時錯誤值自動修正）
            is_bad_total = item.total_episodes is not None and item.total_episodes < meta.online_count
            if meta.total_episodes and (overwrite or not item.total_episodes or is_bad_total):
                item.total_episodes = meta.total_episodes
                changes.append(f"總集數→{meta.total_episodes} ({meta.total_source})")
            elif is_bad_total:
                item.total_episodes = None
                changes.append("總集數→已清除異常小於線上集數的舊值")

            if changes:
                updated_count += 1
                _print(f"  [green]已更新：{' ｜ '.join(changes)}[/green]")
            else:
                extra = f"最新上架：{meta.latest_upload_at}" if meta.latest_upload_at else "無需變更"
                _print(f"  [dim]資料已是最新（線上 {meta.online_count} 集｜{extra}）[/dim]")

        if updated_count:
            self.watchlist_store.save(self.watchlist)
            _print(f"\n[bold green]共更新 {updated_count} 部作品資訊並存檔。[/bold green]")

    def _edit_single_anime_flow(self) -> None:
        raw = _ask("輸入要編輯的作品編號或 SN（q 取消）", default="q")
        if raw.lower() == "q":
            return
        item = self.watchlist_store.find_by_target(self.watchlist, raw)
        if not item:
            _print("[red]找不到該項目。[/red]")
            return

        _print(f"\n[bold]編輯：《{item.name}》 (SN: {item.sn})[/bold]")
        item.resolution = self._prompt_resolution_picker(item.resolution)
        item.total_episodes = self._prompt_total_episodes(item.total_episodes, min_episodes=item.online_episodes or 1)
        item.status, item.schedule_note = self._prompt_schedule_picker(item.schedule_note)
        self.watchlist_store.save(self.watchlist)
        _print(f"[green]已更新《{item.name}》設定。[/green]")

    def action_manage_watchlist(self) -> None:
        while True:
            self._clear_if_enabled()
            _section_divider("追番清單與設定")
            if HAS_RICH and console:
                table = Table(show_header=True, header_style="bold magenta", expand=False)
                table.add_column("#", width=3, justify="right")
                table.add_column("啟用", width=4)
                table.add_column("作品名稱", style="cyan", max_width=24)
                table.add_column("SN", width=6)
                table.add_column("畫質", width=5)
                table.add_column("線上/總", width=7, justify="center")
                table.add_column("狀態 / 更新時間", max_width=20)
                for idx, item in enumerate(self.watchlist, 1):
                    st = "[green]啟用[/green]" if item.enabled else "[dim]暫停[/dim]"
                    sched_col = _format_status_schedule(item)
                    if item.status == "已完結":
                        sched_col = f"[dim]{sched_col}[/dim]"
                    elif item.status == "連載中":
                        sched_col = f"[green]{sched_col}[/green]"
                    table.add_row(
                        str(idx),
                        st,
                        item.name,
                        item.sn,
                        f"{item.resolution}P",
                        _format_ep_ratio(item.online_episodes, item.total_episodes),
                        sched_col,
                    )
                console.print(table)
            else:
                for idx, item in enumerate(self.watchlist, 1):
                    st = "啟用" if item.enabled else "暫停"
                    ratio = _format_ep_ratio(item.online_episodes, item.total_episodes)
                    print(f"  {idx}. [{st}] {item.name} | SN={item.sn} | {item.resolution}P | 集數={ratio} | {_format_status_schedule(item)}")

            dedup_desc = "[cyan]僅依實體檔[/cyan]" if self.config.dedup_mode == "file_only" else "[magenta]檔案+歷史+DB[/magenta]"
            up_desc = "[green]開啟[/green]" if self.config.upgrade_lower_resolution else "[dim]關閉[/dim]"
            boot_desc = "[green]開啟[/green]" if self.config.check_on_startup else "[dim]關閉[/dim]"
            clear_desc = "[green]開啟[/green]" if self.config.clear_screen_on_menu else "[dim]關閉[/dim]"

            _print(
                f"\n[bold]設定狀態：[/bold]去重={dedup_desc} ｜ 720P升級1080P={up_desc} ｜ "
                f"上線快報={boot_desc} ｜ 自動清畫面={clear_desc}\n"
            )
            _print("[bold]清單操作：[/bold]  [cyan]1[/cyan] 新增(搜片名/網址/SN)   [cyan]2[/cyan] 同步/補齊資訊   [cyan]3[/cyan] 編輯單部   [cyan]4[/cyan] 啟用/暫停   [cyan]5[/cyan] 刪除(可多選)")
            _print("[bold]偏好開關：[/bold]  [cyan]6[/cyan] 切換去重模式   [cyan]7[/cyan] 切換720P升級   [cyan]8[/cyan] 切換上線快報   [cyan]9[/cyan] 切換清畫面   [cyan]q[/cyan] 返回")

            try:
                sub = _ask("\n請選擇操作", default="q").lower()
            except KeyboardInterrupt:
                return

            if sub == "q":
                break
            elif sub == "1":
                self._add_anime_flow()
                _pause()
            elif sub == "2":
                self._sync_existing_metadata_flow()
                _pause()
            elif sub == "3":
                self._edit_single_anime_flow()
                _pause()
            elif sub == "4":
                raw = _ask("輸入編號或 SN（可多選，如 2,3，q 取消）", default="q")
                if raw.lower() != "q":
                    toggled = self.watchlist_store.toggle_many(self.watchlist, parse_multi_targets(raw))
                    if toggled:
                        for t in toggled:
                            _print(f"  [green]{t.name} → {'啟用' if t.enabled else '暫停'}[/green]")
                    else:
                        _print("[red]找不到對應項目。[/red]")
                    _pause()
            elif sub == "5":
                raw = _ask("輸入要刪除的編號或 SN（可多選，如 5,6，q 取消）", default="q")
                if raw.lower() != "q":
                    removed = self.watchlist_store.remove_many(self.watchlist, parse_multi_targets(raw))
                    if removed:
                        _print("[green]已刪除：[/green]" + "、".join(r.name for r in removed))
                    else:
                        _print("[red]找不到對應項目。[/red]")
                    _pause()
            elif sub == "6":
                self.config.dedup_mode = "file_and_history" if self.config.dedup_mode == "file_only" else "file_only"
                self.config_store.save(self.config)
            elif sub == "7":
                self.config.upgrade_lower_resolution = not self.config.upgrade_lower_resolution
                self.config_store.save(self.config)
            elif sub == "8":
                self.config.check_on_startup = not self.config.check_on_startup
                self.config_store.save(self.config)
            elif sub == "9":
                self.config.clear_screen_on_menu = not self.config.clear_screen_on_menu
                self.config_store.save(self.config)
            else:
                _print("[yellow]無效選項。[/yellow]")
                _pause()

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
            _print("[red]沒有可修復的音軌。[/red]")
            return

        targets = need_fix
        if not targets and not auto_fix:
            if _confirm("全部檔案皆含 AAC 音軌；若用 QuickTime 播放仍無聲，是否全部無損重洗音軌標頭？", default=False):
                targets = all_with_audio
        elif targets and not auto_fix:
            if not _confirm(f"共有 {len(targets)} 個檔案建議修復為標準 AAC MP4，是否立即執行？", default=True):
                return

        for file_path in targets:
            _print(f"  正在修復：{file_path.name}（按 Ctrl+C 可中斷）...")
            try:
                ok, msg = remux_audio_for_quicktime(file_path, replace_original=True)
            except KeyboardInterrupt:
                _print("  [yellow]已手動中斷音軌修復。[/yellow]")
                return
            _print(f"    [green]修復完成：{msg}[/green]" if ok else f"    [red]修復失敗：{msg}[/red]")

    def action_setup_cookie(self) -> None:
        while True:
            self._clear_if_enabled()
            _section_divider("巴哈姆特 Cookie 管理")
            _print(f"檔案路徑：[cyan]{self.env.cookie_path}[/cyan]")
            _print(f"目前狀態：{self._format_cookie_badge()}\n")
            _print("  [cyan]1[/cyan]. 從剪貼簿防呆寫入並自動刷新驗證（推薦）")
            _print("  [cyan]2[/cyan]. Playwright 半自動抓取（關視窗或 Ctrl+C 立刻中止）")
            _print("  [cyan]3[/cyan]. 開啟文字編輯器手動編輯 cookie.txt")
            _print("  [cyan]4[/cyan]. 純刷新重新讀取並線上驗證權限")
            _print("  [cyan]q[/cyan]. 返回主選單")

            try:
                sub = _ask("\n請選擇操作", default="q").lower()
            except KeyboardInterrupt:
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
                _print("[cyan]正在啟動瀏覽器前往巴哈登入頁（關視窗或按 Ctrl+C 立刻中止）...[/cyan]")
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
                    _print(f"[green]已開啟：{self.env.cookie_path}[/green]（存檔後按 4 刷新）")
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
                _print("[yellow]無效選項。[/yellow]")
                _pause()

    def action_send_digest_notification(self, force_rescan: bool = False) -> None:
        self.refresh_env()
        if not self.notifier.enabled:
            _print("[yellow]目前尚未啟用任何推播管道，請先於 manager_config.json 設定。[/yellow]")
            return

        snaps = self.last_snapshots
        if force_rescan or not snaps:
            _print("[dim]正在掃描最新追番進度以產生簡報...[/dim]")
            try:
                snaps = self._scan_watchlist_snapshots(quiet=True)
            except KeyboardInterrupt:
                _print("[yellow]已中斷掃描。[/yellow]")
                return

        n_title, n_body = self._build_digest_from_snapshots(snaps)
        _print(f"\n[bold cyan]即將推播內容預覽：[/bold cyan]\n{n_title}\n{n_body}\n")
        for _, ok, msg in self.notifier.send(n_title, n_body):
            _print(f"[green]成功：{msg}[/green]" if ok else f"[yellow]{msg}[/yellow]")

    def run_interactive(self, skip_startup_check: bool = False) -> None:
        if self.migrated_watchlist:
            _print(f"[green]已自動將舊版追番清單遷移至獨立檔案：{self.watchlist_store.watchlist_path.name}（manager_config.json 已清理）[/green]")
        if self.config.check_on_startup and not skip_startup_check:
            self._print_startup_briefing()

        while True:
            self._clear_if_enabled()
            self.show_banner()

            _print("\n[bold cyan]【追番與下載】[/bold cyan]")
            _print("  [cyan]1[/cyan]. 下載啟用中追番的新集數    [cyan]2[/cyan]. 僅檢查新集數（表格＋推播）")
            _print("  [cyan]3[/cyan]. 管理追番清單與設定        [cyan]4[/cyan]. 臨時指定網址／SN 下載")
            _print("\n[bold cyan]【工具與維護】[/bold cyan]")
            _print("  [cyan]5[/cyan]. Cookie 管理 (VIP/登入)    [cyan]6[/cyan]. 音軌檢查與 QuickTime 修復")
            _print("  [cyan]7[/cyan]. 開啟下載資料夾            [cyan]8[/cyan]. 立即推播「目前追番簡報」到手機")
            _print("\n  [dim]q. 離開程式[/dim]")

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
                    self.action_send_digest_notification(force_rescan=True)
                    _pause()
                else:
                    _print("[yellow]無效選項，請輸入 1~8 或 q。[/yellow]")
                    _pause()
            except KeyboardInterrupt:
                flush_stdin()
                _print("\n[bold yellow]已手動中斷目前任務，安全返回主選單。[/bold yellow]")
                _pause()
