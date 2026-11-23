"""aniGamerPlus.py 獨立行程群組呼叫、乾淨中斷、殘檔清理與後置熔斷模組。"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .env_detector import EnvironmentInfo
from .episode_fetcher import EpisodeInfo
from .terminal_utils import interruptible_sleep


@dataclass
class DownloadResult:
    sn: str
    episode_label: str
    success: bool
    return_code: int
    interrupted: bool = False
    cleaned_temp_count: int = 0
    cleaned_temp_bytes: int = 0
    message: str = ""


def _get_path_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    if path.is_dir():
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    return total


def cleanup_interrupted_temp(anigamer_dir: Path, sn: str, started_at: float) -> tuple[int, int]:
    """
    中斷後自動清理 aniGamerPlus 的 temp/ 殘留切片與暫存檔。
    僅移除檔名包含該集 SN 或於本次下載開始後新建立／修改的項目，避免誤刪其他無關檔案。
    回傳 (已清理項目數, 釋放位元組數)。
    """
    temp_dir = anigamer_dir / "temp"
    if not temp_dir.is_dir():
        return 0, 0

    cleaned_count = 0
    cleaned_bytes = 0
    sn_str = str(sn).strip()
    cutoff = started_at - 2.0

    for entry in list(temp_dir.iterdir()):
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue

        matches_sn = bool(sn_str) and (sn_str in entry.name)
        is_recent = mtime >= cutoff
        if not (matches_sn or is_recent):
            continue

        size = _get_path_size(entry)
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=False)
            else:
                entry.unlink(missing_ok=True)
            cleaned_count += 1
            cleaned_bytes += size
        except Exception:
            pass

    return cleaned_count, cleaned_bytes


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """乾淨終止整個獨立行程群組（含底層 ffmpeg），避免 gevent Traceback 洗版與孤兒行程殘留。"""
    if proc.poll() is not None:
        return

    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        return

    # POSIX (macOS / Linux): 對整個 session/process group 發送 SIGTERM -> SIGKILL
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass

    try:
        proc.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

    try:
        proc.wait(timeout=2.0)
    except Exception:
        pass


class AniGamerDownloader:
    """以獨立行程群組安全呼叫原生 aniGamerPlus.py，實現無洗版秒停與自動清理暫存殘檔。"""

    def __init__(self, env: EnvironmentInfo) -> None:
        self.env = env
        self._active_proc: subprocess.Popen | None = None
        self._abort_flag = False

    def request_abort(self) -> None:
        """供 Web / GUI 介面立即終止目前正在執行的子行程群組。"""
        self._abort_flag = True
        proc = self._active_proc
        if proc is not None:
            _terminate_process_tree(proc)

    def _ensure_script(self) -> None:
        if not self.env.anigamer_script:
            raise FileNotFoundError(
                f"找不到 aniGamerPlus.py（已檢查 {self.env.root_dir} 與 {self.env.anigamer_dir}）。"
                "請將工具放入 aniGamerPlus 資料夾，或在 manager_config.json 設定 anigamer_dir。"
            )

    def build_command(self, sn: str, mode: str = "single", resolution: str | None = None) -> list[str]:
        self._ensure_script()
        if self.env.anigamer_is_exe:
            cmd = [str(self.env.anigamer_script), "-s", str(sn), "-m", mode]
        else:
            cmd = [
                str(self.env.python_executable),
                str(self.env.anigamer_script),
                "-s",
                str(sn),
                "-m",
                mode,
            ]
        if resolution:
            clean_res = str(resolution).upper().replace("P", "")
            if clean_res.isdigit():
                cmd.extend(["-r", clean_res])
        return cmd

    def _run_isolated(self, cmd: list[str]) -> tuple[int, bool]:
        """
        在獨立行程群組內執行子行程：
        - 按 Ctrl+C 時只有本管理腳本收到 SIGINT，子行程不會觸發 gevent BlockingSwitchOutError 洗版。
        - 由本腳本統一送出行程群組終止訊號，確保 aniGamerPlus 與 ffmpeg 一併乾淨退場。
        """
        popen_kwargs: dict = {"cwd": str(self.env.anigamer_dir)}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True

        self._abort_flag = False
        proc = subprocess.Popen(cmd, **popen_kwargs)
        self._active_proc = proc
        try:
            while True:
                if self._abort_flag:
                    _terminate_process_tree(proc)
                    return -2, True
                rc = proc.poll()
                if rc is not None:
                    return rc, self._abort_flag
                time.sleep(0.15)
        except KeyboardInterrupt:
            _terminate_process_tree(proc)
            return -2, True
        finally:
            self._active_proc = None

    def download_episode(self, episode: EpisodeInfo, resolution: str | None = None) -> DownloadResult:
        """下載單一集數（-m single），若中途遭手動中斷則自動清理 temp/ 殘檔。"""
        cmd = self.build_command(episode.sn, mode="single", resolution=resolution)
        started_at = time.time()
        try:
            rc, interrupted = self._run_isolated(cmd)
            if interrupted:
                c_cnt, c_bytes = cleanup_interrupted_temp(self.env.anigamer_dir, episode.sn, started_at)
                return DownloadResult(
                    sn=episode.sn,
                    episode_label=episode.episode_label,
                    success=False,
                    return_code=rc,
                    interrupted=True,
                    cleaned_temp_count=c_cnt,
                    cleaned_temp_bytes=c_bytes,
                    message="使用者手動中斷 (Ctrl+C)",
                )
            ok = rc == 0
            return DownloadResult(
                sn=episode.sn,
                episode_label=episode.episode_label,
                success=ok,
                return_code=rc,
                message="完成" if ok else f"退出碼 {rc}",
            )
        except Exception as exc:
            return DownloadResult(
                sn=episode.sn,
                episode_label=episode.episode_label,
                success=False,
                return_code=-1,
                message=str(exc),
            )

    def download_latest_direct(self, base_sn: str, resolution: str | None = None) -> tuple[int, bool]:
        """直接委派 aniGamerPlus 下載最新一集（-m latest）。"""
        cmd = self.build_command(base_sn, mode="latest", resolution=resolution)
        started_at = time.time()
        rc, interrupted = self._run_isolated(cmd)
        if interrupted:
            cleanup_interrupted_temp(self.env.anigamer_dir, base_sn, started_at)
        return rc, interrupted

    def download_batch(
        self,
        episodes: list[EpisodeInfo],
        resolution: str | None = None,
        cooldown_seconds: int = 10,
        on_start=None,
        on_complete=None,
        on_cooldown=None,
        should_abort=None,
    ) -> tuple[list[DownloadResult], bool, bool]:
        """
        依序下載集數；回傳 (results, aborted, user_interrupted)。
        - 任意時刻按 Ctrl+C（含冷卻倒數期間）皆可乾淨中止並回報 user_interrupted=True。
        - should_abort(ep, res) 回傳 True 時觸發後置熔斷（aborted=True, user_interrupted=False）。
        """
        results: list[DownloadResult] = []
        total = len(episodes)
        aborted = False
        user_interrupted = False

        for idx, ep in enumerate(episodes, 1):
            if on_start:
                on_start(idx, total, ep)

            res = self.download_episode(ep, resolution=resolution)
            results.append(res)

            if on_complete:
                on_complete(idx, total, ep, res)

            if res.interrupted:
                aborted = True
                user_interrupted = True
                break

            if should_abort and should_abort(ep, res):
                aborted = True
                break

            if idx < total and cooldown_seconds > 0:
                next_ep = episodes[idx]
                if on_cooldown:
                    on_cooldown(cooldown_seconds, next_ep)
                try:
                    interruptible_sleep(cooldown_seconds, step=0.2)
                except KeyboardInterrupt:
                    aborted = True
                    user_interrupted = True
                    break

        return results, aborted, user_interrupted
