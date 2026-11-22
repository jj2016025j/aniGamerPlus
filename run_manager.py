#!/usr/bin/env python3
"""
aniGamerPlus 模組化增量下載工具入口腳本。
請將本檔、MANAGER_README.md 與 ani_manager/ 資料夾一併放入 aniGamerPlus 專案根目錄中執行。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ani_manager.cli_ui import AnimeManagerApp
from ani_manager.env_detector import configure_console_encoding


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="aniGamerPlus 模組化增量更新管理工具（自動過濾已下載、支援雙系統）"
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="非互動模式：立即檢查追番清單並只下載未下載的新集數",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="非互動模式：只檢查線上集數與本地進度差異，不下載",
    )
    parser.add_argument(
        "--sn",
        type=str,
        default=None,
        help="搭配 --update 或 --check，僅針對單一指定 SN 檢查/下載",
    )
    parser.add_argument(
        "--fix-audio",
        action="store_true",
        help="掃描 bangumi 目錄音軌狀態並修復不相容音軌",
    )
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="發送一則測試通知到手機（需先於 manager_config.json 啟用）",
    )
    parser.add_argument(
        "--root",
        type=str,
        default=None,
        help="手動指定 aniGamerPlus.py 所在目錄（預設為腳本所在目錄）",
    )
    return parser.parse_args()


def main() -> None:
    configure_console_encoding()
    args = parse_args()
    root_dir = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent

    app = AnimeManagerApp(root_dir=root_dir)

    if args.test_notify:
        app.show_banner()
        app.action_test_notification()
        return

    if args.fix_audio:
        app.show_banner()
        app.action_audio_check_and_fix(auto_fix=True)
        return

    if args.check:
        app.show_banner()
        app.action_check_or_update(download=False, single_sn=args.sn)
        return

    if args.update:
        app.show_banner()
        app.action_check_or_update(download=True, single_sn=args.sn)
        return

    app.run_interactive()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中斷執行。")
