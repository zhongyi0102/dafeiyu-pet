# -*- mode: python ; coding: utf-8 -*-
"""大肥鱼桌宠 —— PyInstaller 打包配置（单文件，含素材）

产物：dist/大肥鱼桌宠.exe（约 370MB）
  用户**只下载这一个 exe** 就能跑：
    - 代码 + 素材都在 exe 里
    - 首次启动把内置 sprites.zip 解压到
      %LOCALAPPDATA%\\DaFeiYuPet\\sprites（只解一次，之后秒启动）

用法：
  .venv\\Scripts\\python.exe -m PyInstaller 桌宠.spec --noconfirm
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules

# 收集第三方依赖
datas = []
binaries = []
hiddenimports = []

# 基础依赖
for pkg in ("psutil", "requests", "pynvml", "PIL", "yaml"):
    tmp = collect_all(pkg)
    datas += tmp[0]
    binaries += tmp[1]
    hiddenimports += tmp[2]

# ===== OCR（rapidocr-onnxruntime）=====
# 模型文件（.onnx，13MB）+ yaml 配置 + 字典，必须作为 datas 打进去，
# 否则运行时找不到模型会直接失败。
import os as _os
import rapidocr_onnxruntime as _rr
_RR_DIR = _os.path.dirname(_rr.__file__)

# 整个包目录（含 models/ 下的 onnx、config.yaml、字体等）
for _root, _dirs, _files in _os.walk(_RR_DIR):
    for _f in _files:
        if _f.endswith((".onnx", ".yaml", ".yml", ".txt", ".ttf", ".ttc")):
            _src = _os.path.join(_root, _f)
            _rel = _os.path.relpath(_src, _os.path.dirname(_RR_DIR))
            datas.append((_src, _os.path.dirname(_rel)))

# OCR 依赖的纯 Python 包
for pkg in ("rapidocr_onnxruntime", "onnxruntime", "cv2",
            "shapely", "pyclipper", "six"):
    try:
        tmp = collect_all(pkg)
        datas += tmp[0]
        binaries += tmp[1]
        hiddenimports += tmp[2]
    except Exception:
        # cv2 / onnxruntime 用 collect_all 可能失败，退回 collect_submodules
        try:
            hiddenimports += collect_submodules(pkg)
        except Exception:
            pass

# numpy 是 OCR 的硬依赖，必须收集（下面 excludes 里已移除）
for pkg in ("numpy",):
    tmp = collect_all(pkg)
    datas += tmp[0]
    binaries += tmp[1]
    hiddenimports += tmp[2]

# 显式加入 onnxruntime 的核心二进制
try:
    import onnxruntime as _ort
    _ORT_DIR = _os.path.dirname(_ort.__file__)
    for _sub in ("capi",):
        _d = _os.path.join(_ORT_DIR, _sub)
        if _os.path.isdir(_d):
            for _f in _os.listdir(_d):
                if _f.endswith((".dll", ".pyd")):
                    binaries.append((_os.path.join(_d, _f), "onnxruntime/" + _sub))
except Exception:
    pass

# 显式加入 cv2 的核心二进制（cv2.pyd）
# 排除 opencv_videoio_ffmpeg*.dll（29MB 视频解码器，OCR 用不到）
try:
    import cv2 as _cv
    _CV_DIR = _os.path.dirname(_cv.__file__)
    for _f in _os.listdir(_CV_DIR):
        if not _f.endswith((".pyd", ".dll")):
            continue
        if _f.startswith("opencv_videoio_ffmpeg"):
            continue        # 视频解码器，OCR 不需要
        binaries.append((_os.path.join(_CV_DIR, _f), "cv2"))
except Exception:
    pass

# 过滤掉 onnxruntime 的测试数据集（*.onnx 样例文件，OCR 不需要）
datas = [(s, d) for (s, d) in datas
         if not (("onnxruntime" in d or "onnxruntime" in s)
                 and _os.path.basename(s) in ("sigmoid.onnx", "logreg_iris.onnx",
                                              "mul_1.onnx"))]

# 收集 petcore 子模块（PyInstaller 不会自动发现动态 import）
hiddenimports += collect_submodules("petcore")

# ===== Qt Multimedia（语音播放）=====
# 关键：PyInstaller 不会自动收集 Qt 的插件目录，
# 少了 plugins/multimedia/*.dll 就会「播放器不可用」→ 语音静默不响。
# 两个后端都收：windowsmediaplugin（体积小）+ ffmpegmediaplugin（兼容性最好，
# 能处理非常规采样率的 wav；代价约 21MB，占总体积 4.6%）。
hiddenimports += ["PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets"]

try:
    import PySide6 as _PySide6
    _PS6_DIR = _os.path.dirname(_PySide6.__file__)
    _PLUG = _os.path.join(_PS6_DIR, "plugins")

    # 1) 插件：plugins/multimedia → PySide6/plugins/multimedia
    _mm_dir = _os.path.join(_PLUG, "multimedia")
    if _os.path.isdir(_mm_dir):
        for _f in _os.listdir(_mm_dir):
            if _f.lower().endswith(".dll"):
                binaries.append((_os.path.join(_mm_dir, _f),
                                 "PySide6/plugins/multimedia"))

    # 2) Qt6Multimedia 系列 DLL（放 PySide6/ 根，跟其它 Qt DLL 同目录）
    for _f in _os.listdir(_PS6_DIR):
        _low = _f.lower()
        if _low.startswith("qt6multimedia") and _low.endswith(".dll"):
            binaries.append((_os.path.join(_PS6_DIR, _f), "PySide6"))

    # 3) ffmpeg 后端依赖的编解码库（av*/sw*），只收 ffmpegmediaplugin 需要的
    for _f in ("avcodec-63.dll", "avformat-63.dll", "avutil-61.dll",
               "swresample-7.dll", "swscale-10.dll"):
        _p = _os.path.join(_PS6_DIR, _f)
        if _os.path.exists(_p):
            binaries.append((_p, "PySide6"))
except Exception as _ex:
    print("[spec] Qt Multimedia 收集失败: %s" % _ex)

# 图标 + **素材包**（单文件核心）
datas += [
    ("icon.ico", "."),
    ("sprites.zip", "."),
]

a = Analysis(
    ["桌宠.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # numpy / PIL 是 OCR 硬依赖，不能排除
    excludes=["tkinter", "matplotlib", "scipy", "pandas",
              # cv2 的视频解码器（29MB），OCR 不用
              "cv2.gapi"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="大肥鱼桌宠",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX 会破坏 Qt 部分 dll，关掉
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,             # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["icon.ico"],
)
