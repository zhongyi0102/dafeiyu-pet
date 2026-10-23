# -*- coding: utf-8 -*-
r"""打包 GPU 服务端部署包

生成一个 zip，拷到 GPU 机器解压即可部署（含代码 + 模型 + 部署脚本）。
不含 Python 环境（GPU 机器上现装 CUDA 版 torch）。

用法：
  python make_gpu_package.py [输出zip路径]
"""
import os
import shutil
import sys
import zipfile

SRC_ROOT = r"E:\AI\dafeiyu-voice"
PET_ROOT = r"D:\Software\Portable\应用程序\dafeiyu-pet"
DEFAULT_OUT = r"E:\AI\gpu-server-package.zip"


def log(msg):
    print("[pack] %s" % msg, flush=True)


def should_skip(rel):
    """跳过不需要打包的东西。"""
    low = rel.lower().replace("\\", "/")
    skip_parts = (
        "/engines/",           # Python 环境不打包（GPU 机器现装）
        "/work/",              # 工作目录
        "/models/hf-cache/",   # VoxCPM 缓存（首次自动下载）
        "__pycache__",
        ".git",
    )
    for s in skip_parts:
        if s in low:
            return True
    if low.endswith((".log", ".tmp", ".pyc")):
        return True
    return False


def add_tree(zf, src_dir, arc_prefix):
    """递归加入目录。"""
    n = 0
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, src_dir)
            if should_skip(os.path.join(arc_prefix, rel)):
                continue
            arc = os.path.join(arc_prefix, rel).replace("\\", "/")
            try:
                zf.write(full, arc)
                n += 1
            except Exception as ex:
                log("  跳过 %s: %s" % (rel, ex))
    return n


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    log("源目录: %s" % SRC_ROOT)
    log("输出: %s" % out)
    if os.path.exists(out):
        os.remove(out)

    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # 1. 语音服务代码
        log("打包 tools/（服务脚本）…")
        n = add_tree(zf, os.path.join(PET_ROOT, "tools"), "tools")
        log("  %d 个文件" % n)
        total += n

        # 2. 模型
        log("打包 models/（音色 + UVR 模型）…")
        n = add_tree(zf, os.path.join(SRC_ROOT, "models"), "models")
        log("  %d 个文件" % n)
        total += n

        # 3. RVC 源码（含 rmvpe/hubert）
        log("打包 src/rvc-src/（官方源码 + 模型）…")
        n = add_tree(zf, os.path.join(SRC_ROOT, "src", "rvc-src"), "src/rvc-src")
        log("  %d 个文件" % n)
        total += n

        # 4. 部署脚本 + 说明
        log("打包部署脚本…")
        for name in ("GPU服务端部署.bat", "启动语音服务.bat"):
            p = os.path.join(PET_ROOT, "tools", name)
            if os.path.exists(p):
                zf.write(p, name)
                total += 1
        # 写一个 README
        readme = """大肥鱼语音服务端（GPU 版）
========================================

一、前提
  - Windows 10/11
  - Python 3.11（https://www.python.org/downloads/release/python-3119/）
  - NVIDIA 显卡 + 驱动（建议 8GB+ 显存）

二、部署
  1. 解压本包到任意目录，例如 D:\\dafeiyu-voice
  2. 以管理员身份运行「GPU服务端部署.bat」
     脚本会自动：
       - 创建两个 Python 环境（rvc311 / voxcpm-env）
       - 安装 CUDA 版 torch + 所有依赖
       - 检查模型文件
       - 启动两个服务（监听 0.0.0.0）
  3. 记下本机 IP（脚本最后会打印）

三、桌宠端配置
  在另一台机器上：
    桌宠 -> 设置 -> 语音
      服务地址: http://<GPU机器IP>:9880
      VoxCPM 地址: http://<GPU机器IP>:9881

四、防火墙
  首次启动会弹 Windows 防火墙提示，必须允许「专用网络」和「公用网络」。
  或者手动放行：
    netsh advfirewall firewall add rule name="dafeiyu-rvc" dir=in action=allow protocol=TCP localport=9880
    netsh advfirewall firewall add rule name="dafeiyu-vox" dir=in action=allow protocol=TCP localport=9881

五、验证
  在桌宠机器上用浏览器打开：
    http://<GPU机器IP>:9880/health
    http://<GPU机器IP>:9881/health
  能看到 {"ok": true, ...} 就说明通了。

六、性能预期（RTX 3060）
  RVC 转换 4.9 分钟歌：约 20 秒（CPU 需 3.8 分钟）
  VoxCPM2 生成 13 秒音频：约 10 秒（CPU 需 220 秒）
  UVR5 分离：约 15 秒（CPU 需 130 秒）
  RVC 训练音色：30~60 分钟（CPU 无法训练）

七、目录结构
  tools\\               服务脚本
  models\\rvc\\          RVC 音色（xiaoxiao.pth）
  models\\uvr\\          UVR 分离模型
  src\\rvc-src\\         RVC 官方源码 + rmvpe + hubert_base
"""
        zf.writestr("README.txt", readme.encode("utf-8"))
        total += 1

    size = os.path.getsize(out)
    log("完成：%d 个文件，%.2f GB" % (total, size / 1024 / 1024 / 1024))
    log("路径：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
