"""終端互動介面與命令列控制器 v1.4（清爽分區 UI、上線新集數快報、關鍵字搜番、嚴格欄位審核、結構化推播）。"""

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
    AnimeWatchItem,
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
from .episode_fetcher import EpisodeFetchError, EpisodeInfo, fetch_anime_episodes
from .history_tracker import EpisodeDiffResult, HistoryTracker, parse_resolution_int
from .metadata_fetcher import (
    enrich_anime_metadata,
    resolve_candidate_video_sn,
    search_anigamer,
)
from .notifier import (
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
        self.last_snapshots: list[AnimeScanSnapshot] = []

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
        """掃描指定作品的線上集數與本地進度差異，供上線簡報、檢查模式與推播摘要共用。"""
        targets = items if items is not None else [w for w in self.config.watchlist if w.enabled]
        snapshots: list[AnimeScanSnapshot] = []

        for item in targets:
            if not quiet:
                _print(f"[dim]  掃描中：{item.name} (SN: {item.sn})...[/dim]")
            try:
                title, online_eps = fetch_anime_episodes(
                    base_sn=item.sn,
                    cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                    fallback_title=item.name,
                )
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
                        online_count=0,
                        error=str(exc),
                    )
                )

        if items is None:
            self.last_snapshots = snapshots
        return snapshots

    def _build_digest_from_snapshots(self, snapshots: list[AnimeScanSnapshot]) -> tuple[str, str]:
        """將掃描快照轉成結構化手機推播簡報。"""
        new_updates: list[str] = []
        up_to_date: list[str] = []
        errors: list[str] = []

        for snap in snapshots:
            if snap.error or not snap.diff:
                errors.append(f"{snap.item.name}：{snap.error or '無法解析'}")
                continue
            total_str = f"/{snap.item.total_episodes}集" if snap.item.total_episodes else "集"
            sched_str = f"｜{snap.item.schedule_note}" if snap.item.schedule_note else ""
            if snap.diff.missing_episodes:
                ep_str = ", ".join(f"第{e.episode_label}集" for e in snap.diff.missing_episodes[:6])
                if len(snap.diff.missing_episodes) > 6:
                    ep_str += f" 等共 {len(snap.diff.missing_episodes)} 集"
                new_updates.append(
                    f"《{snap.title}》可下載 {ep_str}（線上 {snap.online_count}{total_str}{sched_str}）"
                )
            else:
                up_to_date.append(
                    f"《{snap.title}》本地已有 {snap.diff.total_local_count}/{snap.online_count}{total_str}{sched_str}"
                )

        paused = [w.name for w in self.config.watchlist if not w.enabled]
        return format_check_digest(new_updates, up_to_date, paused, errors)

    def _print_startup_briefing(self) -> None:
        """上線快報：開啟互動介面時快速掃描已啟用作品，一眼列出哪些有新集數可下載。"""
        enabled_items = [w for w in self.config.watchlist if w.enabled]
        if not enabled_items:
            return

        _print(f"[cyan]正在執行上線更新快報（共 {len(enabled_items)} 部啟用中作品，按 Ctrl+C 可略過）...[/cyan]")
        try:
            snaps = self._scan_watchlist_snapshots(enabled_items, quiet=True)
            self.last_snapshots = snaps
        except KeyboardInterrupt:
            _print("[yellow]已略過上線快報。[/yellow]")
            return

    def show_banner(self) -> None:
        """清爽分區儀表板：系統狀態條 + 追番清單摘要 + 上線快報。"""
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

        # 上線快報（若有最新快照）
        snap_by_sn = {s.item.sn: s for s in self.last_snapshots}
        enabled_items = [w for w in self.config.watchlist if w.enabled]
        paused_count = len(self.config.watchlist) - len(enabled_items)

        brief_lines: list[str] = []
        if enabled_items:
            for w in enabled_items:
                snap = snap_by_sn.get(w.sn)
                sched = f" [{w.schedule_note}]" if w.schedule_note else ""
                if snap and snap.diff:
                    if snap.diff.missing_episodes:
                        eps = ",".join(e.episode_label for e in snap.diff.missing_episodes[:5])
                        if len(snap.diff.missing_episodes) > 5:
                            eps += "..."
                        state_txt = f"[bold yellow]有 {len(snap.diff.missing_episodes)} 集新進度 (#{eps})[/bold yellow]"
                    else:
                        state_txt = f"[green]已追平 ({snap.diff.total_local_count}/{snap.online_count}集)[/green]"
                elif snap and snap.error:
                    state_txt = "[red]檢查失敗[/red]"
                else:
                    state_txt = f"[dim]目標 {w.resolution}P[/dim]"
                brief_lines.append(f"  • [bold]{w.name}[/bold]{sched} → {state_txt}")
        else:
            brief_lines.append("  [yellow]（目前沒有啟用中的動畫，請按 3 新增或啟用）[/yellow]")

        header_title = f"啟用中追番 ({len(enabled_items)} 部" + (f"｜暫停 {paused_count} 部" if paused_count else "") + ")"
        body = f"{status_line}\n\n[bold]{header_title}：[/bold]\n" + "\n".join(brief_lines)

        if HAS_RICH and console:
            console.print(Panel(body, title="aniGamerPlus 增量下載管理器 v1.4", border_style="cyan", padding=(0, 2)))
        else:
            print("=" * 72)
            _print(body)
            print("=" * 72)

    def _prompt_schedule_picker(self, auto_schedule: str = "") -> str:
        """結構化更新時間選擇器（告別自由輸入亂碼）。"""
        _print("\n[bold]設定更新時間：[/bold]")
        if auto_schedule:
            _print(f"  [green]0[/green]. 使用自動偵測結果：[bold]{auto_schedule}[/bold]（推薦）")
        _print("  [cyan]1[/cyan]. 選擇每週固定更新（選星期 + 時段）")
        _print("  [cyan]2[/cyan]. 標記為「已完結」")
        _print("  [cyan]3[/cyan]. 留空 / 不定時更新")

        default_opt = "0" if auto_schedule else "1"
        while True:
            mode = _ask("請選擇更新時間方式", default=default_opt)
            if mode == "0" and auto_schedule:
                return auto_schedule
            if mode == "2":
                return "已完結"
            if mode == "3":
                return ""
            if mode == "1":
                _print("  星期：[cyan]1[/cyan]週一  [cyan]2[/cyan]週二  [cyan]3[/cyan]週三  [cyan]4[/cyan]週四  [cyan]5[/cyan]週五  [cyan]6[/cyan]週六  [cyan]7[/cyan]週日")
                while True:
                    d = _ask("請選星期 (1~7)", default="7")
                    if d in WEEKDAY_MAP:
                        day_str = WEEKDAY_MAP[d]
                        break
                    _print("[red]請輸入 1 到 7 的數字。[/red]")

                presets = {"1": "00:00", "2": "00:30", "3": "01:00", "4": "01:30", "5": "22:00", "6": "23:00", "7": "23:30"}
                _print("  常用時段：[cyan]1[/cyan]00:00  [cyan]2[/cyan]00:30  [cyan]3[/cyan]01:00  [cyan]4[/cyan]01:30  [cyan]5[/cyan]22:00  [cyan]6[/cyan]23:00  [cyan]7[/cyan]23:30  [cyan]8[/cyan]自訂時間(HH:MM)")
                while True:
                    t_opt = _ask("請選時段 (1~8)", default="3")
                    if t_opt in presets:
                        return f"每{day_str} {presets[t_opt]}"
                    if t_opt == "8":
                        raw_t = _ask("請輸入 24 小時制時間 (例如 01:00 或 23:30)")
                        valid_t = validate_hhmm(raw_t)
                        if valid_t:
                            return f"每{day_str} {valid_t}"
                        _print("[red]時間格式錯誤，請輸入 00:00 ~ 23:59（例如 01:30）。[/red]")
                    else:
                        _print("[red]請輸入 1 到 8。[/red]")
            _print("[red]無效選項，請重新選擇。[/red]")

    def _prompt_resolution_picker(self, default_res: str) -> str:
        """嚴格畫質選擇器（僅允許白名單解析度）。"""
        while True:
            raw = _ask(f"目標畫質 ({'/'.join(ALLOWED_RESOLUTIONS)})", default=default_res)
            valid = validate_resolution(raw)
            if valid:
                return valid
            _print(f"[red]無效畫質！僅支援：{', '.join(ALLOWED_RESOLUTIONS)}[/red]")

    def _prompt_total_episodes(self, auto_total: int | None = None) -> int | None:
        """嚴格總集數輸入（支援套用 Bangumi 自動查詢結果或留空）。"""
        default_str = str(auto_total) if auto_total else ""
        hint = f"（已自動查到 {auto_total} 話，按 Enter 套用；輸入 - 清空）" if auto_total else "（不知道請直接按 Enter 留空）"
        while True:
            raw = _ask(f"預計總集數{hint}", default=default_str)
            ok, val, err = validate_total_episodes(raw)
            if ok:
                return val
            _print(f"[red]{err}[/red]")

    def action_check_or_update(
        self,
        download: bool = True,
        single_sn: str | None = None,
        force_single_episode: bool = False,
        push_check_notify: bool = False,
    ) -> None:
        """檢查所有啟用中（或指定）追番作品，修復本地集數統計顯示並強化推播報告。"""
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

        downloaded_ok: list[str] = []
        downloaded_fail: list[str] = []
        remaining_after: list[str] = []
        snapshots: list[AnimeScanSnapshot] = []
        vip_eligibility_cache: dict[int, tuple[bool, bool, str]] = {}

        for item in items:
            _print(f"\n[bold cyan]>>> {item.name}[/bold cyan] [dim](SN: {item.sn}｜目標 {item.resolution}P)[/dim]")
            if item.schedule_note:
                _print(f"  [dim]更新時間：{item.schedule_note}[/dim]")

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
                _print(f"  [red]線上解析失敗：{exc}[/red]")
                snapshots.append(AnimeScanSnapshot(item=item, title=item.name, online_count=0, error=str(exc)))
                if download and _confirm("  是否改用 aniGamerPlus 原生指令直接抓「最新一集 (-m latest)」？", default=False):
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
            snapshots.append(AnimeScanSnapshot(item=item, title=title, online_count=len(online_eps), diff=diff))

            total_str = f" / 預計全 {item.total_episodes} 話" if item.total_episodes else ""
            # 修復統計顯示：本地已有 = 已達標 + 可升級低畫質，不再誤印 0 集
            local_total = diff.total_local_count
            low_res_note = f"（其中 {len(diff.upgradable_episodes)} 集低於 {item.resolution}P）" if diff.upgradable_episodes else ""
            _print(
                f"  線上已上架：[bold]{len(online_eps)}[/bold] 集{total_str} ｜ "
                f"本地已有：[green]{local_total}[/green] 集{low_res_note} ｜ "
                f"新集數待下載：[yellow]{len(diff.missing_episodes)}[/yellow] 集"
            )

            missing_queue: list[EpisodeInfo] = list(diff.missing_episodes)
            if missing_queue:
                ep_labels = ", ".join(f"[{ep.episode_label}] (sn={ep.sn})" for ep in missing_queue)
                _print(f"  待下載新集數：{ep_labels}")

            upgrade_queue: list[EpisodeInfo] = []
            target_res_int = parse_resolution_int(item.resolution)

            if diff.upgradable_episodes:
                if target_res_int not in vip_eligibility_cache:
                    _print("[dim]  執行 720P->1080P 升級前置 VIP 檢查...[/dim]")
                    elig = check_vip_upgrade_eligibility(self.env.cookie_path, target_res_int)
                    vip_eligibility_cache[target_res_int] = (elig.allowed, elig.certain, elig.reason)

                allowed, _, reason = vip_eligibility_cache[target_res_int]
                if allowed:
                    _print(f"  [green]{reason}[/green]")
                    upgrade_queue = [ep for ep, _ in diff.upgradable_episodes]
                else:
                    _print(f"  [yellow]{reason}[/yellow]")

            if not missing_queue and not upgrade_queue:
                _print("  [green]已追平最新進度，無需下載。[/green]")
                continue

            if not download:
                continue

            # 下載新集數
            item_aborted = False
            if missing_queue:
                _print("[dim]  （下載或冷卻期間隨時按 Ctrl+C 可安全中斷）[/dim]")
                completed_sns: set[str] = set()

                def _on_start_new(idx: int, total: int, ep: EpisodeInfo) -> None:
                    _print(f"\n[bold blue][新集數 {idx}/{total}] {title} 第 {ep.episode_label} 集 (sn={ep.sn})[/bold blue]")

                def _on_complete_new(idx: int, total: int, ep: EpisodeInfo, res: DownloadResult) -> None:
                    if res.interrupted:
                        downloaded_fail.append(f"{title} 第{ep.episode_label}集 (手動中斷)")
                        _print(f"[yellow]已手動中斷：{title} 第 {ep.episode_label} 集[/yellow]")
                    elif res.success:
                        completed_sns.add(ep.sn)
                        actual_res, fpath = self.tracker.find_highest_local_resolution(ep, anime_name_hint=item.name)
                        self.tracker.mark_downloaded(ep, actual_res or item.resolution, fpath)
                        res_tag = f" {actual_res}P" if actual_res else ""
                        downloaded_ok.append(f"{title} 第{ep.episode_label}集{res_tag}")
                        _print(f"[green]完成：{title} 第 {ep.episode_label} 集{res_tag}[/green]")
                    else:
                        downloaded_fail.append(f"{title} 第{ep.episode_label}集 ({res.message})")
                        _print(f"[red]下載異常 ({res.message})：{title} 第 {ep.episode_label} 集[/red]")

                _, aborted = self.downloader.download_batch(
                    episodes=missing_queue,
                    resolution=item.resolution,
                    cooldown_seconds=self.config.cooldown_seconds,
                    on_start=_on_start_new,
                    on_complete=_on_complete_new,
                )
                leftover = [e for e in missing_queue if e.sn not in completed_sns]
                if leftover:
                    remaining_after.append(f"《{title}》剩餘 {len(leftover)} 集（#{','.join(e.episode_label for e in leftover[:5])}）")
                if aborted:
                    item_aborted = True
                    _print("[yellow]已中斷本次下載批次。[/yellow]")
                    break

            # 下載畫質升級集數（附帶後置解析度熔斷）
            if upgrade_queue and not item_aborted:
                _print(f"\n[magenta]開始畫質升級任務（目標 {target_res_int}P；未達標自動熔斷）[/magenta]")
                circuit_broken = False

                def _on_start_up(idx: int, total: int, ep: EpisodeInfo) -> None:
                    _print(f"\n[bold magenta][升級 {idx}/{total}] {title} 第 {ep.episode_label} 集 (sn={ep.sn})[/bold magenta]")

                def _on_complete_up(idx: int, total: int, ep: EpisodeInfo, res: DownloadResult) -> None:
                    if res.interrupted:
                        _print(f"[yellow]已手動中斷升級：{title} 第 {ep.episode_label} 集[/yellow]")
                    elif not res.success:
                        downloaded_fail.append(f"{title} 第{ep.episode_label}集升級失敗 ({res.message})")

                def _post_check_upgrade(ep: EpisodeInfo, res: DownloadResult) -> bool:
                    nonlocal circuit_broken
                    if not res.success:
                        return False
                    actual_res, fpath = self.tracker.find_highest_local_resolution(ep, anime_name_hint=item.name)
                    if actual_res >= target_res_int - 100:
                        self.tracker.mark_downloaded(ep, actual_res, fpath)
                        downloaded_ok.append(f"{title} 第{ep.episode_label}集 ({actual_res}P升級)")
                        _print(f"[green]後置檢查通過：{title} 第 {ep.episode_label} 集已升級 {actual_res}P！[/green]")
                        return False
                    circuit_broken = True
                    _print(
                        f"[bold red]後置檢查熔斷：[/bold red]{title} 第 {ep.episode_label} 集實際僅 {actual_res or '未知'}P，"
                        "已中止剩餘升級任務避免重複抓 720P。"
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

        if not single_sn:
            self.last_snapshots = snapshots

        # 推播邏輯：下載模式推播「結算報告」；純檢查模式可選擇推播「追番簡報」
        if download and (downloaded_ok or downloaded_fail):
            n_title, n_body = format_download_report(downloaded_ok, downloaded_fail, remaining_after)
            _print(f"\n[bold green]{n_title}[/bold green]\n{n_body}")
            if self.notifier.enabled:
                for _, ok, msg in self.notifier.send(n_title, n_body):
                    _print(f"[dim]推播：{msg}[/dim]" if ok else f"[yellow]推播異常：{msg}[/yellow]")
        elif not download and snapshots and self.notifier.enabled:
            should_push = push_check_notify or _confirm("\n是否將這份追番更新簡報推播到手機？", default=False)
            if should_push:
                n_title, n_body = self._build_digest_from_snapshots(snapshots)
                for _, ok, msg in self.notifier.send(n_title, n_body):
                    _print(f"[green]推播：{msg}[/green]" if ok else f"[yellow]推播異常：{msg}[/yellow]")

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
        """三合一新增流程：支援打片名搜尋、貼網址、貼 SN；全欄位嚴格校驗與時間選擇器。"""
        _print("\n[bold cyan]新增／修改追番作品[/bold cyan]")
        _print("[dim]可直接輸入「片名關鍵字」搜尋動畫瘋，或貼上動畫瘋網址 / 4碼以上 SN（輸入 q 取消）[/dim]")
        raw = _ask("片名關鍵字 / 網址 / SN", default="q")
        if not raw or raw.lower() == "q":
            return

        sn = extract_sn_from_input(raw)
        auto_title = ""
        canonical_sn = ""
        episodes: list[EpisodeInfo] = []

        if sn:
            # 走 SN 驗證
            _print(f"[dim]正在驗證 SN={sn} 並抓取作品資訊（標題、本季週表、總集數）...[/dim]")
            try:
                meta, episodes = enrich_anime_metadata(
                    base_sn=sn,
                    cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                )
                canonical_sn = meta.base_sn
                auto_title = meta.title
                auto_schedule = meta.schedule_note
                auto_total = meta.total_episodes
            except EpisodeFetchError as exc:
                _print(f"[bold red]SN 驗證失敗，已拒絕新增：[/bold red]{exc}")
                return
        else:
            # 若輸入短數字（如 "2"、"3"），直接判定無效避免當關鍵字亂搜
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
            _print(f"[dim]正在解析《{chosen.title}》的基準 SN、更新時間與預定總集數...[/dim]")
            try:
                resolved_title, resolved_sn, episodes = resolve_candidate_video_sn(
                    chosen,
                    cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                )
                meta, episodes = enrich_anime_metadata(
                    base_sn=resolved_sn,
                    fallback_title=resolved_title,
                    cookie_path=self.env.cookie_path if self.env.has_cookie else None,
                )
                canonical_sn = meta.base_sn
                auto_title = meta.title
                auto_schedule = meta.schedule_note
                auto_total = meta.total_episodes
            except EpisodeFetchError as exc:
                _print(f"[red]解析該作品失敗：{exc}[/red]")
                return

        _print(
            f"\n[green]已取得作品資訊：[/green][bold]{auto_title}[/bold] "
            f"(基準 SN: {canonical_sn}｜線上已有 {len(episodes)} 集"
            + (f"｜週表：{auto_schedule}" if auto_schedule else "")
            + (f"｜預計全 {auto_total} 話" if auto_total else "")
            + ")"
        )

        # 嚴格欄位填寫（皆帶合理預設，按 Enter 就能快速通過）
        while True:
            name = _ask("作品名稱（按 Enter 使用官方標題）", default=auto_title)
            if len(name) >= 2 and not name.isdigit():
                break
            _print("[red]作品名稱過短或不能為純數字。[/red]")

        res = self._prompt_resolution_picker(self.config.default_resolution)
        total_eps = self._prompt_total_episodes(auto_total)
        schedule_note = self._prompt_schedule_picker(auto_schedule)

        item = AnimeWatchItem(
            name=name,
            sn=canonical_sn,
            resolution=res,
            schedule_note=schedule_note,
            total_episodes=total_eps,
            enabled=True,
        )
        self.config, is_new = self.config_store.add_or_update_anime(self.config, item)
        action_word = "新增" if is_new else "更新"
        _print(f"\n[bold green]已{action_word}追番項目：{item.name}[/bold green] (SN: {item.sn}｜{item.resolution}P｜{item.schedule_note or '無排程'})")

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
        """用 UI 幫既有的舊追番作品自動補齊／同步更新時間、標題與預計總集數。"""
        if not self.config.watchlist:
            _print("[yellow]追番清單是空的。[/yellow]")
            return

        target_raw = _ask("要補齊哪幾部？（輸入 all 全部補齊，或輸入編號如 2,3，q 取消）", default="all").lower()
        if target_raw == "q":
            return

        if target_raw == "all":
            targets = list(self.config.watchlist)
        else:
            tokens = parse_multi_targets(target_raw)
            targets = [item for t in tokens if (item := self.config_store.find_by_target(self.config, t))]
            if not targets:
                _print("[red]找不到指定的項目。[/red]")
                return

        overwrite = _confirm("若原本已有填寫時間／總集數，是否強制覆蓋？（選 N 則只補空白欄位）", default=False)
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
                _print(f"  [red]無法連線驗證此 SN（疑似無效項目）：{exc}[/red]")
                continue

            changes: list[str] = []
            if meta.title and (overwrite or item.name.startswith("SN-") or item.name.isdigit()):
                item.name = meta.title
                changes.append(f"名稱→{meta.title}")
            if meta.schedule_note and (overwrite or not item.schedule_note):
                item.schedule_note = meta.schedule_note
                changes.append(f"時間→{meta.schedule_note}")
            if meta.total_episodes and (overwrite or not item.total_episodes):
                item.total_episodes = meta.total_episodes
                changes.append(f"總集數→{meta.total_episodes}")

            if changes:
                updated_count += 1
                _print(f"  [green]已補齊：{' ｜ '.join(changes)}[/green]")
            else:
                _print(f"  [dim]無需更新（線上目前 {meta.online_count} 集；若為已完結舊番可手動標記）[/dim]")

        if updated_count:
            self.config_store.save(self.config)
            _print(f"\n[bold green]共完成 {updated_count} 部作品的資訊補齊並存檔。[/bold green]")

    def _edit_single_anime_flow(self) -> None:
        """修改既有作品的畫質、總集數或更新時間（同樣套用選擇器與驗證）。"""
        raw = _ask("輸入要編輯的作品編號或 SN（q 取消）", default="q")
        if raw.lower() == "q":
            return
        item = self.config_store.find_by_target(self.config, raw)
        if not item:
            _print("[red]找不到該項目。[/red]")
            return

        _print(f"\n[bold]編輯：《{item.name}》 (SN: {item.sn})[/bold]")
        item.resolution = self._prompt_resolution_picker(item.resolution)
        item.total_episodes = self._prompt_total_episodes(item.total_episodes)
        item.schedule_note = self._prompt_schedule_picker(item.schedule_note)
        self.config_store.save(self.config)
        _print(f"[green]已更新《{item.name}》設定。[/green]")

    def action_manage_watchlist(self) -> None:
        while True:
            self._clear_if_enabled()
            _section_divider("追番清單與設定")
            if HAS_RICH and console:
                table = Table(show_header=True, header_style="bold magenta", expand=False)
                table.add_column("#", width=3, justify="right")
                table.add_column("狀態", width=5)
                table.add_column("作品名稱", style="cyan", max_width=28)
                table.add_column("SN", width=7)
                table.add_column("畫質", width=6)
                table.add_column("總集", width=5, justify="right")
                table.add_column("更新時間", max_width=18)
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
                    print(f"  {idx}. [{st}] {item.name} | SN={item.sn} | {item.resolution}P | 總集={item.total_episodes or '-'} | {item.schedule_note or '-'}")

            dedup_desc = "[cyan]僅依實體檔[/cyan]" if self.config.dedup_mode == "file_only" else "[magenta]檔案+歷史+DB[/magenta]"
            up_desc = "[green]開啟[/green]" if self.config.upgrade_lower_resolution else "[dim]關閉[/dim]"
            boot_desc = "[green]開啟[/green]" if self.config.check_on_startup else "[dim]關閉[/dim]"
            clear_desc = "[green]開啟[/green]" if self.config.clear_screen_on_menu else "[dim]關閉[/dim]"

            _print(
                f"\n[bold]設定狀態：[/bold]去重={dedup_desc} ｜ 720P升級1080P={up_desc} ｜ "
                f"上線快報={boot_desc} ｜ 自動清畫面={clear_desc}\n"
            )
            _print("[bold]清單操作：[/bold]  [cyan]1[/cyan] 新增(搜片名/網址/SN)   [cyan]2[/cyan] 補齊舊作資訊   [cyan]3[/cyan] 編輯單部   [cyan]4[/cyan] 啟用/暫停   [cyan]5[/cyan] 刪除(可多選)")
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
                    toggled = self.config_store.toggle_many(self.config, parse_multi_targets(raw))
                    if toggled:
                        for t in toggled:
                            _print(f"  [green]{t.name} → {'啟用' if t.enabled else '暫停'}[/green]")
                    else:
                        _print("[red]找不到對應項目。[/red]")
                    _pause()
            elif sub == "5":
                raw = _ask("輸入要刪除的編號或 SN（可多選，如 5,6，q 取消）", default="q")
                if raw.lower() != "q":
                    removed = self.config_store.remove_many(self.config, parse_multi_targets(raw))
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
        """發送實用的「真實追番現況簡報」到所有已啟用管道（取代無聊的測試訊息）。"""
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
        if self.config.check_on_startup and not skip_startup_check:
            self._print_startup_briefing()

        while True:
            self._clear_if_enabled()
            self.show_banner()

            _print("\n[bold cyan]【追番與下載】[/bold cyan]")
            _print("  [cyan]1[/cyan]. 下載啟用中追番的新集數    [cyan]2[/cyan]. 僅檢查新集數（可選推播）")
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
