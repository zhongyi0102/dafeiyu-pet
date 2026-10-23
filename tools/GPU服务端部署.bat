@echo off
chcp 65001 >nul
REM ============================================================
REM  大肥鱼语音服务端 —— GPU 机器部署脚本
REM
REM  用法：在 GPU 机器上以管理员身份运行
REM  前提：已装 Python 3.11 + NVIDIA 驱动
REM ============================================================

setlocal
set ROOT=%~dp0
cd /d "%ROOT%"

echo ============================================
echo  大肥鱼语音服务端部署（GPU）
echo  目录: %ROOT%
echo ============================================
echo.

REM ---------- 1. 检查 Python ----------
echo [1/6] 检查 Python 3.11...
py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo   ✗ 未找到 Python 3.11
    echo   请先安装: https://www.python.org/downloads/release/python-3119/
    pause
    exit /b 1
)
py -3.11 --version
echo.

REM ---------- 2. 检查 NVIDIA 驱动 ----------
echo [2/6] 检查 NVIDIA GPU...
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader >nul 2>&1
if errorlevel 1 (
    echo   ⚠ 未检测到 nvidia-smi，GPU 可能不可用
    echo   继续部署（会用 CPU，很慢）
) else (
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
)
echo.

REM ---------- 3. 创建 RVC 环境 ----------
echo [3/6] 创建 RVC 环境（Python 3.11 + CUDA torch）...
if not exist "%ROOT%engines\rvc311\Scripts\python.exe" (
    py -3.11 -m venv "%ROOT%engines\rvc311"
)
set PY_RVC=%ROOT%engines\rvc311\Scripts\python.exe
"%PY_RVC%" -m pip install --upgrade pip -q
echo   安装 CUDA 版 torch（约 2.5GB，耗时较长）...
"%PY_RVC%" -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121 -q
if errorlevel 1 (
    echo   ✗ torch 安装失败
    pause
    exit /b 1
)
echo   安装 RVC 依赖...
"%PY_RVC%" -m pip install numpy scipy librosa soundfile praat-parselmouth pyworld torchcrepe faiss-cpu transformers safetensors fastapi uvicorn edge-tts ffmpeg-python av -q
echo   安装 UVR 分离（audio-separator）...
"%PY_RVC%" -m pip install audio-separator onnxruntime -q
echo.

REM ---------- 4. 创建 VoxCPM 环境 ----------
echo [4/6] 创建 VoxCPM2 环境...
if not exist "%ROOT%engines\voxcpm-env\Scripts\python.exe" (
    py -3.11 -m venv "%ROOT%engines\voxcpm-env"
)
set PY_VOX=%ROOT%engines\voxcpm-env\Scripts\python.exe
"%PY_VOX%" -m pip install --upgrade pip -q
echo   安装 CUDA 版 torch...
"%PY_VOX%" -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121 -q
echo   安装 voxcpm...
"%PY_VOX%" -m pip install voxcpm fastapi uvicorn -q
echo.

REM ---------- 5. 检查模型文件 ----------
echo [5/6] 检查模型文件...
set MISSING=0
if not exist "%ROOT%models\rvc\xiaoxiao.pth" (
    echo   ✗ 缺少 RVC 音色模型: models\rvc\xiaoxiao.pth
    set MISSING=1
)
if not exist "%ROOT%src\rvc-src\assets\rmvpe\rmvpe.pt" (
    echo   ✗ 缺少 rmvpe: src\rvc-src\assets\rmvpe\rmvpe.pt
    set MISSING=1
)
if not exist "%ROOT%src\rvc-src\assets\hubert_base\pytorch_config.json" (
    if not exist "%ROOT%src\rvc-src\assets\hubert_base\config.json" (
        echo   ✗ 缺少 hubert: src\rvc-src\assets\hubert_base\
        set MISSING=1
    )
)
if not exist "%ROOT%models\uvr\UVR-MDX-NET-Inst_HQ_3.onnx" (
    echo   ⚠ 缺少 UVR 模型（首次分离会自动下载）
)
if %MISSING%==1 (
    echo.
    echo   ⚠ 模型文件不全，请从原机器拷贝以下目录:
    echo     models\  src\rvc-src\assets\
)
echo.

REM ---------- 6. 启动服务 ----------
echo [6/6] 启动服务（监听 0.0.0.0，允许局域网访问）...
set RVC_HOST=0.0.0.0
set RVC_PORT=9880
set VOX_HOST=0.0.0.0
set VOX_PORT=9881
set HF_HOME=%ROOT%models\hf-cache

echo.
echo   RVC 服务: http://0.0.0.0:9880
start "dafeiyu-rvc" /MIN "%PY_RVC%" -u "%ROOT%tools\rvc_server.py"

echo   VoxCPM 服务: http://0.0.0.0:9881
start "dafeiyu-voxcpm" /MIN "%PY_VOX%" -u "%ROOT%tools\voxcpm_server.py"

echo.
echo ============================================
echo  部署完成
echo.
echo  在另一台机器（桌宠）上配置:
echo    设置 -> 语音 -> 服务地址: http://本机IP:9880
echo    VoxCPM 地址: http://本机IP:9881
echo.
echo  本机 IP（在此机器上运行 ipconfig 查看）:
ipconfig | findstr /C:"IPv4"
echo ============================================
pause
