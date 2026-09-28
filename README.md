# Splitrail Dashboard

彙總 Claude Code／Codex CLI／Antigravity CLI 三家 AI coding 工具的用量，產出仿
[OpenUsage](https://www.openusage.ai/) 風格的視覺化網頁：總花費甜甜圈（可切換 Cost／Cost per
MTok／Tokens）、每家的訂閱額度條（5 小時／每週，含重置倒數與 pace 預估）、近 30 天用量趨勢、
分模型花費明細。

**訂閱額度由 [Pane](https://github.com/ItsJazii/pane) 提供**（OpenUsage 的 Windows 版）：儀表板讀
Pane 的本機 API `http://127.0.0.1:6736/v1/usage`，Pane 沒開時額度卡片會提示。Claude／Codex 的
token 換新由 Pane 負責，本儀表板**不讀寫任何登入憑證**，避免兩個程式同時換 token 互相作廢。
Antigravity 額度若 Pane 抓不到，改由儀表板直接讀 Antigravity 的本機服務（App 或 `agy` 執行中才有）。

費用是本機 token 紀錄乘上官方 API 單價的**估算值**，不是官方帳單金額，只拿來看相對趨勢：
- **Claude Code**：由 [splitrail](https://github.com/Piebald-AI/splitrail) 計算。
- **Codex CLI**：自行讀 `~/.codex/sessions`，含 fast（priority）加價、fork／subagent 子 session 去重、
  超過 272K input 的長 context 單價。splitrail 這三項都沒處理；子 session 重播改用跨檔案累計值去重（Pane 會重複計算）。
- **Antigravity**：自行讀 `~/.gemini/antigravity*/conversations`（CLI、IDE、App 都算）。splitrail 3.10.1
  把 token 欄位解讀錯誤且只讀 CLI，數字不可用。
- 單價寫在 `splitrail-summary.py` 的 `CODEX_PRICES`／`GEMINI_PRICES`，依官方價目頁核對（2026-09-28）；
  `codex-auto-review` 紀錄裡沒有實際模型，依推定以 gpt-5.5 計價。

## 安裝（Windows）

1. 這個資料夾需要 [splitrail](https://github.com/Piebald-AI/splitrail) 的執行檔。用
   [GitHub CLI](https://cli.github.com/) 下載並解壓縮到**跟這個 README 同一層資料夾**：
   ```powershell
   gh release download --repo Piebald-AI/splitrail --pattern "*x86_64-pc-windows-msvc.zip"
   # 解壓縮出來的 splitrail.exe 放到這個資料夾（跟 splitrail-summary.py 同層）
   ```
   或直接去 [splitrail Releases](https://github.com/Piebald-AI/splitrail/releases) 手動下載對應平台的壓縮檔。
2. 確認電腦已安裝 Python 3（含 `python` 指令可在任何路徑直接呼叫）。
3. 安裝並開啟 [Pane](https://github.com/ItsJazii/pane/releases)（per-user 安裝，不需系統管理員），
   在 Pane 裡啟用 Claude、Codex、Antigravity。

## 使用

| 指令 | 效果 |
| :-- | :-- |
| `splitrail-summary.bat` | 印出文字表格（各工具的累計/近7天/近30天/今日費用與 token） |
| `splitrail-dashboard.bat` | 產生 `splitrail-dashboard.html` 並自動用瀏覽器打開 |
| `splitrail-dashboard.bat --no-open` | 只產生 HTML，不自動開瀏覽器（適合排程/背景執行） |

## 背景自動更新（選用）

`splitrail-dashboard-loop.bat` 是一個無窮迴圈，每 5 分鐘呼叫一次
`splitrail-dashboard.bat --no-open` 靜默更新網頁；開著的網頁也會每 5 分鐘自動重新載入。

若想「開機/登入時自動啟動」但沒有系統管理員權限（無法用工作排程器 Task Scheduler）：
把一個呼叫 `start /min "" "<這個資料夾路徑>\splitrail-dashboard-loop.bat"` 的
`.bat` 捷徑，放進 Windows 的個人啟動資料夾（`Win+R` 輸入 `shell:startup`）。

## 檔案說明

| 檔案 | 用途 |
| :-- | :-- |
| `splitrail-summary.py` | 主邏輯：跑 `splitrail.exe stats`、彙總 JSON、印表格、產生視覺化 HTML |
| `splitrail-summary.bat` / `splitrail-dashboard.bat` | 免打 `python` 的捷徑（路徑皆為相對於自身所在資料夾，可攜） |
| `splitrail-dashboard-loop.bat` | 背景定時更新迴圈 |
