@echo off
chcp 65001 >nul
REM ============================================================
REM  dafeiyu 语音服务 - GPT-SoVITS (yin1 音色)
REM
REM  与「启动语音服务.bat」(RVC) 独立并存，端口 9882。
REM  GPT-SoVITS 是文本直出：不需要 Edge-TTS，也不联网。
REM
REM  用法：双击运行。看到 "Uvicorn running on http://127.0.0.1:9882"
REM        就说明好了，这个窗口不要关。
REM  首次启动要加载模型（约 10-30 秒），之后常驻。
REM ============================================================

REM 模型根目录（含 models\ 与 GPT-SoVITS\）
set GPT_ROOT=E:\AI\voice-models\yin1
REM 监听地址（只本机用就保持 127.0.0.1）
set GPT_HOST=127.0.0.1
set GPT_PORT=9882

cd /d "%~dp0"
echo ============================================
echo  dafeiyu 语音服务（GPT-SoVITS / yin1）
echo  模型: %GPT_ROOT%\models\yin1.pth
echo  监听: http://%GPT_HOST%:%GPT_PORT%
echo.
echo  首次合成约 8-20 秒（CPU 推理），之后同句更快。
echo  关掉这个窗口语音就停了。
echo ============================================
"%GPT_ROOT%\env311\Scripts\python.exe" -u gptsovits_server.py
echo.
echo 服务已退出。
pause
