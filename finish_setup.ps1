# Finish the install: scheduled task + GitHub repo + Pages. Safe to run again.
#   powershell -ExecutionPolicy Bypass -File finish_setup.ps1
# (.venv, packages and the 5-day backfill are already done.)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 1. Scheduled task, every 15 minutes, hidden window
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$PSScriptRoot\run_hidden.vbs`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "StockDoublingScanner" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "OK: scheduled task StockDoublingScanner registered"
$ErrorActionPreference = "Continue"  # native tools (gh, git) print to stderr on success

# 2. GitHub CLI + login
function Find-Gh {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
  foreach ($p in @("$env:ProgramFiles\GitHub CLI", "$env:LOCALAPPDATA\Programs\GitHub CLI")) {
    if (Test-Path "$p\gh.exe") { $env:Path += ";$p" }
  }
  return [bool](Get-Command gh -ErrorAction SilentlyContinue)
}
if (-not (Find-Gh)) {
  Write-Host "Installing GitHub CLI. If Windows asks for permission, click YES."
  winget install --id GitHub.cli -e --accept-source-agreements --accept-package-agreements
}
if (-not (Find-Gh)) { Write-Host "STOP: GitHub CLI is not installed (installer cancelled?). Run this script again and click YES."; exit 1 }

gh auth status *> $null
if ($LASTEXITCODE -ne 0) { gh auth login --hostname github.com --git-protocol https --web }
$owner = (gh api user --jq .login).Trim()
if (-not $owner) { Write-Host "STOP: GitHub login did not finish. Run this script again."; exit 1 }
gh auth setup-git   # lets run.bat push without prompting
Write-Host "OK: logged in to GitHub as $owner"

# 3. Local repo + commit (fixes the identity left by an earlier failed run)
if (-not (Test-Path ".git")) { git init -b main | Out-Null }
git config user.name $owner
git config user.email "$owner@users.noreply.github.com"
git add -A
$hasCommit = git rev-parse --verify HEAD 2>&1 | Select-String -NotMatch "fatal"
if (-not $hasCommit) {
  git commit -m "Stock doubling scanner" | Out-Null
} elseif ((git log -1 --format=%ae) -like "@*") {
  git commit --amend --reset-author --no-edit | Out-Null
} else {
  git diff --cached --quiet; if ($LASTEXITCODE -ne 0) { git commit -m "update" | Out-Null }
}

# 4. Public repo + push + Pages from main /docs
if (-not (git remote)) {
  gh repo create stock-doubling --public --source . --remote origin --push
} else {
  git push -u origin main
}
if ($LASTEXITCODE -ne 0) { Write-Host "STOP: push to GitHub failed (see above)."; exit 1 }
gh api "repos/$owner/stock-doubling/pages" *> $null
if ($LASTEXITCODE -ne 0) {
  gh api -X POST "repos/$owner/stock-doubling/pages" -f "source[branch]=main" -f "source[path]=/docs" | Out-Null
}
Write-Host "OK: Pages enabled. Site (ready in 1-2 minutes): https://$owner.github.io/stock-doubling/"

# 5. One test run
cmd /c run.bat
Get-Content logs\run.log -Encoding UTF8 -Tail 15
