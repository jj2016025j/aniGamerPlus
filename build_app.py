#!/usr/bin/env python3
"""
AniManager 一鍵跨平台封裝腳本（Windows .exe / macOS .app）。
執行方式：
  python build_app.py
產出位置：
  dist/AniManager/  （將整個資料夾或執行檔放在 aniGamerPlus.exe / aniGamerPlus.py 旁即可雙擊啟動）
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def ensure_mac_icns(assets_dir: Path) -> Path | None:
    """在 macOS 上自動使用內建 sips + iconutil 將 assets/icon.png 轉為 icon.icns。"""
    icns_path = assets_dir / "icon.icns"
    if icns_path.is_file():
        return icns_path
    png_path = assets_dir / "icon.png"
    if platform.system() != "Darwin" or not png_path.is_file():
        return None

    iconset_dir = assets_dir / "AniManager.iconset"
    iconset_dir.mkdir(parents=True, exist_ok=True)
    sizes = [
        ("icon_16x16.png", 16),
        ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32),
        ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128),
        ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256),
        ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512),
        ("icon_512x512@2x.png", 1024),
    ]
    try:
        for fname, px in sizes:
            subprocess.run(
                ["sips", "-z", str(px), str(px), str(png_path), "--out", str(iconset_dir / fname)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return icns_path if icns_path.is_file() else None
    except Exception:
        return None
    finally:
        shutil.rmtree(iconset_dir, ignore_errors=True)


def main() -> None:
    root = Path(__file__).resolve().parent
    assets_dir = root / "assets"
    web_ui_dir = root / "ani_manager" / "web_ui"
    entry_script = root / "gui_app.py"

    if not entry_script.is_file():
        raise FileNotFoundError("找不到 gui_app.py，請在專案根目錄執行本腳本。")

    try:
        import PyInstaller  # type: ignore  # noqa: F401
    except ImportError:
        print("錯誤：尚未安裝 PyInstaller。請先執行：")
        print("  uv pip install pyinstaller pywebview pystray pillow")
        sys.exit(1)

    system = platform.system()
    sep = ";" if system == "Windows" else ":"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onedir",
        "--name",
        "AniManager",
        "--add-data",
        f"{web_ui_dir}{sep}ani_manager/web_ui",
        "--add-data",
        f"{assets_dir}{sep}assets",
        "--hidden-import",
        "pystray",
        "--hidden-import",
        "PIL",
        "--hidden-import",
        "webview",
    ]

    if system == "Windows":
        ico = assets_dir / "icon.ico"
        if ico.is_file():
            cmd.extend(["--icon", str(ico)])
    elif system == "Darwin":
        icns = ensure_mac_icns(assets_dir)
        if icns and icns.is_file():
            cmd.extend(["--icon", str(icns)])

    cmd.append(str(entry_script))

    print("開始執行 PyInstaller 封裝：")
    print(" ", " ".join(cmd))
    subprocess.run(cmd, cwd=str(root), check=True)

    # 隨附範本設定檔到 dist 輸出目錄，方便首次執行直接初始化
    dist_target = root / "dist"
    if system == "Windows":
        app_folder = dist_target / "AniManager"
        for sample in ("manager_config-sample.json", "watchlist-sample.json"):
            src_s = root / sample
            if src_s.is_file() and app_folder.is_dir():
                shutil.copy2(src_s, app_folder / sample)
        print(f"\n✓ Windows 封裝完成：{app_folder / 'AniManager.exe'}")
        print("  請將 AniManager 資料夾內容放在 aniGamerPlus.exe（或 aniGamerPlus.py）與 ffmpeg.exe 同目錄即可雙擊啟動。")
    elif system == "Darwin":
        for sample in ("manager_config-sample.json", "watchlist-sample.json"):
            src_s = root / sample
            if src_s.is_file() and dist_target.is_dir():
                shutil.copy2(src_s, dist_target / sample)
        print(f"\n✓ macOS 封裝完成：{dist_target / 'AniManager.app'}")
        print("  請將 AniManager.app 移至 aniGamerPlus 資料夾內（與 aniGamerPlus.py 同層）即可雙擊啟動。")


if __name__ == "__main__":
    main()
