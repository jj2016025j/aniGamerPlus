"""終端互動介面與命令列控制器 v1.6.0（雙主題對稱排版、操作軌跡回顯、持久化多維排序、加密備份還原）。"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .audio_tools import remux_audio_for_quicktime, scan_directory_audio
from .backup_manager import (
    clean_dragged_path,
    export_backup,
    import_backup,
    inspect_backup_file,
)
from .config_store import (
    ALLOWED_RESOLUTIONS,
    SORT_LABELS,
    WATCHLIST_SORT_OPTIONS,
    WEEKDAY_MAP,
    ConfigStore,
    ManagerConfig,
    extract_sn_from_input,
    parse_multi_targets,
    validate_hhmm,
    validate_resolution,
    validate_total_episodes,
)
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
    DigestEntry,
    DownloadedRecord,
    Notifier,
    format_anime_added_notice,
    format_check_digest,
    format_download_report,
)
from .terminal_utils import clear_screen, flush_stdin
from .watchlist_store import (
    AnimeWatchItem,
    WatchlistStore,
    format_views,
    now_timestamp,
    resolve_targets_from_displayed,
    sort_watchlist,
)

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


def _breadcrumb(path_text: str) -> None:
    """醒目的位置分隔線，讓不清畫面時也能一眼辨識段落起點。"""
    print()
    if HAS_RICH and console:
        console.print(Rule(title=f"[bold cyan]{path_text}[/bold cyan]", style="dim cyan", align="left"))
    else:
        print(f"━━ {path_text} " + "━" * max(4, 58 - len(path_text) * 2))
    print()


def _echo_action(label: str) -> None:
    """使用者按下選項後立即回顯動作軌跡，方便回顧操作過程。"""
    _print(f"[dim]▸ 選擇：{label}[/dim]")


def _echo_return(target: str) -> None:
    _print(f"[dim]◂ 返回：{target}[/dim]\n")


def _ask(label: str, default: str = "") -> str:
    flush_stdin()
    if HAS_RICH:
        return Prompt.ask(label, default=default).strip() if default else Prompt.ask(label).strip()
    suffix = f" [{default}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    return val or default


def _ask_password(label: str) -> str:
    flush_stdin()
    try:
        return getpass.getpass(f"{label}: ").strip()
    except Exception:
        return input(f"{label}: ").strip()


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

    def _current_missing_counts(self) -> dict[str, int]:
        return {
            s.item.sn: len(s.diff.missing_episodes)
            for s in self.last_snapshots
            if s.diff is not None
        }

    def _get_sorted_watchlist(self) -> list[AnimeWatchItem]:
        return sort_watchlist(
            self.watchlist,
            sort_by=self.config.watchlist_sort_by,
            desc=self.config.watchlist_sort_desc,
            missing_counts=self._current_missing_counts(),
        )

    def _format_sort_badge(self) -> str:
        label = SORT_LABELS.get(self.config.watchlist_sort_by, self.config.watchlist_sort_by)
        arrow = "↓" if self.config.watchlist_sort_desc else "↑"
        return f"{label} {arrow}"

    def _format_cookie_badge(self) -> str:
        if not self.env.has_cookie:
            return "[yellow]未設定[/yellow]"
        c = inspect_cookie_file(self.env.cookie_path)
        if not c.valid:
            return "[red]格式異常[/red]"
        if c.tier == "logged_in":
            return f"[green]已登入 ({c.baha_id_masked})[/green]"
        if c.tier == "partial_login":
            return f"[yellow]部分憑證 ({c.baha_id_masked})[/yellow]"
        return "[cyan]訪客[/cyan]"

    def _print_cookie_result(self, result: CookieValidationResult, run_online_check: bool = True) -> None:
        if not result.valid:
            _print(f"  [bold red]✗[/bold red] {result.reason}")
            return

        tier_label = {
            "logged_in": "[green]完整登入憑證[/green]",
            "partial_login": "[yellow]不完整憑證[/yellow]",
            "guest": "[cyan]巴哈訪客[/cyan]",
        }.get(result.tier, result.tier)
        keys_str = ", ".join(result.matched_auth_keys) if result.matched_auth_keys else "無登入欄位"
        _print(
            f"  [green]✓[/green] 狀態：{tier_label} ｜ 帳號：[cyan]{result.baha_id_masked}[/cyan] ｜ "
            f"憑證：{keys_str} ｜ {len(result.cleaned_cookie)} 字元"
        )
        for w in result.warnings:
            _print(f"  [yellow]![/yellow] {w}")

        if run_online_check:
            _print("  [dim]連線動畫瘋確認權限中（Ctrl+C 可略過）...[/dim]")
            try:
                online = verify_cookie_online(result.cleaned_cookie)
                color = {"vip": "green", "logged_in": "green", "guest": "cyan", "blocked": "yellow", "error": "yellow"}.get(
                    online.status, "white"
                )
                _print(f"  [{color}]{online.detail}[/{color}]")
            except KeyboardInterrupt:
                _print("  [yellow]![/yellow] 已略過線上檢查。")

    def _scan_watchlist_snapshots(self, items: list[AnimeWatchItem] | None = None, quiet: bool = False) -> list[AnimeScanSnapshot]:
        targets = items if items is not None else [w for w in self._get_sorted_watchlist() if w.enabled]
        snapshots: list[AnimeScanSnapshot] = []
        changed_wl = False

        for item in targets:
            if not quiet:
                _print(f"  [dim]掃描：{item.name} (SN: {item.sn})...[/dim]")
            try:
                title, online_eps = fetch_anime_episodes(
                    base_sn=item.sn,
                    cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                    fallback_title=item.name,
                )
                if item.online_episodes != len(online_eps):
                    item.online_episodes = len(online_eps)
                    changed_wl = True
                if item.total_episodes is not None and item.total_episodes < len(online_eps):
                    item.total_episodes = None
                    changed_wl = True

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

        if changed_wl:
            self.watchlist_store.save(self.watchlist)
        if items is None:
            self.last_snapshots = snapshots
        return snapshots

    def _build_digest_from_snapshots(self, snapshots: list[AnimeScanSnapshot]) -> tuple[str, str]:
        new_entries: list[DigestEntry] = []
        up_to_date_entries: list[DigestEntry] = []
        errors: list[str] = []

        for snap in snapshots:
            if snap.error or not snap.diff:
                errors.append(f"{snap.item.name}：{snap.error or '無法解析'}")
                continue
            ratio = _format_ep_ratio(snap.online_count, snap.item.total_episodes)
            sched_raw = _format_status_schedule(snap.item)
            sched = "" if sched_raw == "-" else sched_raw
            if snap.diff.missing_episodes:
                range_str = format_episode_ranges(snap.diff.missing_episodes, compact=False)
                new_entries.append(
                    DigestEntry(
                        title=snap.title,
                        progress=ratio,
                        schedule=sched,
                        missing_text=range_str,
                        missing_count=len(snap.diff.missing_episodes),
                    )
                )
            else:
                up_to_date_entries.append(
                    DigestEntry(
                        title=snap.title,
                        progress=ratio,
                        schedule=sched,
                    )
                )

        skipped = [w.name for w in self._get_sorted_watchlist() if not w.enabled]
        return format_check_digest(new_entries, up_to_date_entries, skipped, errors)

    def _dispatch_notifications_live(self, title: str, body: str) -> None:
        """逐管道即時推播並精簡顯示狀態，不洩露網址，失敗時才顯示診斷原因。"""
        providers = self.notifier.enabled_providers
        if not providers:
            _print("  [yellow]![/yellow] 尚未啟用任何推播管道。")
            return

        _print(f"  [dim]推播發送中 ({len(providers)} 個管道)...[/dim]")
        ok_count = 0
        for label, ok, detail in self.notifier.send_iter(title, body):
            if ok:
                ok_count += 1
                _print(f"  [green]✓[/green] {label}")
            else:
                _print(f"  [red]✗[/red] {label} [dim]({detail})[/dim]")
        color = "green" if ok_count == len(providers) else "yellow"
        _print(f"  [{color}]推播完成：{ok_count}/{len(providers)} 成功[/{color}]")

    def _print_startup_briefing(self) -> None:
        enabled_items = [w for w in self._get_sorted_watchlist() if w.enabled]
        if not enabled_items:
            return
        _print(f"[dim]上線快報掃描中（{len(enabled_items)} 部追蹤中，Ctrl+C 可略過）...[/dim]")
        try:
            self.last_snapshots = self._scan_watchlist_snapshots(enabled_items, quiet=True)
        except KeyboardInterrupt:
            _print("[yellow]! 已略過上線快報。[/yellow]")

    def show_banner(self) -> None:
        """精簡儀表板：系統狀態＋追蹤中作品快報（已移除冗餘字眼）。"""
        core_tag = "[green]OK[/green]" if self.env.anigamer_script else "[red]缺核心[/red]"
        ff_tag = "[green]OK[/green]" if self.env.has_ffmpeg else "[red]未裝[/red]"
        notify_tag = f"[green]{'/'.join(self.notifier.enabled_providers)}[/green]" if self.notifier.enabled else "[dim]關[/dim]"
        up_tag = "[green]開[/green]" if self.config.upgrade_lower_resolution else "[dim]關[/dim]"

        status_line = (
            f"[bold]環境：[/bold]{self.env.os_name} (核心:{core_tag} / FFmpeg:{ff_tag})   "
            f"[bold]Cookie：[/bold]{self._format_cookie_badge()}\n"
            f"[bold]策略：[/bold]{self.config.default_resolution}P ｜ 升級:{up_tag} ｜ 推播:{notify_tag}"
        )

        snap_by_sn = {s.item.sn: s for s in self.last_snapshots}
        sorted_items = self._get_sorted_watchlist()
        enabled_items = [w for w in sorted_items if w.enabled]
        skipped_count = len(sorted_items) - len(enabled_items)

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
                        state_txt = f"[green]已下載 ({snap.diff.total_local_count}/{snap.online_count}集)[/green]"
                elif snap and snap.error:
                    state_txt = "[red]檢查失敗[/red]"
                else:
                    ratio = _format_ep_ratio(w.online_episodes, w.total_episodes)
                    state_txt = f"[dim]{ratio}｜{w.resolution}P[/dim]"
                brief_lines.append(f"  • [bold]{w.name}[/bold]{sched} → {state_txt}")
        else:
            brief_lines.append("  [yellow]（目前無追蹤中作品，請進「4. 追番清單管理」設定）[/yellow]")

        header_title = f"追蹤中 ({len(enabled_items)} 部" + (f"｜略過 {skipped_count} 部" if skipped_count else "") + ")"
        body = f"{status_line}\n\n[bold]{header_title}：[/bold]\n" + "\n".join(brief_lines)

        if HAS_RICH and console:
            console.print(Panel(body, title="aniGamerPlus 管理器 v1.6.0", border_style="cyan", padding=(0, 2)))
        else:
            print("=" * 68)
            _print(body)
            print("=" * 68)

    def _render_main_menu(self) -> None:
        """雙主題各自縱向排列、左右對齊並排；終端極窄時自動切換純單欄縱向。"""
        left_rows = [
            "[cyan]1.[/cyan] 檢查與下載",
            "[cyan]2.[/cyan] 臨時下載",
            "[cyan]3.[/cyan] 推播追番簡報",
            "[cyan]4.[/cyan] 追番清單管理",
        ]
        right_rows = [
            "[cyan]5.[/cyan] Cookie 管理",
            "[cyan]6.[/cyan] 檔案修復",
            "[cyan]7.[/cyan] 開啟下載資料夾",
            "[cyan]8.[/cyan] 系統設定",
        ]

        term_width = shutil.get_terminal_size((80, 24)).columns
        print()
        if HAS_RICH and console and term_width >= 58:
            grid = Table.grid(padding=(0, 5))
            grid.add_column(width=22)
            grid.add_column(width=22)
            grid.add_row("[bold cyan]【追番與下載】[/bold cyan]", "[bold cyan]【工具與維護】[/bold cyan]")
            for l_txt, r_txt in zip(left_rows, right_rows):
                grid.add_row(f"  {l_txt}", f"  {r_txt}")
            console.print(grid)
        else:
            _print("[bold cyan]【追番與下載】[/bold cyan]")
            for l_txt in left_rows:
                _print(f"  {l_txt}")
            _print("\n[bold cyan]【工具與維護】[/bold cyan]")
            for r_txt in right_rows:
                _print(f"  {r_txt}")
        _print("\n  [dim]q. 離開（預設）[/dim]")

    def _render_check_tables(self, snapshots: list[AnimeScanSnapshot], show_sn_table: bool = True) -> None:
        if HAS_RICH and console:
            summary = Table(title="更新檢查總覽", show_header=True, header_style="bold cyan")
            summary.add_column("作品名稱", style="bold", max_width=26)
            summary.add_column("狀態 / 更新", max_width=16)
            summary.add_column("線上/總", justify="center", width=8)
            summary.add_column("本地已有", justify="center", width=10)
            summary.add_column("待下載", style="yellow")

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
                    miss_str = "[green]已下載[/green]"
                summary.add_row(snap.title, sched, ratio, local_str, miss_str)
            console.print(summary)
        else:
            print("\n=== 更新檢查總覽 ===")
            for snap in snapshots:
                if snap.error or not snap.diff:
                    print(f"  {snap.title} | 錯誤：{snap.error}")
                    continue
                ratio = _format_ep_ratio(snap.online_count, snap.item.total_episodes)
                miss = format_episode_ranges(snap.diff.missing_episodes) if snap.diff.missing_episodes else "已下載"
                print(f"  {snap.title} | {ratio} | 本地:{snap.diff.total_local_count} | {miss}")

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
            sn_tbl = Table(title="待下載 SN 明細", show_header=True, header_style="bold magenta")
            sn_tbl.add_column("作品名稱", style="cyan", max_width=28)
            sn_tbl.add_column("集數", justify="right", width=10)
            sn_tbl.add_column("SN", justify="center", width=10)
            for name_col, ep_col, sn_col in pending_rows:
                sn_tbl.add_row(name_col, ep_col, sn_col)
            console.print(sn_tbl)
        else:
            print("--- 待下載 SN 明細 ---")
            for name_col, ep_col, sn_col in pending_rows:
                prefix = f"{name_col}\n" if name_col else ""
                print(f"{prefix}    {ep_col:<8}  SN={sn_col}")

    def _prompt_schedule_picker(self, auto_schedule: str = "") -> tuple[str, str]:
        if auto_schedule:
            _print(f"  [green]0.[/green] 套用偵測結果 [dim]({auto_schedule})[/dim]")
        _print("  [cyan]1.[/cyan] 連載中 [dim](選星期＋時段)[/dim]")
        _print("  [cyan]2.[/cyan] 已完結")
        _print("  [cyan]3.[/cyan] 留空")

        default_opt = "0" if auto_schedule else "1"
        while True:
            mode = _ask("選擇", default=default_opt)
            if mode == "0" and auto_schedule:
                st = "已完結" if "已完結" in auto_schedule else "連載中"
                return st, auto_schedule
            if mode == "2":
                return "已完結", "已完結"
            if mode == "3":
                return "", ""
            if mode == "1":
                _print("  [cyan]1[/cyan]週一  [cyan]2[/cyan]週二  [cyan]3[/cyan]週三  [cyan]4[/cyan]週四  [cyan]5[/cyan]週五  [cyan]6[/cyan]週六  [cyan]7[/cyan]週日")
                while True:
                    d = _ask("星期 (1~7)", default="7")
                    if d in WEEKDAY_MAP:
                        day_str = WEEKDAY_MAP[d]
                        break
                    _print("  [red]✗[/red] 請輸入 1~7。")

                presets = {"1": "00:00", "2": "00:30", "3": "01:00", "4": "01:30", "5": "22:00", "6": "23:00", "7": "23:30"}
                _print("  [cyan]1[/cyan]00:00  [cyan]2[/cyan]00:30  [cyan]3[/cyan]01:00  [cyan]4[/cyan]01:30  [cyan]5[/cyan]22:00  [cyan]6[/cyan]23:00  [cyan]7[/cyan]23:30  [cyan]8[/cyan]自訂")
                while True:
                    t_opt = _ask("時段 (1~8)", default="3")
                    if t_opt in presets:
                        return "連載中", f"每{day_str} {presets[t_opt]}"
                    if t_opt == "8":
                        raw_t = _ask("輸入時間 (HH:MM)")
                        valid_t = validate_hhmm(raw_t)
                        if valid_t:
                            return "連載中", f"每{day_str} {valid_t}"
                        _print("  [red]✗[/red] 格式需為 00:00 ~ 23:59。")
                    else:
                        _print("  [red]✗[/red] 請輸入 1~8。")
            _print("  [red]✗[/red] 無效選項。")

    def _prompt_resolution_picker(self, default_res: str) -> str:
        opts = "/".join(ALLOWED_RESOLUTIONS)
        while True:
            raw = _ask(f"輸入畫質 ({opts})", default=default_res)
            valid = validate_resolution(raw)
            if valid:
                return valid
            _print(f"  [red]✗[/red] 僅支援：{', '.join(ALLOWED_RESOLUTIONS)}")

    def _prompt_total_episodes(self, auto_total: int | None = None, min_episodes: int = 1) -> int | None:
        default_str = str(auto_total) if auto_total else ""
        label = "輸入總集數 (- 清空)" if auto_total else "輸入總集數 (Enter 留空)"
        while True:
            raw = _ask(label, default=default_str)
            ok, val, err = validate_total_episodes(raw)
            if not ok:
                _print(f"  [red]✗[/red] {err}")
                continue
            if val is not None and val < min_episodes:
                _print(f"  [red]✗[/red] 不能小於線上已上架集數 ({min_episodes})。")
                continue
            return val

    def _execute_downloads(
        self,
        snapshots: list[AnimeScanSnapshot],
        upgrade_plans: dict[str, list[EpisodeInfo]],
        interactive: bool = True,
    ) -> None:
        downloaded_ok: list[DownloadedRecord] = []
        downloaded_fail: list[str] = []
        remaining_after: list[str] = []
        was_interrupted = False
        completed_by_sn: dict[str, set[str]] = {}

        for snap in snapshots:
            if snap.error or not snap.diff:
                if interactive and _confirm(f"\n《{snap.title}》解析失敗，改用原生 -m latest 抓最新一集？", default=False):
                    _, interrupted = self.downloader.download_latest_direct(snap.item.sn, snap.item.resolution)
                    if interrupted:
                        was_interrupted = True
                        _print("  [yellow]![/yellow] 已乾淨中斷下載。")
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
                _print(f"\n[bold blue]開始下載：《{title}》{r_str}（共 {len(missing_queue)} 集｜Ctrl+C 可中斷）[/bold blue]")

                def _on_start_new(idx: int, total: int, ep: EpisodeInfo) -> None:
                    _print(f"\n[bold blue][{idx}/{total}] {title} 第 {ep.episode_label} 集 (sn={ep.sn})[/bold blue]")

                def _on_complete_new(idx: int, total: int, ep: EpisodeInfo, res: DownloadResult) -> None:
                    if res.interrupted:
                        downloaded_fail.append(f"《{title}》第 {ep.episode_label} 集（手動中斷）")
                        clean_note = ""
                        if res.cleaned_temp_count > 0:
                            sz = _format_size_mb(res.cleaned_temp_bytes)
                            clean_note = f"；已清理 temp/ 殘檔 {res.cleaned_temp_count} 項" + (f" ({sz})" if sz else "")
                        _print(f"\n  [yellow]![/yellow] 已中斷：《{title}》第 {ep.episode_label} 集{clean_note}")
                    elif res.success:
                        completed_sns.add(ep.sn)
                        actual_res, fpath = self.tracker.find_highest_local_resolution(ep, anime_name_hint=item.name)
                        self.tracker.mark_downloaded(ep, actual_res or item.resolution, fpath)
                        res_tag = f"{actual_res}P" if actual_res else f"{item.resolution}P"
                        downloaded_ok.append(DownloadedRecord(title=title, episode=ep, resolution_label=res_tag))
                        _print(f"  [green]✓[/green] 完成：《{title}》第 {ep.episode_label} 集 ({res_tag})")
                    else:
                        downloaded_fail.append(f"《{title}》第 {ep.episode_label} 集（{res.message}）")
                        _print(f"  [red]✗[/red] 下載異常 ({res.message})：《{title}》第 {ep.episode_label} 集")

                def _on_cooldown_new(secs: int, next_ep: EpisodeInfo) -> None:
                    _print(f"  [dim]冷卻 {secs} 秒後下載第 {next_ep.episode_label} 集（Ctrl+C 可無損中止）...[/dim]")

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
                    _print("  [yellow]![/yellow] 已中止本次下載批次。")
                    break

            if upgrade_queue and not item_aborted:
                target_res_int = parse_resolution_int(item.resolution)
                _print(f"\n[magenta]開始畫質升級：《{title}》（目標 {target_res_int}P）[/magenta]")
                circuit_broken = False

                def _on_start_up(idx: int, total: int, ep: EpisodeInfo) -> None:
                    _print(f"\n[bold magenta][升級 {idx}/{total}] {title} 第 {ep.episode_label} 集 (sn={ep.sn})[/bold magenta]")

                def _on_complete_up(idx: int, total: int, ep: EpisodeInfo, res: DownloadResult) -> None:
                    if res.interrupted:
                        clean_note = ""
                        if res.cleaned_temp_count > 0:
                            sz = _format_size_mb(res.cleaned_temp_bytes)
                            clean_note = f"；已清理 temp/ 殘檔 {res.cleaned_temp_count} 項" + (f" ({sz})" if sz else "")
                        _print(f"\n  [yellow]![/yellow] 已中斷升級：《{title}》第 {ep.episode_label} 集{clean_note}")
                    elif not res.success:
                        downloaded_fail.append(f"《{title}》第 {ep.episode_label} 集升級失敗（{res.message}）")

                def _on_cooldown_up(secs: int, next_ep: EpisodeInfo) -> None:
                    _print(f"  [dim]冷卻 {secs} 秒後升級第 {next_ep.episode_label} 集（Ctrl+C 可無損中止）...[/dim]")

                def _post_check_upgrade(ep: EpisodeInfo, res: DownloadResult) -> bool:
                    nonlocal circuit_broken
                    if not res.success:
                        return False
                    actual_res, fpath = self.tracker.find_highest_local_resolution(ep, anime_name_hint=item.name)
                    if actual_res >= target_res_int - 100:
                        self.tracker.mark_downloaded(ep, actual_res, fpath)
                        downloaded_ok.append(DownloadedRecord(title=title, episode=ep, resolution_label=f"{actual_res}P升級"))
                        _print(f"  [green]✓[/green] 升級成功：《{title}》第 {ep.episode_label} 集 ({actual_res}P)")
                        return False
                    circuit_broken = True
                    _print(f"  [red]✗[/red] 後置熔斷：實際僅 {actual_res or '未知'}P，已中止剩餘升級。")
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
                    _print("  [yellow]![/yellow] 已中止本次升級批次。")
                    break

        for snap in snapshots:
            if snap.error or not snap.diff or not snap.diff.missing_episodes:
                continue
            done_sns = completed_by_sn.get(snap.item.sn, set())
            leftover = [e for e in snap.diff.missing_episodes if e.sn not in done_sns]
            if leftover:
                r_txt = format_episode_ranges(leftover, compact=False)
                remaining_after.append(f"《{snap.title}》剩餘 {r_txt}（共 {len(leftover)} 集）")

        if downloaded_ok or downloaded_fail or (was_interrupted and remaining_after):
            n_title, n_body = format_download_report(
                downloaded_ok,
                downloaded_fail,
                remaining_after,
                interrupted=was_interrupted,
            )
            border = "yellow" if was_interrupted else "green"
            print()
            if HAS_RICH and console:
                console.print(Panel(n_body, title=n_title, border_style=border, padding=(0, 2)))
            else:
                print(f"=== {n_title} ===\n{n_body}")

            if not was_interrupted and not downloaded_fail:
                _print(f"  [bold green]✓[/bold green] 全部下載完成（共 {len(downloaded_ok)} 集）。")

            if self.notifier.enabled:
                should_push = True
                if was_interrupted:
                    should_push = _confirm("\n本次為手動中斷，仍要推播結算到手機嗎？", default=False) if interactive else False
                if should_push:
                    print()
                    self._dispatch_notifications_live(n_title, n_body)
        else:
            _print("\n  [bold green]✓[/bold green] 所有追蹤作品皆已下載，本次無需下載。")

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
            _print(f"\n[bold blue]強制單集下載（忽略紀錄｜Ctrl+C 可中斷）：sn={single_sn}[/bold blue]")
            res = self.downloader.download_episode(ep, resolution=self.config.default_resolution)
            if res.interrupted:
                _print("  [yellow]![/yellow] 已中斷單集下載。")
            elif res.success:
                _print(f"  [green]✓[/green] 單集下載完成：sn={single_sn}")
            else:
                _print(f"  [red]✗[/red] 下載異常：{res.message}")
            return

        if single_sn:
            items = [
                next(
                    (w for w in self.watchlist if w.sn == single_sn),
                    AnimeWatchItem(name=f"SN-{single_sn}", sn=single_sn, resolution=self.config.default_resolution),
                )
            ]
        else:
            items = [w for w in self._get_sorted_watchlist() if w.enabled]

        if not items:
            _print("  [yellow]![/yellow] 目前沒有追蹤中的作品，請進「4. 追番清單管理」設定。")
            return

        _print(f"[dim]掃描 {len(items)} 部作品中...[/dim]")
        snapshots = self._scan_watchlist_snapshots(items, quiet=False)
        if not single_sn:
            self.last_snapshots = snapshots

        print()
        self._render_check_tables(snapshots, show_sn_table=True)

        # VIP 升級前置檢查（合併提示不洗版）
        vip_eligibility_cache: dict[int, tuple[bool, bool, str]] = {}
        upgrade_plans: dict[str, list[EpisodeInfo]] = {}
        skipped_upgrade_count = 0

        for snap in snapshots:
            if snap.error or not snap.diff or not snap.diff.upgradable_episodes:
                continue
            target_res_int = parse_resolution_int(snap.item.resolution)
            if target_res_int not in vip_eligibility_cache:
                _print("\n  [dim]確認 720P→1080P 升級權限...[/dim]")
                elig = check_vip_upgrade_eligibility(self.env.cookie_path, target_res_int)
                vip_eligibility_cache[target_res_int] = (elig.allowed, elig.certain, elig.reason)
            allowed, _, _ = vip_eligibility_cache[target_res_int]
            if allowed:
                upgrade_plans[snap.item.sn] = [ep for ep, _ in snap.diff.upgradable_episodes]
            else:
                skipped_upgrade_count += 1

        if upgrade_plans:
            _print(f"  [green]✓[/green] VIP 驗證通過：將升級 {len(upgrade_plans)} 部作品。")
        elif skipped_upgrade_count:
            _print(f"  [yellow]![/yellow] 已略過 {skipped_upgrade_count} 部畫質升級（目前非 VIP，避免重抓 720P）。")

        if not download:
            total_miss = sum(len(s.diff.missing_episodes) for s in snapshots if s.diff)
            if total_miss == 0:
                _print("\n  [bold green]✓[/bold green] 檢查完成：所有追蹤作品皆已下載。")
            else:
                _print(f"\n  [bold cyan]✓[/bold cyan] 檢查完成：共有 {total_miss} 集待下載。")

            if not interactive:
                if push_check_notify and snapshots and self.notifier.enabled:
                    n_title, n_body = self._build_digest_from_snapshots(snapshots)
                    self._dispatch_notifications_live(n_title, n_body)
                return

            # 互動快捷列：看完結果可直接決定下載或推播，不必重跑
            if total_miss > 0 or upgrade_plans:
                opts_txt = "[cyan]d[/cyan] 立即下載"
                if self.notifier.enabled:
                    opts_txt += "   [cyan]n[/cyan] 推播簡報"
                opts_txt += "   [dim]q 返回[/dim]"
                _print(f"\n  後續操作：{opts_txt}")
                act = _ask("請選擇", default="q").lower()
                if act == "d":
                    _echo_action("立即下載本次缺漏集數")
                    self._execute_downloads(snapshots, upgrade_plans, interactive=True)
                elif act == "n" and self.notifier.enabled:
                    _echo_action("推播本次追番簡報")
                    n_title, n_body = self._build_digest_from_snapshots(snapshots)
                    self._dispatch_notifications_live(n_title, n_body)
            elif snapshots and self.notifier.enabled and _confirm("\n是否將簡報推播到手機？", default=False):
                _echo_action("推播追番簡報")
                n_title, n_body = self._build_digest_from_snapshots(snapshots)
                self._dispatch_notifications_live(n_title, n_body)
            return

        self._execute_downloads(snapshots, upgrade_plans, interactive=interactive)

    def action_check_and_download_menu(self) -> None:
        self._clear_if_enabled()
        _breadcrumb("主選單 › 檢查與下載")
        _print("  [cyan]1.[/cyan] 只檢查不下載")
        _print("  [cyan]2.[/cyan] 檢查並立即下載")
        _print("  [dim]q. 返回[/dim]")

        sub = _ask("\n請選擇", default="1").lower()
        if sub == "q":
            _echo_return("主選單")
            return
        if sub == "1":
            _echo_action("1. 只檢查不下載")
            self.action_check_or_update(download=False, interactive=True)
            _pause()
        elif sub == "2":
            _echo_action("2. 檢查並立即下載")
            self.action_check_or_update(download=True, interactive=True)
            _pause()
        else:
            _print("  [yellow]![/yellow] 無效選項。")
            _pause()
        _echo_return("主選單")

    def action_ad_hoc_download(self) -> None:
        self._clear_if_enabled()
        _breadcrumb("主選單 › 臨時下載")
        raw = _ask("貼上動畫瘋網址或 SN (q 取消)", default="q")
        if not raw or raw.lower() == "q":
            _echo_return("主選單")
            return
        sn = extract_sn_from_input(raw)
        if not sn:
            _print("  [red]✗[/red] 無效 SN（需為 4 碼以上數字或含 ?sn= 網址）。")
            _pause()
            _echo_return("主選單")
            return

        _print("\n  [cyan]1.[/cyan] 檢查全集並下載缺漏")
        _print("  [cyan]2.[/cyan] 強制下載此單集 [dim](忽略紀錄)[/dim]")
        _print("  [dim]q. 取消[/dim]")
        mode = _ask("選擇模式", default="1").lower()
        if mode == "1":
            _echo_action(f"臨時下載全集缺漏 (SN: {sn})")
            self.action_check_or_update(download=True, single_sn=sn, force_single_episode=False)
            _pause()
        elif mode == "2":
            _echo_action(f"強制單集下載 (SN: {sn})")
            self.action_check_or_update(download=True, single_sn=sn, force_single_episode=True)
            _pause()
        _echo_return("主選單")

    def _add_anime_flow(self) -> None:
        _breadcrumb("主選單 › 追番清單管理 › 新增作品")
        raw = _ask("輸入片名關鍵字 / 網址 / SN (q 取消)", default="q")
        if not raw or raw.lower() == "q":
            return

        sn = extract_sn_from_input(raw)
        if sn:
            _print(f"  [dim]驗證 SN={sn} 並擷取資訊...[/dim]")
            try:
                meta, episodes = enrich_anime_metadata(
                    base_sn=sn,
                    cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                )
            except EpisodeFetchError as exc:
                _print(f"  [red]✗[/red] 驗證失敗：{exc}")
                return
        else:
            if raw.isdigit():
                _print("  [red]✗[/red] SN 至少為 4 碼數字；搜尋作品請輸入片名。")
                return

            _print(f"  [dim]搜尋動畫瘋：「{raw}」...[/dim]")
            try:
                candidates = search_anigamer(raw, cookie_path=self.env.cookie_path if self.env.has_cookie else None)
            except EpisodeFetchError as exc:
                _print(f"  [red]✗[/red] 搜尋失敗：{exc}")
                return

            if not candidates:
                _print(f"  [yellow]![/yellow] 找不到與「{raw}」相符的作品。")
                return

            if HAS_RICH and console:
                tbl = Table(title=f"搜尋結果：{raw}", show_header=True, header_style="bold cyan")
                tbl.add_column("#", width=3, justify="right")
                tbl.add_column("作品名稱", style="bold")
                tbl.add_column("年份", width=9)
                tbl.add_column("上架", width=8)
                for idx, c in enumerate(candidates, 1):
                    tbl.add_row(str(idx), c.title, c.year_text or "-", c.ep_count_text or "-")
                console.print(tbl)
            else:
                for idx, c in enumerate(candidates, 1):
                    print(f"  {idx}. {c.title} ({c.year_text} / {c.ep_count_text})")

            pick = _ask(f"選擇編號 (1~{len(candidates)}，q 取消)", default="q").lower()
            if pick == "q" or not pick.isdigit() or not (1 <= int(pick) <= len(candidates)):
                _print("  [dim]已取消。[/dim]")
                return

            chosen = candidates[int(pick) - 1]
            _echo_action(f"選取《{chosen.title}》")
            _print("  [dim]解析基準 SN、週表與總集數...[/dim]")
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
                _print(f"  [red]✗[/red] 解析失敗：{exc}")
                return

        preview_lines = (
            f"[bold]作品：[/bold]{meta.title}\n"
            f"[bold]SN：[/bold]{meta.base_sn}   "
            f"[bold]畫質：[/bold]{self.config.default_resolution}P   "
            f"[bold]進度：[/bold]{_format_ep_ratio(meta.online_count, meta.total_episodes)}"
            + (f"   [bold]熱門度：[/bold]{format_views(meta.views)}" if meta.views else "")
            + f"\n[bold]狀態：[/bold]{meta.schedule_note or '未偵測'}"
            + (f"   [dim](最新上架 {meta.latest_upload_at})[/dim]" if meta.latest_upload_at else "")
        )
        print()
        if HAS_RICH and console:
            console.print(Panel(preview_lines, title="偵測結果預覽", border_style="green", padding=(0, 2)))
        else:
            _print(preview_lines)

        _print("\n  [cyan]1.[/cyan] 快速導入 [dim](直接套用上方偵測結果)[/dim]")
        _print("  [cyan]2.[/cyan] 逐步自訂 [dim](共 4 項設定)[/dim]")
        _print("  [dim]q. 取消[/dim]")
        import_mode = _ask("選擇導入方式", default="1").lower()
        if import_mode == "q":
            _print("  [dim]已取消新增。[/dim]")
            return

        if import_mode == "2":
            _echo_action("2. 逐步自訂設定")
            _print("\n[bold cyan][1/4] 作品名稱[/bold cyan]")
            while True:
                name = _ask("輸入名稱", default=meta.title)
                if len(name) >= 2 and not name.isdigit():
                    break
                _print("  [red]✗[/red] 名稱過短或不可為純數字。")

            _print("\n[bold cyan][2/4] 目標畫質[/bold cyan]")
            res = self._prompt_resolution_picker(self.config.default_resolution)

            _print("\n[bold cyan][3/4] 預計總集數[/bold cyan]")
            total_eps = self._prompt_total_episodes(meta.total_episodes, min_episodes=meta.online_count)

            _print("\n[bold cyan][4/4] 連載狀態與更新時間[/bold cyan]")
            status, schedule_note = self._prompt_schedule_picker(meta.schedule_note)
        else:
            _echo_action("1. 快速導入")
            name = meta.title
            res = self.config.default_resolution
            total_eps = meta.total_episodes
            status = meta.status
            schedule_note = meta.schedule_note

        item = AnimeWatchItem(
            name=name,
            sn=meta.base_sn,
            resolution=res,
            schedule_note=schedule_note,
            total_episodes=total_eps,
            online_episodes=meta.online_count,
            status=status,
            enabled=True,
            added_at=now_timestamp(),
            latest_upload_at=meta.latest_upload_at,
            views=meta.views,
        )
        self.watchlist, is_new = self.watchlist_store.add_or_update(self.watchlist, item)
        verb = "新增" if is_new else "更新"
        _print(f"\n  [bold green]✓[/bold green] 已{verb}：《{item.name}》(SN: {item.sn}｜{_format_ep_ratio(item.online_episodes, item.total_episodes)}｜{_format_status_schedule(item)})")

        if is_new and self.notifier.enabled and _confirm("\n推播「新增追番通知」到手機？", default=False):
            n_title, n_body = format_anime_added_notice(
                name=item.name,
                sn=item.sn,
                online_count=len(episodes),
                total_episodes=item.total_episodes,
                schedule_note=item.schedule_note,
                resolution=item.resolution,
            )
            self._dispatch_notifications_live(n_title, n_body)

    def _sync_existing_metadata_flow(self, displayed: list[AnimeWatchItem]) -> None:
        _breadcrumb("主選單 › 追番清單管理 › 編輯與管理 › 自動同步資訊")
        if not displayed:
            _print("  [yellow]![/yellow] 追番清單是空的。")
            return

        target_raw = _ask("輸入對象 (all 全部 / 編號或 SN，多筆用逗號，q 取消)", default="all").lower()
        if target_raw == "q":
            return

        targets = list(displayed) if target_raw == "all" else resolve_targets_from_displayed(displayed, parse_multi_targets(target_raw))
        if not targets:
            _print("  [red]✗[/red] 找不到對應項目。")
            return

        overwrite = _confirm("強制覆蓋已填寫的時間與總集數？（選 N 僅補空白並修異常值）", default=False)
        updated_count = 0

        for item in targets:
            _print(f"\n  [dim]同步中：《{item.name}》(SN: {item.sn})...[/dim]")
            try:
                meta, _ = enrich_anime_metadata(
                    base_sn=item.sn,
                    fallback_title=item.name,
                    cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                    query_external_total=True,
                )
            except KeyboardInterrupt:
                _print("  [yellow]![/yellow] 已手動中斷同步。")
                break
            except EpisodeFetchError as exc:
                _print(f"  [red]✗[/red] 無法驗證 SN：{exc}")
                continue

            changes: list[str] = []
            if item.online_episodes != meta.online_count:
                item.online_episodes = meta.online_count
                changes.append(f"線上→{meta.online_count}")
            if meta.latest_upload_at and item.latest_upload_at != meta.latest_upload_at:
                item.latest_upload_at = meta.latest_upload_at
                changes.append(f"上架→{meta.latest_upload_at[:10]}")
            if meta.views and item.views != meta.views:
                item.views = meta.views
                changes.append(f"熱門→{format_views(meta.views)}")
            if meta.title and (overwrite or item.name.startswith("SN-") or item.name.isdigit()):
                item.name = meta.title
                changes.append(f"名稱→{meta.title}")
            if meta.status and (overwrite or not item.status):
                item.status = meta.status
                changes.append(f"狀態→{meta.status}")
            if meta.schedule_note and (overwrite or not item.schedule_note):
                item.schedule_note = meta.schedule_note
                changes.append(f"時間→{meta.schedule_note}")

            is_bad_total = item.total_episodes is not None and item.total_episodes < meta.online_count
            if meta.total_episodes and (overwrite or not item.total_episodes or is_bad_total):
                item.total_episodes = meta.total_episodes
                changes.append(f"總集→{meta.total_episodes}")
            elif is_bad_total:
                item.total_episodes = None
                changes.append("總集→已清除異常舊值")

            if changes:
                updated_count += 1
                _print(f"  [green]✓[/green] 《{item.name}》：{' ｜ '.join(changes)}")
            else:
                _print(f"  [dim]• 《{item.name}》無需變更[/dim]")

        if updated_count:
            self.watchlist_store.save(self.watchlist)
            _print(f"\n  [bold green]✓[/bold green] 共更新 {updated_count} 部作品資訊並存檔。")

    def _edit_single_anime_flow(self, displayed: list[AnimeWatchItem]) -> None:
        _breadcrumb("主選單 › 追番清單管理 › 編輯與管理 › 手動編輯單部")
        raw = _ask("輸入編號或 SN (q 取消)", default="q")
        if raw.lower() == "q":
            return
        matched = resolve_targets_from_displayed(displayed, [raw])
        if not matched:
            _print("  [red]✗[/red] 找不到該項目。")
            return

        item = matched[0]
        _print(f"\n[bold]編輯：《{item.name}》(SN: {item.sn})[/bold]")
        _print("\n[bold cyan][1/3] 目標畫質[/bold cyan]")
        item.resolution = self._prompt_resolution_picker(item.resolution)
        _print("\n[bold cyan][2/3] 預計總集數[/bold cyan]")
        item.total_episodes = self._prompt_total_episodes(item.total_episodes, min_episodes=item.online_episodes or 1)
        _print("\n[bold cyan][3/3] 連載狀態與更新時間[/bold cyan]")
        item.status, item.schedule_note = self._prompt_schedule_picker(item.schedule_note)
        self.watchlist_store.save(self.watchlist)
        _print(f"\n  [bold green]✓[/bold green] 已更新《{item.name}》。")

    def _delete_anime_flow(self, displayed: list[AnimeWatchItem]) -> None:
        """附帶明確名稱回顯與二次確認的刪除流程，徹底防止誤刪。"""
        _breadcrumb("主選單 › 追番清單管理 › 編輯與管理 › 刪除作品")
        raw = _ask("輸入要刪除的編號或 SN（多筆用逗號，q 取消）", default="q")
        if raw.lower() == "q":
            return
        matched = resolve_targets_from_displayed(displayed, parse_multi_targets(raw))
        if not matched:
            _print("  [red]✗[/red] 找不到對應項目。")
            return

        _print("\n[bold yellow]即將從追番清單移除以下作品（不影響已下載影片檔）：[/bold yellow]")
        for it in matched:
            _print(f"  • 《{it.name}》(SN: {it.sn})")

        if not _confirm(f"\n確認刪除以上 {len(matched)} 部作品？", default=False):
            _print("  [dim]已取消刪除。[/dim]")
            return

        removed = self.watchlist_store.remove_by_sns(self.watchlist, {it.sn for it in matched})
        _print("  [green]✓[/green] 已刪除：" + "、".join(f"《{r.name}》" for r in removed))

    def _edit_and_manage_submenu(self) -> None:
        while True:
            self._clear_if_enabled()
            displayed = self._get_sorted_watchlist()
            _breadcrumb("主選單 › 追番清單管理 › 編輯與管理")
            self._render_watchlist_table(displayed)

            print()
            _print("  [cyan]1.[/cyan] 切換追蹤狀態 [dim](納入檢查 / 略過)[/dim]")
            _print("  [cyan]2.[/cyan] 自動同步資訊 [dim](集數 / 時間 / 總集 / 熱門度)[/dim]")
            _print("  [cyan]3.[/cyan] 手動編輯單部")
            _print("  [cyan]4.[/cyan] 刪除作品 [dim](需二次確認)[/dim]")
            _print("  [dim]q. 返回[/dim]")

            try:
                sub = _ask("\n請選擇", default="q").lower()
            except KeyboardInterrupt:
                _echo_return("追番清單管理")
                return

            if sub == "q":
                _echo_return("追番清單管理")
                break
            elif sub == "1":
                _echo_action("1. 切換追蹤狀態")
                raw = _ask("輸入編號或 SN（多筆用逗號，q 取消）", default="q")
                if raw.lower() != "q":
                    matched = resolve_targets_from_displayed(displayed, parse_multi_targets(raw))
                    toggled = self.watchlist_store.toggle_by_sns(self.watchlist, {it.sn for it in matched})
                    if toggled:
                        for t in toggled:
                            st_txt = "[green]追蹤[/green]" if t.enabled else "[dim]略過[/dim]"
                            _print(f"  [green]✓[/green] 《{t.name}》→ {st_txt}")
                    else:
                        _print("  [red]✗[/red] 找不到對應項目。")
                    _pause()
            elif sub == "2":
                _echo_action("2. 自動同步資訊")
                self._sync_existing_metadata_flow(displayed)
                _pause()
            elif sub == "3":
                _echo_action("3. 手動編輯單部")
                self._edit_single_anime_flow(displayed)
                _pause()
            elif sub == "4":
                _echo_action("4. 刪除作品")
                self._delete_anime_flow(displayed)
                _pause()
            else:
                _print("  [yellow]![/yellow] 無效選項。")
                _pause()

    def _sort_watchlist_submenu(self) -> None:
        self._clear_if_enabled()
        _breadcrumb("主選單 › 追番清單管理 › 調整排序")
        _print(f"目前排序：[bold cyan]{self._format_sort_badge()}[/bold cyan]\n")

        for idx, (key, label, _) in enumerate(WATCHLIST_SORT_OPTIONS, 1):
            mark = " [green]◀ 目前[/green]" if key == self.config.watchlist_sort_by else ""
            _print(f"  [cyan]{idx}.[/cyan] {label}{mark}")
        _print(f"  [cyan]r.[/cyan] 反轉目前方向 [dim]({'降冪 ↓ → 升冪 ↑' if self.config.watchlist_sort_desc else '升冪 ↑ → 降冪 ↓'})[/dim]")
        _print("  [dim]q. 返回[/dim]")

        sub = _ask("\n請選擇排序規則", default="q").lower()
        if sub == "q":
            _echo_return("追番清單管理")
            return
        if sub == "r":
            self.config.watchlist_sort_desc = not self.config.watchlist_sort_desc
            self.config_store.save(self.config)
            _echo_action(f"反轉排序 → {self._format_sort_badge()}")
            _print(f"  [green]✓[/green] 已儲存排序：{self._format_sort_badge()}")
            _pause()
            _echo_return("追番清單管理")
            return
        if sub.isdigit() and 1 <= int(sub) <= len(WATCHLIST_SORT_OPTIONS):
            key, label, default_desc = WATCHLIST_SORT_OPTIONS[int(sub) - 1]
            self.config.watchlist_sort_by = key
            self.config.watchlist_sort_desc = default_desc
            self.config_store.save(self.config)
            _echo_action(f"設定排序 → {self._format_sort_badge()}")
            _print(f"  [green]✓[/green] 已套用並記住排序：{self._format_sort_badge()}")
            if key == "popularity" and all(w.views is None for w in self.watchlist):
                _print("  [yellow]![/yellow] 提示：現有作品尚未同步觀看數，請進「2. 編輯與管理 → 2. 自動同步資訊」補齊。")
            _pause()
        else:
            _print("  [yellow]![/yellow] 無效選項。")
            _pause()
        _echo_return("追番清單管理")

    def _render_watchlist_table(self, displayed: list[AnimeWatchItem]) -> None:
        show_views = self.config.watchlist_sort_by == "popularity"
        if HAS_RICH and console:
            table = Table(
                title=f"追番清單（排序：{self._format_sort_badge()}）",
                show_header=True,
                header_style="bold magenta",
                expand=False,
            )
            table.add_column("#", width=3, justify="right")
            table.add_column("追蹤", width=4)
            table.add_column("作品名稱", style="cyan", max_width=24)
            table.add_column("SN", width=6)
            table.add_column("畫質", width=5)
            table.add_column("線上/總", width=7, justify="center")
            if show_views:
                table.add_column("熱門度", width=8, justify="right")
            table.add_column("狀態 / 更新時間", max_width=20)

            for idx, item in enumerate(displayed, 1):
                st = "[green]追蹤[/green]" if item.enabled else "[dim]略過[/dim]"
                sched_col = _format_status_schedule(item)
                if item.status == "已完結":
                    sched_col = f"[dim]{sched_col}[/dim]"
                elif item.status == "連載中":
                    sched_col = f"[green]{sched_col}[/green]"
                row = [
                    str(idx),
                    st,
                    item.name,
                    item.sn,
                    f"{item.resolution}P",
                    _format_ep_ratio(item.online_episodes, item.total_episodes),
                ]
                if show_views:
                    row.append(format_views(item.views))
                row.append(sched_col)
                table.add_row(*row)
            console.print(table)
        else:
            print(f"=== 追番清單（排序：{self._format_sort_badge()}）===")
            for idx, item in enumerate(displayed, 1):
                st = "追蹤" if item.enabled else "略過"
                ratio = _format_ep_ratio(item.online_episodes, item.total_episodes)
                extra = f" | 熱門={format_views(item.views)}" if show_views else ""
                print(f"  {idx}. [{st}] {item.name} | SN={item.sn} | {item.resolution}P | {ratio}{extra} | {_format_status_schedule(item)}")

    def action_manage_watchlist(self) -> None:
        while True:
            self._clear_if_enabled()
            displayed = self._get_sorted_watchlist()
            _breadcrumb("主選單 › 追番清單管理")
            self._render_watchlist_table(displayed)

            print()
            _print("  [cyan]1.[/cyan] 新增作品 [dim](搜片名 / 網址 / SN)[/dim]")
            _print("  [cyan]2.[/cyan] 編輯與管理 [dim](追蹤狀態 / 同步 / 編輯 / 刪除)[/dim]")
            _print(f"  [cyan]3.[/cyan] 調整排序 [dim](目前：{self._format_sort_badge()})[/dim]")
            _print("  [dim]q. 返回[/dim]")

            try:
                sub = _ask("\n請選擇", default="q").lower()
            except KeyboardInterrupt:
                _echo_return("主選單")
                return

            if sub == "q":
                _echo_return("主選單")
                break
            elif sub == "1":
                _echo_action("1. 新增作品")
                self._add_anime_flow()
                _pause()
            elif sub == "2":
                _echo_action("2. 編輯與管理")
                self._edit_and_manage_submenu()
            elif sub == "3":
                _echo_action("3. 調整排序")
                self._sort_watchlist_submenu()
            else:
                _print("  [yellow]![/yellow] 無效選項。")
                _pause()

    def action_audio_check_and_fix(self, auto_fix: bool = False) -> None:
        _print(f"[dim]掃描音軌：{self.env.bangumi_dir}[/dim]")
        inspections = scan_directory_audio(self.env.bangumi_dir)
        if not inspections:
            _print("  [yellow]![/yellow] 下載目錄中找不到影片檔。")
            return

        need_fix: list[Path] = []
        for ins in inspections:
            if ins.error:
                status = f"[red]錯誤：{ins.error}[/red]"
            elif not ins.has_audio:
                status = "[red]無音軌[/red]"
            elif ins.quicktime_friendly:
                status = f"[green]正常 ({ins.audio_codec}, {ins.channels}ch)[/green]"
            else:
                status = f"[yellow]可能不相容 QuickTime ({ins.audio_codec})[/yellow]"
                need_fix.append(ins.file_path)
            _print(f"  • {ins.file_path.name} → {status}")

        all_with_audio = [ins.file_path for ins in inspections if ins.has_audio]
        if not all_with_audio:
            _print("  [red]✗[/red] 沒有可修復的音軌。")
            return

        targets = need_fix
        if not targets and not auto_fix:
            if _confirm("\n全部檔案皆含 AAC 音軌；若 QuickTime 仍無聲，要無損重洗音軌標頭嗎？", default=False):
                targets = all_with_audio
        elif targets and not auto_fix:
            if not _confirm(f"\n共有 {len(targets)} 個檔案建議修復為標準 AAC MP4，立即執行？", default=True):
                return

        for file_path in targets:
            _print(f"  [dim]修復中：{file_path.name}（Ctrl+C 可中斷）...[/dim]")
            try:
                ok, msg = remux_audio_for_quicktime(file_path, replace_original=True)
            except KeyboardInterrupt:
                _print("  [yellow]![/yellow] 已中斷音軌修復。")
                return
            _print(f"  [green]✓[/green] 完成：{msg}" if ok else f"  [red]✗[/red] 失敗：{msg}")

    def action_file_repair_menu(self) -> None:
        """可擴充的檔案修復子選單（目前包含 QuickTime 音軌修復，預留後續修復模組）。"""
        while True:
            self._clear_if_enabled()
            _breadcrumb("主選單 › 檔案修復")
            _print("  [cyan]1.[/cyan] 影片音軌檢測與修復 [dim](QuickTime 無聲修復)[/dim]")
            _print("  [dim]q. 返回[/dim]")

            try:
                sub = _ask("\n請選擇", default="q").lower()
            except KeyboardInterrupt:
                _echo_return("主選單")
                return

            if sub == "q":
                _echo_return("主選單")
                break
            elif sub == "1":
                _echo_action("1. 影片音軌檢測與修復")
                self.action_audio_check_and_fix()
                _pause()
            else:
                _print("  [yellow]![/yellow] 無效選項。")
                _pause()

    def action_setup_cookie(self) -> None:
        while True:
            self._clear_if_enabled()
            _breadcrumb("主選單 › Cookie 管理")
            _print(f"檔案：[cyan]{self.env.cookie_path}[/cyan]")
            _print(f"狀態：{self._format_cookie_badge()}\n")
            _print("  [cyan]1.[/cyan] 剪貼簿寫入並驗證")
            _print("  [cyan]2.[/cyan] Playwright 半自動抓取 [dim](關視窗或 Ctrl+C 中止)[/dim]")
            _print("  [cyan]3.[/cyan] 開啟編輯器手動修改")
            _print("  [cyan]4.[/cyan] 重新讀取並線上驗證")
            _print("  [dim]q. 返回[/dim]")

            try:
                sub = _ask("\n請選擇", default="q").lower()
            except KeyboardInterrupt:
                _echo_return("主選單")
                return

            if sub == "q":
                _echo_return("主選單")
                break
            elif sub == "1":
                _echo_action("1. 剪貼簿寫入並驗證")
                ok, content = read_clipboard(self.env.raw_os)
                if not ok:
                    _print(f"  [red]✗[/red] {content}")
                else:
                    check = validate_and_sanitize_cookie(content)
                    if not check.valid:
                        _print(f"  [red]✗[/red] 已攔截（保留原檔）：{check.reason}")
                    else:
                        write_cookie_file(self.env.cookie_path, check.cleaned_cookie)
                        self.refresh_env()
                        _print("  [green]✓[/green] 已寫入 cookie.txt 並刷新：")
                        self._print_cookie_result(check, run_online_check=True)
                _pause()
            elif sub == "2":
                _echo_action("2. Playwright 半自動抓取")
                _print("  [dim]正在開啟瀏覽器登入頁（關視窗或按 Ctrl+C 立刻中止）...[/dim]")
                ok, msg, check = capture_cookie_via_playwright(self.env.cookie_path)
                flush_stdin()
                if not ok:
                    _print(f"  [yellow]![/yellow] {msg}")
                else:
                    self.refresh_env()
                    _print(f"  [green]✓[/green] {msg}")
                    if check:
                        self._print_cookie_result(check, run_online_check=True)
                _pause()
            elif sub == "3":
                _echo_action("3. 開啟編輯器手動修改")
                if open_cookie_in_editor(self.env.cookie_path, self.env.raw_os):
                    _print(f"  [green]✓[/green] 已開啟：{self.env.cookie_path}（存檔後選 4 驗證）")
                else:
                    _print(f"  [yellow]![/yellow] 請手動編輯：{self.env.cookie_path}")
                _pause()
            elif sub == "4":
                _echo_action("4. 重新讀取並線上驗證")
                self.refresh_env()
                check = inspect_cookie_file(self.env.cookie_path)
                self._print_cookie_result(check, run_online_check=True)
                _pause()
            else:
                _print("  [yellow]![/yellow] 無效選項。")
                _pause()

    def _backup_and_restore_submenu(self) -> None:
        while True:
            self._clear_if_enabled()
            _breadcrumb("主選單 › 系統設定 › 備份與還原")
            _print("  [cyan]1.[/cyan] 匯出設定包 (.anibak) [dim](打包私密設定與追番清單)[/dim]")
            _print("  [cyan]2.[/cyan] 匯入設定包 (.anibak) [dim](支援跨系統路徑校正)[/dim]")
            _print("  [dim]q. 返回[/dim]")

            try:
                sub = _ask("\n請選擇", default="q").lower()
            except KeyboardInterrupt:
                _echo_return("系統設定")
                return

            if sub == "q":
                _echo_return("系統設定")
                break
            elif sub == "1":
                _echo_action("1. 匯出設定包")
                _print("\n[dim]核心必含：manager_config.json、watchlist.json、cookie.txt[/dim]")
                inc_upstream = _confirm("一併打包原專案 config.json（含 UA / JA3 指紋）？", default=True)
                inc_history = _confirm("一併打包 download_history.json（下載歷史）？", default=False)
                _print("\n[dim]建議設定密碼以 AES-256 加密（直接按 Enter 則不加密）[/dim]")
                pwd = _ask_password("設定加密密碼")
                if pwd:
                    pwd2 = _ask_password("再次確認密碼")
                    if pwd != pwd2:
                        _print("  [red]✗[/red] 兩次密碼不一致，已取消匯出。")
                        _pause()
                        continue

                res = export_backup(
                    env=self.env,
                    config=self.config,
                    watchlist_path=self.watchlist_store.watchlist_path,
                    include_upstream_config=inc_upstream,
                    include_history=inc_history,
                    password=pwd,
                )
                if res.success and res.output_path:
                    _print(f"\n  [bold green]✓[/bold green] 匯出成功：{res.output_path}")
                    _print(f"  [dim]包含：{', '.join(res.included_files)} ｜ {res.cipher_name} ｜ {_format_size_mb(res.size_bytes)}[/dim]")
                    if _confirm("要立即開啟 exports 資料夾嗎？", default=False):
                        _open_folder(res.output_path.parent, self.env.raw_os)
                else:
                    _print(f"  [red]✗[/red] 匯出失敗：{res.message}")
                _pause()

            elif sub == "2":
                _echo_action("2. 匯入設定包")
                raw_path = _ask("貼上或拖曳 .anibak 檔案路徑至此 (q 取消)", default="q")
                if not raw_path or raw_path.lower() == "q":
                    continue
                bak_path = clean_dragged_path(raw_path)
                info = inspect_backup_file(bak_path)
                if not info.valid:
                    _print(f"  [red]✗[/red] {info.reason}")
                    _pause()
                    continue

                pwd = ""
                if info.encrypted:
                    _print(f"  [dim]偵測到加密備份 ({info.cipher_name})[/dim]")
                    pwd = _ask_password("請輸入解密密碼")

                _print("\n  [cyan]1.[/cyan] 完整覆蓋還原 [dim](覆蓋前會自動留本機快照)[/dim]")
                _print("  [cyan]2.[/cyan] 僅合併追番清單 [dim](保留本機現有設定與金鑰)[/dim]")
                _print("  [dim]q. 取消[/dim]")
                m = _ask("選擇還原模式", default="1").lower()
                if m == "q":
                    continue
                mode = "merge_watchlist" if m == "2" else "full"

                res = import_backup(
                    file_path=bak_path,
                    env=self.env,
                    watchlist_path=self.watchlist_store.watchlist_path,
                    watchlist_store=self.watchlist_store,
                    current_watchlist=self.watchlist,
                    password=pwd,
                    mode=mode,
                )
                if res.success:
                    self.refresh_env()
                    _print(f"\n  [bold green]✓[/bold green] {res.message}")
                    _print(f"  [dim]來源：{res.source_os} ({res.exported_at}) ｜ 項目：{', '.join(res.restored_files)}[/dim]")
                    for adj in res.path_adjustments:
                        _print(f"  [yellow]![/yellow] 跨系統校正：{adj}")
                    if res.snapshot_dir:
                        _print(f"  [dim]覆蓋前舊檔快照：{res.snapshot_dir}[/dim]")
                else:
                    _print(f"  [red]✗[/red] 匯入失敗：{res.message}")
                _pause()
            else:
                _print("  [yellow]![/yellow] 無效選項。")
                _pause()

    def action_system_settings(self) -> None:
        """獨立系統設定選單：狀態直接對齊顯示在右側，並整合備份與還原。"""
        while True:
            self._clear_if_enabled()
            _breadcrumb("主選單 › 系統設定")
            dedup_val = "[cyan]僅依實體檔[/cyan]" if self.config.dedup_mode == "file_only" else "[magenta]檔案＋歷史＋DB[/magenta]"
            up_val = "[green]開啟[/green]" if self.config.upgrade_lower_resolution else "[dim]關閉[/dim]"
            boot_val = "[green]開啟[/green]" if self.config.check_on_startup else "[dim]關閉[/dim]"
            clr_val = "[green]開啟[/green]" if self.config.clear_screen_on_menu else "[dim]關閉[/dim]"

            rows = [
                ("1.", "去重模式", dedup_val),
                ("2.", "720P 自動升級", up_val),
                ("3.", "上線自動快報", boot_val),
                ("4.", "切換自動清畫面", clr_val),
                ("5.", "預設目標畫質", f"[cyan]{self.config.default_resolution}P[/cyan]"),
                ("6.", "備份與還原", "[dim]匯出 / 匯入 .anibak[/dim]"),
            ]
            if HAS_RICH and console:
                grid = Table.grid(padding=(0, 3))
                grid.add_column(width=5)
                grid.add_column(width=16)
                grid.add_column()
                for num, label, val in rows:
                    grid.add_row(f"  [cyan]{num}[/cyan]", label, val)
                console.print(grid)
            else:
                for num, label, val in rows:
                    _print(f"  {num} {label:<12}  {val}")
            _print("\n  [dim]q. 返回[/dim]")

            try:
                sub = _ask("\n請選擇", default="q").lower()
            except KeyboardInterrupt:
                _echo_return("主選單")
                return

            if sub == "q":
                _echo_return("主選單")
                break
            elif sub == "1":
                self.config.dedup_mode = "file_and_history" if self.config.dedup_mode == "file_only" else "file_only"
                self.config_store.save(self.config)
                label = "僅依實體檔" if self.config.dedup_mode == "file_only" else "檔案＋歷史＋DB"
                _echo_action(f"去重模式 → {label}")
            elif sub == "2":
                self.config.upgrade_lower_resolution = not self.config.upgrade_lower_resolution
                self.config_store.save(self.config)
                _echo_action(f"720P 自動升級 → {'開啟' if self.config.upgrade_lower_resolution else '關閉'}")
            elif sub == "3":
                self.config.check_on_startup = not self.config.check_on_startup
                self.config_store.save(self.config)
                _echo_action(f"上線自動快報 → {'開啟' if self.config.check_on_startup else '關閉'}")
            elif sub == "4":
                self.config.clear_screen_on_menu = not self.config.clear_screen_on_menu
                self.config_store.save(self.config)
                _echo_action(f"切換自動清畫面 → {'開啟' if self.config.clear_screen_on_menu else '關閉'}")
            elif sub == "5":
                _echo_action("5. 修改預設目標畫質")
                self.config.default_resolution = self._prompt_resolution_picker(self.config.default_resolution)
                self.config_store.save(self.config)
                _print(f"  [green]✓[/green] 預設畫質已設為 {self.config.default_resolution}P。")
                _pause()
            elif sub == "6":
                _echo_action("6. 備份與還原")
                self._backup_and_restore_submenu()
            else:
                _print("  [yellow]![/yellow] 無效選項。")
                _pause()

    def action_send_digest_notification(self, force_rescan: bool = False) -> None:
        self.refresh_env()
        if not self.notifier.enabled:
            _print("  [yellow]![/yellow] 尚未啟用任何推播管道，請先於 manager_config.json 設定。")
            return

        snaps = self.last_snapshots
        if force_rescan or not snaps:
            _print("  [dim]掃描最新追番進度中...[/dim]")
            try:
                snaps = self._scan_watchlist_snapshots(quiet=True)
            except KeyboardInterrupt:
                _print("  [yellow]![/yellow] 已中斷掃描。")
                return

        n_title, n_body = self._build_digest_from_snapshots(snaps)
        print()
        if HAS_RICH and console:
            console.print(Panel(n_body, title=f"推播預覽｜{n_title}", border_style="cyan", padding=(0, 2)))
        else:
            print(f"=== 推播預覽｜{n_title} ===\n{n_body}")
        print()
        self._dispatch_notifications_live(n_title, n_body)

    def run_interactive(self, skip_startup_check: bool = False) -> None:
        if self.migrated_watchlist:
            _print(f"[green]✓ 已自動將追番清單遷移至獨立檔案：{self.watchlist_store.watchlist_path.name}[/green]")
        if self.config.check_on_startup and not skip_startup_check:
            self._print_startup_briefing()

        menu_labels = {
            "1": "1. 檢查與下載",
            "2": "2. 臨時下載",
            "3": "3. 推播追番簡報",
            "4": "4. 追番清單管理",
            "5": "5. Cookie 管理",
            "6": "6. 檔案修復",
            "7": "7. 開啟下載資料夾",
            "8": "8. 系統設定",
        }

        while True:
            self._clear_if_enabled()
            self.show_banner()
            self._render_main_menu()

            try:
                choice = _ask("\n請選擇", default="q").lower()
            except KeyboardInterrupt:
                _print("\n[dim]已離開。[/dim]")
                break

            if choice in ("q", "quit", "exit"):
                _print("[dim]已離開。[/dim]")
                break

            if choice in menu_labels:
                _echo_action(menu_labels[choice])

            try:
                if choice == "1":
                    self.action_check_and_download_menu()
                elif choice == "2":
                    self.action_ad_hoc_download()
                elif choice == "3":
                    _breadcrumb("主選單 › 推播追番簡報")
                    self.action_send_digest_notification(force_rescan=True)
                    _pause()
                    _echo_return("主選單")
                elif choice == "4":
                    self.action_manage_watchlist()
                elif choice == "5":
                    self.action_setup_cookie()
                elif choice == "6":
                    self.action_file_repair_menu()
                elif choice == "7":
                    self.env.bangumi_dir.mkdir(parents=True, exist_ok=True)
                    _open_folder(self.env.bangumi_dir, self.env.raw_os)
                    _print(f"  [green]✓[/green] 已開啟：{self.env.bangumi_dir}")
                    _pause()
                elif choice == "8":
                    self.action_system_settings()
                else:
                    _print("  [yellow]![/yellow] 無效選項，請輸入 1~8 或 q。")
                    _pause()
            except KeyboardInterrupt:
                flush_stdin()
                _print("\n  [yellow]![/yellow] 已手動中斷任務，返回主選單。")
                _pause()
