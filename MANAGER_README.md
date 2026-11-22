# aniGamerPlus 模組化增量下載管理工具 (v1.3.1)

本工具為 `miyouzi/aniGamerPlus` 的獨立模組化管理層。**不修改原本的 `aniGamerPlus.py` 與 `README.md`**，同時支援「同目錄外掛」與「獨立私人倉庫」兩種架構。

---

## 1. 版本改進重點

* **四管道手機推播 (`v1.3.1`)**：新增 **LINE Messaging API (Push Message)** 推播支援，與 `ntfy`、`Discord`、`Telegram` 各自獨立開關，可任意同時啟用多個管道；內建 `user_id` 格式防呆與 401/400/429 錯誤中文診斷。
* **更清晰的終端閱讀節奏 (`v1.3.0`)**：所有操作完成後新增明確視覺分隔線與「按 Enter 返回」停頓；所有選單統一改用 **`q`** 離開／返回。
* **全功能即時中斷 (`Ctrl+C`) 與緩衝清理 (`terminal_utils.py`)**：下載、冷卻、瀏覽器抓取、音軌修復皆可 `Ctrl+C` 秒退，且每次進入選單自動清空殘留鍵盤緩衝。
* **720P → 1080P 畫質升級前後雙重檢查**：開抓前檢查 VIP 資格，抓完第一集以檔名＋`ffprobe` 雙核對實際解析度，若仍降級 720P 立即熔斷中止剩餘升級任務。
* **Playwright Cookie 抓取優化**：優先呼叫本機正式 Chrome 降低被擋機率，關視窗或按 `Ctrl+C` 立刻中止。

---

## 2. 模組架構

```text
aniGamerPlus/ (或獨立私人倉庫)
├── run_manager.py           # 主入口（互動選單與 --update / --check / --force / --fix-audio / --test-notify）
├── MANAGER_README.md        # 本說明文件
└── ani_manager/
    ├── terminal_utils.py    # 終端緩衝區清理 (flush_stdin) 與可中斷睡眠 (interruptible_sleep)
    ├── env_detector.py      # 跨平台偵測、自動尋找 aniGamerPlus.py (支援 vendor/aniGamerPlus) 與 .venv
    ├── config_store.py      # 追番清單、去重模式、多管道通知設定讀寫（自動平滑升級設定檔欄位）
    ├── cookie_manager.py    # Cookie 防呆驗證、VIP 前置檢查、剪貼簿直寫、Playwright 抓取
    ├── episode_fetcher.py   # 動畫瘋線上集數與 SN 解析
    ├── history_tracker.py   # 雙模式去重引擎與下載後實際畫質查驗
    ├── downloader.py        # 可即時中斷的 aniGamerPlus 子行程呼叫與後置熔斷批次引擎
    ├── audio_tools.py       # ffprobe 解析度／音軌檢測與 ffmpeg 無損重洗 AAC
    ├── notifier.py          # 四管道手機推播（ntfy / Discord / Telegram / LINE）
    └── cli_ui.py            # 互動選單與操作控制器
```

---

## 3. 四管道手機推播設定（`manager_config.json`）

四個管道皆可同時設為 `true`。升級到 `v1.3.1` 後只要啟動一次程式，就會自動把 `"line"` 區塊補進現有 `manager_config.json`（保留既有設定）：

```json
"notifications": {
  "ntfy": {
    "enabled": false,
    "endpoint": "https://ntfy.sh/你的隨機頻道名"
  },
  "discord": {
    "enabled": false,
    "webhook_url": "https://discord.com/api/webhooks/你的Webhook網址"
  },
  "telegram": {
    "enabled": false,
    "token": "7123456789:AAH...",
    "chat_id": "5123456789"
  },
  "line": {
    "enabled": true,
    "channel_access_token": "貼上 Channel access token (long-lived)",
    "user_id": "U開頭的33碼 Your user ID"
  }
}
```

### LINE Messaging API 設定重點（每月免費 200 則、免架伺服器）
1. 至 [LINE Official Account Manager](https://manager.line.biz/) 建立免費官方帳號 → **設定 → Messaging API → 啟用 Messaging API**。
2. 至 [LINE Developers Console](https://developers.line.biz/console/) 進入該 Messaging API Channel：
   * **Basic settings 分頁最下方** → 複製 **`Your user ID`**（`U` 開頭，填入 `user_id`）。
   * **Messaging API 分頁最下方** → 在 `Channel access token (long-lived)` 點 **Issue** 並複製整串 Token（填入 `channel_access_token`）。
   * 同頁掃描 **QR Code** 用手機 LINE 將這個官方帳號**加為好友**。
3. 執行主選單 **`8`**（或 `.venv/bin/python run_manager.py --test-notify`）測試推播。

---

## 4. 快速啟動與套件補裝

若 `.venv` 由 `uv venv` 建立，補裝選配套件請使用 `uv pip install`：

```bash
uv pip install rich playwright
.venv/bin/playwright install chromium
```

啟動互動介面：
* macOS：`.venv/bin/python run_manager.py`
* Windows：`python run_manager.py`
