# Splitrail Dashboard

彙總 Claude Code／Codex CLI／Antigravity CLI 三家 AI coding 工具的用量，產出仿
[OpenUsage](https://www.openusage.ai/) 風格的視覺化網頁：總花費甜甜圈（可切換 Cost／Cost per
MTok／Tokens）、每家的訂閱額度條（5 小時／每週，含重置倒數與 pace 預估）、近 30 天用量趨勢、
分模型花費明細。

訂閱額度直接查官方用量 API，不經過 splitrail：Claude 用 Claude Code CLI 的
`~/.claude/.credentials.json`、Codex 用 `~/.codex/auth.json`（token 過期時卡片會提示，在終端機
跑一次該 CLI 即可刷新）；Antigravity 只在 App 或 `agy` 執行中才抓得到。

費用是用本機 token 記錄反推的**估算值**，不是官方帳單金額，只拿來看相對趨勢，
對帳請以官方帳單為準。Antigravity CLI 的用量目前因上游 [splitrail](https://github.com/Piebald-AI/splitrail)
本身的一個 bug（idx 誤配，metadata 綁在 user step 上時被漏算）而不準確。

## 安裝（Windows）

1. 這個資料夾需要 [splitrail](https://github.com/Piebald-AI/splitrail) 的執行檔。用
   [GitHub CLI](https://cli.github.com/) 下載並解壓縮到**跟這個 README 同一層資料夾**：
   ```powershell
   gh release download --repo Piebald-AI/splitrail --pattern "*x86_64-pc-windows-msvc.zip"
   # 解壓縮出來的 splitrail.exe 放到這個資料夾（跟 splitrail-summary.py 同層）
   ```
   或直接去 [splitrail Releases](https://github.com/Piebald-AI/splitrail/releases) 手動下載對應平台的壓縮檔。
2. 確認電腦已安裝 Python 3（含 `python` 指令可在任何路徑直接呼叫）。

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
