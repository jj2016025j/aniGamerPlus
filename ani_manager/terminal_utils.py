"""終端機輸入緩衝清理、安全停頓與可中斷等待工具模組。"""

from __future__ import annotations

import sys
import time


def flush_stdin() -> None:
    """
    清空終端機尚未被讀取的殘留按鍵，避免在下載／抓取等待期間誤按的字元
    在任務結束後直接湧入下一個選單提示。
    """
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


def interruptible_sleep(seconds: float, step: float = 0.2) -> None:
    """
    分段睡眠，確保在冷卻倒數或輪詢期間按 Ctrl+C 能即時觸發 KeyboardInterrupt。
    """
    if seconds <= 0:
        return
    end_time = time.monotonic() + seconds
    while True:
        remaining = end_time - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(step, remaining))
