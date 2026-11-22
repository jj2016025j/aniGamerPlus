"""aniGamerPlus.py 呼叫與任務執行模組。"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from .env_detector import EnvironmentInfo
from .episode_fetcher import EpisodeInfo


@dataclass
class DownloadResult:
    sn: str
    episode_label: str
    success: bool
    return_code: int
    message: str = ""


class AniGamerDownloader:
    """以獨立子行程安全呼叫原生 aniGamerPlus.py，保留原專案所有解密與下載能力。"""

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

    def download_episode(self, episode: EpisodeInfo, resolution: str | None = None) -> DownloadResult:
        """下載單一集數（-m single，會優先要求目標畫質，非 VIP 時由原程式自動降級 720P）。"""
        cmd = self.build_command(episode.sn, mode="single", resolution=resolution)
        try:
            completed = subprocess.run(cmd, cwd=str(self.env.anigamer_dir))
            ok = completed.returncode == 0
            return DownloadResult(
                sn=episode.sn,
                episode_label=episode.episode_label,
                success=ok,
                return_code=completed.returncode,
                message="完成" if ok else f"退出碼 {completed.returncode}",
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            return DownloadResult(
                sn=episode.sn,
                episode_label=episode.episode_label,
                success=False,
                return_code=-1,
                message=str(exc),
            )

    def download_latest_direct(self, base_sn: str, resolution: str | None = None) -> int:
        """直接委派 aniGamerPlus 下載最新一集（-m latest）。"""
        cmd = self.build_command(base_sn, mode="latest", resolution=resolution)
        completed = subprocess.run(cmd, cwd=str(self.env.anigamer_dir))
        return completed.returncode

    def download_batch(
        self,
        episodes: list[EpisodeInfo],
        resolution: str | None = None,
        cooldown_seconds: int = 10,
        on_start=None,
        on_complete=None,
    ) -> list[DownloadResult]:
        """依序下載篩選後的缺漏集數，並於集數之間插入冷卻間隔。"""
        results: list[DownloadResult] = []
        total = len(episodes)

        for idx, ep in enumerate(episodes, 1):
            if on_start:
                on_start(idx, total, ep)

            res = self.download_episode(ep, resolution=resolution)
            results.append(res)

            if on_complete:
                on_complete(idx, total, ep, res)

            if idx < total and cooldown_seconds > 0:
                time.sleep(cooldown_seconds)

        return results
