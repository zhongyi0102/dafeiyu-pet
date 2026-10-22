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
    "你是屏幕观察员,只客观描述,不评价、不扮演角色。总共不超过180字。\n"
    "先判断用户此刻最可能在做什么、状态如何(专注/摸鱼/卡住/放松),"
    "再列出支撑判断的2到3个画面证据。\n"
    "屏幕文字以【参考文字】为准,不要自己抄录;大段文字最多引用5个字。\n"
    "注意区分:画面里的人物、角色、动物、立绘都是用户正在看的内容,"
    "不是用户本人,也不是你;描述时写“画面里的角色”,不要写成“主人在画面里”。\n"
    "画面类型(视频截图/静态图片/游戏画面/网页/软件界面)拿不准就写“不确定”,不要硬猜。\n"
    "没有的项写“无”。严格按下面格式输出:\n"
)

FORMAT_TAIL = (
    "理解:(用1到2句话说清:这是什么、想表达什么或发生了什么、"
    "画面里最值得一说的一个细节;看不懂就写\"看不懂\")\n"
    "状态:(一句话,用户在做什么、什么状态)\n"
    "证据:(2到3条,用分号隔开)\n"
)

MUSIC_PROMPT = COMMON_RULES + (
    "场景:音乐播放器\n"
    "歌名/歌手:(只认【窗口标题】或界面上明确标着「正在播放」旁的文字;"
    "歌词、滚动大字、纯文字的一句话都**不是歌名**,严禁填进这里;"
    "窗口标题取不到时写『无』,宁可空着也不要拿歌词凑)\n"
    "歌词:(屏幕上出现的歌词一句,原样摘录,没有写\"无\")\n"
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
    "画面类型:(视频截图/静态图片,拿不准写“不确定”)\n"
    "内容:(视频标题或类型)\n"
    "画面:(一句话,画面里的人或角色是在演什么)\n"
) + FORMAT_TAIL

GAME_PROMPT = COMMON_RULES + (
    "场景:游戏\n"
    "游戏名:(只认窗口标题或界面明确写的,猜不到写“无”)\n"
    "局势:(战斗/等待/菜单/结算等)\n"
) + FORMAT_TAIL

# 聊天场景:不转述具体内容,保护隐私
CHAT_PROMPT = (
    "你是屏幕观察员,只客观描述。总共不超过80字。\n"
    "这是聊天软件,严禁转述任何聊天内容、人名、群名、头像细节。\n"
    "只输出:\n"
    "场景:聊天\n"
    "理解:(只从界面层面判断:在私聊/群聊、气氛热闹还是冷清,一句话;"
    "不要涉及任何具体内容)\n"
    "状态:(在和人聊天/在刷群消息,气氛如何,一句话)\n"
    "证据:(只写界面层面的特征)\n"
)

GENERAL_PROMPT = COMMON_RULES + (
    "场景:(例如网页、文档、桌面、设置界面)\n"
    "画面类型:(视频截图/静态图片/游戏画面/网页/软件界面,拿不准写“不确定”)\n"
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

    【小改说明】原版直接整图跑 RapidOCR，实测在「元素较多」的截图里
    检测模型会漏掉大字号标题（例如网易云的歌名「晴天」整条丢失），
    但把同一块单独裁剪出来又能识别。

    现在改为：
      1. 先用调低的 box_thresh 跑整图（多数情况足够）
      2. 如果大图（长边>640）且结果偏少，再上下两半各跑一次合并
    这样既保持速度，又避免漏掉关键标题。
    """
    global _ocr_engine
    th = OCR_MIN_CONF if min_conf is None else float(min_conf)
    try:
        if _ocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_engine = RapidOCR()
        import numpy as np

        img = img.convert("RGB")
        w, h = img.size

        def _one(im):
            """跑一次，返回 [(文字, 置信度)]。"""
            try:
                res, _ = _ocr_engine(np.array(im), box_thresh=0.3,
                                     unclip_ratio=2.0)
            except TypeError:
                # 老版本 RapidOCR 不支持这些参数
                res, _ = _ocr_engine(np.array(im))
            return [(r[1], float(r[2])) for r in (res or [])]

        items = _one(img)

        # 大图且结果偏少 → 上下两半再跑一次，补齐漏检
        if max(w, h) > 640 and len(items) < 6:
            half = h // 2
            for box in ((0, 0, w, half), (0, half, w, h)):
                items += _one(img.crop(box))

        # 去重 + 按门槛过滤
        seen = set()
        out = []
        for text, conf in items:
            t = (text or "").strip()
            if not t or conf <= th or t in seen:
                continue
            seen.add(t)
            out.append(t)
        return " | ".join(out)[:max_chars]
    except Exception:
        return ""

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


def extract_understanding(vision_text):
    """从视觉输出里取“理解:”那一行(可能跨行,取到下一个字段为止)。"""
    m = re.search(
        r"理解[::]\s*(.+?)(?=\n(?:状态|证据|场景|歌名|歌词|画面类型|主要内容|关键信息)[::]|\Z)",
        vision_text, re.S)
    if not m:
        return ""
    return " ".join(m.group(1).split()).strip()


def reply_head(text):
    """取回复开头两个字,用于反套路比对。跳过开头的括号舞台提示。"""
    t = (text or "").strip()
    # 跳过句首括号舞台提示（中文/英文括号都算）
    t2 = re.sub(r"^[（(][^）)]{0,12}[）)]\s*", "", t)
    if not t2:
        t2 = t
    t2 = re.sub(r"^[\s\-—~～]+", "", t2)
    return t2[:2]


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
    if scene == "music" and (title or "").strip():
        info += ("【提示】窗口标题一般格式为「歌名 - 歌手 - 播放器」,歌名以窗口标题为准;"
                 "【参考文字】里其余文字多为歌词,不要当成歌名。\n")

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
    understand = extract_understanding(vision_text)

    # ── 第二步:给带人设的聊天模型的消息 ──
    parts = ["[屏幕观察]", f"窗口标题:{title or '无'}", vision_text.strip()]
    # 把「理解」原文单独再强调一次（视觉输出里已有，但聊天模型容易只看状态）
    if understand:
        parts.append(f"画面理解:{understand}")
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
    "- 你是桌面上的宠物,不在屏幕画面里。画面里的人物、角色、动物都是主人"
    "正在看的内容,不是你,不要说“本鲸鱼”之类。\n"
    "- 像真人聊天一样说话:先给一个自然的反应(惊讶、吐槽、好奇、心疼、"
    "得意都行),再说想法。不要先描述“我看到屏幕上有……”。\n"
    "- 一定用到画面里一个具体细节(颜色、物品、表情、动作、文字之一)。\n"
    "- 长短随情况变化:多数1到2句,有话想说时可到3句,偶尔反问主人一句。"
    "开头和句式每次都不同,不要连续用“哼”“笨蛋主人”“才不是”开头。\n"
    "- 傲娇是调味,不是每句都要有:嘴上嫌弃,话里能看出关心。\n"
    "- 看不懂就坦率说“这是什么呀”,不要硬编。\n"
    "- 只有观察结果明确写了歌名/作品名才能用《》;歌词和猜测的名字不加书名号。\n"
    "- 如果有“上次看屏”信息且相关,可自然带一句,不相关就不提。\n"
    "- 不说“注意休息”“适当运动”这类放哪都成立的话。\n"
    "- 括号舞台提示最多一个,放句首,可以没有。\n"
    "- 不超过80字。\n"
)

def postprocess_reply(text, max_len=80):
    """回复超长时,截到最近的句号/感叹号/问号。"""
    text = text.strip()
    if len(text) <= max_len:
        return text
    cut = max(text.rfind(c, 0, max_len) for c in "。!?!?~")
    return text[:cut + 1] if cut > 20 else text[:max_len]
