#!/usr/bin/env python3
"""
AniManager v2.0.1 一鍵跨平台封裝腳本（Windows .exe / macOS .app）。
執行方式：
  python build_app.py                  # 一般封裝（乾淨產出 AniManager.app 或 dist/AniManager/AniManager.exe）
  python build_app.py --bundle-core    # All-in-One 完整包（Windows 自動連同最新 aniGamerPlus.exe 與本機 ffmpeg.exe 一併打包）
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import urllib.request
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
        ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
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


def _bundle_windows_core_and_ffmpeg(root: Path, app_folder: Path) -> None:
    """嘗試將最新 aniGamerPlus.exe（優先用本機已有，否則抓官方 GitHub 最新版）與 ffmpeg/ffprobe 一併收進 Windows 輸出資料夾。"""
    local_exe = root / "aniGamerPlus.exe"
    target_exe = app_folder / "aniGamerPlus.exe"
    if local_exe.is_file():
        shutil.copy2(local_exe, target_exe)
        print("  ✓ 已收入本機 aniGamerPlus.exe")
    else:
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/miyouzi/aniGamerPlus/releases/latest",
                headers={"User-Agent": "AniManager-Builder/2.0.1", "Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                rel = json.loads(resp.read().decode("utf-8", errors="replace"))
            tag = rel.get("tag_name", "")
            exe_url = next(
                (a["browser_download_url"] for a in rel.get("assets", []) if a.get("name", "").lower().endswith(".exe")),
                "",
            )
            if exe_url:
                print(f"  正在下載官方 aniGamerPlus.exe ({tag})...")
                dl_req = urllib.request.Request(exe_url, headers={"User-Agent": "AniManager-Builder/2.0.1"})
                with urllib.request.urlopen(dl_req, timeout=60) as r, target_exe.open("wb") as out:
                    shutil.copyfileobj(r, out)
                (app_folder / ".anigamer_core_version").write_text(tag, encoding="utf-8")
                print(f"  ✓ 已打包官方最新 aniGamerPlus.exe ({tag})")
        except Exception as exc:
            print(f"  ! 略過自動抓取 aniGamerPlus.exe（{exc}）")

    for bin_name in ("ffmpeg.exe", "ffprobe.exe"):
        src_bin = root / bin_name
        if not src_bin.is_file():
            found = shutil.which(bin_name.replace(".exe", ""))
            src_bin = Path(found) if found else None
        if src_bin and src_bin.is_file():
            shutil.copy2(src_bin, app_folder / bin_name)
            print(f"  ✓ 已收入 {bin_name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AniManager 跨平台封裝腳本")
    parser.add_argument("--bundle-core", action="store_true", help="一併將 aniGamerPlus 核心與 FFmpeg 收入成品包（All-in-One）")
    args = parser.parse_args()

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
        print("  uv pip install pyinstaller pywebview pillow cryptography")
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
        "--collect-submodules",
        "webview",
        "--hidden-import",
        "PIL",
        "--hidden-import",
        "webview",
    ]

    if system == "Windows":
        cmd.extend(["--hidden-import", "pystray"])
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

    dist_target = root / "dist"
    if system == "Windows":
        app_folder = dist_target / "AniManager"
        for sample in ("manager_config-sample.json", "watchlist-sample.json"):
            src_s = root / sample
            if src_s.is_file() and app_folder.is_dir():
                shutil.copy2(src_s, app_folder / sample)
        if args.bundle_core and app_folder.is_dir():
            _bundle_windows_core_and_ffmpeg(root, app_folder)
        print(f"\n✓ Windows 封裝完成：{app_folder / 'AniManager.exe'}")
    elif system == "Darwin":
        # 移除 PyInstaller 在 macOS 順帶留下的散裝資料夾 dist/AniManager，只留乾淨的 AniManager.app 避免混淆
        redundant_dir = dist_target / "AniManager"
        if redundant_dir.is_dir():
            shutil.rmtree(redundant_dir, ignore_errors=True)
        print(f"\n✓ macOS 封裝完成：{dist_target / 'AniManager.app'}")
        print("  請只把 dist/AniManager.app（單一個應用程式）移至 aniGamerPlus 根目錄雙擊啟動即可。")


if __name__ == "__main__":
    main()
