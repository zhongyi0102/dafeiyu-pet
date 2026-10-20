@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "sprites\anim" (
    echo.
    echo  [!] 缺少动画素材，请先运行「安装.bat」
    echo.
    pause
    exit /b 1
)
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "桌宠.py"
) else (
    where pythonw >nul 2>nul
    if errorlevel 1 (
        echo.
        echo  [!] 没有找到 Python，请先运行「安装.bat」
        echo.
        pause
        exit /b 1
    )
    start "" pythonw "桌宠.py"
)