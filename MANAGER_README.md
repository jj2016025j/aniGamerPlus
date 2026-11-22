# aniGamerPlus 模組化增量下載管理工具 (v1.4.1)

本工具為 `miyouzi/aniGamerPlus` 的獨立模組化管理層。**不修改原本的 `aniGamerPlus.py` 與 `README.md`**，同時支援「同目錄外掛」與「獨立私人倉庫」兩種架構。

---

## 1. v1.4 核心更新

* **新增追番三合一 + 自動補齊中繼資料 (`metadata_fetcher.py`)**：
  * 支援直接輸入**片名關鍵字搜尋動畫瘋**、貼網址或貼 4 碼以上 SN。
  * 新增時自動抓取官方標題、動畫瘋本季週表更新時間、並透過免費公開 Bangumi API 查詢預定總集數。
  * 選單 `3 → 2` 支援替**既有舊作品一鍵自動補齊**空白的標題、更新時間與預定總集數。
* **嚴格欄位審核與選擇器 (`config_store.py`, `cli_ui.py`)**：
  * 擋掉過短或不存在的無效 SN（例如 `2`、`3`），畫質限白名單（1080/720/540/360），總集數限 `1~2000` 正整數。
  * 更新時間改為**互動選擇器**（套用自動偵測／選星期 1~7 + 常用時段或驗證 `HH:MM`／標記已完結／留空）。
  * 刪除與切換啟用／暫停支援**多選**（例如一次輸入 `5,6`）。
* **清爽分區 UI 與上線快報 (`cli_ui.py`)**：
  * 啟動程式上線時自動快速掃描所有啟用中追番，於首頁面板直接呈現「哪部有新集數可下載、哪部已追平」。
  * 精簡儀表板與分組選單，搭配自動清畫面（可在選單 `3 → 9` 開關）讓窄版 VS Code 終端依然乾淨好讀。
  * 修復開啟 720P 升級時「本地已有集數誤顯示為 0 集」的統計問題。
* **結構化實用推播 (`notifier.py`)**：
  * 四管道（ntfy / Discord / Telegram / LINE）推播全面改版：支援「追番更新簡報」、「下載結算報告（含成功／失敗／剩餘待補）」、「新增追番通知」。
  * 主選單 `8`（或 `--test-notify`）直接發送一份真實的「目前追番進度簡報」到手機；也可下 `--check --notify` 一行檢查並推播。

---

## 2. 模組架構

```text
aniGamerPlus/ (或獨立私人倉庫)
├── run_manager.py            # 主入口
├── MANAGER_README.md         # 本說明文件
└── ani_manager/
    ├── terminal_utils.py     # 終端緩衝清理、清畫面與可中斷睡眠
    ├── env_detector.py       # 跨平台與路徑偵測
    ├── config_store.py       # 設定檔、多選清單管理與嚴格輸入驗證器
    ├── metadata_fetcher.py   # 動畫瘋片名搜尋、本季週表解析、Bangumi 總集數查詢
    ├── episode_fetcher.py    # 動畫瘋線上集數與 SN 解析
    ├── cookie_manager.py     # Cookie 防呆驗證、VIP 前置檢查、Playwright 抓取
    ├── history_tracker.py    # 雙模式去重與後置解析度核對
    ├── downloader.py         # 可中斷 Popen 呼叫與後置熔斷引擎
    ├── audio_tools.py        # ffprobe / ffmpeg 音軌與解析度工具
    ├── notifier.py           # 四管道推播與結構化簡報／結算報告格式化
    └── cli_ui.py             # 分區清爽 TUI 主控制器
```

---

## 3. 常用指令

| 需求 | 指令 |
|---|---|
| 啟動互動介面（含上線新集數快報） | `.venv/bin/python run_manager.py`（Windows 用 `python`） |
| 啟動互動介面（略過上線掃描秒開） | `.venv/bin/python run_manager.py --no-startup-check` |
| 一鍵下載所有啟用中作品的新集數 | `.venv/bin/python run_manager.py --update` |
| 僅檢查更新並直接推播簡報到手機 | `.venv/bin/python run_manager.py --check --notify` |
| 強制單集測試（忽略紀錄） | `.venv/bin/python run_manager.py --sn 51813 --force` |

---

## 4. v1.4.1 修復與顯示優化

* 改由動畫瘋最新一集播放頁的「上架時間」直接推算每週更新星期／時段與是否已完結（超過 21 天未更新自動標記已完結並同步總集數）。
* 追番清單新增 `線上/總` 欄位並持久化目前線上上架集數，自動修正異常小於線上集數的舊總集數。
* Bangumi 查詢補上繁轉簡與新版 v0 POST API，提升繁體輕小說長片名命中率。
* 連續集數自動壓縮為區間（如 `第 1~7 集`、`#1~7`），檢查結果改採總覽表格＋縱向 SN 明細表呈現。
