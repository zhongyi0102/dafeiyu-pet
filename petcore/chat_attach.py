# -*- coding: utf-8 -*-
r"""chat_attach.py — 聊天附件（图片 / 文本文件）的采集、压缩与存储

设计要点：
  - chats.json 里**不存**图片本体或 base64，只存缩略图路径 + 视觉描述
  - 缩略图落盘到 <APP_DIR>/chat_images/，长边约 256px
  - 文本文件只读前 TEXT_MAX_CHARS 字，超出截断并标注
  - 发送给视觉模型的是压缩后的图（沿用视觉页「最长边 / 质量」设置）

目录结构：
  chat_images/
    <session_id>/
      <msg_ts>_<idx>.jpg      ← 缩略图（256px，气泡显示用）
      <msg_ts>_<idx>_send.jpg ← 发送给视觉模型的压缩图（可清理）
"""
import logging
import os
import re

_logger = logging.getLogger("dafeiyu")

IMG_DIR_NAME = "chat_images"
THUMB_SIDE = 256            # 缩略图长边
MAX_IMAGES = 4              # 一次最多几张图
TEXT_MAX_CHARS = 8000       # 文本文件读取上限
TEXT_EXTS = (".txt", ".md", ".py", ".json", ".csv", ".log")
IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")


def is_image(path):
    return os.path.splitext(str(path or ""))[1].lower() in IMG_EXTS


def is_text_file(path):
    return os.path.splitext(str(path or ""))[1].lower() in TEXT_EXTS


def images_dir(app_dir):
    d = os.path.join(app_dir, IMG_DIR_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def session_dir(app_dir, session_id):
    d = os.path.join(images_dir(app_dir), str(session_id or "default"))
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def make_thumb(src, dst, side=THUMB_SIDE):
    """生成缩略图（长边 side）。成功返回 dst，失败返回 ""。"""
    try:
        from PIL import Image
        im = Image.open(src)
        im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, float(side) / max(1, max(w, h)))
        if scale < 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                           Image.Resampling.LANCZOS)
        d = os.path.dirname(os.path.abspath(dst))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        im.save(dst, "JPEG", quality=82)
        return dst
    except Exception as ex:
        _logger.warning("[附件] 生成缩略图失败: %s: %s", type(ex).__name__, ex)
        return ""


def shrink_for_send(src, dst, max_side=1024, quality=85):
    """按视觉页设置压缩（发送用）。成功返回 dst。"""
    try:
        from PIL import Image
        im = Image.open(src).convert("RGB")
        w, h = im.size
        scale = min(1.0, float(max_side) / max(1, max(w, h)))
        if scale < 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                           Image.Resampling.LANCZOS)
        d = os.path.dirname(os.path.abspath(dst))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        im.save(dst, "JPEG", quality=max(40, min(95, int(quality))))
        return dst
    except Exception as ex:
        _logger.warning("[附件] 压缩失败: %s: %s", type(ex).__name__, ex)
        return ""


def read_text_file(path, limit=TEXT_MAX_CHARS):
    """读文本文件前 limit 字。返回 (内容, 是否截断, 错误)。"""
    try:
        size = os.path.getsize(path)
    except Exception:
        size = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(limit + 1)
    except Exception as ex:
        return "", False, "%s: %s" % (type(ex).__name__, ex)
    truncated = len(data) > limit
    if truncated:
        data = data[:limit]
    return data, truncated, ""


def store_image(app_dir, session_id, src, ts, idx, max_side=1024, quality=85):
    """存一张图：缩略图 + 发送用压缩图。返回附件 dict。"""
    d = session_dir(app_dir, session_id)
    base = "%d_%d" % (int(float(ts) * 1000), int(idx))
    thumb = os.path.join(d, base + ".jpg")
    send = os.path.join(d, base + "_send.jpg")
    make_thumb(src, thumb)
    shrink_for_send(src, send, max_side=max_side, quality=quality)
    return {
        "kind": "image",
        "thumb": thumb,
        "path": send or thumb,
        "name": os.path.basename(src),
        "size": _safe_size(src),
    }


def store_text(path, limit=TEXT_MAX_CHARS):
    """读文本文件，返回附件 dict（不落盘副本）。"""
    content, truncated, err = read_text_file(path, limit)
    return {
        "kind": "text",
        "name": os.path.basename(path),
        "size": _safe_size(path),
        "content": content,
        "truncated": bool(truncated),
        "error": err,
    }


def _safe_size(path):
    try:
        return int(os.path.getsize(path))
    except Exception:
        return 0


def delete_session_images(app_dir, session_id):
    """删除某会话的全部附件图片（删除对话时调用）。返回删除数量。"""
    import shutil
    d = os.path.join(images_dir(app_dir), str(session_id or ""))
    n = 0
    try:
        if os.path.isdir(d):
            for root, _dirs, files in os.walk(d):
                n += len(files)
            shutil.rmtree(d, ignore_errors=True)
    except Exception as ex:
        _logger.warning("[附件] 删除会话图片失败: %s: %s",
                        type(ex).__name__, ex)
    return n


def gc_orphans(app_dir, live_paths):
    """清理不在任何会话里引用的附件文件。返回删除数量。"""
    live = set()
    for p in (live_paths or ()):
        try:
            live.add(os.path.normcase(os.path.abspath(str(p))))
        except Exception:
            pass
    n = 0
    base = images_dir(app_dir)
    try:
        for root, _dirs, files in os.walk(base):
            for f in files:
                fp = os.path.join(root, f)
                if os.path.normcase(os.path.abspath(fp)) in live:
                    continue
                try:
                    os.remove(fp)
                    n += 1
                except Exception:
                    pass
    except Exception:
        pass
    return n


def format_text_attachment(att):
    """把文本附件拼进用户消息（明确分隔）。"""
    name = att.get("name") or "附件"
    body = att.get("content") or ""
    parts = ["【附件:%s】" % name]
    if att.get("truncated"):
        parts.append("（已截取前 %d 字）" % TEXT_MAX_CHARS)
    parts.append(body)
    parts.append("【附件结束】")
    return "\n".join(parts)


def image_history_text(desc, name=""):
    """历史里给模型的图片占位（不重发图）。"""
    d = re.sub(r"\s+", " ", (desc or "")).strip()
    return "【用户发送了图片:%s】" % (d or "（无描述）")
