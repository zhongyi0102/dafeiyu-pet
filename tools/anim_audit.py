# -*- coding: utf-8 -*-
"""anim_audit.py —— 逐帧动画质量审计（不依赖视觉模型）。

用可量化的指标找出「不协调」，比肉眼更可靠：
  1. 相邻帧差异（jerk）：突变点定位
  2. 速度/加速度曲线：是否平滑，有无顿挫
  3. 脚底线漂移：角色是否"踩不实"
  4. 面积变化：挤压拉伸是否守恒
  5. 质心轨迹：是否有意外抖动
  6. 循环闭合度：首尾是否接得上

用法：python anim_audit.py <帧目录或 png 列表...>
"""

import math
import os
import sys

try:
    import numpy as np
    from PIL import Image
except ImportError:
    sys.stderr.write("需要 Pillow 和 numpy：pip install Pillow numpy\n")
    raise SystemExit(2)


def load_frames(paths):
    out = []
    for p in paths:
        im = Image.open(p).convert("RGBA")
        out.append(np.asarray(im).astype(np.float32))
    return out


def metrics(arr):
    """单帧指标：bbox、面积、质心、脚底线、alpha 质量"""
    a = arr[..., 3]
    ys, xs = np.where(a > 12)
    if len(xs) == 0:
        return None
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    w = a / 255.0
    tot = w.sum()
    cx = float((w * np.arange(a.shape[1])[None, :]).sum() / tot)
    cy = float((w * np.arange(a.shape[0])[:, None]).sum() / tot)
    return {
        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
        "w": x1 - x0 + 1, "h": y1 - y0 + 1,
        "area": float(tot), "cx": cx, "cy": cy,
        "bottom": y1, "top": y0,
        "alpha_mass": float(tot) / max(1.0, float((x1 - x0 + 1) * (y1 - y0 + 1))),
    }


def frame_diff(a, b):
    """两帧归一化差异（0~1），只看 RGB 忽略完全透明的部分"""
    m = np.maximum(a[..., 3:4], b[..., 3:4]) / 255.0
    d = np.abs(a[..., :3] - b[..., :3]) / 255.0
    denom = m.sum()
    if denom <= 0:
        return 0.0
    return float((d * m).sum() / denom)


def audit(paths, name="", fps=15.0, verbose=True):
    frames = load_frames(paths)
    ms = [metrics(f) for f in frames]
    valid = [(i, m) for i, m in enumerate(ms) if m]
    if len(valid) < 2:
        print("  帧数太少或全透明，跳过")
        return None

    n = len(valid)
    idx = [i for i, _ in valid]
    ws = [m["w"] for _, m in valid]
    hs = [m["h"] for _, m in valid]
    bots = [m["bottom"] for _, m in valid]
    cxs = [m["cx"] for _, m in valid]
    cys = [m["cy"] for _, m in valid]
    areas = [m["area"] for _, m in valid]

    diffs = [frame_diff(frames[idx[i]], frames[idx[i + 1]]) for i in range(n - 1)]

    def vel(v):
        return [v[i + 1] - v[i] for i in range(len(v) - 1)]

    def acc(v):
        d = vel(v)
        return [d[i + 1] - d[i] for i in range(len(d) - 1)]

    d_w = vel(ws)
    a_w = acc(ws)
    d_cx = vel(cxs)
    d_cy = vel(cys)

    issues = []

    # 1) 相邻帧差异突变（jerk）
    if diffs:
        mean_d = sum(diffs) / len(diffs)
        for i, d in enumerate(diffs):
            if mean_d > 0 and d > mean_d * 2.6 and d > 0.06:
                issues.append("帧%d→%d 画面突变（差异 %.3f，均值 %.3f）"
                              % (idx[i], idx[i + 1], d, mean_d))

    # 2) 宽度加速度过大（顿挫）
    if a_w:
        mx = max(abs(x) for x in a_w)
        if mx > max(6.0, max(ws) * 0.10):
            i = max(range(len(a_w)), key=lambda k: abs(a_w[k]))
            issues.append("帧%d→%d 宽度加速度过大 %.1f px（顿挫感）"
                          % (idx[i + 1], idx[i + 2], a_w[i]))

    # 3) 宽度方向反转（该单调的地方抖了）
    if len(d_w) >= 3:
        for i in range(1, len(d_w) - 1):
            a, b = d_w[i - 1], d_w[i]
            if a * b < 0 and abs(a) > 3 and abs(b) > 3:
                issues.append("帧%d 宽度方向反转（%.1f → %.1f）"
                              % (idx[i + 1], a, b))

    # 4) 脚底线漂移
    drift = max(bots) - min(bots)
    if drift > 3:
        issues.append("脚底线漂移 %d px（最大 %d，最小 %d）" % (drift, max(bots), min(bots)))

    # 5) 面积突变（挤压拉伸不自然）
    if areas:
        mean_a = sum(areas) / len(areas)
        for i, a in enumerate(areas):
            if mean_a > 0 and abs(a - mean_a) / mean_a > 0.45:
                issues.append("帧%d 面积偏离均值 %.0f%%（挤压过猛）"
                              % (idx[i], abs(a - mean_a) / mean_a * 100))

    # 6) 质心抖动（无意的左右晃）
    if len(d_cx) >= 4:
        sign_flips = sum(1 for i in range(len(d_cx) - 1) if d_cx[i] * d_cx[i + 1] < 0
                         and abs(d_cx[i]) > 1.2 and abs(d_cx[i + 1]) > 1.2)
        if sign_flips > max(2, len(d_cx) // 3):
            issues.append("质心左右抖动 %d 次（疑似不稳定）" % sign_flips)

    # 7) 循环闭合
    loop_diff = frame_diff(frames[idx[0]], frames[idx[-1]])

    # 8) 节奏均匀性（差异序列的方差）
    rhythm = 0.0
    if diffs and sum(diffs) > 0:
        m = sum(diffs) / len(diffs)
        rhythm = math.sqrt(sum((d - m) ** 2 for d in diffs) / len(diffs)) / m

    if verbose:
        label = name or os.path.basename(os.path.dirname(paths[0]))
        print("【%s】%d 帧 · %.0f fps" % (label, n, fps))
        print("  宽度   %s" % ws)
        print("  高度   %s" % hs)
        print("  脚底线 %s" % bots)
        print("  帧差异 %s" % [round(d, 3) for d in diffs])
        print("  面积   %s" % [round(a) for a in areas])
        print("  闭合度 %.3f（越小首尾越接得上）" % loop_diff)
        print("  节奏方差 %.2f（<0.6 较均匀，>1.0 忽快忽慢）" % rhythm)
        if issues:
            print("  ⚠ 问题 %d 处：" % len(issues))
            for it in issues:
                print("     - " + it)
        else:
            print("  ✓ 未发现明显问题")

    return {
        "n": n, "widths": ws, "heights": hs, "bottoms": bots,
        "diffs": diffs, "areas": areas, "loop": loop_diff,
        "rhythm": rhythm, "issues": issues,
    }


def collect(paths):
    """把参数整理成帧列表：目录或单个文件"""
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += sorted(os.path.join(p, f) for f in os.listdir(p)
                          if f.lower().endswith(".png"))
        elif os.path.isfile(p):
            out.append(p)
    return out


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    paths = collect(argv[1:])
    if not paths:
        print("没找到 png 帧")
        return 2
    r = audit(paths)
    return 0 if r else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
