# -*- coding: utf-8 -*-
r"""make_cover.py — AI 翻唱流水线（UVR5 人声分离 + RVC 换音色）

流程：
  1. 输入一首歌（mp3/wav/flac）
  2. audio-separator（UVR 内核）分离出 人声 / 伴奏
  3. 人声走 RVC 换成目标音色
  4. 混音输出成品

用法：
  E:\AI\engines\rvc311\Scripts\python.exe make_cover.py <歌曲文件> [--out 目录]

输出：
  <out>/vocals.wav      分离出的人声
  <out>/instrumental.wav 伴奏
  <out>/converted.wav   换音色后的人声
  <out>/cover.wav       混音成品
"""
import argparse
import os
import subprocess
import sys
import tempfile

RVC_ENV = r"E:\AI\engines\rvc311\Scripts\python.exe"
RVC_MODEL = os.environ.get(
    "RVC_MODEL", r"E:\AI\models\rvc\xiaoxiao.pth")
# UVR 模型：MDX-Net 人声分离（质量好，CPU 可跑）
UVR_MODEL = os.environ.get(
    "UVR_MODEL", "UVR-MDX-NET-Inst_HQ_3.onnx")
UVR_DIR = os.environ.get("UVR_DIR", r"E:\AI\models\uvr")


def log(msg):
    print("[cover] %s" % msg, flush=True)


def separate(input_path, out_dir):
    """用 audio-separator 分离人声/伴奏。返回 (vocals, instrumental)。"""
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(UVR_DIR, exist_ok=True)
    log("分离人声（模型 %s）…" % UVR_MODEL)
    try:
        from audio_separator.separator import Separator
    except Exception as ex:
        raise RuntimeError("audio-separator 未安装: %s" % ex)

    sep = Separator(
        model_file_dir=UVR_DIR,
        output_dir=out_dir,
        output_format="wav",
    )
    # 加载模型（首次会自动下载）
    sep.load_model(model_filename=UVR_MODEL)
    outputs = sep.separate(input_path)
    log("分离完成: %s" % outputs)

    vocals = instrumental = None
    for f in outputs:
        p = os.path.join(out_dir, f) if not os.path.isabs(f) else f
        low = os.path.basename(p).lower()
        if "vocal" in low and "instrument" not in low:
            vocals = p
        elif "instrument" in low or "no_vocal" in low or "karaoke" in low:
            instrumental = p
    if vocals is None:
        # 兜底：取第一个
        vocals = os.path.join(out_dir, outputs[0])
    return vocals, instrumental


def convert_vocal(vocals_path, out_path):
    """用 RVC 官方流程把分离出的人声换成目标音色。"""
    log("RVC 换音色 → %s" % os.path.basename(RVC_MODEL))
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "rvc_convert_cli.py")
    r = subprocess.run(
        [RVC_ENV, "-u", script, vocals_path, out_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("RVC 转换失败:\n%s\n%s"
                           % (r.stdout[-1500:], r.stderr[-1500:]))
    if not os.path.exists(out_path):
        raise RuntimeError("RVC 未产出文件: %s" % out_path)
    log("换音色完成: %s" % out_path)


def mix(converted, instrumental, out_path):
    """混音（无人声伴奏则直接复制人声）。"""
    if not instrumental or not os.path.exists(instrumental):
        log("没有伴奏，直接输出人声")
        import shutil
        shutil.copy(converted, out_path)
        return
    log("混音…")
    try:
        import numpy as np
        import soundfile as sf
        v, sr1 = sf.read(converted, dtype="float32")
        b, sr2 = sf.read(instrumental, dtype="float32")
        if sr1 != sr2:
            import librosa
            b = librosa.resample(b, orig_sr=sr2, target_sr=sr1)
        if v.ndim > 1:
            v = v.mean(axis=1)
        if b.ndim > 1:
            b = b.mean(axis=1)
        n = min(len(v), len(b))
        v, b = v[:n], b[:n]
        # 人声稍大，伴奏压低
        out = v * 0.75 + b * 0.55
        peak = np.abs(out).max()
        if peak > 0:
            out = out / peak * 0.95
        sf.write(out_path, out, sr1)
        log("混音完成: %s" % out_path)
    except Exception as ex:
        log("混音失败（改为只输出人声）: %s" % ex)
        import shutil
        shutil.copy(converted, out_path)


def main():
    ap = argparse.ArgumentParser(description="AI 翻唱：UVR 分离 + RVC 换音色")
    ap.add_argument("input", help="歌曲文件（mp3/wav/flac）")
    ap.add_argument("--out", default="", help="输出目录（默认与输入同目录）")
    args = ap.parse_args()

    src = os.path.abspath(args.input)
    if not os.path.exists(src):
        log("找不到文件: %s" % src)
        return 1
    out_dir = args.out or os.path.join(
        os.path.dirname(src), "cover_" + os.path.splitext(os.path.basename(src))[0])
    os.makedirs(out_dir, exist_ok=True)
    log("输入: %s" % src)
    log("输出: %s" % out_dir)

    vocals, inst = separate(src, out_dir)
    log("人声: %s" % vocals)
    log("伴奏: %s" % (inst or "(无)"))

    converted = os.path.join(out_dir, "converted.wav")
    convert_vocal(vocals, converted)

    final = os.path.join(out_dir, "cover.wav")
    mix(converted, inst, final)
    log("✓ 完成: %s" % final)
    return 0


if __name__ == "__main__":
    sys.exit(main())
