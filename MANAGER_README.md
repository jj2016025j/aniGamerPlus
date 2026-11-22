# aniGamerPlus 模組化增量下載管理工具 (v1.1)

本工具為 `miyouzi/aniGamerPlus` 的外掛式模組化管理層。**不覆蓋原本的 `aniGamerPlus.py` 與原專案的 `README.md`**，完整保留原專案核心與 Git 同步能力，並將各功能拆成獨立模組。

---

## 1. 目錄與模組架構

解壓縮後直接放入 `aniGamerPlus/` 根目錄（與 `aniGamerPlus.py` 同層）：

```text
aniGamerPlus/
├── aniGamerPlus.py          # 原生核心（不修改）
├── README.md                # 原專案文件（不修改，避免 Git 衝突）
├── run_manager.py           # 本工具入口（互動選單 / 命令列參數）
├── MANAGER_README.md        # 本工具使用說明
├── manager_config.json      # 首次執行自動生成（追番清單與設定，勿上傳 Git）
├── download_history.json    # 首次執行自動生成（已下載紀錄，勿上傳 Git）
└── ani_manager/             # 各功能獨立模組
    ├── env_detector.py      # 自動辨識 Windows / macOS / Linux、.venv、FFmpeg、Cookie
    ├── config_store.py      # 追番清單與設定讀寫
    ├── cookie_manager.py    # VIP Cookie 嚴格防呆驗證、剪貼簿直寫、線上狀態檢查、Playwright 自動抓取
    ├── episode_fetcher.py   # 解析動畫瘋頁面，抓出該作品最新上架的所有集數與 SN
    ├── history_tracker.py   # 三層去重引擎（bangumi 實體檔 + JSON + aniGamer.db）
    ├── downloader.py        # 呼叫原生 aniGamerPlus.py，僅抓缺漏集數 (-m single)
    ├── audio_tools.py       # ffprobe 音軌檢測 + ffmpeg 無損重寫 AAC（修 QuickTime 無聲）
    ├── notifier.py          # 手機推播模組（支援 ntfy / Discord / Telegram）
    └── cli_ui.py            # 終端互動選單（自動套用 rich，未裝時降級純文字）
```

---

## 2. 啟動方式（Windows / macOS）

### macOS / Linux
```bash
cd /Users/leechiensheng/開發/錄影/anime/aniGamerPlus
.venv/bin/python run_manager.py
```

### Windows (PowerShell)
```powershell
cd <你的aniGamerPlus路徑>
python run_manager.py
```

---

## 3. 常用命令列指令

| 需求 | 指令 |
|---|---|
| 進入互動選單 | `python run_manager.py`（macOS 用 `.venv/bin/python run_manager.py`） |
| **一鍵增量更新**（自動跳過已下載，只抓新集數） | `python run_manager.py --update` |
| 僅檢查是否有新集數（不下載） | `python run_manager.py --check` |
| 僅檢查並下載指定 SN 作品 | `python run_manager.py --update --sn 51813` |
| 掃描並修復影片音軌（QuickTime 無聲） | `python run_manager.py --fix-audio` |
| 發送測試推播到手機 | `python run_manager.py --test-notify` |

---

## 4. VIP Cookie 管理（選單 5）

內建 4 種操作且具備**防呆攔截**（若剪貼簿為空、複製到網址／文字／一般訪客 Cookie，或缺少 `HttpOnly` 驗證欄位 `BAHARUNE`/`BAHAENUR`，會直接拒絕寫入並保留原本的 `cookie.txt`）：

1. **剪貼簿防呆寫入並立即刷新驗證（推薦）**：從瀏覽器複製完 Cookie 後按 `1`，自動清洗（並自動移除含中文的非 ASCII `BAHANICK` 避免編碼報錯）、寫入 `cookie.txt`、刷新環境並線上驗證登入狀態。
2. **純刷新重新讀取並驗證**：手動改完 `cookie.txt` 後按 `2`，立刻重讀檔案並顯示驗證結果。
3. **Playwright 半自動腳本抓取（免開 F12）**：自動彈出獨立無痕瀏覽器前往巴哈登入頁，登入後自動擷取含 `HttpOnly` 的完整 Cookie 並直接關閉視窗（不按登出，保留 Session）。
   * 若尚未安裝 Playwright，執行一次即可啟用：
     ```bash
     pip install playwright
     playwright install chromium
     ```
4. **開啟文字編輯器手動編輯**。

---

## 5. 手機推播通知設定（`notifier.py`）

當執行 `--update` 或選單下載完新集數後，可自動推播結果到手機。打開 `manager_config.json` 修改 `notification` 區塊即可：

### 方案 A：ntfy（最簡單、免費、免申請帳號與 Token）
1. 手機安裝免費 App **ntfy**（Android / iOS 皆有）。
2. 在 App 內點 `+` 訂閱一個自己取的隨機頻道名稱（例如 `ani-notify-7k9x2m`，取複雜一點避免跟別人撞名）。
3. 打開 `manager_config.json` 改成：
   ```json
   "notification": {
     "enabled": true,
     "provider": "ntfy",
     "endpoint": "https://ntfy.sh/ani-notify-7k9x2m",
     "token": "",
     "chat_id": ""
   }
   ```
4. 執行 `python run_manager.py --test-notify`（或在選單按 `7`），手機立即收到測試通知即完成。

### 方案 B：Discord Webhook（有裝 Discord 就不用多裝 App）
1. 在自己的私人 Discord 伺服器頻道 → **編輯頻道 → 整合 → Webhook → 建立並複製 URL**。
2. 在 `manager_config.json` 填入：
   ```json
   "notification": {
     "enabled": true,
     "provider": "discord",
     "endpoint": "https://discord.com/api/webhooks/你的網址",
     "token": "",
     "chat_id": ""
   }
   ```

### 方案 C：Telegram Bot
填入 `"provider": "telegram"`、BotFather 給的 `"token"` 與自己的 `"chat_id"`。

---

## 6. Git 上傳注意事項（`.gitignore`）

請務必在 `.gitignore` 加入以下規則，避免個人 Cookie 與紀錄外洩到公開 GitHub Fork：

```gitignore
cookie.txt
download_history.json
manager_config.json
aniGamer.db
bangumi/
.venv/
__pycache__/
*.pyc
```
