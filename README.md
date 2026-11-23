<h1 align="center">AniManager — 動畫瘋跨裝置增量下載管理器 (v2.0.0)</h1>

<p align="center">
  基於 <a href="https://github.com/miyouzi/aniGamerPlus">miyouzi/aniGamerPlus</a> 的模組化桌面視窗、手機遙控與增量追番管理系統
</p>

本專案在不修改底層 `aniGamerPlus.py` / `aniGamerPlus.exe` 核心的前提下，提供**一體化桌面應用（PyWebView 視窗＋內建後端＋系統匣常駐）**、**手機／跨裝置瀏覽器遙控與影片回傳**，並完整保留原本的終端機互動工具 `run_manager.py`。

> **查閱原專案完整說明**：原版 `aniGamerPlus` 的 Docker 部署、FTP 上傳、代理設定與完整 `config.json` / CLI 參數文件，保留於 **[README_UPSTREAM.md](README_UPSTREAM.md)**。

---

## 核心功能亮點 (v2.0.0)

* **一體化桌面視窗＋手機跨裝置遙控（`gui_app.py`、`web_server.py`、`web_ui/`）**
  * 雙擊啟動獨立桌面視窗（無終端黑窗），背後自動啟動內建 Web 引擎（預設連接埠 `8765`）。
  * 首頁自動偵測內網 Wi-Fi 與 Tailscale VPN 網址並產生 QR Code，手機或另一台電腦開瀏覽器即可同步管理追番、搜片名新增作品、觸發下載與查看即時日誌。
  * **影片直接傳送到操作裝置**：支援從手機瀏覽器一鍵「下載到此裝置」，並可開啟「遠端發起下載完成後自動回傳至目前瀏覽器（可選傳完自動刪除主機副本）」。
* **外部瀏覽器安全鎖（PIN 碼＋設備記憶＋生物辨識）**
  * 本機電腦視窗免輸密碼；外部 IP 連入時可開啟 PIN 碼驗證，勾選「記住此設備」後 90 天內免重複輸入（變更 PIN 碼或按一鍵登出設備後立即失效）。
  * 在 `https://`（如 Tailscale HTTPS）或本機環境下，支援綁定裝置的 **Touch ID / Face ID / Windows Hello** 生物辨識快速解鎖。
* **三種背景自動運作模式（自由切換）**
  * **僅手動操作**：關閉視窗即結束程式。
  * **系統匣常駐輪詢**：關閉視窗縮入右下角／頂部選單列，依設定間隔靜默檢查下載。
  * **系統原生排程（最省電、0 常駐）**：一鍵註冊 macOS `launchd` 或 Windows 工作排程器，平時 0 記憶體佔用，電腦休眠喚醒後自動補跑。
* **完整保留終端介面與既有模組**
  * 終端版 `run_manager.py` 與所有 `ani_manager/` 模組（片名搜番、最新集上架時間推算週表與完結、7 種持久化排序、畫質升級 VIP 前後雙重防護、四管道推播、AES-256 `.anibak` 加密備份還原）完全共用同一套資料檔。

---

## 安裝與啟動方式

### 1. 安裝依賴

```bash
# macOS / Linux（建議使用 uv + Python 3.12）
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -r requirements.txt
uv pip install rich pywebview pystray pillow cryptography

# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install rich pywebview pystray pillow cryptography
```

### 2. 啟動方式

| 模式 | macOS / Linux | Windows |
|---|---|---|
| **啟動桌面視窗應用（含手機遙控引擎）** | `.venv/bin/python gui_app.py` | `python gui_app.py` |
| **純背景後端模式（無視窗）** | `.venv/bin/python gui_app.py --server` | `python gui_app.py --server` |
| **終端機互動選單（保留原版 CLI）** | `.venv/bin/python run_manager.py` | `python run_manager.py` |
| **無互動一鍵增量下載（排程用）** | `.venv/bin/python run_manager.py --update` | `python run_manager.py --update` |

---

## 打包成獨立 `.exe` / `.app`

專案內已附帶本機一鍵打包腳本 `build_app.py` 與 GitHub Actions 雲端雙平台編譯設定 `.github/workflows/release.yml`：

```bash
# 本機一鍵封裝（Windows 產出 dist/AniManager/AniManager.exe；macOS 產出 dist/AniManager.app）
python build_app.py
```

詳細操作步驟（包含在 Mac 上利用 GitHub Actions 免開桌機自動編譯 Windows `.exe`）請參閱隨附的 `PACKAGING_GUIDE.md`。
