"""
screen_vision.py —「看看屏幕」增强模块(可直接放进 dafeiyu-pet 项目)

流程:
  1. 点右键「看看屏幕」时,先调用 get_fg_info() 记下前台窗口(必须在框选开始前)
  2. 用户框选完成后,调用 analyze_screen(...) 得到 (给聊天模型的消息, 场景名)
  3. 聊天模型回复后,调用 mem.save(...) 记住这次

依赖: pillow, pywin32, psutil;可选: rapidocr-onnxruntime(没装则自动跳过 OCR)
  pip install pillow pywin32 psutil rapidocr-onnxruntime
"""
import base64
import io
import json
import re
import time
from pathlib import Path

from PIL import Image

# ───────────────────────── 1. 前台窗口 + 场景分类 ─────────────────────────

def get_fg_info():
    """返回 (窗口标题, 进程名)。要在框选截图之前调用。"""
    try:
        import win32gui
        import win32process
        import psutil
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc = psutil.Process(pid).name()
        return title, proc
    except Exception:
        return "", ""


COMMON_RULES = (
    "你是屏幕观察员,只客观描述,不评价、不扮演角色。总共不超过150字。\n"
    "先判断用户此刻最可能在做什么、状态如何(专注/摸鱼/卡住/放松),"
    "再列出支撑判断的2到3个画面证据。\n"
    "屏幕文字以【参考文字】为准,不要自己抄录;大段文字最多引用5个字。\n"
    "没有的项写“无”。严格按下面格式输出:\n"
)

FORMAT_TAIL = (
    "状态:(一句话,用户在做什么、什么状态)\n"
    "证据:(2到3条,用分号隔开)\n"
)

MUSIC_PROMPT = COMMON_RULES + (
    "场景:音乐播放器\n"
    "歌名/歌手:(优先用【窗口标题】,取不到再看图)\n"
    "播放状态:(播放中/暂停)\n"
    "意境:(歌词或画面的氛围,一句话)\n"
) + FORMAT_TAIL

CODE_PROMPT = COMMON_RULES + (
    "场景:写代码\n"
    "语言/文件:(例如 Python / main.py)\n"
    "报错:(有则写关键词,无则写“无”)\n"
) + FORMAT_TAIL

VIDEO_PROMPT = COMMON_RULES + (
    "场景:看视频\n"
    "内容:(视频标题或类型)\n"
    "画面:(一句话)\n"
) + FORMAT_TAIL

GAME_PROMPT = COMMON_RULES + (
    "场景:游戏\n"
    "游戏名:\n"
    "局势:(战斗/等待/菜单/结算等)\n"
) + FORMAT_TAIL

# 聊天场景:不转述具体内容,保护隐私
CHAT_PROMPT = (
    "你是屏幕观察员,只客观描述。总共不超过60字。\n"
    "这是聊天软件,严禁转述任何聊天内容、人名、群名。\n"
    "只输出:\n场景:聊天\n状态:(在和人聊天/在刷群消息,气氛如何,一句话)\n证据:(只写界面层面的特征)\n"
)

GENERAL_PROMPT = COMMON_RULES + (
    "场景:(例如网页、文档、桌面、设置界面)\n"
    "主要内容:(一句话)\n"
    "关键信息:(最多3条,如标题、数字、报错)\n"
) + FORMAT_TAIL

SCENES = {
    "music": (["cloudmusic", "qqmusic", "spotify", "foobar", "kugou", "kuwo"], MUSIC_PROMPT),
    "code":  (["code", "pycharm", "idea64", "devenv", "sublime", "cursor", "windowsterminal"], CODE_PROMPT),
    "video": (["potplayer", "vlc", "mpv", "bilibili"], VIDEO_PROMPT),
    "game":  (["yuanshen", "genshin", "wangzhe", "league", "valorant", "steam"], GAME_PROMPT),
    "chat":  (["wechat", "weixin", "qq", "telegram", "dingtalk", "feishu", "lark"], CHAT_PROMPT),
}

# 浏览器里的内容靠窗口标题关键词判断
TITLE_HINTS = [
    (["哔哩哔哩", "bilibili", "youtube", "抖音", "优酷", "爱奇艺"], "video", VIDEO_PROMPT),
    (["网易云音乐", "QQ音乐", "Spotify"], "music", MUSIC_PROMPT),
]


def pick_scene(proc, title):
    p = (proc or "").lower().replace(".exe", "")
    for name, (keys, prompt) in SCENES.items():
        if any(k in p for k in keys):
            return name, prompt
    for keys, name, prompt in TITLE_HINTS:
        if any(k.lower() in (title or "").lower() for k in keys):
            return name, prompt
    return "general", GENERAL_PROMPT


# ───────────────────────── 2. 本地 OCR(可选) ─────────────────────────

_ocr_engine = None


# OCR 置信度门槛
# 【小改说明】原版硬编码 >0.8，实测把「晴天」(0.634)、「周杰伦」(0.703)
# 这类关键歌名/标题全过滤掉了，导致视觉模型拿不到【参考文字】。
# 现在降到 0.5（仍能滤掉明显噪声），并把阈值提成模块常量便于调整。
OCR_MIN_CONF = 0.5


def run_ocr(img: Image.Image, max_chars=400, min_conf=None):
    """返回识别到的文字，没装 OCR 或失败返回空串。

    min_conf 默认用 OCR_MIN_CONF（0.5）。太低会混入噪声，太高会丢关键信息。
    """
    global _ocr_engine
    th = OCR_MIN_CONF if min_conf is None else float(min_conf)
    try:
        if _ocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_engine = RapidOCR()
        import numpy as np
        result, _ = _ocr_engine(np.array(img.convert("RGB")))
        texts = [r[1] for r in (result or []) if float(r[2]) > th]
        return " | ".join(texts)[:max_chars]
    except Exception:
        return ""


# ───────────────────────── 3. 图片预处理 ─────────────────────────

def warmup_ocr(blocking=False):
    """预热 OCR 引擎（首次加载很慢）。

    blocking=False 时开后台线程，不阻塞调用方（PySide6 界面线程）。
    没装 rapidocr 时静默返回 False。
    """
    import threading

    def _load():
        global _ocr_engine
        try:
            if _ocr_engine is None:
                from rapidocr_onnxruntime import RapidOCR
                _ocr_engine = RapidOCR()
        except Exception:
            pass

    if blocking:
        _load()
        return _ocr_engine is not None
    threading.Thread(target=_load, daemon=True).start()
    return True


def ocr_available():
    """是否装了 OCR（不触发加载）。"""
    try:
        import importlib.util as _u
        return _u.find_spec("rapidocr_onnxruntime") is not None
    except Exception:
        return False


def preprocess(img: Image.Image, is_selection: bool):
    """
    返回 (给OCR的图, 给视觉模型的图)。
    - 框选区域短边 < 512:放大(最多2倍),小字更清楚
    - 整屏截图:发给视觉模型的图压到长边 1024;OCR 用较大的图(长边≤2000)
    """
    img = img.convert("RGB")
    w, h = img.size

    if is_selection and min(w, h) < 512:
        scale = min(2.0, 512 / min(w, h))
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        return img, img

    # OCR 图
    ocr_img = img
    if max(w, h) > 2000:
        s = 2000 / max(w, h)
        ocr_img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    # 视觉图
    vis_img = img
    if max(w, h) > 1024:
        s = 1024 / max(w, h)
        vis_img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    return ocr_img, vis_img


def img_to_data_url(img: Image.Image, quality=85):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ───────────────────────── 4. 记住上一次 ─────────────────────────

class ScreenMemory:
    def __init__(self, path="screen_memory.json", keep=3):
        self.path = Path(path)
        self.keep = keep
        self.items = []
        try:
            self.items = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            pass

    def save(self, scene, state, reply=""):
        self.items.append({"t": time.time(), "scene": scene, "state": state, "reply": reply})
        self.items = self.items[-self.keep:]
        try:
            self.path.write_text(json.dumps(self.items, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _ago(t):
        m = int((time.time() - t) / 60)
        if m < 1:
            return "刚刚"
        if m < 60:
            return f"{m}分钟前"
        h = m // 60
        return f"{h}小时前" if h < 24 else f"{h // 24}天前"

    def last_summary(self):
        """给聊天模型的“上次看屏”,超过1天的不带。"""
        if not self.items:
            return ""
        it = self.items[-1]
        if time.time() - it["t"] > 86400:
            return ""
        return f"上次看屏:{self._ago(it['t'])},{it['state']}"

    def recent_replies(self):
        """最近几次回复,用于“别和这些重复”。"""
        return [i["reply"] for i in self.items if i.get("reply")]


def extract_state(vision_text):
    """从视觉输出里取“状态:”那一行,存进记忆。"""
    m = re.search(r"状态[::]\s*(.+)", vision_text)
    return m.group(1).strip() if m else vision_text.strip()[:40]


# ───────────────────────── 5. 视觉模型自动选择 ─────────────────────────

def pick_vision_model(model_ids):
    """从可用模型里挑视觉模型并排序,第一个为推荐。

    【小改说明】原版视觉词表只认 "glm-4v",认不出 glm-4.6v / glm-5v / glm-5.3-flash,
    导致智谱的视觉模型被误判为非视觉、排到最后。现在改用正则匹配
    「glm-<数字>[.<数字>]v」这类形式,并补充 deepseek-vision / -vl 等常见写法。
    非视觉模型一律返回 -1,排序时排最后。
    """
    VIS_RE = re.compile(
        r"(glm-\d+(?:\.\d+)?v)"          # glm-4v / glm-4.6v / glm-5v
        r"|(\bvl\b|-vl-|-vl$)"            # qwen-vl / xxx-vl
        r"|(vision)"                        # xxx-vision
        r"|(4o|omni|gemini|claude)"         # gpt-4o / omni / gemini / claude
        r"|(deepseek-v4-flash-vision)"      # DeepSeek 视觉
    )

    def score(mid):
        m = (mid or "").lower()
        if not VIS_RE.search(m):
            return -1                        # 非视觉 → 排最后
        s = 100.0
        ver = re.search(r"glm-(\d+)(?:\.(\d+))?", m)
        if ver:
            major = int(ver.group(1))
            minor = int(ver.group(2) or 0)
            s += major * 10 + minor
        size = re.search(r"(\d+)b", m)
        if size:
            s += min(int(size.group(1)), 200) / 10
        if any(k in m for k in ("max", "plus", "pro", "large", "turbo")):
            s += 8
        if any(k in m for k in ("mini", "small", "lite", "flash", "tiny",
                                "3b", "2b", "1b")):
            s -= 15
        return s

    return sorted(model_ids, key=score, reverse=True)

def analyze_screen(img, fg_info, mem, vision_call, is_selection=True,
                   vision_system_override=None):
    """
    img:           PIL.Image,用户框选/截取的画面
    fg_info:       get_fg_info() 的返回值 (title, proc)
    mem:           ScreenMemory 实例
    vision_call:   你现有的视觉请求函数,签名 vision_call(messages) -> str
                   messages 是 OpenAI 兼容格式(含 image_url);
                   temperature 请在你的函数里设成 0.2
    vision_system_override: 设置页里用户自定义的视觉提示词(有则替换场景提示词,
                   但仍会附加窗口信息和参考文字)
    返回 (chat_user_msg, scene, state)
      chat_user_msg 直接作为“用户消息”交给聊天模型(带人设的那一步)
    """
    title, proc = fg_info
    scene, scene_prompt = pick_scene(proc, title)

    ocr_img, vis_img = preprocess(img, is_selection)

    # 聊天场景不做 OCR,避免把私人内容送出去
    ocr_text = "" if scene == "chat" else run_ocr(ocr_img)

    info = f"【窗口标题】{title or '无'}\n【进程】{proc or '无'}\n"
    if ocr_text:
        info += f"【参考文字】{ocr_text}\n"

    system = vision_system_override or scene_prompt
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": info + "请观察这张截图。"},
            {"type": "image_url", "image_url": {"url": img_to_data_url(vis_img)}},
        ]},
    ]
    vision_text = vision_call(messages)
    state = extract_state(vision_text)

    # ── 第二步:给带人设的聊天模型的消息 ──
    parts = ["[屏幕观察]", f"窗口标题:{title or '无'}", vision_text.strip()]
    last = mem.last_summary()
    if last:
        parts.append(last)
    recent = mem.recent_replies()
    if recent:
        parts.append("你最近说过(别重复这些角度):" + " / ".join(recent[-3:]))
    parts.append("\n主人让你看了一眼屏幕,用你的人设回应。")
    chat_user_msg = "\n".join(parts)
    return chat_user_msg, scene, state


# 追加到聊天人设后面的“看屏规则”(直接拼到 system prompt 末尾)
CHAT_LOOK_RULES = (
    "\n\n看屏幕时的回复规则:\n"
    "- 1到2句,不超过60字。\n"
    "- 先点出主人在干什么(歌名、软件、内容),再吐槽或关心。\n"
    "- 不要复述屏幕上的原文。\n"
    "- 如果有“上次看屏”信息且相关,可以自然提一句连续性(例如问报错解决没),不相关就不提。\n"
    "- 傲娇:嘴上嫌弃,话里带关心,不要每次都以“才不是”开头。\n"
    "- 括号舞台提示最多一个,放在句首。\n"
)


def postprocess_reply(text, max_len=80):
    """回复超长时,截到最近的句号/感叹号/问号。"""
    text = text.strip()
    if len(text) <= max_len:
        return text
    cut = max(text.rfind(c, 0, max_len) for c in "。!?!?~")
    return text[:cut + 1] if cut > 20 else text[:max_len]
