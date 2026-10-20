# -*- coding: utf-8 -*-
"""convert_all.py —— 把 dsh-pet 素材批量转成帧序列（保持原始时长）。

用法：python tools/convert_all.py <dsh-pet 根目录>

关键：原视频是 10.04 秒 / 24fps。要保持原速，必须
    帧数 / fps ≈ 10.04
本脚本用 12fps 抽帧（约 120 帧），时长仍是 10 秒，播放流畅、体积可接受。
转换后自动量化 PNG（256 色）把体积压到约 1/4。
"""

import os
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
MAKE = os.path.join(APP, "tools", "make_frames.py")

# 动作名 -> dsh-pet 素材名
MAP = [
    ("idle_breath", "待机呼吸休闲"),
    ("idle_stretch", "超大伸懒腰"),
    ("idle_hum", "悠闲哼歌"),
    ("idle_fan", "摇扇纳凉"),
    ("idle_rubeye", "照镜子"),
    ("idle_shy2", "女仆屈膝礼仪"),
    ("idle_yawn", "哈欠连天"),
    ("idle_bubble", "鲸鱼吐泡泡特效"),
    ("idle_play", "原地专心玩魔方"),
    ("idle_360", "小幅度原地360度旋转展示"),
    ("idle_whale", "蓝鲸现世"),
    ("idle_dance", "可爱宅舞"),
    ("idle_music", "小提琴演奏"),
    ("idle_flute", "吹笛子"),
    ("idle_code", "写代码"),
    ("idle_brush", "晨间刷牙"),
    ("idle_lie", "原地小憩沉眠"),
    ("idle_sleep", "原地小憩沉眠"),
    ("walk", "螃蟹走路"),
    ("hop", "原地漂浮踏步"),
    ("drag", "被鼠标拖拽悬空反馈"),
    ("shy", "点击回应-害羞惊讶"),
    ("tickle", "点击回应-挠痒咯咯笑"),
    ("fx_newyear2", "写福字"),
    ("fx_dragon", "舞狮头"),
    ("fx_midautumn", "中秋赏月吃月饼"),
    ("fx_halloween", "讨糖南瓜灯"),
    ("fx_winter", "堆雪人"),
    ("fx_chrysanth", "插茱萸赏菊"),
    ("fx_river", "放河灯"),
]


def quantize_dir(d):
    """PNG 量化到 256 色，体积降到约 1/4，alpha 保留。"""
    from PIL import Image
    n = 0
    for f in os.listdir(d):
        if not f.endswith(".png"):
            continue
        fp = os.path.join(d, f)
        im = Image.open(fp).convert("RGBA")
        q = im.quantize(colors=256, method=Image.Quantize.FASTOCTREE,
                        dither=Image.Dither.NONE)
        q.save(fp, "PNG", optimize=True)
        n += 1
    return n


def main(argv):
    if len(argv) < 2:
        print("用法: python tools/convert_all.py <dsh-pet 根目录>")
        return 2
    root = argv[1]
    webm = os.path.join(root, "assets", "webm")
    if not os.path.isdir(webm):
        print("找不到素材目录: %s" % webm)
        return 2

    anim_root = os.path.join(APP, "sprites", "anim")
    ok = 0
    fail = []
    for act, src_name in MAP:
        src = os.path.join(webm, src_name + ".webm")
        if not os.path.isfile(src):
            fail.append("%s（缺素材 %s）" % (act, src_name))
            continue
        out = os.path.join(anim_root, act)
        if os.path.isdir(out):
            for f in os.listdir(out):
                if f.endswith(".png"):
                    try:
                        os.remove(os.path.join(out, f))
                    except OSError:
                        pass
        cmd = [PY, MAKE, src, act, "--fps", "12", "--margin", "6", "--quiet"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isdir(out):
            fail.append("%s（转换失败：%s）" % (act, (r.stderr or "")[:120]))
            continue
        n = quantize_dir(out)
        size = sum(os.path.getsize(os.path.join(out, f))
                   for f in os.listdir(out) if f.endswith(".png"))
        print("  %-14s %3d 帧  %5.1f MB  原速 10s" % (act, n, size / 1024.0 / 1024.0))
        ok += 1

    print()
    print("完成 %d / %d" % (ok, len(MAP)))
    if fail:
        print("失败:")
        for x in fail:
            print("  - " + x)
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
