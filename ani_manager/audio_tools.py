"""影片音軌檢測、解析度探測與 QuickTime 相容性修復模組（ffprobe / ffmpeg）。"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv"}


@dataclass
class AudioInspection:
    file_path: Path
    has_audio: bool
    audio_codec: str
    sample_rate: str
    channels: int
    quicktime_friendly: bool
    error: str = ""


def probe_video_height(file_path: Path) -> int:
    """使用 ffprobe 取得影片實際垂直解析度高度（例如 1080 / 720），失敗回傳 0。"""
    if not shutil.which("ffprobe") or not file_path.is_file():
        return 0
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=height",
        "-of",
        "csv=p=0",
        str(file_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
        val = (proc.stdout or "").strip().splitlines()
        if val and val[0].strip().isdigit():
            return int(val[0].strip())
    except Exception:
        pass
    return 0


def inspect_audio(file_path: Path) -> AudioInspection:
    """使用 ffprobe 檢查檔案是否含音軌與編碼是否相容 macOS QuickTime。"""
    if not shutil.which("ffprobe"):
        return AudioInspection(
            file_path=file_path,
            has_audio=False,
            audio_codec="unknown",
            sample_rate="",
            channels=0,
            quicktime_friendly=False,
            error="系統未安裝 ffprobe",
        )

    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-select_streams",
        "a",
        str(file_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams", [])
        if not streams:
            return AudioInspection(
                file_path=file_path,
                has_audio=False,
                audio_codec="none",
                sample_rate="",
                channels=0,
                quicktime_friendly=False,
            )
        first = streams[0]
        codec = str(first.get("codec_name", "unknown")).lower()
        sample_rate = str(first.get("sample_rate", ""))
        channels = int(first.get("channels", 0) or 0)
        qt_ok = codec == "aac" and file_path.suffix.lower() == ".mp4"
        return AudioInspection(
            file_path=file_path,
            has_audio=True,
            audio_codec=codec,
            sample_rate=sample_rate,
            channels=channels,
            quicktime_friendly=qt_ok,
        )
    except Exception as exc:
        return AudioInspection(
            file_path=file_path,
            has_audio=False,
            audio_codec="error",
            sample_rate="",
            channels=0,
            quicktime_friendly=False,
            error=str(exc),
        )


def remux_audio_for_quicktime(file_path: Path, replace_original: bool = True) -> tuple[bool, str]:
    """影像無損複製（-c:v copy），音軌轉標準 AAC 192k + faststart，解決 QuickTime 沒聲音問題。"""
    if not shutil.which("ffmpeg"):
        return False, "系統未安裝 ffmpeg"

    temp_output = file_path.with_name(f"{file_path.stem}.fixed_tmp.mp4")
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(file_path),
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(temp_output),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    try:
        _, stderr = proc.communicate()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        if temp_output.exists():
            temp_output.unlink(missing_ok=True)
        raise

    if proc.returncode != 0 or not temp_output.is_file():
        if temp_output.exists():
            temp_output.unlink(missing_ok=True)
        return False, (stderr or "").strip() or "ffmpeg 轉檔失敗"

    if replace_original:
        target = file_path.with_suffix(".mp4")
        file_path.unlink(missing_ok=True)
        temp_output.replace(target)
        return True, str(target)

    final_output = file_path.with_name(f"{file_path.stem}_fixed.mp4")
    temp_output.replace(final_output)
    return True, str(final_output)


def scan_directory_audio(bangumi_dir: Path) -> list[AudioInspection]:
    """批次檢查目錄內所有影片音軌狀態。"""
    if not bangumi_dir.is_dir():
        return []
    videos = sorted(p for p in bangumi_dir.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS)
    return [inspect_audio(v) for v in videos]
