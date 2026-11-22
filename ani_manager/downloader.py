"""aniGamerPlus.py 可即時中斷呼叫、後置熔斷與任務執行模組。"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

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
    message: str = ""


class AniGamerDownloader:
    """以可安全終止的子行程呼叫原生 aniGamerPlus.py，確保 Ctrl+C 可即時中斷而不遺留殭屍行程。"""

    def __init__(self, env: EnvironmentInfo) -> None:
        self.env = env

    def _ensure_script(self) -> None:
        if not self.env.anigamer_script:
            raise FileNotFoundError(
                f"找不到 aniGamerPlus.py（已檢查 {self.env.root_dir} 與 {self.env.anigamer_dir}）。"
                "請將工具放入 aniGamerPlus 資料夾，或在 manager_config.json 設定 anigamer_dir。"
            )

    def build_command(self, sn: str, mode: str = "single", resolution: str | None = None) -> list[str]:
        self._ensure_script()
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

    def _run_interruptible(self, cmd: list[str]) -> tuple[int, bool]:
        """以 Popen 執行子行程；按 Ctrl+C 時立即終止子行程並回報 interrupted=True。"""
        proc = subprocess.Popen(cmd, cwd=str(self.env.anigamer_dir))
        try:
            rc = proc.wait()
            return rc, False
        except KeyboardInterrupt:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            return -2, True

    def download_episode(self, episode: EpisodeInfo, resolution: str | None = None) -> DownloadResult:
        """下載單一集數（-m single）。"""
        cmd = self.build_command(episode.sn, mode="single", resolution=resolution)
        try:
            rc, interrupted = self._run_interruptible(cmd)
            if interrupted:
                return DownloadResult(
                    sn=episode.sn,
                    episode_label=episode.episode_label,
                    success=False,
                    return_code=rc,
                    interrupted=True,
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
        return self._run_interruptible(cmd)

    def download_batch(
        self,
        episodes: list[EpisodeInfo],
        resolution: str | None = None,
        cooldown_seconds: int = 10,
        on_start=None,
        on_complete=None,
        should_abort=None,
    ) -> tuple[list[DownloadResult], bool]:
        """
        依序下載集數；支援：
        - 任意時刻 Ctrl+C 秒退（包含冷卻倒數期間）
        - should_abort(ep, res) 回傳 True 時立即熔斷中止剩餘任務（例如後置畫質檢查發現仍降級 720P）
        """
        results: list[DownloadResult] = []
        total = len(episodes)
        aborted = False

        for idx, ep in enumerate(episodes, 1):
            if on_start:
                on_start(idx, total, ep)

            res = self.download_episode(ep, resolution=resolution)
            results.append(res)

            if on_complete:
                on_complete(idx, total, ep, res)

            if res.interrupted:
                aborted = True
                break

            if should_abort and should_abort(ep, res):
                aborted = True
                break

            if idx < total and cooldown_seconds > 0:
                try:
                    interruptible_sleep(cooldown_seconds, step=0.2)
                except KeyboardInterrupt:
                    aborted = True
                    break

        return results, aborted
