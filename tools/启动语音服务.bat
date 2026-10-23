@echo off
chcp 65001 >nul
REM ============================================================
REM  dafeiyu 语音服务启动脚本（RVC + Edge-TTS）
REM  用法：双击运行，或放到开机启动
REM ============================================================

REM 代理（Edge-TTS 需要联网，按你的实际情况改；不需要就注释掉）
set HTTPS_PROXY=http://127.0.0.1:3067
set HTTP_PROXY=http://127.0.0.1:3067
set NO_PROXY=127.0.0.1,localhost

REM 模型路径
set RVC_MODEL=E:\AI\rvc-models\xiaoxiao.safetensors
REM 音色（Edge-TTS 基准，会再经 RVC 转换）
set TTS_VOICE=zh-CN-XiaoxiaoNeural
REM 监听
set RVC_HOST=0.0.0.0
set RVC_PORT=9880

cd /d "%~dp0"
echo ============================================
echo  dafeiyu 语音服务
echo  模型: %RVC_MODEL%
echo  监听: http://%RVC_HOST%:%RVC_PORT%
echo ============================================
"E:\AI\rvc311\Scripts\python.exe" -u rvc_server.py
pause
