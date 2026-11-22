# aniGamerPlus 模組化增量下載管理工具 (v1.3)

本工具為 `miyouzi/aniGamerPlus` 的獨立模組化管理層。**不修改原本的 `aniGamerPlus.py` 與 `README.md`**，同時支援「同目錄外掛」與「獨立私人倉庫」兩種架構。

---

## 1. v1.3 重要改進

* **更清晰的終端閱讀節奏**：所有操作完成後新增明確視覺分隔線與「按 Enter 返回」停頓，不再瞬間被主選單洗掉結果；所有選單統一改用 **`q`** 離開／返回。
* **完整狀態可視化**：首頁面板與選單 `3` 均直接顯示目前「**720P 自動升級 1080P**」開關、「**去重模式**」、「**Cookie 狀態**」與「**目前預設下載作品**」。
* **全功能即時中斷 (`Ctrl+C`) 與緩衝清理 (`terminal_utils.py`)**：
  * 下載子行程、冷卻倒數、瀏覽器抓取、音軌修復皆支援 `Ctrl+C` 即時安全中斷並返回選單（不留下殭屍行程）。
  * 每次進入選單前自動清空終端輸入緩衝區，徹底杜絕等待期間誤按的字元污染下一個選項。
* **720P → 1080P 畫質升級前後雙重檢查**：
  * **前置檢查**：開抓升級佇列前先確認目前 Cookie 是否具 VIP 權限；非 VIP 直接攔截跳過，絕不把現有 720P 丟進去重抓。
  * **後置熔斷**：若無法預判而進入試抓，第一集完成後立刻檢查實際產出畫質（檔名＋`ffprobe`）；若 `aniGamerPlus.py` 仍降級為 720P，**立即熔斷中止**剩餘集數的升級任務。
* **Playwright Cookie 抓取優化**：優先呼叫本機正式 Chrome 並隱藏自動化旗標降低被擋機率；偵測到使用者關掉視窗或按 `Ctrl+C` 會立刻退出，不再乾等 180 秒。

---

## 2. 模組架構

```text
aniGamerPlus/ (或獨立私人倉庫)
├── run_manager.py           # 主入口（支援互動選單與 --update / --check / --force / --fix-audio / --test-notify）
├── MANAGER_README.md        # 本說明文件
└── ani_manager/
    ├── terminal_utils.py    # 終端緩衝區清理 (flush_stdin) 與可中斷睡眠 (interruptible_sleep)
    ├── env_detector.py      # 跨平台偵測、自動尋找 aniGamerPlus.py (支援 vendor/aniGamerPlus) 與 .venv
    ├── config_store.py      # 追番清單、去重模式、多管道通知設定讀寫
    ├── cookie_manager.py    # Cookie 防呆驗證、VIP 前置檢查、剪貼簿直寫、Playwright 抓取
    ├── episode_fetcher.py   # 動畫瘋線上集數與 SN 解析
    ├── history_tracker.py   # 雙模式去重引擎與下載後實際畫質查驗
    ├── downloader.py        # 可即時中斷的 aniGamerPlus 子行程呼叫與後置熔斷批次引擎
    ├── audio_tools.py       # ffprobe 解析度／音軌檢測與 ffmpeg 無損重洗 AAC
    ├── notifier.py          # 多管道手機推播（ntfy / Discord / Telegram 可同時啟用）
    └── cli_ui.py            # 互動選單與操作控制器
```

---

## 3. 快速啟動與套件補裝

若 `.venv` 由 `uv venv` 建立，補裝 `rich` 或 `playwright` 請使用 `uv pip install`：

```bash
uv pip install rich playwright
.venv/bin/playwright install chromium
```

啟動互動介面：
* macOS：`.venv/bin/python run_manager.py`
* Windows：`python run_manager.py`
