# 股票翻倍計畫：一次性安裝。用 PowerShell 在這個資料夾執行：
#   powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = (Get-Command py -ErrorAction SilentlyContinue)
if ($py) { $pyCmd = "py" } elseif (Get-Command python -ErrorAction SilentlyContinue) { $pyCmd = "python" }
else { throw "找不到 Python，請先安裝 Python 3.11 以上（python.org），安裝時勾選 Add to PATH。" }

if (-not (Test-Path ".venv")) { & $pyCmd -m venv .venv }
& ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q

$env:PYTHONUTF8 = "1"
# 第一次：補算過去 5 個交易日，並自動挑出持股
& ".venv\Scripts\python.exe" scan.py --backfill 5

# 每 15 分鐘執行一次（程式自己判斷盤中、收盤或休市，夏令時間自動處理）
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$PSScriptRoot\run_hidden.vbs`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "StockDoublingScanner" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "完成：排程 StockDoublingScanner 已建立，每 15 分鐘執行。紀錄在 logs\run.log。"
