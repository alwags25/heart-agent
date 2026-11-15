@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  HeartAgent 打包:产出 dist/HeartAgent.exe
echo  前置: pip install pyinstaller
echo ============================================
python -m PyInstaller --onefile --name HeartAgent --clean --noconfirm --add-data "web;web" --add-data "config.example.json;." --add-data "extensions\ext_time_now.py;extensions" --add-data "runner.py;." main.py
echo.
echo 完成: dist\HeartAgent.exe (拷到任意目录双击运行)
pause
