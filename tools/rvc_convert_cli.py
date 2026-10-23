# -*- coding: utf-8 -*-
r"""rvc_convert_cli.py — 单个音频文件的 RVC 换音色（命令行）

用 RVC 官方 VC 流程（与 rvc_server.py 一致）。

用法：
  E:\AI\dafeiyu-voice\engines\rvc311\Scripts\python.exe rvc_convert_cli.py <输入音频> <输出wav> \
      [--pitch 0] [--speaker 0]

依赖：
  E:\AI\dafeiyu-voice\src\rvc-src（官方源码 + rmvpe + hubert_base）
  E:\AI\dafeiyu-voice\models\rvc\xiaoxiao.pth（音色）
"""
import argparse
import os
import sys

RVC_SRC = os.environ.get("RVC_SRC", r"E:\AI\dafeiyu-voice\src\rvc-src")
MODEL_PATH = os.environ.get(
    "RVC_MODEL", r"E:\AI\dafeiyu-voice\models\rvc\xiaoxiao.pth")


def log(msg):
    """打印日志（兼容 GBK 控制台：编码失败时替换字符，绝不因此崩溃）。"""
    try:
        print("[rvc-cli] %s" % msg, flush=True)
    except UnicodeEncodeError:
        import sys as _s
        try:
            _s.stdout.buffer.write(
                ("[rvc-cli] %s\n" % msg).encode("utf-8", "replace"))
            _s.stdout.buffer.flush()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="输入音频文件")
    ap.add_argument("output", help="输出 wav 路径")
    ap.add_argument("--pitch", type=int, default=0,
                    help="变调半音数（男声转女声常用 +12）")
    ap.add_argument("--speaker", type=int, default=0, help="说话人 ID")
    ap.add_argument("--f0-method", default="rmvpe", choices=["rmvpe", "pm"])
    ap.add_argument("--rms-mix", type=float, default=0.25)
    ap.add_argument("--protect", type=float, default=0.33)
    args = ap.parse_args()

    if not os.path.exists(args.input):
        log("找不到输入: %s" % args.input)
        return 1

    # 官方代码需要工作目录 + weight_root 指向模型目录
    os.chdir(RVC_SRC)
    sys.path.insert(0, RVC_SRC)
    os.environ["weight_root"] = os.path.dirname(os.path.abspath(MODEL_PATH))
    os.environ.setdefault("index_root", os.path.join(RVC_SRC, "logs"))
    os.environ.setdefault("outside_index_root",
                          os.path.join(RVC_SRC, "assets", "indices"))
    os.environ.setdefault("rmvpe_root",
                          os.path.join(RVC_SRC, "assets", "rmvpe"))

    log("加载官方 RVC…")
    # Config() 内部用 argparse 解析 sys.argv，会吞掉本脚本的参数
    # 所以在导入/实例化前先清空 argv
    _saved_argv = sys.argv[:]
    sys.argv = [sys.argv[0]]
    try:
        from configs.config import Config
        from infer.vc.modules import VC

        cfg = Config()
    finally:
        sys.argv = _saved_argv
    log("device=%s dtype=%s" % (cfg.device, cfg.dtype))
    vc = VC(cfg)
    model_name = os.path.basename(MODEL_PATH)
    log("音色模型: %s（weight_root=%s）"
        % (model_name, os.environ["weight_root"]))
    vc.get_vc(model_name)

    log("转换: %s（pitch=%+d, f0=%s）"
        % (os.path.basename(args.input), args.pitch, args.f0_method))
    status, result = vc.vc_single(
        args.speaker,
        args.input,
        args.pitch,
        args.f0_method,
        "",                 # 不用特征索引
        0.0,
        0,                  # 不重采样
        args.rms_mix,
        args.protect,
    )
    if not result or result[0] is None or result[1] is None:
        log("转换失败: %s" % status)
        return 1

    sr, audio = result[0], result[1]
    import numpy as np
    import wave
    a = np.asarray(audio, dtype=np.float32)
    peak = np.abs(a).max()
    if peak > 0:
        a = a / peak * 0.95
    pcm = (a * 32767).astype(np.int16)
    out = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    log("✓ 完成: %s（%d Hz, %.1f 秒）"
        % (out, sr, len(a) / float(sr)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
