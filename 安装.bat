@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

title 大肥鱼桌宠 - 安装向导

echo.
echo  ╔══════════════════════════════════════════╗
echo  ║        大肥鱼桌宠 · 一键安装             ║
echo  ╚══════════════════════════════════════════╝
echo.

REM ============================================================
REM 1. 检查 Python
REM ============================================================
echo [1/4] 检查 Python...
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
    where py >nul 2>nul && set "PY=py"
)
if not defined PY (
    echo.
    echo  [!] 没有找到 Python。
    echo.
    echo      请先安装 Python 3.11 或更高版本：
    echo      https://www.python.org/downloads/
    echo.
    echo      安装时务必勾选 "Add Python to PATH"
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
echo      找到 Python %PYVER%
echo.

REM ============================================================
REM 2. 创建虚拟环境并装依赖
REM ============================================================
echo [2/4] 准备运行环境...
if not exist ".venv\Scripts\python.exe" (
    echo      创建虚拟环境...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo  [!] 创建虚拟环境失败。
        pause
        exit /b 1
    )
)
echo      安装依赖（首次约需 2-5 分钟）...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo.
    echo  [!] 依赖安装失败，请检查网络。
    echo      可以试试国内镜像：
    echo      .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    pause
    exit /b 1
)
echo      依赖就绪
echo.

REM ============================================================
REM 3. 检查 / 下载素材
REM ============================================================
echo [3/4] 检查动画素材...
set "NEED_DL=0"
if not exist "sprites\anim" set "NEED_DL=1"
if exist "sprites\anim" (
    for /d %%d in ("sprites\anim\*") do (
        if not defined FIRST_DIR set "FIRST_DIR=1"
    )
    if not defined FIRST_DIR set "NEED_DL=1"
)

if "%NEED_DL%"=="0" (
    echo      素材已就绪
    goto :launch
)

echo      素材缺失（约 310 MB），开始下载...
echo.
echo      下载地址：
echo      https://github.com/zhongyi0102/dafeiyu-pet/releases/latest
echo.
echo      如果自动下载很慢或失败，请手动下载 dafeiyu-sprites.zip
echo      然后解压到本目录（会得到 sprites\anim\ 文件夹）
echo.

REM 用 PowerShell 下载（curl 不一定有）
set "ZIP=%TEMP%\dafeiyu-sprites.zip"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue';" ^
  "$u='https://github.com/zhongyi0102/dafeiyu-pet/releases/download/v1.0.0/dafeiyu-sprites.zip';" ^
  "Write-Host '      正在下载...';" ^
  "try { Invoke-WebRequest -Uri $u -OutFile '%ZIP%' -UseBasicParsing; Write-Host '      下载完成' }" ^
  "catch { Write-Host '      下载失败: ' $_.Exception.Message; exit 1 }"

if errorlevel 1 goto :manual_dl

echo      正在解压...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Expand-Archive -Path '%ZIP%' -DestinationPath '.' -Force; Write-Host '      解压完成'"
if errorlevel 1 goto :manual_dl

del "%ZIP%" >nul 2>nul
echo.
echo      素材已就绪
echo.
goto :launch

:manual_dl
echo.
echo  ┌────────────────────────────────────────────┐
echo  │  自动下载失败，请手动操作：                │
echo  │                                            │
echo  │  1. 浏览器打开下面的地址                   │
echo  │     github.com/zhongyi0102/dafeiyu-pet     │
echo  │     /releases/latest                       │
echo  │                                            │
echo  │  2. 下载 dafeiyu-sprites.zip（310 MB）     │
echo  │                                            │
echo  │  3. 解压到本目录，确保存在：               │
echo  │     sprites\anim\idle_breath\0001.png      │
echo  │                                            │
echo  │  4. 然后重新运行本脚本                     │
echo  └────────────────────────────────────────────┘
echo.
pause
exit /b 1

:launch
echo [4/4] 启动桌宠...
echo.
echo      启动后请查看系统托盘（右下角）找大肥鱼
echo      右键宠物可以打开菜单；首次使用请设置 API Key
echo.
timeout /t 2 >nul
start "" ".venv\Scripts\pythonw.exe" "桌宠.py"
echo      已启动。本窗口可以关闭。
timeout /t 3 >nul
exit /b 0
