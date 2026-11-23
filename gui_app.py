#!/usr/bin/env python3
"""
AniManager v2.0.1 桌面應用與跨裝置後端一體入口。
- 修正 macOS Cocoa/AppKit 副執行緒啟動 pystray 導致的 NSMenu 閃退問題：
  * Windows / Linux：支援背景系統匣常駐縮放。
  * macOS：關閉視窗時安全最小化至 Dock 並保留背景服務（按 Cmd+Q 或點介面「完全結束」即可徹底退出）。
- 支援 --server 純背景後端引擎模式、--minimized 與 --update 排程模式。
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
    parser = argparse.ArgumentParser(description="AniManager 桌面與跨裝置增量下載管理器 v2.0.1")
    parser.add_argument("--server", action="store_true", help="純背景後端引擎模式（不開啟桌面視窗，供手機或瀏覽器連線）")
    parser.add_argument("--minimized", action="store_true", help="啟動後縮入系統匣或 Dock 背景運作")
    parser.add_argument("--update", action="store_true", help="無互動增量下載模式（供系統排程呼叫）")
    parser.add_argument("--check", action="store_true", help="無互動檢查模式")
    parser.add_argument("--port", type=int, default=None, help="覆寫 Web 服務連接埠（預設 8765）")
    parser.add_argument("--root", type=str, default=None, help="手動指定工作根目錄")
    return parser.parse_args()


def _create_tray_icon(backend: AniManagerBackend, on_show_window, on_quit):
    """建立 Windows / Linux 系統匣常駐圖示（macOS 禁止在副執行緒建立 AppKit NSMenu，直接略過防閃退）。"""
    if sys.platform == "darwin":
        return None
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
        pystray.MenuItem("完全結束程式", lambda icon, item: on_quit()),
    )
    return pystray.Icon("AniManager", image, "AniManager 動畫瘋管理器", menu)


def main() -> None:
    configure_console_encoding()
    args = parse_args()
    root_dir = Path(args.root).resolve() if args.root else None

    if args.update or args.check:
        from ani_manager.cli_ui import AnimeManagerApp

        cli_app = AnimeManagerApp(root_dir=root_dir)
        cli_app.action_check_or_update(download=args.update, interactive=False)
        return

    backend = AniManagerBackend(root_dir=root_dir)
    if args.port:
        backend.config.web_port = args.port

    server = AniManagerWebServer(backend)
    _, actual_port = server.start()
    local_url = f"http://127.0.0.1:{actual_port}"
    backend.log("ok", f"AniManager v2.0.1 引擎已啟動：{local_url}")

    if backend.config.check_on_startup:
        backend.start_check_or_download_task(download=False, remote_origin=False)

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

    try:
        import webview  # type: ignore
    except ImportError:
        print(f"未偵測到 pywebview，已自動以系統預設瀏覽器開啟：{local_url}")
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

    backend.on_quit_callback = quit_app

    # 僅在 Windows / Linux 啟用背景執行緒 pystray（macOS 改用原生 Dock 最小化保留背景，絕不閃退）
    use_tray = (sys.platform != "darwin") and (
        backend.config.close_to_tray or backend.config.background_mode == "tray" or args.minimized
    )
    if use_tray:
        tray = _create_tray_icon(backend, show_window, quit_app)
        if tray is not None:
            tray_holder["tray"] = tray
            threading.Thread(target=tray.run, daemon=True, name="AniManagerTray").start()

    start_hidden = bool(args.minimized or backend.config.start_minimized) and (tray_holder["tray"] is not None)
    win = webview.create_window(
        title="AniManager — 動畫瘋增量下載管理器",
        url=local_url,
        width=1240,
        height=820,
        min_size=(400, 660),
        hidden=start_hidden,
    )
    window_holder["win"] = win

    def on_closing() -> bool:
        if window_holder["quitting"]:
            return True
        if backend.config.close_to_tray:
            if sys.platform == "darwin":
                try:
                    win.minimize()
                    backend.log("info", "已縮小至 Dock 保留背景引擎（手機仍可連線；完全關閉請按 Cmd+Q 或左側「完全結束」）。")
                    return False
                except Exception:
                    pass
            elif tray_holder["tray"] is not None:
                try:
                    win.hide()
                    backend.log("info", "視窗已縮小至系統匣，手機仍可透過區網或 Tailscale 連線。")
                    return False
                except Exception:
                    pass
        server.stop()
        return True

    win.events.closing += on_closing
    icon_path = backend.env.bundle_dir / "assets" / "icon.png"
    # macOS App bundle 已由 .icns 控制圖示，且 Cocoa webview.start 不需傳 icon 參數
    if sys.platform == "darwin":
        webview.start()
    else:
        webview.start(icon=str(icon_path) if icon_path.is_file() else None)
    server.stop()


if __name__ == "__main__":
    main()
