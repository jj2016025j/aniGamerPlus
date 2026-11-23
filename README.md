<h1 align="center">aniGamerPlus Modular Manager (v1.5.0)</h1>

<p align="center">
  基於 <a href="https://github.com/miyouzi/aniGamerPlus">miyouzi/aniGamerPlus</a> 的外掛式模組化增量下載與追番管理工具
</p>

本分支在不修改底層 `aniGamerPlus.py` 解密與串流核心的前提下，新增獨立的模組化管理層（`run_manager.py` + `ani_manager/`），提供互動式終端介面（Rich TUI）、片名搜番、自動更新時間與完結判斷、三檔分離架構、畫質升級雙重防護與四管道手機推播。

> **查閱原專案完整說明**：原版 `aniGamerPlus` 的 Docker 部署、FTP 上傳、Web Dashboard、代理設定與完整 `config.json` / CLI 參數文件，已完整保留於 **[README_UPSTREAM.md](README_UPSTREAM.md)**。

---

## 注意事項（承襲自原專案底層規範）

* **FFmpeg 必備**：本專案依賴 `ffmpeg` 與 `ffprobe` 進行影片合併、解析度查驗與音軌修復，請先安裝並加入系統 `PATH`（macOS：`brew install ffmpeg`；Windows：`winget install Gyan.FFmpeg`）。
* **Cookie 使用風險**：使用 Cookie 解析存在巴哈姆特帳號遭封鎖之風險且不可解封，請自行評估後使用（原專案說明見 [Issue #207](https://github.com/miyouzi/aniGamerPlus/issues/207)）。若使用 Cookie，建議由無痕視窗登入抓取後直接關閉視窗（勿點登出），且不要將 `cookie.txt` 上傳至公開倉庫。
* **遇到人機驗證 (`ERROR: 該 sn 下真的有動畫？`)**：請參考 [README_UPSTREAM.md](README_UPSTREAM.md#配置ja3akamai指紋以規避人機驗證)，於 `config.json` 填入與抓取 Cookie 相同瀏覽器的 `ua`、`ja3` 與 `akamai` 指紋。

---

## 核心特色

* **三檔分離與範本機制（v1.5.0）**：將「系統設定＋推播金鑰」、「追番清單」、「下載歷史快取」徹底分離；隨附 `manager_config-sample.json` 與 `watchlist-sample.json` 供跨機參考，並支援舊版 `manager_config.json` 無痛自動遷移。
* **片名搜番與自動中繼資料**：新增作品支援直接輸入片名關鍵字搜尋動畫瘋、貼網址或 SN；自動透過最新一集上架時間推算每週更新時段與完結狀態，並結合 Bangumi API（含繁轉簡）補齊預定總集數。
* **增量過濾與畫質升級雙重防護**：支援「僅依實體檔（刪影片即重抓）」與「檔案＋歷史＋DB（歸檔防重抓）」兩種去重模式；開啟 720P→1080P 升級時具備 VIP 前置驗證與降級自動熔斷機制，防止非 VIP 重複抓 720P。
* **乾淨中斷與殘檔清理**：將 `aniGamerPlus.py` 與 `ffmpeg` 放入獨立行程群組啟動，隨時按 `Ctrl+C` 秒停且零 `gevent` 洗版，中斷後自動清理 `temp/` 殘留切片。
* **四管道手機推播**：同時支援 **ntfy、Discord Webhook、Telegram Bot、LINE Messaging API**（可任意同時開啟多個），發送分組區間化的追番進度簡報與下載結算報告。
* **雙系統自動相容**：自動偵測 Windows / macOS / Linux、專案內 `.venv`、剪貼簿工具與系統編輯器，並內建 `ffprobe`/`ffmpeg` QuickTime 無聲修復工具。

---

## 檔案與模組架構

### 資料三檔分離設計

| 檔案 | 性質 | 是否應進 Git | 說明 |
|---|---|---|---|
| `manager_config-sample.json` | 設定範本 | **是** | 無金鑰空殼範本，首次啟動無正式檔時自動據此建立 |
| `manager_config.json` | 系統偏好＋推播金鑰 | **否（嚴禁上傳）** | 存放去重模式、開關與 ntfy/Discord/TG/LINE 憑證 |
| `watchlist-sample.json` | 追番清單範本 | **是** | 示範格式用 |
| `watchlist.json` | 個人追番清單 | 視需求（不含金鑰） | 1 部番 1 筆，無敏感資訊，方便備份與跨裝置同步 |
| `download_history.json` | 下載快取紀錄 | **否** | 以集為單位自動累積，可隨時刪除重建 |

### 程式模組結構

```text
aniGamerPlus/ (或獨立私人倉庫)
├── aniGamerPlus.py              # 原專案核心（保持不變）
├── README.md                    # 本整合使用說明
├── README_UPSTREAM.md           # 原專案完整說明文件備份
├── run_manager.py               # 本工具主入口
├── manager_config-sample.json   # 系統設定範本
├── watchlist-sample.json        # 追番清單範本
└── ani_manager/
    ├── env_detector.py          # 跨平台、.venv、FFmpeg 與原專案路徑偵測（支援 vendor/aniGamerPlus）
    ├── config_store.py          # 系統偏好與推播金鑰讀寫、嚴格輸入驗證器
    ├── watchlist_store.py       # 獨立追番清單 (watchlist.json) 讀寫與批次管理
    ├── episode_fetcher.py       # 動畫瘋線上集數、上架時間解析與區間壓縮
    ├── metadata_fetcher.py      # 動畫瘋片名搜尋、連載/完結判斷與 Bangumi 總集數查詢
    ├── cookie_manager.py        # Cookie 防呆驗證、剪貼簿直寫、VIP 前置檢查與 Playwright 抓取
    ├── history_tracker.py       # 雙模式去重引擎與下載後實際解析度核對
    ├── downloader.py            # 獨立行程群組呼叫、乾淨中斷、temp/ 清理與後置熔斷
    ├── audio_tools.py           # ffprobe 檢測與 ffmpeg 無損重洗 AAC（修 QuickTime 無聲）
    ├── notifier.py              # 四管道手機推播與分組區間結算報告產生器
    ├── terminal_utils.py        # 終端輸入緩衝清理、清畫面與可中斷睡眠
    └── cli_ui.py                # 分區清爽 TUI 互動介面
```

---

## 安裝與快速啟動

### 1. 環境與依賴安裝

建議使用 Python 3.12 虛擬環境（避免新版本 Python 編譯 `gevent` 失敗）：

```bash
# macOS / Linux（建議使用 uv）
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -r requirements.txt
uv pip install rich

# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install rich
```
*(若要啟用 Cookie 選單中的 Playwright 半自動抓取，再補裝 `uv pip install playwright` 與 `.venv/bin/playwright install chromium` 即可。)*

### 2. 啟動指令

| 需求 | macOS / Linux | Windows |
|---|---|---|
| 啟動互動介面（含上線快報） | `.venv/bin/python run_manager.py` | `python run_manager.py` |
| 秒開互動介面（略過上線掃描） | `.venv/bin/python run_manager.py --no-startup-check` | `python run_manager.py --no-startup-check` |
| 一鍵下載所有啟用中追番的新集數 | `.venv/bin/python run_manager.py --update` | `python run_manager.py --update` |
| 僅檢查更新並推播簡報到手機 | `.venv/bin/python run_manager.py --check --notify` | `python run_manager.py --check --notify` |
| 強制下載單一 SN（Cookie 測試） | `.venv/bin/python run_manager.py --sn 51813 --force` | `python run_manager.py --sn 51813 --force` |
| 掃描並修復影片音軌（QuickTime） | `.venv/bin/python run_manager.py --fix-audio` | `python run_manager.py --fix-audio` |

---

## 四管道手機推播設定

編輯 `manager_config.json` 的 `notifications` 區塊，四個管道各自獨立開關，可任意同時啟用多個。設定完成後在主選單按 **`8`** 即可發送一份真實的「目前追番進度簡報」驗證推播：

* **ntfy（免註冊最簡單）**：手機裝免費 App `ntfy` 訂閱自訂隨機頻道，填入 `"endpoint": "https://ntfy.sh/你的隨機頻道名"`。
* **Discord**：在私人伺服器頻道建立 Webhook，填入 `"webhook_url"`。
* **Telegram**：找 `@BotFather` 建立 Bot 拿 `token`，先傳一句訊息給新 Bot，再開瀏覽器訪問 `https://api.telegram.org/bot<TOKEN>/getUpdates`（保留 `bot`、刪掉 `<>`）取得 `chat_id`。
* **LINE Messaging API（每月免費 200 則、免架伺服器）**：
  1. 在 [LINE Official Account Manager](https://manager.line.biz/) 建立免費官方帳號並啟用 Messaging API。
  2. 在 [LINE Developers Console](https://developers.line.biz/console/)（**請關閉網頁翻譯以免字元變形**）：
     * `Basic settings` 分頁最下方複製 **`Your user ID`**（必為**大寫 `U`** 開頭 33 碼）填入 `user_id`。
     * `Messaging API` 分頁最下方 `Channel access token (long-lived)` 點 **Issue** 填入 `channel_access_token`（注意不是 Channel secret）。
     * 掃描同頁 QR Code 將此官方帳號加為好友。

---

## Git 版本控制安全提醒（`.gitignore`）

為避免個人憑證或暫存檔上傳至公開倉庫，請確認 `.gitignore` 已包含以下項目：

```gitignore
# 個人憑證與敏感設定（嚴禁上傳）
cookie.txt
invalid_cookie.txt
manager_config.json

# 執行期紀錄與媒體檔
download_history.json
aniGamer.db
bangumi/
temp/
logs/
.venv/
__pycache__/
*.pyc
```

---

## 鳴謝與授權

* 底層下載核心來自 [miyouzi/aniGamerPlus](https://github.com/miyouzi/aniGamerPlus)，遵循原專案 `LICENSE` 授權規範。
