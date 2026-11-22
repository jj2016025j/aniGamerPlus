# aniGamerPlus 模組化增量下載管理工具

本工具為 `miyouzi/aniGamerPlus` 的外掛式模組化管理層。**不覆蓋原本的 `aniGamerPlus.py`**，完整保留原專案的解密與串流下載核心（也避免日後 `git pull` 衝突），同時把「環境偵測」、「追番設定」、「線上集數解析」、「三層去重過濾」、「單集呼叫下載」、「音軌檢查修復」、「通知外掛」與「互動介面」拆成獨立模組。

---

## 1. 目錄與模組架構

解壓縮後，將所有檔案直接放進原本的 `aniGamerPlus/` 資料夾根目錄（與 `aniGamerPlus.py` 同層）：

```text
aniGamerPlus/
├── aniGamerPlus.py          # 原生核心（保持不變）
├── run_manager.py           # 主程式入口（互動選單 / 單行指令）
├── README.md                # 本使用說明
├── manager_config.json      # 首次執行自動生成（追番清單與設定）
├── download_history.json    # 首次執行自動生成（本地下載紀錄）
└── ani_manager/             # 各功能獨立模組
    ├── env_detector.py      # 自動辨識 Windows / macOS / Linux、.venv、FFmpeg、VIP Cookie
    ├── config_store.py      # 追番清單與設定檔讀寫
    ├── episode_fetcher.py   # 解析動畫瘋頁面，抓出該作品最新上架的所有集數與 SN
    ├── history_tracker.py   # 三層去重引擎（實體檔掃描 + JSON 紀錄 + aniGamer.db）
    ├── downloader.py        # 呼叫原生 aniGamerPlus.py，精準只抓缺漏集數 (-m single)
    ├── audio_tools.py       # ffprobe 音軌檢測 + ffmpeg 無損重洗 AAC（修 QuickTime 無聲）
    ├── notifier.py          # 手機推播外掛介面（預設關閉，預留 ntfy / Discord / Telegram）
    └── cli_ui.py            # 終端互動選單（自動套用 rich，未安裝時降級純文字）
```

---

## 2. 安裝與啟動（Windows / macOS 通用）

1. 將壓縮檔內的所有檔案解壓縮到你的 `aniGamerPlus/` 根目錄。
2. 核心功能全採用 Python 標準函式庫（若環境已有 `rich` 與 `requests` 會自動啟用美化介面與強化連線）。
3. 開啟終端機切到 `aniGamerPlus/` 目錄執行：

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
*(若資料夾內存在 `.venv`，`env_detector.py` 呼叫 `aniGamerPlus.py` 時會自動優先使用該虛擬環境的 Python。)*

---

## 3. 自動過濾已下載與增量更新原理

每次檢查更新時，`history_tracker.py` 會依序交叉比對三個來源：

1. **`bangumi/` 實體影片檔掃描**：自動解析既有檔名（如 `【動畫瘋】學生會也有洞！[1][720P].mp4`），你先前已經抓完的第 1～7 集會自動匯入紀錄並跳過，**不會重複下載**。
2. **`download_history.json`**：記錄每次下載成功的 SN、集數、畫質與時間。
3. **`aniGamer.db`**：唯讀 `aniGamerPlus` 原生 SQLite 紀錄。

**畫質升級開關**：
* 預設 `upgrade_lower_resolution: false`：只要本地已有該集檔案就跳過。
* 若之後補上 VIP 想把舊的 720P 換成 1080P，進選單 **3 → 3** 開啟「自動畫質升級」，系統就會只針對低於目標畫質的集數重新下載。

---

## 4. 常用指令（每週手動補新集數只要一行）

首次啟動會自動把《學生會也有洞！》（SN `51813`，每週日 01:00 更新，全 12 話）寫入 `manager_config.json`。

| 需求 | 指令 |
|---|---|
| 開啟互動式選單 | `python run_manager.py`（macOS 用 `.venv/bin/python run_manager.py`） |
| **一鍵增量更新**（自動略過已下載，只抓新集數） | `python run_manager.py --update` |
| 僅檢查線上是否已出新集數（不下載） | `python run_manager.py --check` |
| 只針對單一作品 SN 檢查並補抓 | `python run_manager.py --update --sn 51813` |
| 掃描並修復影片音軌（解決 QuickTime 無聲） | `python run_manager.py --fix-audio` |

---

## 5. 常用維護與未來擴充

* **設定 VIP Cookie**：進入主選單選 `5`，貼上從瀏覽器開發者工具（Network → `ani.gamer.com.tw` → Request Headers → `cookie`）複製的完整值即可寫入 `cookie.txt`。建議用無痕視窗登入抓取後直接關閉視窗，不要按登出。
* **影片沒聲音檢查與修復**：進入主選單選 `4`，會列出每支影片的音軌編碼狀態，並可一鍵用 `ffmpeg -c:v copy -c:a aac` 無損重寫音軌標頭。
* **未來若想開啟手機提醒**：打開 `manager_config.json` 設定 `notification`（支援 `ntfy`、`discord`、`telegram`），不需改動任何程式檔。
