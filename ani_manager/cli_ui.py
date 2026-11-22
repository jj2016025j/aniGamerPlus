"""終端互動介面與命令列控制器（優先使用 rich，未安裝時自動降級為純文字介面）。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .audio_tools import remux_audio_for_quicktime, scan_directory_audio
from .config_store import AnimeWatchItem, ConfigStore, ManagerConfig
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


def _read_clipboard(raw_os: str) -> tuple[bool, str]:
    """跨平台讀取系統剪貼簿，避開終端機單行 1024 bytes 輸入上限。"""
    cmds: list[list[str]] = []
    if raw_os == "Darwin":
        cmds.append(["pbpaste"])
    elif raw_os == "Windows":
        cmds.append(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"])
    else:
        for tool, args in (
            ("wl-paste", ["wl-paste", "--no-newline"]),
            ("xclip", ["xclip", "-selection", "clipboard", "-o"]),
            ("xsel", ["xsel", "--clipboard", "--output"]),
        ):
            if shutil.which(tool):
                cmds.append(args)

    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
            if proc.returncode == 0:
                return True, proc.stdout
        except Exception:
            continue

    return False, "無法自動讀取剪貼簿（請改用編輯器手動開啟貼上）。"


def _open_text_file(path: Path, raw_os: str) -> bool:
    """用系統預設文字編輯器開啟檔案。"""
    try:
        if not path.exists():
            path.write_text("", encoding="utf-8")
        if raw_os == "Darwin":
            subprocess.run(["open", "-e", str(path)], check=False)
        elif raw_os == "Windows":
            subprocess.Popen(["notepad.exe", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
        return True
    except Exception:
        return False


def _sanitize_cookie(raw_text: str) -> str:
    """清理前綴與換行，整理成單行 Cookie 字串。"""
    lines = [line.strip() for line in raw_text.strip().splitlines() if line.strip()]
    joined = "; ".join(lines) if len(lines) > 1 and "=" in lines[0] else "".join(lines)
    if joined.lower().startswith("cookie:"):
        joined = joined.split(":", 1)[1].strip()
    return joined.strip()


class AnimeManagerApp:
    """串接各獨立模組的主控制器。"""

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = (root_dir or Path.cwd()).resolve()
        self.config_store = ConfigStore(self.root_dir / "manager_config.json")
        self.config: ManagerConfig = self.config_store.load()
        self.env: EnvironmentInfo = detect_environment(self.root_dir, self.config.bangumi_dir)
        self.tracker = HistoryTracker(
            history_path=self.root_dir / "download_history.json",
            db_path=self.env.db_path,
            bangumi_dir=self.env.bangumi_dir,
        )
        self.downloader = AniGamerDownloader(self.env)
        self.notifier = Notifier(self.config.notification)

    def refresh_env(self) -> None:
        self.config = self.config_store.load()
        self.env = detect_environment(self.root_dir, self.config.bangumi_dir)
        self.tracker = HistoryTracker(
            history_path=self.root_dir / "download_history.json",
            db_path=self.env.db_path,
            bangumi_dir=self.env.bangumi_dir,
        )
        self.downloader = AniGamerDownloader(self.env)

    def show_banner(self) -> None:
        core_ok = "[green]已偵測[/green]" if self.env.anigamer_script else "[red]找不到 aniGamerPlus.py[/red]"
        vip_ok = "[green]已設定 cookie.txt[/green]" if self.env.has_cookie else "[yellow]未設定 (免費 720P + 廣告)[/yellow]"
        ff_ok = "[green]已就緒[/green]" if self.env.has_ffmpeg else "[red]未安裝[/red]"
        body = (
            f"[bold]系統：[/bold]{self.env.os_name}   "
            f"[bold]核心：[/bold]{core_ok}   "
            f"[bold]VIP：[/bold]{vip_ok}   "
            f"[bold]FFmpeg：[/bold]{ff_ok}\n"
            f"[bold]Python：[/bold]{self.env.python_executable}\n"
            f"[bold]下載目錄：[/bold]{self.env.bangumi_dir}"
        )
        if HAS_RICH and console:
            console.print(Panel(body, title="aniGamerPlus 模組化增量管理工具", border_style="cyan"))
        else:
            print("=" * 60)
            _print(body)
            print("=" * 60)

    def action_check_or_update(self, download: bool = True, single_sn: str | None = None) -> None:
        """檢查所有（或指定）追番作品，自動過濾已下載集數，只抓新更新集數。"""
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
            _print("[yellow]追番清單目前是空的，請先新增作品。[/yellow]")
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
            )

            total_str = f" / 預定全 {item.total_episodes} 話" if item.total_episodes else ""
            _print(
                f"  線上已上架：[bold]{len(online_eps)}[/bold] 集{total_str} ｜ "
                f"已下載：[green]{len(diff.already_downloaded)}[/green] 集 ｜ "
                f"待下載新集數：[yellow]{len(diff.missing_episodes)}[/yellow] 集"
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
            self.notifier.send("動畫瘋下載完成", summary_text)

    def action_manage_watchlist(self) -> None:
        while True:
            _print("\n[bold]目前追番清單：[/bold]")
            if HAS_RICH and console:
                table = Table(show_header=True, header_style="bold magenta")
                table.add_column("#", width=4)
                table.add_column("作品名稱", style="cyan")
                table.add_column("基準 SN")
                table.add_column("畫質")
                table.add_column("集數上限")
                table.add_column("更新備註")
                for idx, item in enumerate(self.config.watchlist, 1):
                    table.add_row(
                        str(idx),
                        item.name,
                        item.sn,
                        f"{item.resolution}P",
                        str(item.total_episodes or "-"),
                        item.schedule_note or "-",
                    )
                console.print(table)
            else:
                for idx, item in enumerate(self.config.watchlist, 1):
                    print(f"  {idx}. {item.name} | SN={item.sn} | {item.resolution}P | {item.schedule_note}")

            _print("  [cyan]1[/cyan]. 新增 / 修改追番   [cyan]2[/cyan]. 刪除追番   [cyan]3[/cyan]. 切換「720P 自動升級 1080P」   [cyan]0[/cyan]. 返回主選單")
            sub = _ask("請選擇操作", default="0")

            if sub == "1":
                sn = _ask("請輸入該作品任一集的 SN（網址 ?sn= 後面的數字）")
                if not sn.isdigit():
                    _print("[red]SN 必須為純數字。[/red]")
                    continue
                name = _ask("作品名稱（留空將自動抓標題或以 SN 命名）", default=f"SN-{sn}")
                res = _ask("目標畫質 (1080 / 720 / 540)", default=self.config.default_resolution)
                total_raw = _ask("預計總集數（不知道可留空）", default="")
                note = _ask("更新時間備註（例如：每週日 01:00）", default="")
                item = AnimeWatchItem(
                    name=name,
                    sn=sn,
                    resolution=res,
                    schedule_note=note,
                    total_episodes=int(total_raw) if total_raw.isdigit() else None,
                )
                self.config = self.config_store.add_or_update_anime(self.config, item)
                _print("[green]已更新追番清單。[/green]")
            elif sub == "2":
                target = _ask("輸入要刪除的編號或 SN")
                if self.config_store.remove_anime(self.config, target):
                    _print("[green]已刪除。[/green]")
                else:
                    _print("[red]找不到該項目。[/red]")
            elif sub == "3":
                self.config.upgrade_lower_resolution = not self.config.upgrade_lower_resolution
                self.config_store.save(self.config)
                state = "開啟（若已有 720P 且設定 1080P 會重抓）" if self.config.upgrade_lower_resolution else "關閉（只要有檔就跳過）"
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
        """透過剪貼簿直接寫入或開啟編輯器更新 cookie.txt，避開終端輸入字數上限。"""
        _print("\n[bold]設定或更新巴哈姆特 VIP Cookie (cookie.txt)[/bold]")
        _print(f"檔案位置：[cyan]{self.env.cookie_path}[/cyan]")
        _print(
            "[dim]取得方式：無痕視窗登入巴哈 VIP -> 動畫瘋首頁按 F12 -> Network -> 重新整理 -> "
            "點第一個 ani.gamer.com.tw -> 複製 Request Headers 裡的 cookie 值（抓完直接關視窗，勿點登出）。[/dim]\n"
        )
        _print("  [cyan]1[/cyan]. 直接從「系統剪貼簿」讀取並寫入 cookie.txt（推薦，無字數限制且不顯示敏感內容）")
        _print("  [cyan]2[/cyan]. 用文字編輯器開啟 cookie.txt 手動貼上並存檔")
        _print("  [cyan]0[/cyan]. 返回主選單")

        sub = _ask("請選擇更新方式", default="1")
        if sub == "1":
            ok, content = _read_clipboard(self.env.raw_os)
            if not ok:
                _print(f"[red]{content}[/red]")
                return
            cleaned = _sanitize_cookie(content)
            if len(cleaned) < 20 or "=" not in cleaned:
                _print("[red]剪貼簿內容看起來不像有效的 Cookie 字串（長度過短或不含 '='），已取消寫入。[/red]")
                return
            self.env.cookie_path.write_text(cleaned + "\n", encoding="utf-8")
            self.refresh_env()
            _print(f"[green]已成功從剪貼簿寫入 cookie.txt（共 {len(cleaned)} 字元）。[/green]")
        elif sub == "2":
            if _open_text_file(self.env.cookie_path, self.env.raw_os):
                _print(f"[green]已開啟編輯器：{self.env.cookie_path}[/green]")
                _print("請貼上完整 Cookie 並存檔後，按 Enter 重新偵測狀態。")
                input()
                self.refresh_env()
                status = "[green]已偵測到有效內容[/green]" if self.env.has_cookie else "[yellow]檔案仍為空或過短[/yellow]"
                _print(f"目前狀態：{status}")
            else:
                _print(f"[yellow]無法自動開啟編輯器，請手動編輯：{self.env.cookie_path}[/yellow]")

    def run_interactive(self) -> None:
        while True:
            self.show_banner()
            _print("\n[bold]主選單：[/bold]")
            _print("  [cyan]1[/cyan]. 檢查並下載新更新集數（自動過濾已下載）")
            _print("  [cyan]2[/cyan]. 僅檢查更新狀態（不下載）")
            _print("  [cyan]3[/cyan]. 管理追番清單與畫質設定")
            _print("  [cyan]4[/cyan]. 檢查影片音軌 / 修復 QuickTime 無聲問題")
            _print("  [cyan]5[/cyan]. 設定或更新 VIP cookie.txt")
            _print("  [cyan]6[/cyan]. 開啟下載資料夾 (bangumi)")
            _print("  [cyan]0[/cyan]. 離開")

            choice = _ask("\n請選擇功能", default="1")
            if choice == "1":
                self.action_check_or_update(download=True)
            elif choice == "2":
                self.action_check_or_update(download=False)
            elif choice == "3":
                self.action_manage_watchlist()
            elif choice == "4":
                self.action_audio_check_and_fix()
            elif choice == "5":
                self.action_setup_cookie()
            elif choice == "6":
                self.env.bangumi_dir.mkdir(parents=True, exist_ok=True)
                _open_folder(self.env.bangumi_dir, self.env.raw_os)
            elif choice == "0":
                _print("[dim]已離開。[/dim]")
                break