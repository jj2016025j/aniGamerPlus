#!/usr/bin/env python3
"""
aniGamerPlus 模組化增量下載工具入口腳本 (v1.4)。
支援同目錄外掛模式與獨立私人倉庫模式（vendor/aniGamerPlus）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ani_manager.cli_ui import AnimeManagerApp
from ani_manager.config_store import extract_sn_from_input
from ani_manager.env_detector import configure_console_encoding


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="aniGamerPlus 模組化增量更新管理工具 v1.4")
    parser.add_argument("--update", action="store_true", help="非互動模式：檢查所有啟用中追番並下載新集數")
    parser.add_argument("--check", action="store_true", help="非互動模式：僅檢查線上集數與本地差異")
    parser.add_argument("--notify", action="store_true", help="搭配 --check，檢查完成後自動將追番簡報推播到手機")
    parser.add_argument("--sn", type=str, default=None, help="指定單一作品 SN 或動畫瘋網址")
    parser.add_argument("--force", action="store_true", help="搭配 --sn，忽略去重強制下載該單集")
    parser.add_argument("--fix-audio", action="store_true", help="掃描 bangumi 目錄並修復不相容音軌")
    parser.add_argument("--test-notify", action="store_true", help="掃描目前追番並推播一份真實簡報到手機")
    parser.add_argument("--no-startup-check", action="store_true", help="進入互動選單時略過上線快報掃描")
    parser.add_argument("--root", type=str, default=None, help="手動指定工作根目錄")
    return parser.parse_args()


def main() -> None:
    configure_console_encoding()
    args = parse_args()
    root_dir = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent
    sn = extract_sn_from_input(args.sn) if args.sn else None

    app = AnimeManagerApp(root_dir=root_dir)

    if args.test_notify:
        app.show_banner()
        app.action_send_digest_notification(force_rescan=True)
        return

    if args.fix_audio:
        app.show_banner()
        app.action_audio_check_and_fix(auto_fix=True)
        return

    if args.force and sn:
        app.show_banner()
        app.action_check_or_update(download=True, single_sn=sn, force_single_episode=True)
        return

    if args.check:
        app.show_banner()
        app.action_check_or_update(download=False, single_sn=sn, push_check_notify=args.notify)
        return

    if args.update:
        app.show_banner()
        app.action_check_or_update(download=True, single_sn=sn)
        return

    app.run_interactive(skip_startup_check=args.no_startup_check)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中斷執行。")
