# -*- coding: utf-8 -*-
r"""card_reader.py — 解析现成人设卡（Character Card V2 / PNG 内嵌）

支持格式：
  1. 纯文本 / Markdown / 任意文本文件（回退）
  2. JSON 角色卡（SillyTavern / TavernAI / Chub 的 V2 规范）
  3. PNG 内嵌角色卡（图片元数据里存 base64 JSON）

V2 规范字段优先级（拼成一段人设文本时按此顺序）：
  system_prompt > description > personality > scenario > mes_example
  first_mes 只作为「开场白」附注，不混进人设正文。

为什么这样排：
  system_prompt 是作者对模型最直接的要求；
  description/personality 是人设主体；
  scenario 提供背景；
  mes_example 是对话示例，放最后起示范作用。
"""
import base64
import io
import json
import logging
import os

_logger = logging.getLogger("dafeiyu")

# 参与人设正文的字段（按拼接顺序）
_BODY_FIELDS = (
    ("system_prompt", "系统要求"),
    ("description", "角色描述"),
    ("personality", "性格"),
    ("scenario", "场景"),
    ("mes_example", "对话示例"),
)

# PNG 里存角色卡的元数据键（V2 用 chara，V3 用 ccv3）
_PNG_KEYS = ("ccv3", "chara")


def _s(v):
    """安全转字符串并 strip。"""
    if isinstance(v, str):
        return v.strip()
    if v is None:
        return ""
    return str(v).strip()


def _clean_example(text):
    """清理 mes_example 里的 {{char}} / {{user}} 宏，换成可读称呼。"""
    t = _s(text)
    if not t:
        return ""
    # <START> 是分隔标记，去掉
    t = t.replace("<START>", "").strip()
    t = t.replace("{{char}}", "你").replace("{{user}}", "用户")
    return t


def build_persona_from_card(data):
    """把 V2 卡片的 data 对象拼成一段人设文本。

    返回 (人设文本, 名字, 开场白)。
    """
    if not isinstance(data, dict):
        return "", "", ""

    parts = []
    for key, label in _BODY_FIELDS:
        raw = data.get(key)
        if key == "mes_example":
            raw = _clean_example(raw)
        else:
            raw = _s(raw)
        if not raw:
            continue
        # 给每段加个轻量标题，让模型能分清层次
        parts.append("【%s】\n%s" % (label, raw))

    name = _s(data.get("name"))
    greeting = _s(data.get("first_mes")) or _s(data.get("alternate_greetings") and
                                               (data.get("alternate_greetings") or [""])[0])
    greeting = greeting.replace("{{char}}", "你").replace("{{user}}", "用户")

    # 有名字就放最前面
    text = ""
    if name:
        text = "你的名字是「%s」。\n" % name
    text += "\n\n".join(parts)
    return text.strip(), name, greeting


def _try_json(obj):
    """obj 是解析后的 JSON，尝试按 V2 卡片读取。

    返回 (ok, 人设文本, 名字, 开场白)。
    """
    if not isinstance(obj, dict):
        return False, "", "", ""

    # V2: {"spec": "chara_card_v2", "data": {...}}
    if isinstance(obj.get("data"), dict):
        txt, name, greet = build_persona_from_card(obj["data"])
        if txt:
            return True, txt, name, greet

    # 裸对象：直接当 data 用（有些导出会去掉 spec 外壳）
    if any(k in obj for k in ("description", "personality", "name")):
        txt, name, greet = build_persona_from_card(obj)
        if txt:
            return True, txt, name, greet

    return False, "", "", ""


def _extract_from_png(path):
    """从 PNG 元数据里提取角色卡 JSON。返回 (ok, text, name, greeting)。"""
    try:
        from PIL import Image
    except Exception:
        return False, "", "", ""
    try:
        with Image.open(path) as im:
            info = dict(im.info or {})
    except Exception as ex:
        _logger.debug("[角色卡] 打开 PNG 失败: %s", ex)
        return False, "", "", ""

    for key in _PNG_KEYS:
        raw = info.get(key)
        if raw is None:
            continue
        try:
            # 通常是 base64 编码的 JSON
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            s = str(raw).strip()
            payload = base64.b64decode(s, validate=False)
            obj = json.loads(payload.decode("utf-8", "replace"))
        except Exception:
            # 有的直接存明文 JSON
            try:
                obj = json.loads(str(raw))
            except Exception:
                continue
        ok, txt, name, greet = _try_json(obj)
        if ok:
            return True, txt, name, greet

    return False, "", "", ""


def read_card(path, text_reader=None):
    """读取人设卡。自动识别 JSON / PNG / 纯文本。

    path: 文件路径
    text_reader: 读纯文本的回调 (path) -> (text, truncated, err)，
                 不传就自己读。

    返回 (ok, 人设文本, 名字, 开场白, 说明)。
    """
    p = _s(path)
    if not p or not os.path.isfile(p):
        return False, "", "", "", "文件不存在"

    ext = os.path.splitext(p)[1].lower()

    # 1) JSON 角色卡
    if ext in (".json",):
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                obj = json.load(f)
        except Exception as ex:
            return False, "", "", "", "JSON 解析失败：%s" % ex
        ok, txt, name, greet = _try_json(obj)
        if ok:
            return True, txt, name, greet, "JSON 角色卡"
        return False, "", "", "", "JSON 里没有找到角色卡字段"

    # 2) PNG 内嵌角色卡
    if ext in (".png",):
        ok, txt, name, greet = _extract_from_png(p)
        if ok:
            return True, txt, name, greet, "PNG 内嵌角色卡"
        # PNG 里没有卡片数据：当成普通图片，不支持
        return False, "", "", "", "这张 PNG 里没有角色卡数据"

    # 3) 其它图片格式也试一下（部分平台用 webp）
    if ext in (".webp", ".jpg", ".jpeg"):
        ok, txt, name, greet = _extract_from_png(p)
        if ok:
            return True, txt, name, greet, "图片内嵌角色卡"

    # 4) 回退到纯文本
    if text_reader is not None:
        data, truncated, err = text_reader(p)
        if err:
            return False, "", "", "", err
        text = _s(data)
        if not text:
            return False, "", "", "", "文件是空的"
        note = "文本文件" + ("（较长已截断）" if truncated else "")
        return True, text, "", "", note

    return False, "", "", "", "不支持的文件格式"
