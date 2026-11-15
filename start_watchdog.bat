@echo off
chcp 65001 >nul
cd /d "%~dp0"
title HeartAgent Watchdog
:loop
echo [%time%] 启动 HeartAgent...
python main.py --no-browser
echo [%time%] 服务退出(退出码 %errorlevel%),5 秒后自动重启...
timeout /t 5 /nobreak >nul
goto loop
