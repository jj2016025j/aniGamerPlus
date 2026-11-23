"""底層 aniGamerPlus 核心版本檢測與一鍵下載更新模組（支援「跟隨開發者最新」與「鎖定當前版本」策略）。"""

from __future__ import annotations

import json
import re
import shutil
import urllib.request
from pathlib import Path
from typing import Any

from .env_detector import EnvironmentInfo

UPSTREAM_RELEASE_API = "https://api.github.com/repos/miyouzi/aniGamerPlus/releases/latest"
UA = "AniManager/2.0.1"


def detect_local_core_version(env: EnvironmentInfo) -> str:
    """嘗試讀取本機 aniGamerPlus.py 內標記的版本號。"""
    if not env.anigamer_script:
        return "未安裝"
    if env.anigamer_is_exe:
        ver_marker = env.anigamer_dir / ".anigamer_core_version"
        if ver_marker.is_file():
            return ver_marker.read_text(encoding="utf-8", errors="ignore").strip() or "已安裝 (.exe)"
        return "已安裝 (.exe)"
    try:
        text = env.anigamer_script.read_text(encoding="utf-8", errors="ignore")[:8000]
        m = re.search(r"v\d+(?:\.\d+)+", text)
        if m:
            return m.group(0)
    except Exception:
        pass
    return "原始碼版 (.py)"


def fetch_upstream_latest_release(timeout: int = 8) -> dict[str, Any]:
    """查詢 miyouzi/aniGamerPlus 官方最新 Release 資訊。"""
    req = urllib.request.Request(
        UPSTREAM_RELEASE_API,
        headers={"User-Agent": UA, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))

    tag = str(data.get("tag_name", "") or "")
    exe_url = ""
    for asset in data.get("assets", []):
        name = str(asset.get("name", "")).lower()
        if name.endswith(".exe") and "anigamerplus" in name:
            exe_url = str(asset.get("browser_download_url", ""))
            break
    return {
        "tag": tag,
        "published_at": str(data.get("published_at", ""))[:10],
        "html_url": str(data.get("html_url", "")),
        "exe_url": exe_url,
    }


def check_core_status(env: EnvironmentInfo, policy: str = "latest") -> dict[str, Any]:
    local_ver = detect_local_core_version(env)
    result: dict[str, Any] = {
        "has_core": env.anigamer_script is not None,
        "core_file": env.anigamer_script.name if env.anigamer_script else "未找到",
        "local_version": local_ver,
        "has_ffmpeg": env.has_ffmpeg,
        "policy": policy,
        "latest_tag": "",
        "update_available": False,
        "can_auto_download_exe": env.raw_os == "Windows",
        "release_url": "https://github.com/miyouzi/aniGamerPlus/releases/latest",
        "error": "",
    }
    try:
        rel = fetch_upstream_latest_release()
        result["latest_tag"] = rel["tag"]
        result["published_at"] = rel["published_at"]
        result["release_url"] = rel["html_url"] or result["release_url"]
        result["exe_url"] = rel["exe_url"]
        if rel["tag"] and local_ver != rel["tag"] and policy == "latest":
            result["update_available"] = True
    except Exception as exc:
        result["error"] = f"無法查詢 GitHub 最新版：{exc}"
    return result


def download_latest_windows_core(env: EnvironmentInfo) -> tuple[bool, str]:
    """一鍵從官方 GitHub Release 下載最新 aniGamerPlus.exe 至工作目錄（僅限 Windows）。"""
    if env.raw_os != "Windows":
        return False, "macOS / Linux 請直接使用同目錄的 aniGamerPlus.py（或在原專案資料夾 git pull 更新）。"
    try:
        rel = fetch_upstream_latest_release(timeout=10)
        exe_url = rel.get("exe_url", "")
        if not exe_url:
            return False, "官方最新 Release 中未找到 aniGamerPlus.exe 資產。"
        target_dir = env.anigamer_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = target_dir / "aniGamerPlus.exe.downloading"
        final_path = target_dir / "aniGamerPlus.exe"

        req = urllib.request.Request(exe_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as resp, tmp_path.open("wb") as out:
            shutil.copyfileobj(resp, out)

        if final_path.exists():
            backup_path = target_dir / "aniGamerPlus.exe.bak"
            shutil.copy2(final_path, backup_path)
        shutil.move(str(tmp_path), str(final_path))
        (target_dir / ".anigamer_core_version").write_text(rel["tag"], encoding="utf-8")
        return True, f"已下載並更新 aniGamerPlus.exe ({rel['tag']})"
    except Exception as exc:
        return False, f"下載核心失敗：{exc}"
