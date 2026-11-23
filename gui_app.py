#!/usr/bin/env python3
"""
AniManager v2.0.0 桌面應用與跨裝置後端一體入口。
- 雙擊直接開啟原生視窗（PyWebView）；關閉時可依設定縮入系統匣背景掛機，供手機瀏覽器隨時連線。
- 支援 --server 純背景引擎模式（無視窗）、--minimized 啟動即縮入系統匣、以及 --update 排程自動更新。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

from ani_manager.env_detector import configure_console_encoding
from ani_manager.web_server import AniManagerBackend, AniManagerWebServer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AniManager 桌面與跨裝置增量下載管理器 v2.0.0")
    parser.add_argument("--server", action="store_true", help="純背景後端引擎模式（不開啟桌面視窗，供手機或瀏覽器連線）")
    parser.add_argument("--minimized", action="store_true", help="啟動後直接縮入系統匣背景運作")
    parser.add_argument("--update", action="store_true", help="無互動增量下載模式（供系統排程呼叫）")
    parser.add_argument("--check", action="store_true", help="無互動檢查模式")
    parser.add_argument("--port", type=int, default=None, help="覆寫 Web 服務連接埠（預設 8765）")
    parser.add_argument("--root", type=str, default=None, help="手動指定工作根目錄")
    return parser.parse_args()


def _create_tray_icon(backend: AniManagerBackend, on_show_window, on_quit):
    """建立跨平台系統匣常駐圖示（需安裝 pystray 與 Pillow；未安裝則自動略過）。"""
    try:
        import pystray  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:
        return None

    icon_file = backend.env.bundle_dir / "assets" / "icon.png"
    if icon_file.is_file():
        image = Image.open(icon_file).resize((64, 64))
    else:
        image = Image.new("RGB", (64, 64), (15, 23, 42))

    menu = pystray.Menu(
        pystray.MenuItem("開啟 AniManager 視窗", lambda icon, item: on_show_window(), default=True),
        pystray.MenuItem(
            "立即檢查並下載更新",
            lambda icon, item: backend.start_check_or_download_task(download=True, remote_origin=False),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("結束程式", lambda icon, item: on_quit()),
    )
    tray = pystray.Icon("AniManager", image, "AniManager 動畫瘋管理器", menu)
    return tray


def main() -> None:
    configure_console_encoding()
    args = parse_args()
    root_dir = Path(args.root).resolve() if args.root else None

    # 若由系統排程以 --update 或 --check 喚醒，直接複用穩定 CLI 核心執行並結束（0 殘留）
    if args.update or args.check:
        from ani_manager.cli_ui import AnimeManagerApp

        cli_app = AnimeManagerApp(root_dir=root_dir)
        cli_app.action_check_or_update(download=args.update, interactive=False)
        return

    backend = AniManagerBackend(root_dir=root_dir)
    if args.port:
        backend.config.web_port = args.port

    server = AniManagerWebServer(backend)
    host, actual_port = server.start()
    local_url = f"http://127.0.0.1:{actual_port}"
    backend.log("ok", f"AniManager v2.0.0 引擎已啟動：{local_url}")

    if backend.config.check_on_startup:
        backend.start_check_or_download_task(download=False, remote_origin=False)

    # 純背景後端模式 (--server)
    if args.server:
        print(f"AniManager 後端服務已啟動：{local_url}")
        print("按 Ctrl+C 停止服務。")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
        return

    # 嘗試載入 PyWebView 原生桌面視窗
    try:
        import webview  # type: ignore
    except ImportError:
        print(f"未偵測到 pywebview，已自動以系統預設瀏覽器開啟：{local_url}")
        print("（若要啟用原生獨立視窗，請執行：uv pip install pywebview）")
        webbrowser.open(local_url)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
        return

    window_holder: dict = {"win": None, "quitting": False}
    tray_holder: dict = {"tray": None}

    def show_window() -> None:
        win = window_holder.get("win")
        if win is not None:
            try:
                win.show()
                win.restore()
            except Exception:
                pass

    def quit_app() -> None:
        window_holder["quitting"] = True
        tray = tray_holder.get("tray")
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        win = window_holder.get("win")
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        server.stop()
        os._exit(0)

    use_tray = backend.config.close_to_tray or backend.config.background_mode == "tray" or args.minimized
    if use_tray:
        tray = _create_tray_icon(backend, show_window, quit_app)
        if tray is not None:
            tray_holder["tray"] = tray
            threading.Thread(target=tray.run, daemon=True, name="AniManagerTray").start()

    start_hidden = bool(args.minimized or backend.config.start_minimized) and (tray_holder["tray"] is not None)
    win = webview.create_window(
        title="AniManager — 動畫瘋增量下載管理器",
        url=local_url,
        width=1220,
        height=800,
        min_size=(400, 660),
        hidden=start_hidden,
    )
    window_holder["win"] = win

    def on_closing() -> bool:
        if window_holder["quitting"]:
            return True
        if backend.config.close_to_tray and tray_holder["tray"] is not None:
            try:
                win.hide()
                backend.log("info", "視窗已縮小至系統匣，手機仍可透過區網網址連線。")
                return False  # 阻止銷毀視窗，保留背景服務
            except Exception:
                pass
        server.stop()
        return True

    win.events.closing += on_closing
    icon_path = backend.env.bundle_dir / "assets" / "icon.png"
    webview.start(icon=str(icon_path) if icon_path.is_file() else None)
    server.stop()


if __name__ == "__main__":
    main()
