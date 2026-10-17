# 股票翻倍計畫：掃描器

每次執行會抓 Yahoo Finance 日線，替持股和候選池打 0–100 分，判斷進出場與汰弱留強，輸出 docs/data.json，由 docs/index.html 手機網頁讀取。檔案裡只有代號、分數和訊號，沒有任何金額。

## 安裝（Windows）

需要 Python 3.11 以上和 git。在這個資料夾開 PowerShell，執行 powershell -ExecutionPolicy Bypass -File setup.ps1。它會建立 .venv、安裝套件、補算過去 5 個交易日並挑出持股，再建立 Windows 排程 StockDoublingScanner，每 15 分鐘在背景執行 run.bat。程式自己判斷美股盤中、收盤或休市（夏令時間自動處理），有新資料才推上 GitHub Pages。執行紀錄在 logs\run.log。

## 要改的地方：config.toml

- [holdings]：你自己指定的持股（目前只有 SOXL）。程式不會自動換掉這些，只發訊號。
- 其餘名額（共 16 檔）由程式從候選池挑分數最高的補滿，每個主題最多 4 檔，名單存在 state/portfolio.json。之後若候選股連續 3 個收盤日比最弱的「自動持股」高 15 分，程式會在收盤後自動換掉它，一天最多換一檔。
- [candidates]：約 75 檔候選池，依主題分組，可自由增刪。
- weights、swap_gap、swap_days：評分權重和換股門檻。

## 評分

相對強弱 30（1/3/6 個月報酬減 SPY，排百分位）、資金流 25（MFI、CMF、上漲日量比）、趨勢 20（20/50/200 日線）、反彈 15（多頭拉回止跌），風險最多扣 10 分（波動率與最大回撤）。

state/history.json 存最近 60 個收盤確認日的分數，用來算換股連續天數，請勿刪除。state/portfolio.json 是自動挑選的持股，刪掉的話下次會重新挑。
