"""系統原生背景排程管理模組（macOS launchd / Windows 工作排程器 / Linux crontab）。"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

from .env_detector import EnvironmentInfo

MAC_PLIST_LABEL = "com.animanager.autoupdate"
WIN_TASK_PREFIX = "AniManagerAutoUpdate"


def _build_update_command(env: EnvironmentInfo) -> list[str]:
    """產生背景排程要執行的無互動增量下載指令。"""
    if env.is_frozen:
        return [str(Path(sys.executable).resolve()), "--update"]
    run_script = env.root_dir / "run_manager.py"
    return [str(env.python_executable), str(run_script), "--update"]


def _mac_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_PLIST_LABEL}.plist"


def get_os_schedule_status(env: EnvironmentInfo) -> dict:
    """查詢目前作業系統是否已註冊自動更新排程。"""
    if env.raw_os == "Darwin":
        plist_file = _mac_plist_path()
        installed = plist_file.is_file()
        times: list[str] = []
        if installed:
            try:
                with plist_file.open("rb") as f:
                    data = plistlib.load(f)
                intervals = data.get("StartCalendarInterval", [])
                if isinstance(intervals, dict):
                    intervals = [intervals]
                for iv in intervals:
                    h = int(iv.get("Hour", 0))
                    m = int(iv.get("Minute", 0))
                    times.append(f"{h:02d}:{m:02d}")
            except Exception:
                pass
        return {
            "supported": True,
            "platform": "macOS launchd",
            "installed": installed,
            "times": times,
            "detail": f"已註冊 launchd 排程 ({', '.join(times)})" if installed else "尚未註冊系統排程",
        }

    if env.raw_os == "Windows":
        try:
            proc = subprocess.run(
                ["schtasks", "/Query", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
            )
            found = WIN_TASK_PREFIX in (proc.stdout or "")
            return {
                "supported": True,
                "platform": "Windows 工作排程器",
                "installed": found,
                "times": [],
                "detail": "已註冊 Windows 工作排程器任務" if found else "尚未註冊系統排程",
            }
        except Exception as exc:
            return {"supported": True, "platform": "Windows 工作排程器", "installed": False, "times": [], "detail": str(exc)}

    # Linux crontab
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        found = "ANIMANAGER_AUTOUPDATE" in (proc.stdout or "")
        return {
            "supported": True,
            "platform": "Linux crontab",
            "installed": found,
            "times": [],
            "detail": "已註冊 crontab 排程" if found else "尚未註冊系統排程",
        }
    except Exception:
        return {"supported": False, "platform": env.os_name, "installed": False, "times": [], "detail": "此環境不支援自動註冊排程"}


def install_os_schedule(env: EnvironmentInfo, times: list[str]) -> tuple[bool, str]:
    """依使用者設定的時段列表（如 ['02:00', '14:00']）註冊系統原生排程。"""
    clean_times = [t.strip() for t in times if ":" in t.strip()] or ["02:00", "14:00"]
    cmd = _build_update_command(env)

    if env.raw_os == "Darwin":
        plist_file = _mac_plist_path()
        plist_file.parent.mkdir(parents=True, exist_ok=True)
        logs_dir = env.root_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        intervals = []
        for t in clean_times:
            hh, mm = t.split(":", 1)
            intervals.append({"Hour": int(hh), "Minute": int(mm)})

        plist_payload = {
            "Label": MAC_PLIST_LABEL,
            "ProgramArguments": cmd,
            "WorkingDirectory": str(env.root_dir),
            "StartCalendarInterval": intervals,
            "StandardOutPath": str(logs_dir / "scheduler_out.log"),
            "StandardErrorPath": str(logs_dir / "scheduler_err.log"),
            "RunAtLoad": False,
        }
        # 先卸載舊任務（若存在）
        subprocess.run(["launchctl", "unload", str(plist_file)], capture_output=True, check=False)
        with plist_file.open("wb") as f:
            plistlib.dump(plist_payload, f)
        res = subprocess.run(["launchctl", "load", str(plist_file)], capture_output=True, text=True, check=False)
        if res.returncode != 0:
            return False, f"launchctl 載入失敗：{res.stderr.strip() or res.stdout.strip()}"
        return True, f"已註冊 macOS launchd 排程（每日 {', '.join(clean_times)}；電腦休眠喚醒後會自動補跑）。"

    if env.raw_os == "Windows":
        remove_os_schedule(env)
        quoted_cmd = " ".join(f'"{part}"' for part in cmd)
        created: list[str] = []
        for idx, t in enumerate(clean_times, 1):
            task_name = f"{WIN_TASK_PREFIX}_{idx}"
            res = subprocess.run(
                [
                    "schtasks",
                    "/Create",
                    "/F",
                    "/SC",
                    "DAILY",
                    "/TN",
                    task_name,
                    "/TR",
                    quoted_cmd,
                    "/ST",
                    t,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if res.returncode != 0:
                return False, f"Windows 工作排程建立失敗：{res.stderr.strip() or res.stdout.strip()}"
            created.append(t)
        return True, f"已註冊 Windows 工作排程器（每日 {', '.join(created)}）。"

    if env.raw_os == "Linux":
        remove_os_schedule(env)
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        existing = proc.stdout if proc.returncode == 0 else ""
        lines = [ln for ln in existing.splitlines() if "ANIMANAGER_AUTOUPDATE" not in ln]
        joined_cmd = " ".join(f'"{c}"' for c in cmd)
        for t in clean_times:
            hh, mm = t.split(":", 1)
            lines.append(f"{int(mm)} {int(hh)} * * * cd \"{env.root_dir}\" && {joined_cmd} # ANIMANAGER_AUTOUPDATE")
        new_cron = "\n".join(lines).strip() + "\n"
        ins = subprocess.run(["crontab", "-"], input=new_cron, text=True, capture_output=True, check=False)
        if ins.returncode != 0:
            return False, f"crontab 寫入失敗：{ins.stderr.strip()}"
        return True, f"已註冊 Linux crontab 排程（每日 {', '.join(clean_times)}）。"

    return False, f"不支援的作業系統：{env.os_name}"


def remove_os_schedule(env: EnvironmentInfo) -> tuple[bool, str]:
    """移除系統原生排程。"""
    if env.raw_os == "Darwin":
        plist_file = _mac_plist_path()
        if plist_file.is_file():
            subprocess.run(["launchctl", "unload", str(plist_file)], capture_output=True, check=False)
            plist_file.unlink(missing_ok=True)
        return True, "已移除 macOS launchd 自動更新排程。"

    if env.raw_os == "Windows":
        for idx in range(1, 10):
            subprocess.run(
                ["schtasks", "/Delete", "/F", "/TN", f"{WIN_TASK_PREFIX}_{idx}"],
                capture_output=True,
                check=False,
            )
        return True, "已移除 Windows 工作排程器任務。"

    if env.raw_os == "Linux":
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            lines = [ln for ln in proc.stdout.splitlines() if "ANIMANAGER_AUTOUPDATE" not in ln]
            new_cron = ("\n".join(lines).strip() + "\n") if lines else ""
            subprocess.run(["crontab", "-"], input=new_cron, text=True, check=False)
        return True, "已移除 Linux crontab 排程。"

    return False, f"不支援的作業系統：{env.os_name}"
