@echo off
rem Stock scanner: called every 15 min by Task Scheduler. Pushes new data to GitHub Pages.
cd /d "%~dp0"
if not exist logs mkdir logs
set PYTHONUTF8=1
".venv\Scripts\python.exe" scan.py >> logs\run.log 2>&1
git add docs state config.toml >> logs\run.log 2>&1
git diff --cached --quiet && exit /b 0
git commit -m "update data" >> logs\run.log 2>&1
git push >> logs\run.log 2>&1
