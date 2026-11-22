"""跨平台環境自動偵測模組（Windows / macOS / Linux，支援獨立倉庫與同目錄外掛模式）。"""

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
    anigamer_dir: Path
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


def resolve_anigamer_location(root_dir: Path, custom_anigamer_dir: str = "") -> tuple[Path, Path | None]:
    """自動尋找 aniGamerPlus.py 位置（支援同目錄外掛與獨立倉庫 vendor/aniGamerPlus）。"""
    candidates: list[Path] = []
    if custom_anigamer_dir.strip():
        p = Path(custom_anigamer_dir).expanduser()
        candidates.append(p if p.is_absolute() else (root_dir / p).resolve())

    candidates.extend(
        [
            root_dir,
            root_dir / "vendor" / "aniGamerPlus",
            root_dir / "aniGamerPlus",
            root_dir.parent / "aniGamerPlus",
        ]
    )

    for folder in candidates:
        script = folder / "aniGamerPlus.py"
        if script.is_file():
            return folder.resolve(), script.resolve()

    fallback_dir = candidates[0] if custom_anigamer_dir.strip() else root_dir
    return fallback_dir.resolve(), None


def resolve_python_executable(root_dir: Path, anigamer_dir: Path) -> Path:
    """優先尋找 aniGamerPlus 目錄或管理端目錄下的 .venv Python。"""
    search_roots = [anigamer_dir, root_dir]
    rel_candidates = [
        Path(".venv/Scripts/python.exe"),
        Path(".venv/bin/python"),
        Path("venv/Scripts/python.exe"),
        Path("venv/bin/python"),
    ]
    for base in search_roots:
        for rel in rel_candidates:
            candidate = base / rel
            if candidate.is_file():
                return candidate
    return Path(sys.executable)


def detect_environment(
    root_dir: Path | None = None,
    custom_bangumi_dir: str | None = None,
    custom_anigamer_dir: str = "",
) -> EnvironmentInfo:
    resolved_root = (root_dir or Path.cwd()).resolve()
    raw_os = platform.system()
    os_map = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}
    os_name = os_map.get(raw_os, raw_os)

    anigamer_dir, anigamer_script = resolve_anigamer_location(resolved_root, custom_anigamer_dir)

    if custom_bangumi_dir:
        bangumi_dir = Path(custom_bangumi_dir).expanduser()
        if not bangumi_dir.is_absolute():
            bangumi_dir = (anigamer_dir / bangumi_dir).resolve()
    else:
        bangumi_dir = anigamer_dir / "bangumi"

    db_path = anigamer_dir / "aniGamer.db"
    cookie_path = anigamer_dir / "cookie.txt"
    has_cookie = cookie_path.is_file() and cookie_path.stat().st_size > 10

    return EnvironmentInfo(
        raw_os=raw_os,
        os_name=os_name,
        root_dir=resolved_root,
        anigamer_dir=anigamer_dir,
        anigamer_script=anigamer_script,
        python_executable=resolve_python_executable(resolved_root, anigamer_dir),
        bangumi_dir=bangumi_dir,
        db_path=db_path,
        cookie_path=cookie_path,
        has_cookie=has_cookie,
        has_ffmpeg=shutil.which("ffmpeg") is not None,
        has_ffprobe=shutil.which("ffprobe") is not None,
    )
