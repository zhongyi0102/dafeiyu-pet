# -*- coding: utf-8 -*-
r"""vision_tools.py — 视觉工具箱（Python 本地实现）

对应 dsh-vision-toolkit 的能力，全部用 PIL + numpy 本地实现，
不依赖 Node、不依赖外部服务。

纯本地（0 token）：
  dominant_colors     主色提取
  pixel_diff          像素对比 + 热力图
  long_screenshot_ocr 长截图分块 OCR
  extract_foreground  前景抠图（透明 PNG）

需要视觉模型：
  ground_element      元素定位（返回原图坐标）
  detect_elements     元素清单

坐标统一用原图像素格式 [x1, y1, x2, y2]。
"""
import io
import logging
import os

_logger = logging.getLogger("dafeiyu")


def _log(msg, level=logging.INFO):
    try:
        _logger.log(level, msg)
    except Exception:
        pass


# ===== 主色提取 =====

def dominant_colors(img, region=None, top=8, sample_side=100):
    """提取图片主色。

    img        PIL.Image
    region     (x1,y1,x2,y2) 可选，只统计该区域
    top        返回多少个主色
    sample_side 缩略采样边长（越大越准越慢，100 已足够）

    返回 [{"rgb": (r,g,b), "hex": "#rrggbb", "ratio": 0.42}, ...]
    按占比降序。
    """
    try:
        from PIL import Image
    except Exception as ex:
        _log("[视觉] PIL 不可用: %s" % ex, logging.WARNING)
        return []

    try:
        im = img
        if region:
            x1, y1, x2, y2 = [int(v) for v in region]
            im = im.crop((x1, y1, x2, y2))
        im = im.convert("RGB")
        if im.width <= 0 or im.height <= 0:
            return []

        # 缩略采样，加速
        scale = max(im.width, im.height) / float(sample_side)
        if scale > 1:
            im = im.resize((max(1, int(im.width / scale)),
                            max(1, int(im.height / scale))),
                           Image.Resampling.NEAREST)

        # 量化到固定调色板，避免颜色过于分散
        q = im.quantize(colors=max(2, min(64, top * 6)),
                        method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette() or []
        counts = q.getcolors(maxcolors=100000) or []
        total = sum(c for c, _ in counts) or 1

        out = []
        for cnt, idx in counts:
            base = idx * 3
            if base + 2 >= len(pal):
                continue
            r, g, b = pal[base], pal[base + 1], pal[base + 2]
            out.append({"rgb": (r, g, b),
                        "hex": "#%02x%02x%02x" % (r, g, b),
                        "ratio": cnt / float(total)})
        out.sort(key=lambda x: -x["ratio"])

        # 过滤过于接近的颜色（避免一堆近似灰）
        picked = []
        for c in out:
            if len(picked) >= top:
                break
            if any(_color_dist(c["rgb"], p["rgb"]) < 24 for p in picked):
                continue
            picked.append(c)
        return picked
    except Exception as ex:
        _log("[视觉] 主色提取失败: %s: %s"
             % (type(ex).__name__, ex), logging.WARNING)
        return []


def _color_dist(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


# ===== 像素对比 =====

def pixel_diff(img_a, img_b, threshold=16, grid=8, heatmap_path=None):
    """逐像素对比两张图。

    返回 {
      "ratio": 差异像素占比 0~1,
      "diff_pixels": 差异像素数,
      "total_pixels": 总像素数,
      "regions": [{"box":[x1,y1,x2,y2], "ratio":0.12}, ...],  # 最热的 grid 个区域
      "heatmap": 热力图路径或 None,
    }
    """
    try:
        from PIL import Image, ImageChops
        import numpy as np
    except Exception as ex:
        _log("[视觉] 对比依赖缺失: %s" % ex, logging.WARNING)
        return {"ratio": 0.0, "diff_pixels": 0, "total_pixels": 0,
                "regions": [], "heatmap": None}

    try:
        a = img_a.convert("RGB")
        b = img_b.convert("RGB")
        if b.size != a.size:
            b = b.resize(a.size, Image.Resampling.LANCZOS)

        na = np.asarray(a, dtype=np.int16)
        nb = np.asarray(b, dtype=np.int16)
        delta = np.abs(na - nb).max(axis=2)
        mask = delta > int(threshold)

        total = int(mask.size)
        diff = int(mask.sum())
        ratio = (diff / float(total)) if total else 0.0

        # 8x8 网格统计
        h, w = mask.shape
        regions = []
        gh = max(1, h // grid)
        gw = max(1, w // grid)
        for gy in range(grid):
            for gx in range(grid):
                y1, y2 = gy * gh, min(h, (gy + 1) * gh)
                x1, x2 = gx * gw, min(w, (gx + 1) * gw)
                if y2 <= y1 or x2 <= x1:
                    continue
                cell = mask[y1:y2, x1:x2]
                r = float(cell.sum()) / float(cell.size or 1)
                if r <= 0:
                    continue
                regions.append({"box": [int(x1), int(y1), int(x2), int(y2)],
                                "ratio": r})
        regions.sort(key=lambda x: -x["ratio"])
        regions = regions[:grid]

        # 热力图
        hp = None
        if heatmap_path:
            try:
                rgba = np.zeros((h, w, 4), dtype=np.uint8)
                # 差异处红色，透明度按差值强度
                strength = np.clip(delta, 0, 255).astype(np.uint8)
                rgba[..., 0] = 255
                rgba[..., 3] = np.where(mask, np.maximum(60, strength), 0)
                hm = Image.fromarray(rgba, "RGBA")
                hm.save(heatmap_path, "PNG")
                hp = heatmap_path
            except Exception as ex:
                _log("[视觉] 热力图生成失败: %s: %s"
                     % (type(ex).__name__, ex), logging.DEBUG)

        return {"ratio": ratio, "diff_pixels": diff, "total_pixels": total,
                "regions": regions, "heatmap": hp}
    except Exception as ex:
        _log("[视觉] 像素对比失败: %s: %s"
             % (type(ex).__name__, ex), logging.WARNING)
        return {"ratio": 0.0, "diff_pixels": 0, "total_pixels": 0,
                "regions": [], "heatmap": None}
