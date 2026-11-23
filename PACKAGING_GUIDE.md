# AniManager v2.0.0 封裝成獨立執行檔（.exe / .app）操作手冊

本文件說明如何將 **AniManager** 封裝成免裝 Python、一般使用者雙擊即可開啟的獨立桌面軟體（Windows `.exe` 與 macOS `.app`）。

---

## 核心架構說明（為什麼這樣打包最穩？）

封裝腳本 `build_app.py` 會將以下項目打包進單一獨立應用資料夾中：
1. **桌面視窗外殼**（`gui_app.py` + `PyWebView` + 系統匣圖示 `pystray`）
2. **內建網頁前後端**（`ani_manager/web_server.py` + `ani_manager/web_ui/`）
3. **所有追番、搜番、排程、推播與加密備份模組**（`ani_manager/*.py`）
4. **應用程式圖示**（`assets/icon.ico` 與 macOS 自動生成的 `assets/icon.icns`）

**刻意不將底層 `aniGamerPlus` 硬編進同一個 `.exe`**：
原作者的下載核心依賴 `gevent` 與 TLS 指紋配置，獨立放在外部資料夾有兩大好處：
* 封裝成功率接近 100%，不會遇到隱藏套件漏檔崩潰。
* 日後原作者更新 `aniGamerPlus.exe` 或 `aniGamerPlus.py` 時，直接替換該檔案即可，完全不用重新編譯 GUI。

---

## 方法一：本機一行指令封裝（適合本機快速測試）

### A. 在 Windows 桌機上製作 `.exe`

1. 打開 PowerShell，進入專案根目錄並確認已安裝封裝套件：
   ```powershell
   .\.venv\Scripts\Activate.ps1
   pip install pyinstaller pywebview pystray pillow cryptography
   ```
2. 執行一行打包腳本：
   ```powershell
   python build_app.py
   ```
3. **驗證產出結果**：
   * 編譯完成後會在 `dist\AniManager\` 資料夾產生 **`AniManager.exe`**（已自動嵌入應用程式圖示與前端介面）。
   * 發布給其他電腦使用時，只需把 **`dist\AniManager\` 整個資料夾**複製過去，並把原作者官方 Release 的 **`aniGamerPlus.exe`**（或 `aniGamerPlus.py`）與 **`ffmpeg.exe`** 放進同一個資料夾，雙擊 `AniManager.exe` 即可使用。

### B. 在 macOS 上製作 `.app`

1. 打開終端機進入專案目錄，安裝封裝套件：
   ```bash
   uv pip install pyinstaller pywebview pystray pillow cryptography
   ```
2. 執行同一行打包腳本（腳本會自動呼叫 macOS 內建 `sips` 與 `iconutil` 將 `assets/icon.png` 轉為 `icon.icns`）：
   ```bash
   .venv/bin/python build_app.py
   ```
3. **驗證產出結果**：
   * 產出位於 **`dist/AniManager.app`**。
   * 將 `AniManager.app` 放進 `aniGamerPlus` 專案資料夾內（與 `aniGamerPlus.py` 同層），雙擊圖示即可啟動獨立視窗。

---

## 方法二：使用 GitHub Actions 雲端自動編譯（在 Mac 上免開桌機直接拿 Windows `.exe`）

專案內已包含 **`.github/workflows/release.yml`**。只要把這份檔案推送到你的 GitHub 倉庫，以後每次發布新版本，GitHub 雲端會**同時啟動 Windows 與 macOS 虛擬機**幫你編譯打包，並自動上架到倉庫的 **Releases** 頁面。

### 操作步驟

1. **將 `v2.0.0`（含 `.github/workflows/release.yml`）推送到 GitHub**
   * 在 GitHub Desktop 完成 Commit 並按 **`Push origin`**。
2. **建立版本標籤（Tag）觸發自動編譯**
   * **方式 A（GitHub Desktop）**：切換到 `History` 分頁 → 對最新一筆 Commit **按右鍵 → `Create Tag...`** → 輸入 `v2.0.0`（**必須以小寫 `v` 開頭**）→ 點 `Create Tag` → 按上方 **`Push origin`**。
   * **方式 B（GitHub 網頁版手動觸發測試）**：打開你的 GitHub 倉庫網頁 → 點上方 **`Actions`** 分頁 → 左側選 **`Build & Release AniManager (Windows & macOS)`** → 點右側 **`Run workflow`**。
3. **下載編譯好的執行檔**
   * 觸發後約 3～5 分鐘，Actions 亮起綠色勾勾：
     * 若是推 `v*` Tag 觸發：直接點進倉庫右側的 **`Releases`** 頁面，即可看到自動附帶的 **`AniManager-Windows-x64.zip`** 與 **`AniManager-macOS.zip`**。
     * 若是手動按 `Run workflow` 觸發：點進該次執行紀錄最下方的 **`Artifacts`** 區塊下載即可。

---

## 常見問題排解

* **Windows 執行時被 SmartScreen 提示「已保護您的電腦」**
  * 原因：個人自行編譯的 `.exe` 未購買付費商業程式碼簽章憑證，Windows 首次執行會跳提示。
  * 解決：點擊提示視窗上的 **「其他資訊」→「仍要執行」** 即可（後續啟動不會再詢問）。
* **為什麼選擇 `--onedir`（資料夾模式）而非 `--onefile`（單一執行檔）？**
  * PyInstaller 的 `--onefile` 每次雙擊都必須先把數十 MB 檔案解壓縮到系統暫存區，啟動慢 3～5 秒且極易被防毒軟體誤報隔離；`--onedir` 啟動速度為秒開且穩定度高得多。
