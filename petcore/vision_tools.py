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


# ===== 长截图 OCR =====

def _find_cut_rows(img, start, end, band=60):
    """在 [start, end) 里找「低内容」的横线位置（整行接近纯色）。

    返回候选 y 列表，按「越接近纯色越靠前」排序。
    """
    try:
        from PIL import Image
        import numpy as np
    except Exception:
        return []
    try:
        w, h = img.size
        y0 = max(0, int(start))
        y1 = min(h, int(end))
        if y1 - y0 < 4:
            return []
        strip = img.crop((0, y0, w, y1)).convert("L")
        arr = np.asarray(strip, dtype=np.uint8)
        # 每行的标准差：越小越「平」
        std = arr.std(axis=1)
        order = np.argsort(std)  # 从小到大
        out = []
        for idx in order[:max(1, (y1 - y0) // 20)]:
            y = int(y0 + idx)
            if y <= y0 or y >= y1:
                continue
            out.append((float(std[idx]), y))
        out.sort()
        return [y for _s, y in out[:8]]
    except Exception:
        return []


def long_screenshot_ocr(img, chunk_h=1200, overlap=100, max_chars=4000,
                        ocr_func=None):
    """长截图分块 OCR。

    img       PIL.Image
    chunk_h   每块高度（像素）
    overlap   相邻块重叠高度，避免切断文字
    ocr_func  外部 OCR 函数，签名 func(img, max_chars=...) -> str
              默认用 screen_vision.run_ocr

    返回 {"markdown": 合并后的文本, "chunks": [{"box":[...], "chars":N}, ...],
          "count": 块数}
    """
    try:
        from PIL import Image
    except Exception as ex:
        _log("[视觉] PIL 不可用: %s" % ex, logging.WARNING)
        return {"markdown": "", "chunks": [], "count": 0}

    if ocr_func is None:
        try:
            from screen_vision import run_ocr as ocr_func  # noqa: F811
        except Exception as ex:
            _log("[视觉] OCR 不可用: %s" % ex, logging.WARNING)
            return {"markdown": "", "chunks": [], "count": 0}

    try:
        im = img.convert("RGB")
        w, h = im.size
        if h <= chunk_h:
            # 不够长，直接一次
            txt = ""
            try:
                txt = ocr_func(im, max_chars=max_chars) or ""
            except Exception as ex:
                _log("[视觉] OCR 失败: %s" % ex, logging.DEBUG)
            return {"markdown": txt.strip(),
                    "chunks": [{"box": [0, 0, w, h], "chars": len(txt)}],
                    "count": 1}

        chunks = []
        texts = []
        y = 0
        guard = 0
        while y < h and guard < 200:
            guard += 1
            end = min(h, y + chunk_h)
            # 尽量在低内容行切开
            if end < h:
                cands = _find_cut_rows(im, end - 80, min(h, end + 80))
                if cands:
                    end = cands[0]
                    if end - y < chunk_h // 2:
                        end = min(h, y + chunk_h)
            box = (0, y, w, end)
            piece = im.crop(box)
            t = ""
            try:
                t = ocr_func(piece, max_chars=max_chars) or ""
            except Exception as ex:
                _log("[视觉] 分块 OCR 失败(y=%d): %s" % (y, ex), logging.DEBUG)
            chunks.append({"box": [0, int(y), int(w), int(end)],
                           "chars": len(t)})
            texts.append(t.strip())
            if end >= h:
                break
            # 下一块回退 overlap，避免切断
            y = max(y + 1, end - overlap)

        # 合并：去掉相邻块重复的尾部/头部行
        merged = []
        prev_lines = []
        for t in texts:
            lines = [ln for ln in t.split(chr(10)) if ln.strip()]
            if not lines:
                continue
            # 去掉与上一块尾部重复的行
            skip = 0
            for k in range(min(len(prev_lines), len(lines)), 0, -1):
                if prev_lines[-k:] == lines[:k]:
                    skip = k
                    break
            merged.extend(lines[skip:])
            prev_lines = lines

        md = chr(10).join(merged)
        if len(md) > max_chars:
            md = md[:max_chars] + "…"
        return {"markdown": md, "chunks": chunks, "count": len(chunks)}
    except Exception as ex:
        _log("[视觉] 长截图 OCR 失败: %s: %s"
             % (type(ex).__name__, ex), logging.WARNING)
        return {"markdown": "", "chunks": [], "count": 0}


# ===== 前景抠图 =====

def extract_foreground(img, tolerance=40, out_path=None, max_side=512):
    """把主体从纯色背景里抠出来，输出透明 PNG。

    做法：取四边中点颜色作为背景色，从图像四边做 BFS 泛洪，
    色距 <= tolerance 的相邻像素标记为背景（alpha=0）。

    返回 {"path": 输出路径或 None, "bg": (r,g,b), "removed_ratio": 0.0~1.0}
    """
    try:
        from PIL import Image
        import numpy as np
    except Exception as ex:
        _log("[视觉] 抠图依赖缺失: %s" % ex, logging.WARNING)
        return {"path": None, "bg": None, "removed_ratio": 0.0}

    try:
        im = img.convert("RGB")
        # 缩到 max_side 加速
        scale = max(im.width, im.height) / float(max_side)
        small = im
        if scale > 1:
            small = im.resize((max(1, int(im.width / scale)),
                               max(1, int(im.height / scale))),
                              Image.Resampling.LANCZOS)
        arr = np.asarray(small, dtype=np.int16)
        h, w = arr.shape[0], arr.shape[1]

        # 四边中点取样，取出现次数最多的作为背景色
        samples = [arr[0, w // 2], arr[h - 1, w // 2],
                   arr[h // 2, 0], arr[h // 2, w - 1]]
        bg = tuple(int(v) for v in samples[0])
        for s in samples[1:]:
            d0 = sum(abs(int(s[i]) - bg[i]) for i in range(3))
            d1 = sum(abs(int(s[i]) - bg[i]) for i in range(3))
            if d0 < d1:
                bg = tuple(int(v) for v in s)

        tol = max(1, int(tolerance))
        # 与背景色的曼哈顿距离
        dist = (np.abs(arr - np.array(bg, dtype=np.int16)).sum(axis=2))
        close = dist <= tol

        # 从四边 BFS 泛洪（只穿过 close 像素）
        from collections import deque
        visited = np.zeros((h, w), dtype=bool)
        dq = deque()
        for x in range(w):
            for y in (0, h - 1):
                if close[y, x] and not visited[y, x]:
                    visited[y, x] = True
                    dq.append((y, x))
        for y in range(h):
            for x in (0, w - 1):
                if close[y, x] and not visited[y, x]:
                    visited[y, x] = True
                    dq.append((y, x))
        while dq:
            y, x = dq.popleft()
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx] \
                        and close[ny, nx]:
                    visited[ny, nx] = True
                    dq.append((ny, nx))

        removed = float(visited.sum()) / float(h * w or 1)
        alpha = np.where(visited, 0, 255).astype(np.uint8)
        rgba = np.dstack([np.asarray(small, dtype=np.uint8), alpha])
        out = Image.fromarray(rgba, "RGBA")

        # 恢复到原尺寸（保持透明）
        if scale > 1:
            out = out.resize((im.width, im.height), Image.Resampling.LANCZOS)

        if out_path:
            try:
                d = os.path.dirname(os.path.abspath(out_path))
                if d and not os.path.isdir(d):
                    os.makedirs(d, exist_ok=True)
                out.save(out_path, "PNG")
            except Exception as ex:
                _log("[视觉] 保存抠图失败: %s: %s"
                     % (type(ex).__name__, ex), logging.WARNING)
                out_path = None
        return {"path": out_path, "bg": bg, "removed_ratio": removed}
    except Exception as ex:
        _log("[视觉] 抠图失败: %s: %s"
             % (type(ex).__name__, ex), logging.WARNING)
        return {"path": None, "bg": None, "removed_ratio": 0.0}

