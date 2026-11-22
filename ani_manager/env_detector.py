"""跨平台環境自動偵測模組（Windows / macOS / Linux）。"""

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EnvironmentInfo:
    raw_os: str
    os_name: str
    root_dir: Path
    anigamer_script: Path | None
    python_executable: Path
    bangumi_dir: Path
    db_path: Path
    cookie_path: Path
    has_cookie: bool
    has_ffmpeg: bool
    has_ffprobe: bool


def configure_console_encoding() -> None:
    """修正 Windows 終端機輸出日文或特殊符號時可能發生的編碼錯誤。"""
    if sys.platform == "win32":
        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            if stream and hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass


def resolve_python_executable(root_dir: Path) -> Path:
    """優先尋找專案目錄下的 .venv Python，避免跨環境缺套件。"""
    candidates = [
        root_dir / ".venv" / "Scripts" / "python.exe",  # Windows
        root_dir / ".venv" / "bin" / "python",          # macOS / Linux
        root_dir / "venv" / "Scripts" / "python.exe",
        root_dir / "venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def detect_environment(root_dir: Path | None = None, custom_bangumi_dir: str | None = None) -> EnvironmentInfo:
    """偵測作業系統、aniGamerPlus 核心檔、虛擬環境、Cookie 與 FFmpeg 狀態。"""
    resolved_root = (root_dir or Path.cwd()).resolve()
    raw_os = platform.system()
    os_map = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}
    os_name = os_map.get(raw_os, raw_os)

    script_path = resolved_root / "aniGamerPlus.py"
    anigamer_script = script_path if script_path.is_file() else None

    if custom_bangumi_dir:
        bangumi_dir = Path(custom_bangumi_dir).expanduser()
        if not bangumi_dir.is_absolute():
            bangumi_dir = (resolved_root / bangumi_dir).resolve()
    else:
        bangumi_dir = resolved_root / "bangumi"

    db_path = resolved_root / "aniGamer.db"
    cookie_path = resolved_root / "cookie.txt"
    has_cookie = cookie_path.is_file() and cookie_path.stat().st_size > 20

    return EnvironmentInfo(
        raw_os=raw_os,
        os_name=os_name,
        root_dir=resolved_root,
        anigamer_script=anigamer_script,
        python_executable=resolve_python_executable(resolved_root),
        bangumi_dir=bangumi_dir,
        db_path=db_path,
        cookie_path=cookie_path,
        has_cookie=has_cookie,
        has_ffmpeg=shutil.which("ffmpeg") is not None,
        has_ffprobe=shutil.which("ffprobe") is not None,
    )
