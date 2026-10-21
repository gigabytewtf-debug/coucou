@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Sagban bot
:loop
where py >nul 2>&1
if %errorlevel%==0 (py -3 bot.py) else (python bot.py)
echo.
echo Bot to'xtadi. 10 soniyadan keyin qayta ishga tushadi (yopish uchun oynani yoping)...
timeout /t 10 >nul
goto loop
