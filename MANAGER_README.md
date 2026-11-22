# aniGamerPlus 模組化增量下載管理工具 (v1.2)

本工具為 `miyouzi/aniGamerPlus` 的獨立模組化管理層。**不修改原本的 `aniGamerPlus.py` 與 `README.md`**，且同時支援「放進原目錄」與「獨立建立私人 GitHub 倉庫」兩種架構。

---

## 1. 兩種部署架構（推薦方案 B，不用公開 Fork）

### 方案 A：放進原本的 `aniGamerPlus/` 資料夾
將本壓縮包直接解壓縮到 `aniGamerPlus/` 根目錄即可執行。

### 方案 B：獨立私人倉庫（免 Fork、免公開、零衝突）
在自己的 GitHub 建一個全新的 **Private（私人）倉庫**，目錄結構如下：
```text
my-anime-manager/              # 你的私人倉庫
├── run_manager.py
├── MANAGER_README.md
├── .gitignore                 # 忽略 vendor/、cookie.txt、*.json、.venv/
├── ani_manager/               # 本工具所有模組
└── vendor/
    └── aniGamerPlus/          # git clone https://github.com/miyouzi/aniGamerPlus.git
```
`env_detector.py` 會自動偵測 `vendor/aniGamerPlus/aniGamerPlus.py`（也可在 `manager_config.json` 的 `anigamer_dir` 指定電腦上任何路徑）。原作者更新時只需進 `vendor/aniGamerPlus` 跑 `git pull`。

---

## 2. 安裝選配套件（解決 macOS `pip` 被擋與 `rich` 黃線問題）

若你的 `.venv` 是透過 `uv venv` 建立的，環境內預設不含 `pip` 指令，直接打 `pip install` 會誤觸發 Homebrew 系統 Python 的 PEP 668 保護。請改用 **`uv pip install`**：

```bash
# 安裝美化介面套件 rich 與半自動抓 Cookie 用的 playwright
uv pip install rich playwright

# 下載 Playwright 專用獨立 Chromium（只在需要使用「半自動抓 Cookie」時需要）
.venv/bin/playwright install chromium
```
*(Windows 請用 `.venv\Scripts\playwright install chromium`。安裝完 `rich` 後，若 VS Code 仍有黃線，按 `Cmd+Shift+P` 選 `Python: Select Interpreter` 指向 `.venv/bin/python` 即可。)*

---

## 3. 核心功能與模組對照

| 模組 | 功能說明 |
|---|---|
| `env_detector.py` | 跨平台辨識 Windows / macOS / Linux、自動尋找 `aniGamerPlus.py` 與 `.venv` |
| `config_store.py` | 讀寫 `manager_config.json`，支援貼上動畫瘋網址自動抽出 SN、各番獨立開關 |
| `cookie_manager.py` | 防呆攔截非 Cookie 雜訊、**允許訪客與非 VIP 登入帳號**、剪貼簿直寫、Playwright 抓取 |
| `episode_fetcher.py` | 解析動畫瘋頁面自動抓取作品標題與最新上架集數清單 |
| `history_tracker.py` | 支援兩種去重模式：`file_only`（刪影片即重抓）與 `file_and_history`（歸檔防重抓） |
| `downloader.py` | 呼叫原生 `aniGamerPlus.py -m single`，維持優先 1080P（非 VIP 自動降級 720P） |
| `audio_tools.py` | `ffprobe` 檢測音軌與 `ffmpeg` 無損重洗 AAC（修 QuickTime 無聲） |
| `notifier.py` | 多管道手機推播（`ntfy` / `Discord` / `Telegram` 可任意同時啟用） |
| `cli_ui.py` | 首頁面板直接顯示目前預設下載作品、畫質策略、Cookie 狀態與去重模式 |

---

## 4. 刪除影片重測怎麼運作？

* **預設 `dedup_mode: "file_only"`**：只要把 `bangumi/` 裡的某幾集影片檔刪掉，再次檢查更新就會自動偵測缺漏並重新下載，**不需要手動清歷史紀錄**。
* 若未來把看完的影片搬去外接硬碟歸檔、不想被重抓，進選單 **3 → 4** 切換成 `file_and_history` 即可。
* 若連影片都不想刪、只想強制重抓單一 SN 測試 Cookie，可選主選單 **`4` → `2`**，或直接執行：
  ```bash
  .venv/bin/python run_manager.py --sn 51813 --force
  ```

---

## 5. 多管道手機推播設定（可同時啟用多個）

打開 `manager_config.json`，在 `notifications` 區塊把想用的管道 `enabled` 改成 `true`（可三選一、二選二或三個全開）：

```json
"notifications": {
  "ntfy": {
    "enabled": true,
    "endpoint": "https://ntfy.sh/你的隨機頻道名"
  },
  "discord": {
    "enabled": true,
    "webhook_url": "https://discord.com/api/webhooks/你的Webhook網址"
  },
  "telegram": {
    "enabled": false,
    "token": "你的BotToken",
    "chat_id": "你的ChatID"
  }
}
```
存檔後進主選單按 **`8`**（或跑 `--test-notify`）即可一次測試所有已啟用的管道。
