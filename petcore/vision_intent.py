# -*- coding: utf-8 -*-
r"""vision_intent.py — 视觉工具的自然语言意图识别

本地关键词匹配，不调模型。命中才触发对应工具，不命中走普通聊天。

规则顺序很重要：先匹配「特征更明确」的意图（diff / long_ocr / colors /
foreground / detect），最后才是 ground（因为「哪里」太泛）。
"""
import re

# (意图, 触发词正则)。顺序即优先级。
RULES = (
    # 像素对比：必须带「对比/比较/差异/不一样」这类明确词
    ("diff", re.compile(
        r"(?:哪里不一样|哪里不同|哪儿不一样|有啥区别|有什么不同)"
        r"|(?:对比|比较).{0,10}图"
        r"|(?:图|截图).{0,6}(?:差异|区别|对比)"
        r"|差在哪")),
    # 长截图 OCR
    ("long_ocr", re.compile(
        r"(?:长截图|长图|整页|滚动截图).{0,10}(?:读|识别|提取|文字|内容)"
        r"|(?:读|识别|提取).{0,6}(?:长截图|长图|整页)")),
    # 主色提取
    ("colors", re.compile(
        r"(?:什么颜色|哪些颜色|主色|配色|色值|色板|用了.{0,4}色)")),
    # 前景抠图
    ("foreground", re.compile(
        r"(?:抠图|抠出来|去背景|透明背景|提取主体|把.{0,8}抠)")),
    # 元素清单
    ("detect", re.compile(
        r"(?:有哪些|列出|都有什么|都有啥|几个).{0,8}"
        r"(?:按钮|图标|链接|输入框|元素|控件|标签|菜单|选项)"
        r"|(?:按钮|图标|元素|控件|链接|输入框).{0,6}"
        r"(?:有哪些|列出|都有什么)")),
    # 元素定位（放最后：「哪里」太泛，容易被别的意图误吃）
    ("ground", re.compile(
        r"(?:在哪|在哪里|位置|定位)"
        r"|(?:帮我找|找出|找到|找一下).{1,20}"
        r"|(?:按钮|图标|链接|输入框|菜单|选项).{0,4}(?:在哪|在哪里)")),
)

# 提取「目标」的正则（ground 用），按优先级尝试
TARGET_PATTERNS = (
    # 「帮我找一下 XX 在哪」→ 取「找」和「在哪」之间
    re.compile(r"(?:帮我找|找出|找到|找一下|找)\s*(?:一下)?\s*"
               r"([^\s，。！？的]{1,24}?)(?:在哪|在哪里|位置|$|[，。！？])"),
    # 「XX 按钮在哪里」→ 取「在哪里」之前
    re.compile(r"([\u4e00-\u9fa5A-Za-z0-9_\- ]{1,24}?)"
               r"(?:按钮|图标|链接|输入框|菜单|选项)?\s*(?:在哪|在哪里)"),
    # 引号包裹：「保存」
    re.compile(r"[「『\"]([^」』\"]{1,40})[」』\"]"),
    # 兜底：找 X
    re.compile(r"(?:找|定位)\s*([^\s，。！？]{1,24})"),
)

# 元素类型（detect 用）
KIND_WORDS = ("按钮", "图标", "链接", "输入框", "菜单", "选项",
              "控件", "元素", "标签", "复选框", "下拉框")

# 无意义的 target（提取失败时的残留词）
_JUNK = {"一下", "一下", "这个", "那个", "里", "的", "了", "呢", "啊", "吧"}


def detect_intent(text):
    """识别视觉工具意图。

    返回 (intent, target)：
      intent 为 "" 表示不命中（走普通聊天）
      target 为提取到的目标描述（ground/detect 用），可能为空
    """
    t = (text or "").strip()
    if not t or len(t) > 200:
        return "", ""
    for name, pat in RULES:
        if pat.search(t):
            target = ""
            if name == "ground":
                target = extract_target(t)
            elif name == "detect":
                target = extract_kind(t)
            return name, target
    return "", ""


def extract_target(text):
    """从「帮我找 XX 在哪」里抠出 XX。"""
    t = (text or "").strip()
    for pat in TARGET_PATTERNS:
        for m in pat.finditer(t):
            s = (m.group(1) or "").strip(" 的了呢啊吧，。！？的")
            if not s or s in _JUNK:
                continue
            # 去掉残留的「一下」
            if s.startswith("一下"):
                s = s[2:].strip()
            if not s or s in _JUNK:
                continue
            if len(s) <= 40:
                return s
    return ""


def extract_kind(text):
    """从「图里有哪些按钮」里抠出「按钮」。"""
    t = (text or "").strip()
    for w in KIND_WORDS:
        if w in t:
            return w
    return "可点击的元素"
