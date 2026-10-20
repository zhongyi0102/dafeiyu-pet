# -*- mode: python ; coding: utf-8 -*-
"""大肥鱼桌宠 —— PyInstaller 打包配置

拆分后（petcore 包）的打包方式：
  - 代码全部打进 exe（桌宠.py + petcore/）
  - **素材不打进 exe**（310MB 太大），放在 exe 同目录的 sprites/ 下
  - 单文件模式（onefile）：双击即用

用法：
  .venv\\Scripts\\python.exe -m PyInstaller 桌宠.spec --noconfirm
  产物：dist/大肥鱼桌宠.exe
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

# 只打 icon（素材走外部）
datas += [("icon.ico", ".")]

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
