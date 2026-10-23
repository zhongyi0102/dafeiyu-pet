# -*- coding: utf-8 -*-
r"""_separate.py — UVR5 人声分离（命令行，供 cover.py 调用）

用法：python _separate.py <音频文件> <输出目录>
"""
import os
import sys


def main():
    if len(sys.argv) < 3:
        print("用法: _separate.py <音频> <输出目录>", file=sys.stderr)
        return 2
    src = sys.argv[1]
    out_dir = sys.argv[2]
    if not os.path.exists(src):
        print("找不到文件: %s" % src, file=sys.stderr)
        return 1
    os.makedirs(out_dir, exist_ok=True)

    model_dir = r"E:\AI\dafeiyu-voice\models\uvr"
    model = "UVR-MDX-NET-Inst_HQ_3.onnx"

    try:
        from audio_separator.separator import Separator
    except Exception as ex:
        print("audio-separator 未安装: %s" % ex, file=sys.stderr)
        return 1

    sep = Separator(model_file_dir=model_dir, output_dir=out_dir,
                    output_format="wav")
    sep.load_model(model_filename=model)
    outs = sep.separate(src)
    for o in outs:
        print(os.path.basename(o))
    return 0


if __name__ == "__main__":
    sys.exit(main())
