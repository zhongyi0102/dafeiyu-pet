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

for pkg in ("psutil", "requests", "pynvml"):
    tmp = collect_all(pkg)
    datas += tmp[0]
    binaries += tmp[1]
    hiddenimports += tmp[2]

# 收集 petcore 子模块（PyInstaller 不会自动发现动态 import）
hiddenimports += collect_submodules("petcore")

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
    excludes=["tkinter", "matplotlib", "numpy", "PIL", "scipy", "pandas"],
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
