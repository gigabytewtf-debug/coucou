@echo off
chcp 65001 >nul
echo Bot to'xtatilmoqda...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and ($_.CommandLine -like '*bot.py*' -or $_.CommandLine -like '*start.bat*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Bot to'xtadi.
timeout /t 3 >nul
