"""終端機輸入緩衝清理、清畫面與可中斷等待工具模組。"""

from __future__ import annotations

import os
import sys
import time


def flush_stdin() -> None:
    """清空終端機尚未讀取的殘留按鍵，避免等待期間誤按的字元污染下一個選單。"""
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return
    except Exception:
        return

    if sys.platform == "win32":
        try:
            import msvcrt

            while msvcrt.kbhit():
                msvcrt.getwch()
        except Exception:
            pass
    else:
        try:
            import termios

            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except Exception:
            pass


def clear_screen() -> None:
    """跨平台清空目前終端機視窗（保留捲動歷史），讓介面維持整潔。"""
    try:
        if not sys.stdout or not sys.stdout.isatty():
            return
        os.system("cls" if sys.platform == "win32" else "clear")
    except Exception:
        pass


def interruptible_sleep(seconds: float, step: float = 0.2) -> None:
    """分段睡眠，確保冷卻倒數或輪詢期間按 Ctrl+C 能即時中斷。"""
    if seconds <= 0:
        return
    end_time = time.monotonic() + seconds
    while True:
        remaining = end_time - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(step, remaining))
