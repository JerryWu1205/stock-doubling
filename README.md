# 股票翻倍計畫：掃描器（美股、台股）

每次執行會抓 Yahoo Finance 日線，替美股和台股的持股與候選池各自打 0–100 分，判斷進出場與汰弱留強，並產生股價、走勢圖資料，由 docs/index.html 手機網頁讀取。網頁最上方可以切換美股 / 台股。檔案裡只有代號、股價、分數和訊號，沒有任何持股金額。

## 安裝（Windows）

需要 Python 3.11 以上和 git。在這個資料夾開 PowerShell，執行 powershell -ExecutionPolicy Bypass -File setup.ps1。它會建立 .venv、安裝套件並建立 Windows 排程 StockDoublingScanner，每 15 分鐘在背景執行 run.bat。程式自己判斷每個市場是盤中、收盤還是休市，有新資料才推上 GitHub Pages。執行紀錄在 logs\run.log。

網頁可以「加到主畫面」當成 App 用（docs/manifest.webmanifest 和圖示）。

其他指令：python scan.py --market tw（只跑台股）、python scan.py --mode close（強制收盤確認）、python scan.py --demo（模擬資料，不連網）。

## 時間

| | 美股 | 台股 |
|---|---|---|
| 盤中暫定 | 美東 9:30–16:00（夏令時間自動處理） | 台北 9:00–14:30 |
| 收盤確認 | 美東 16:00–18:00 重跑幾次 | 台北 14:30–16:00 重跑幾次 |
| 大盤基準 | SPY | 0050 |
| 報價延遲 | 約即時 | 約 20 分鐘 |

國定假日、颱風假不用設定：盤中時段抓到的資料不是今天，就當休市。

## 要改的地方：config.toml

- [us] / [tw]：兩個市場各一段。不想跑某個市場，在那段加 enabled = false。
- [us.holdings] / [tw.holdings]：你自己指定的持股，程式不會自動換掉，只發訊號。其餘名額（共 16 檔）由程式從候選池挑分數最高的補滿，每個主題最多 4 檔。之後若候選股連續 3 個收盤日比最弱的「自動持股」高 15 分，程式會在收盤後自動換掉它，一天最多換一檔。
- [us.candidates] / [tw.candidates]：候選池，依主題分組。台股只寫數字代號，程式自己判斷上市（.TW）或上櫃（.TWO），結果存在 state/tw/suffix.json。
- [tw.names]：網頁上顯示的台股中文名稱。
- weights、swap_gap、swap_days：評分權重和換股門檻，兩個市場共用。

## 兩套名單：強勢、跌深

網頁上可以切換「強勢」和「跌深」。兩套用同一批股票，各自評分、各自持股（state/<市場>/history_dip.json、portfolio_dip.json），換股規則一樣。跌深名單只收「可進場」的股票，沒有合格的就空著。backtest.py 用過去一年的資料回測三種選股方式（強勢、強勢等回檔、跌深反彈）和大盤，結果在網頁的「績效」分頁；回測不含手續費、稅和滑價，只供研究參考。

## 評分（強勢）

相對強弱 30（1/3/6 個月報酬減大盤，在同市場裡排百分位）、資金流 25（MFI、CMF、上漲日量比）、趨勢 20（20/50/200 日線）、反彈 15（多頭拉回止跌），風險最多扣 10 分（波動率與最大回撤）。評分用還原股價，除權息不會打亂均線。

台股漲跌停：收盤價等於漲停 / 跌停價就標「漲停」「跌停」，那天成交量不準，資金流沿用前一天的值；漲停的候選股先列「觀察」，因為實際上買不到。

## 股價與走勢圖

- 列表上的股價是最新一次掃描的價格（未還原，和看盤軟體一致），盤中每 15 分鐘更新，不是即時報價。
- 點開個股可看「今日」5 分鐘走勢加 VWAP，以及 1 個月 / 6 個月 / 1 年日 K：20/50/200 日均線、自動趨勢線、支撐壓力、52 週高低、成交量和 20 日均量（爆量標深色）、選入 / 換股 / 進出場 / 除息標記。副圖可選 MFI、MACD、RSI、OBV、CMF，下方還有分數走勢。
- 「跌破上升趨勢線」「突破壓力區」「創 52 週新高」「爆量上漲」「量價背離」等提示由 charts.py 算出，只供參考，不影響分數。
- 日 K 圖檔在收盤確認時更新；盤中網頁會把今天這根 K 補上。

## 輸出檔（docs/data/）

- us.json、tw.json：列表。每檔有 ticker、name、theme、score、components、price、prev_close、chg_1d_pct、limit、bar（今天的開高低收量）、signal、reasons、alerts；另有 mode、generated_at、generated_at_local、price_time、quote_delay_minutes、stale_after_minutes、swaps、portfolio_changes、errors。
- charts/<市場>/<代號>.json：一年日 K、均線、量價指標、trendlines、levels、hi52、lo52、markers、dividends、scores。
- intraday/<市場>.json：當天所有股票的 5 分鐘價格和 VWAP（時間是當地時鐘）。
- markets.json：有哪些市場、各自最後更新時間。
- perf/<市場>.json：回測曲線、報酬、最大回檔、換股次數，以及兩套名單的實際紀錄（只有百分比）。收盤確認時更新。
- us.json / tw.json 的 books.dip 是跌深名單；每檔另有 score_chg（和前一個收盤日比的分數變化）和 spark（近 30 天收盤）。圖檔另有 rs（相對大盤線）和 dip（跌深分數和標記）。
- docs/data.json：美股列表的舊位置，給還沒重新整理的舊網頁用。

state/<市場>/history.json 存最近 60 個收盤確認日的分數和訊號，用來算換股連續天數，請勿刪除。state/<市場>/portfolio.json 是自動挑選的持股，刪掉的話下次會重新挑。

docs/vendor/ 是走勢圖用的 TradingView Lightweight Charts 4.2.0（Apache 2.0，授權在同一資料夾）。
