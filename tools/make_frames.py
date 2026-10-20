# -*- coding: utf-8 -*-
"""make_frames.py —— 把 AI 生成的视频处理成桌宠用的帧序列。

流程：
    视频 → ffmpeg 抽帧(fps) → 抠背景(chromakey / rembg) → 去绿边+羽化+预乘alpha
         → 自动裁剪(并集+边距) → 脚底线对齐 → 循环处理(首尾淡接) → 输出 png + 预览 GIF

输出：sprites/anim/<动作名>/0001.png ...

用法示例：
    python tools/make_frames.py dance.mp4 dance
    python tools/make_frames.py dance.mp4 dance --fps 20 --key-color "#00ff00"
    python tools/make_frames.py dance.mp4 dance --rembg --loop-fix
    python tools/make_frames.py --check          # 只检查依赖

依赖：ffmpeg（需在 PATH，或用 --ffmpeg 指定）；Pillow、numpy（在 .venv 里）。
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import numpy as np
    from PIL import Image, ImageSequence
except ImportError as _ex:  # pragma: no cover
    sys.stderr.write(
        "缺少依赖：%s\n请先运行：\n"
        "    .venv\\Scripts\\python.exe -m pip install Pillow numpy\n" % _ex)
    raise SystemExit(2)


APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANIM_DIR = os.path.join(APP_DIR, "sprites", "anim")


# ============================ 依赖检查 ============================

def find_ffmpeg(explicit=None):
    """返回 ffmpeg 可执行文件路径；找不到返回 None。"""
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        return None
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    # Windows 常见安装位置兜底
    for cand in (
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        os.path.join(APP_DIR, "ffmpeg", "bin", "ffmpeg.exe"),
    ):
        if os.path.isfile(cand):
            return cand
    return None


def check_deps(explicit_ffmpeg=None, need_rembg=False, quiet=False):
    """检查依赖，返回 (ok, ffmpeg_path)。缺失时打印明确提示。"""
    ok = True
    ff = find_ffmpeg(explicit_ffmpeg)
    if not ff:
        ok = False
        sys.stderr.write(
            "\n[缺少 ffmpeg]\n"
            "  make_frames.py 需要 ffmpeg 来抽帧。请任选一种方式安装：\n"
            "    1) winget install Gyan.FFmpeg\n"
            "    2) 下载 https://www.gyan.dev/ffmpeg/builds/ 解压后把 bin 目录加入 PATH\n"
            "    3) 把 ffmpeg.exe 放到项目下的 ffmpeg/bin/ 里\n"
            "  已装好但不在 PATH 时，可用 --ffmpeg \"C:\\path\\to\\ffmpeg.exe\" 指定。\n"
            "  验证：ffmpeg -version\n\n")
    elif not quiet:
        try:
            out = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=10)
            first = (out.stdout or "").splitlines()[0] if out.stdout else "?"
            print("  ffmpeg : %s\n           %s" % (ff, first))
        except Exception:
            print("  ffmpeg : %s" % ff)
    if not quiet:
        print("  Pillow : %s" % getattr(Image, "__version__", "?"))
        print("  numpy  : %s" % getattr(np, "__version__", "?"))
        if need_rembg:
            try:
                import rembg  # noqa: F401
                print("  rembg  : 已安装")
            except ImportError:
                ok = False
                sys.stderr.write(
                    "\n[缺少 rembg]\n  用了 --rembg 但没装：\n"
                    "    .venv\\Scripts\\python.exe -m pip install rembg onnxruntime\n\n")
    return ok, ff


# ============================ ffmpeg 抽帧 ============================

def probe_video(ff, path):
    """用 ffprobe 读时长/尺寸/帧率（没有 ffprobe 就返回 None）。"""
    probe = os.path.join(os.path.dirname(ff), "ffprobe.exe" if os.name == "nt" else "ffprobe")
    if not os.path.isfile(probe):
        probe = shutil.which("ffprobe")
    if not probe:
        return None
    try:
        cmd = [probe, "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height,r_frame_rate,duration",
               "-show_entries", "format=duration", "-of", "json", path]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
        st = (data.get("streams") or [{}])[0]
        dur = st.get("duration") or (data.get("format") or {}).get("duration") or 0
        fr = st.get("r_frame_rate") or "0/1"
        try:
            num, den = fr.split("/")
            fps = float(num) / float(den or 1)
        except Exception:
            fps = 0.0
        return {"w": st.get("width"), "h": st.get("height"),
                "duration": float(dur or 0), "fps": fps}
    except Exception:
        return None


def has_alpha_video(video):
    """webm/mov 里可能带 alpha（VP9/ProRes 4444），需要特殊解码才不丢透明。"""
    ext = os.path.splitext(video)[1].lower()
    return ext in (".webm", ".mov", ".mkv")


def extract_frames(ff, video, out_dir, fps=15, start=None, end=None, quiet=False):
    """用 ffmpeg 按 fps 抽成 png 序列，返回文件列表（已排序）。

    带 alpha 的视频（webm VP9 / mov）需要显式指定解码器，否则 alpha 会被丢掉、
    背景变成纯黑。这里自动判断并加 `-c:v libvpx-vp9`。
    """
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, "%05d.png")
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", str(start)]
    if end is not None:
        cmd += ["-to", str(end)]
    if has_alpha_video(video):
        cmd += ["-c:v", "libvpx-vp9"]      # 保留 VP9 的 alpha 平面
    cmd += ["-i", video, "-vf", "fps=%s" % fps, "-vsync", "0",
            "-pix_fmt", "rgba", pattern]
    if not quiet:
        print("  抽帧: %s" % " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError("ffmpeg 抽帧失败：\n%s" % (res.stderr or "").strip()[:800])
    files = sorted(
        os.path.join(out_dir, n) for n in os.listdir(out_dir) if n.lower().endswith(".png"))
    if not files:
        raise RuntimeError("ffmpeg 没有产出任何帧（视频为空或参数不对）")
    return files


# ============================ 抠图 ============================

def _hex_to_rgb(text):
    t = (text or "").strip().lstrip("#")
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    if len(t) != 6:
        raise ValueError("颜色格式应为 #RRGGBB：%s" % text)
    return tuple(int(t[i:i + 2], 16) for i in (0, 2, 4))


def chroma_key(img, key=(0, 255, 0), similarity=0.35, blend=0.10):
    """在 RGB 空间抠纯色背景，返回带 alpha 的图。

    similarity 越大抠得越狠（0~1）；blend 控制边缘过渡带宽度。
    """
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    key = np.array(key, dtype=np.float32)
    # 与键色的距离（归一化到 0~1）
    dist = np.sqrt(((arr - key) ** 2).sum(axis=2)) / 441.67
    thr = max(1e-4, float(similarity))
    alpha = np.clip((dist - thr) / max(1e-4, float(blend)), 0.0, 1.0)
    # 完全透明的像素把颜色也清掉，避免后续缩放带出绿边
    alpha = alpha * 255.0
    out = np.dstack([arr, alpha]).astype(np.uint8)
    res = Image.fromarray(out, "RGBA")
    return res


def rembg_key(img, session=None):
    """备用方案：用 rembg 逐帧抠图（需要已安装 rembg）。"""
    try:
        from rembg import remove
    except ImportError:
        raise RuntimeError("用了 --rembg 但没安装：pip install rembg onnxruntime")
    return remove(img.convert("RGBA"), session=session)


def despill(img, strength=1.0):
    """去绿边：绿色明显高于红蓝时把绿压到红蓝的较大值。"""
    arr = np.asarray(img.convert("RGBA"), dtype=np.float32)
    r, g, b, a = arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3]
    limit = np.maximum(r, b)
    mask = g > limit
    if mask.any():
        g2 = np.where(mask, limit + (g - limit) * (1.0 - max(0.0, min(1.0, strength))), g)
        arr[..., 1] = g2
    arr[..., 3] = a
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")


def feather(img, radius=1.2):
    """边缘羽化：对 alpha 做一次轻量模糊（用 Pillow 自带高斯）。"""
    if radius <= 0:
        return img
    from PIL import ImageFilter
    r, g, b, a = img.split()
    a = a.filter(ImageFilter.GaussianBlur(radius))
    return Image.merge("RGBA", (r, g, b, a))


def premultiply(img):
    """预乘 alpha：RGB *= A，避免缩放/合成时边缘发黑或发白。"""
    arr = np.asarray(img.convert("RGBA"), dtype=np.float32)
    a = arr[..., 3:4] / 255.0
    arr[..., :3] = arr[..., :3] * a
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")


def has_usable_alpha(img):
    """这一帧是否已经带有效的 alpha（比如从 webm 直接解码出来的）。"""
    if img.mode != "RGBA":
        return False
    a = np.asarray(img.getchannel("A"), dtype=np.uint8)
    total = a.size
    if total == 0:
        return False
    transparent = int((a < 8).sum())
    opaque = int((a > 200).sum())
    # 有相当比例透明、也有相当比例不透明，才算"带有效 alpha"
    return transparent > total * 0.05 and opaque > total * 0.005


def process_frame(img, args, rembg_session=None):
    """单帧完整处理（抠图→去边→羽化→预乘），返回 RGBA。"""
    # 视频自带 alpha（webm VP9 等）时跳过抠图，避免把已经透明的背景再抠一次
    if getattr(args, "_alpha_ready", False) or has_usable_alpha(img):
        out = img.convert("RGBA")
        if args.despill > 0:
            out = despill(out, args.despill)
        if args.feather > 0:
            out = feather(out, args.feather)
        return premultiply(out)
    if args.rembg:
        out = rembg_key(img, rembg_session)
    else:
        out = chroma_key(img, _hex_to_rgb(args.key_color),
                         args.similarity, args.blend)
    if args.despill > 0:
        out = despill(out, args.despill)
    if args.feather > 0:
        out = feather(out, args.feather)
    out = premultiply(out)
    return out


# ============================ 裁剪 / 对齐 ============================

def alpha_bbox(img, threshold=8):
    """返回不透明区域 bbox（x0,y0,x1,y1），全透明返回 None。"""
    a = np.asarray(img.getchannel("A"), dtype=np.uint8)
    ys, xs = np.where(a > threshold)
    if len(xs) == 0:
        return None
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def unify_frames(frames, margin=8, feet_align=True, verbose=True):
    """所有帧统一到同一画布：取不透明并集 + 边距；可选脚底线对齐。"""
    bboxes = [alpha_bbox(f) for f in frames]
    valid = [b for b in bboxes if b is not None]
    if not valid:
        raise RuntimeError("所有帧都是全透明的，检查抠图参数（相似度是否太高）")
    x0 = min(b[0] for b in valid)
    y0 = min(b[1] for b in valid)
    x1 = max(b[2] for b in valid)
    y1 = max(b[3] for b in valid)

    if feet_align:
        # 脚底线对齐：每帧最低不透明像素统一到同一条基线
        bottoms = [b[3] for b in valid]
        baseline = max(bottoms)
        # 让所有帧的底边都落在 baseline，画布底边留 margin
        top = y0
        bottom = baseline
        cw = (x1 - x0) + margin * 2
        ch = (bottom - top) + margin * 2
        out = []
        for f, b in zip(frames, bboxes):
            canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            if b is None:
                out.append(canvas)
                continue
            # 参考 dsh-pet：不做逐帧脚底强制对齐（素材镜头运动会变成上下跳）
            dy = 0
            # 先裁到该帧自己的不透明区域，再按相对位置贴：
            # 直接贴整帧会把 bbox 坐标和贴纸坐标叠两次，内容被推出画布
            crop = f.crop(b)
            canvas.paste(crop, (b[0] - x0 + margin, b[1] + dy - y0 + margin),
                         crop.getchannel("A"))
            out.append(canvas)
        if verbose:
            print("  对齐: 画布 %dx%d，脚底线 y=%d（并集 bbox=%s）"
                  % (cw, ch, ch - margin, (x0, y0, x1, y1)))
        return out
    else:
        cw = (x1 - x0) + margin * 2
        ch = (y1 - y0) + margin * 2
        out = []
        for f in frames:
            canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            canvas.paste(f, (-x0 + margin, -y0 + margin), f.getchannel("A"))
            out.append(canvas)
        if verbose:
            print("  裁剪: 画布 %dx%d（并集 bbox=%s）" % (cw, ch, (x0, y0, x1, y1)))
        return out


# ============================ 循环处理 ============================

def frame_diff(a, b):
    """两帧平均像素差（0~255），用于检测首尾差异。"""
    aa = np.asarray(a.convert("RGBA"), dtype=np.float32)
    bb = np.asarray(b.convert("RGBA"), dtype=np.float32)
    return float(np.abs(aa - bb).mean())


def fix_loop(frames, threshold=6.0, max_drop_ratio=0.25, verbose=True):
    """首尾淡接：若首尾差异大，裁掉尾部若干帧让首尾接近。

    返回 (frames, 说明文字)。裁掉的帧数不超过总帧数的 max_drop_ratio。
    """
    n = len(frames)
    if n < 3:
        return frames, "帧数太少，跳过循环处理"
    d0 = frame_diff(frames[0], frames[-1])
    if verbose:
        print("  首尾差异: %.2f（阈值 %.1f）" % (d0, threshold))
    if d0 <= threshold:
        return frames, "首尾已经很接近（差异 %.2f），无需处理" % d0

    best = None
    best_d = d0
    max_drop = max(1, int(n * max_drop_ratio))
    for drop in range(1, max_drop + 1):
        d = frame_diff(frames[0], frames[n - 1 - drop])
        if d < best_d:
            best_d = d
            best = drop
        if d <= threshold:
            best = drop
            best_d = d
            break
    if best is None:
        return frames, "裁尾也没能明显改善（最佳差异 %.2f），保留原样" % best_d
    kept = frames[:n - best]
    msg = "裁掉尾部 %d 帧，首尾差异 %.2f → %.2f" % (best, d0, best_d)
    if verbose:
        print("  循环处理: " + msg)
    return kept, msg


# ============================ 预览 GIF ============================

def save_preview_gif(frames, out_path, fps=15, bg=(255, 255, 255)):
    """生成带底色的预览 GIF（白底 / 黑底各一份）。"""
    if not frames:
        return
    flat = []
    for f in frames:
        base = Image.new("RGBA", f.size, tuple(bg) + (255,))
        base.alpha_composite(f)
        flat.append(base.convert("RGB"))
    dur = max(20, int(round(1000.0 / max(1.0, float(fps)))))
    flat[0].save(out_path, save_all=True, append_images=flat[1:],
                 duration=dur, loop=0, optimize=False, disposal=2)


# ============================ 主流程 ============================

def build_parser():
    ap = argparse.ArgumentParser(
        description="把视频转成桌宠帧序列（sprites/anim/<动作名>/0001.png ...）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python tools/make_frames.py dance.mp4 dance\n"
               "  python tools/make_frames.py dance.mp4 dance --fps 20 --similarity 0.4\n"
               "  python tools/make_frames.py dance.mp4 dance --rembg --loop-fix\n")
    ap.add_argument("video", nargs="?", help="输入视频文件")
    ap.add_argument("action", nargs="?", help="动作名（输出目录名）")
    ap.add_argument("--out", default=None, help="输出根目录（默认 sprites/anim）")
    ap.add_argument("--fps", type=float, default=15.0, help="抽帧帧率（默认 15）")
    ap.add_argument("--start", default=None, help="从第几秒开始（可选）")
    ap.add_argument("--end", default=None, help="到第几秒结束（可选）")
    ap.add_argument("--key-color", default="#00ff00", help="背景键色（默认 #00ff00）")
    ap.add_argument("--similarity", type=float, default=0.35,
                    help="抠色相似度 0~1，越大抠得越狠（默认 0.35）")
    ap.add_argument("--blend", type=float, default=0.10,
                    help="边缘过渡带 0~1（默认 0.10）")
    ap.add_argument("--despill", type=float, default=1.0,
                    help="去绿边强度 0~1，0 关闭（默认 1.0）")
    ap.add_argument("--feather", type=float, default=1.2,
                    help="边缘羽化半径，0 关闭（默认 1.2）")
    ap.add_argument("--margin", type=int, default=8, help="画布边距像素（默认 8）")
    ap.add_argument("--no-feet-align", action="store_true", help="关闭脚底线对齐")
    ap.add_argument("--rembg", action="store_true", help="用 rembg 逐帧抠图（备用方案）")
    ap.add_argument("--loop-fix", action="store_true", help="首尾淡接：裁掉尾部多余帧")
    ap.add_argument("--loop-threshold", type=float, default=6.0,
                    help="首尾差异阈值（默认 6.0）")
    ap.add_argument("--max-drop", type=float, default=0.25,
                    help="循环处理最多裁掉总帧数的比例（默认 0.25）")
    ap.add_argument("--no-gif", action="store_true", help="不生成预览 GIF")
    ap.add_argument("--keep-raw", action="store_true", help="保留抽帧的原始 png")
    ap.add_argument("--every", type=int, default=1,
                    help="每隔 N 帧取 1（降采样，减小体积；默认 1=全取）")
    ap.add_argument("--max-frames", type=int, default=0,
                    help="最多保留多少帧（0=不限）；超出时按 --every 自动加大步长")
    ap.add_argument("--ffmpeg", default=None, help="ffmpeg.exe 路径（不在 PATH 时）")
    ap.add_argument("--check", action="store_true", help="只检查依赖")
    ap.add_argument("--quiet", action="store_true", help="少打印")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)

    print("== 依赖检查 ==")
    ok, ff = check_deps(args.ffmpeg, need_rembg=args.rembg, quiet=False)
    if args.check:
        print("\n结论：%s" % ("依赖齐全，可以开始" if ok else "有依赖缺失，见上方提示"))
        return 0 if ok else 1
    if not ok:
        return 2

    if not args.video or not args.action:
        print("\n用法：python tools/make_frames.py <视频> <动作名> [参数]\n"
              "      python tools/make_frames.py --check")
        return 2
    if not os.path.isfile(args.video):
        print("[错误] 找不到视频文件：%s" % args.video)
        return 2
    action = re.sub(r"[^\w\-]+", "_", args.action).strip("_")
    if not action:
        print("[错误] 动作名不合法")
        return 2

    root = args.out or ANIM_DIR
    out_dir = os.path.join(root, action)
    info = probe_video(ff, args.video)
    if info and not args.quiet:
        print("\n== 视频信息 ==")
        print("  %sx%s · %.2f fps · %.2f 秒"
              % (info.get("w"), info.get("h"), info.get("fps"), info.get("duration")))
        est = (info.get("duration") or 0) * args.fps
        if est > 240:
            print("  提示：按 %.0f fps 会抽出约 %.0f 帧，可能过多，可调低 --fps" % (args.fps, est))

    raw_dir = tempfile.mkdtemp(prefix="frames_raw_")
    rembg_session = None
    try:
        print("\n== 抽帧 ==")
        raw = extract_frames(ff, args.video, raw_dir, args.fps,
                             args.start, args.end, args.quiet)
        total_raw = len(raw)
        step = max(1, int(args.every or 1))
        if args.max_frames and total_raw > args.max_frames:
            step = max(step, int(math.ceil(total_raw / float(args.max_frames))))
        if step > 1:
            raw = raw[::step]
            print("  共 %d 帧，降采样 step=%d → %d 帧" % (total_raw, step, len(raw)))
        else:
            print("  共 %d 帧" % len(raw))

        if args.rembg:
            try:
                from rembg import new_session
                rembg_session = new_session()
                print("  rembg 会话已创建")
            except Exception as ex:
                print("  [警告] rembg 初始化失败，改用 chromakey：%s" % ex)
                args.rembg = False

        print("\n== 抠图 / 去边 / 预乘 ==")
        frames = []
        for i, path in enumerate(raw):
            img = Image.open(path).convert("RGBA")
            frames.append(process_frame(img, args, rembg_session))
            if not args.quiet and (i + 1) % 20 == 0:
                print("  %d / %d" % (i + 1, len(raw)))
        print("  处理完成 %d 帧" % len(frames))

        print("\n== 裁剪与对齐 ==")
        frames = unify_frames(frames, args.margin,
                              feet_align=not args.no_feet_align, verbose=not args.quiet)

        loop_msg = "未启用"
        if args.loop_fix:
            print("\n== 循环处理 ==")
            # fix_loop 只裁尾部帧，画布尺寸/脚底线已在上一步统一，无需再裁一次
            frames, loop_msg = fix_loop(frames, args.loop_threshold,
                                        args.max_drop, verbose=not args.quiet)

        print("\n== 输出 ==")
        if os.path.isdir(out_dir):
            old = [n for n in os.listdir(out_dir) if n.lower().endswith(".png")]
            if old:
                print("  清理旧帧 %d 张" % len(old))
                for n in old:
                    try:
                        os.remove(os.path.join(out_dir, n))
                    except Exception:
                        pass
        os.makedirs(out_dir, exist_ok=True)
        for i, f in enumerate(frames):
            f.save(os.path.join(out_dir, "%04d.png" % (i + 1)), "PNG")
        print("  已写入 %d 帧 → %s" % (len(frames), out_dir))
        w, h = frames[0].size
        print("  画布尺寸 %dx%d（所有帧一致）" % (w, h))

        if not args.no_gif:
            gif_dir = os.path.join(APP_DIR, "tools", "preview")
            os.makedirs(gif_dir, exist_ok=True)
            g1 = os.path.join(gif_dir, "%s_white.gif" % action)
            g2 = os.path.join(gif_dir, "%s_black.gif" % action)
            save_preview_gif(frames, g1, args.fps, (255, 255, 255))
            save_preview_gif(frames, g2, args.fps, (0, 0, 0))
            print("  预览 GIF：\n    %s\n    %s" % (g1, g2))

        print("\n== 完成 ==")
        print("  动作名：%s" % action)
        print("  帧数：%d · 画布 %dx%d · fps %.1f" % (len(frames), w, h, args.fps))
        print("  循环处理：%s" % loop_msg)
        print("  下一步：在 桌宠.py 的 ACTIONS 里登记（或用同名目录直接生效），"
              "然后在右键「动作」菜单里点播检查")
        return 0
    except Exception as ex:
        print("\n[失败] %s" % ex)
        return 1
    finally:
        if not args.keep_raw:
            shutil.rmtree(raw_dir, ignore_errors=True)
        else:
            print("  原始帧保留在：%s" % raw_dir)


if __name__ == "__main__":
    sys.exit(main())
