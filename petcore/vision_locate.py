# -*- coding: utf-8 -*-
r"""vision_locate.py — 元素定位与清单（需要视觉模型）

对应 dsh-vision-toolkit 的 vision_ground / vision_detect。

设计要点（借鉴上游踩过的坑）：
  - 坐标顺序必须明确 [x1,y1,x2,y2]，Qwen/Gemini 曾有坐标顺序错位问题
  - 图片先缩放到 ≤ MAX_SIDE，回算原图坐标
  - 严格 JSON 解析，失败整批丢弃，不猜测
  - found=false / 空数组时明确返回，不编造
  - 输出附带原图坐标，可直接交给裁剪
"""
import json
import logging
import re

_logger = logging.getLogger("dafeiyu")


def _log(msg, level=logging.INFO):
    try:
        _logger.log(level, msg)
    except Exception:
        pass


MAX_SIDE = 1024          # 送给视觉模型的图片最长边
MAX_ELEMENTS = 40        # 元素清单最多返回多少个


def _prepare(img, max_side=MAX_SIDE):
    """缩放图片，返回 (缩放后的 PIL.Image, scale, 原图尺寸)。

    scale = 原图边长 / 缩放后边长；回算坐标时乘以它。
    """
    from PIL import Image
    w, h = img.size
    longest = max(w, h)
    if longest <= max_side:
        return img.convert("RGB"), 1.0, (w, h)
    s = longest / float(max_side)
    nw, nh = max(1, int(w / s)), max(1, int(h / s))
    small = img.convert("RGB").resize((nw, nh), Image.Resampling.LANCZOS)
    return small, s, (w, h)


def _img_to_data_url(img, quality=85):
    """PIL.Image → data:image/jpeg;base64,..."""
    import base64
    import io as _io
    buf = _io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:image/jpeg;base64," + b64


def _extract_json(text):
    """从模型输出里抠出第一个 JSON 对象/数组。失败返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    # 去掉 ```json 代码块
    if "```" in t:
        parts = [p for p in t.split("```") if p.strip()]
        for p in parts:
            p2 = p.strip()
            if p2.startswith("json"):
                p2 = p2[4:].strip()
            if p2.startswith("{") or p2.startswith("["):
                t = p2
                break
    try:
        return json.loads(t)
    except Exception:
        pass
    # 兜底：找第一个 { 到最后一个 }
    for pair in (("{", "}"), ("[", "]")):
        i = t.find(pair[0])
        j = t.rfind(pair[1])
        if i >= 0 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    return None


def _norm_box(box, scale, orig_size):
    """把缩放图坐标转成原图坐标，并夹到图像范围内。

    返回 [x1,y1,x2,y2] 或 None。
    """
    try:
        if not isinstance(box, (list, tuple)) or len(box) < 4:
            return None
        vals = []
        for v in box[:4]:
            vals.append(float(v))
        # 坐标可能是归一化 0~1
        if all(0.0 <= v <= 1.0 for v in vals):
            ow, oh = orig_size
            x1, y1, x2, y2 = (vals[0] * ow, vals[1] * oh,
                              vals[2] * ow, vals[3] * oh)
        else:
            x1, y1, x2, y2 = (vals[0] * scale, vals[1] * scale,
                              vals[2] * scale, vals[3] * scale)
        # 保证 x1<x2, y1<y2
        if x1 > x2:
            x1, x2 = x2, x1
        if y1 > y2:
            y1, y2 = y2, y1
        ow, oh = orig_size
        x1 = max(0, min(int(round(x1)), ow - 1))
        y1 = max(0, min(int(round(y1)), oh - 1))
        x2 = max(x1 + 1, min(int(round(x2)), ow))
        y2 = max(y1 + 1, min(int(round(y2)), oh))
        return [x1, y1, x2, y2]
    except Exception:
        return None


GROUND_PROMPT = """在图中定位「{target}」。

只输出 JSON，不要任何解释，不要 markdown 代码块：
{{"found": true, "box": [x1, y1, x2, y2], "label": "看到的东西"}}

规则：
- 坐标基于这张图的像素，顺序固定为 [左, 上, 右, 下]（x1,y1,x2,y2）
- 必须是整数像素值，x1 < x2，y1 < y2
- 找不到就输出 {{"found": false}}
- 不要猜测；不确定就输出 found=false"""

DETECT_PROMPT = """列出图中所有的「{kind}」。

只输出 JSON 数组，不要任何解释，不要 markdown 代码块：
[{{"id": 1, "name": "元素名称", "box": [x1, y1, x2, y2]}}]

规则：
- 坐标基于这张图的像素，顺序固定为 [左, 上, 右, 下]（x1,y1,x2,y2）
- 必须是整数像素值，x1 < x2，y1 < y2
- 按从上到下、从左到右排序
- 最多列出 {limit} 个
- 不确定的元素不要列；图中没有就输出 []"""


def ground_element(img, target, vision_call, timeout_hint=None):
    """定位单个元素，返回原图坐标。

    img          PIL.Image
    target       要找的东西（如「提交按钮」「红色的图标」）
    vision_call  视觉模型调用函数，签名 func(messages) -> str
                 复用桌宠现有的 vision_call（OpenAI 兼容 messages）

    返回 {
      "found": bool,
      "box": [x1,y1,x2,y2] 或 None,   # 原图像素坐标
      "label": 模型看到的描述,
      "raw": 模型原始输出（截断，排错用）,
      "err": 错误信息或 "",
    }
    """
    out = {"found": False, "box": None, "label": "", "raw": "", "err": ""}
    try:
        if not (target or "").strip():
            out["err"] = "target 为空"
            return out
        if vision_call is None:
            out["err"] = "视觉模型不可用"
            return out

        small, scale, orig_size = _prepare(img)
        url = _img_to_data_url(small)
        prompt = GROUND_PROMPT.format(target=target.strip())
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }]
        text = vision_call(messages)
        out["raw"] = (text or "")[:300]
        data = _extract_json(text)
        if not isinstance(data, dict):
            out["err"] = "模型输出无法解析为 JSON"
            _log("[视觉] ground 解析失败: %r" % out["raw"][:120])
            return out
        if not data.get("found"):
            out["label"] = str(data.get("label") or "")
            return out
        box = _norm_box(data.get("box"), scale, orig_size)
        if box is None:
            out["err"] = "模型返回的坐标无效"
            return out
        out["found"] = True
        out["box"] = box
        out["label"] = str(data.get("label") or "")
        return out
    except Exception as ex:
        out["err"] = "%s: %s" % (type(ex).__name__, ex)
        _log("[视觉] ground 失败: %s" % out["err"], logging.WARNING)
        return out


def detect_elements(img, kind, vision_call, limit=MAX_ELEMENTS):
    """列出图中某类元素，返回原图坐标清单。

    kind 可为「按钮」「输入框」「图标」「链接」等自然语言。

    返回 {"elements": [{"id","name","box":[x1,y1,x2,y2]}], "err": ""}
    """
    out = {"elements": [], "err": ""}
    try:
        k = (kind or "").strip() or "可点击的元素"
        if vision_call is None:
            out["err"] = "视觉模型不可用"
            return out

        small, scale, orig_size = _prepare(img)
        url = _img_to_data_url(small)
        prompt = DETECT_PROMPT.format(kind=k, limit=int(limit))
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }]
        text = vision_call(messages)
        data = _extract_json(text)
        if not isinstance(data, list):
            out["err"] = "模型输出不是数组"
            _log("[视觉] detect 解析失败: %r" % (text or "")[:120])
            return out

        items = []
        for i, it in enumerate(data[:limit]):
            if not isinstance(it, dict):
                continue
            box = _norm_box(it.get("box"), scale, orig_size)
            if box is None:
                continue
            try:
                eid = int(it.get("id") or (i + 1))
            except Exception:
                eid = i + 1
            items.append({"id": eid,
                          "name": str(it.get("name") or "")[:40],
                          "box": box})
        out["elements"] = items
        return out
    except Exception as ex:
        out["err"] = "%s: %s" % (type(ex).__name__, ex)
        _log("[视觉] detect 失败: %s" % out["err"], logging.WARNING)
        return out


def crop_box(img, box):
    """按原图坐标裁剪，返回 PIL.Image 或 None。"""
    try:
        x1, y1, x2, y2 = [int(v) for v in box]
        w, h = img.size
        x1 = max(0, min(x1, w - 1))
        y1 = max(0, min(y1, h - 1))
        x2 = max(x1 + 1, min(x2, w))
        y2 = max(y1 + 1, min(y2, h))
        return img.crop((x1, y1, x2, y2))
    except Exception as ex:
        _log("[视觉] 裁剪失败: %s" % ex, logging.DEBUG)
        return None


def draw_boxes(img, boxes, color=(255, 60, 60), width=3):
    """在原图上画框（返回新图，不改原图）。

    boxes 可为 [x1,y1,x2,y2] 或 [{"box":[...]}, ...]
    """
    try:
        from PIL import ImageDraw
        out = img.convert("RGB").copy()
        d = ImageDraw.Draw(out)
        for b in (boxes or []):
            box = b.get("box") if isinstance(b, dict) else b
            if not box or len(box) < 4:
                continue
            d.rectangle([int(box[0]), int(box[1]),
                         int(box[2]), int(box[3])],
                        outline=color, width=max(1, int(width)))
        return out
    except Exception as ex:
        _log("[视觉] 画框失败: %s" % ex, logging.DEBUG)
        return img
