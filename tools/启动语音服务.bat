@echo off
chcp 65001 >nul
REM ============================================================
REM  dafeiyu 语音服务（只做 RVC 音色转换）
REM
REM  说明：TTS 由桌宠自己完成（Edge-TTS 在桌宠进程里正常），
REM        本服务只负责把基准语音转成晓晓音色。
REM        纯本地计算，不联网，所以这里不需要代理。
REM
REM  用法：双击运行。看到 "Uvicorn running on http://0.0.0.0:9880"
REM        就说明好了，这个窗口不要关。
REM ============================================================

REM 模型路径
set RVC_MODEL=E:\AI\dafeiyu-voice\models\rvc\xiaoxiao.safetensors
REM 监听地址（只本机用就改成 127.0.0.1 更安全）
set RVC_HOST=127.0.0.1
set RVC_PORT=9880

cd /d "%~dp0"
echo ============================================
echo  dafeiyu 语音服务（RVC 音色转换）
echo  模型: %RVC_MODEL%
echo  监听: http://%RVC_HOST%:%RVC_PORT%
echo.
echo  首次转换约 10 秒（加载模型），之后同句 1 秒内。
echo  关掉这个窗口语音就停了。
echo ============================================
"E:\AI\dafeiyu-voice\engines\rvc311\Scripts\python.exe" -u rvc_server.py
echo.
echo 服务已退出。
pause
