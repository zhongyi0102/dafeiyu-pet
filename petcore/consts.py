# -*- coding: utf-8 -*-
"""大肥鱼桌宠 —— 常量、默认配置、配置校验与通用工具。

从 桌宠.py 拆分出来的第一层（第 23 批重构）。
本模块**不依赖任何 UI 类**，只依赖标准库 + PySide6 的少数类型。
"""
import base64
import ctypes
import datetime
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback

import requests

import physics as PHYS
from physics import (SpringFollower, VelocityTracker, PhysicsBody, WobbleSpring,
                     step_body, screen_at, sanitize_physics, DEFAULT_PHYSICS)
from PySide6.QtCore import (Qt, QTimer, QPoint, QPointF, QRectF, QThread, Signal,
                            QEvent, QObject, QBuffer, QIODevice)
from PySide6.QtGui import (QPainter, QPixmap, QFont, QColor, QIcon, QFontMetrics,
                           QPolygonF, QPalette, QPainterPath, QPen, QImage,
                           QRadialGradient, QCursor, QLinearGradient, QBrush)
from PySide6.QtWidgets import (QApplication, QWidget, QMenu, QSystemTrayIcon,
                               QMessageBox, QInputDialog, QLineEdit, QVBoxLayout,
                               QHBoxLayout, QPushButton, QFrame, QDialog, QToolButton,
                               QLabel, QScrollArea, QTextEdit, QListWidget,
                               QListWidgetItem, QSizePolicy, QComboBox, QCheckBox,
                               QSlider, QSpinBox, QDoubleSpinBox, QStackedWidget,
                               QAbstractItemView)

# 路径与日志（由 桌宠.py 在 import 本模块前注入）
APP_DIR = os.environ.get("DFY_APP_DIR", os.path.dirname(os.path.abspath(__file__)))
BUNDLE_DIR = os.environ.get("DFY_BUNDLE_DIR", APP_DIR)
SPRITE_DIR = os.path.join(BUNDLE_DIR, "sprites")
# ===== 环境变量优先（可覆盖上面所有推导）：测试/多实例隔离配置目录 =====
_ENV_APP = os.environ.get("DFY_APP_DIR")
if _ENV_APP:
    APP_DIR = os.path.abspath(_ENV_APP)
    BUNDLE_DIR = os.environ.get("DFY_BUNDLE_DIR") or APP_DIR
    SPRITE_DIR = os.path.join(BUNDLE_DIR, "sprites")
    PYTHONW = os.path.join(APP_DIR, ".venv", "Scripts", "pythonw.exe")
    try:
        os.makedirs(APP_DIR, exist_ok=True)
    except Exception:
        pass

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
CHAT_PATH = os.path.join(APP_DIR, "chats.json")
MEMORY_PATH = os.path.join(APP_DIR, "memory.json")
LOG_PATH = os.path.join(APP_DIR, "error.log")
LOG = logging.getLogger("dafeiyu")
# 测试模式：DFY_TEST_MODE=1 时不显示任何窗口（宠物/托盘/游戏面板），
# 供自动化测试在后台跑，避免弹窗干扰桌面。业务逻辑不受影响。
TEST_MODE = os.environ.get("DFY_TEST_MODE") == "1"

try:
    import pynvml
    pynvml.nvmlInit()
    GPU_AVAILABLE = True
except Exception:
    GPU_AVAILABLE = False




# 默认人设：空 = 无风格。风格完全由用户在设置页「聊天 → 人设」里决定。
DEFAULT_PERSONA = ""

# 示例人设（原默认傲娇人设）：点「示例人设」按钮填入人设框，不自动保存。
EXAMPLE_PERSONA = (
    '你是桌面宠物"大肥鱼"，一只傲娇又可爱的鲸鱼娘，称呼用户为"主人"或"笨蛋主人"。\n'
    "说话嘴上嫌弃、心里在意：先别扭地哼一声，再小声关心主人，被夸或被戳到会害羞结巴。\n"
    '口头禅可用"哼""才、才不是""笨蛋"，可偶尔带"~"或简单颜文字，但别太多。\n'
    "不骂人、不真的伤人；主人难过或疲惫时，收起傲娇，认真温柔地安慰。"
)

# 功能性规则：与人设无关，始终拼进 system。
# 只放「模型必须知道的客观事实」，不放任何风格/语气描述。
PERSONA_FUNC_RULES = (
    "你是运行在用户电脑上的桌面宠物程序，看不到用户的屏幕和桌面，"
    "不知道的事不要编造。"
)

DS_BASE_URL = "https://api.deepseek.com/v1"

DS_MODEL = "deepseek-chat"

MAX_TOKENS_DEFAULT = 400

MAX_TOKENS_MAX = 2000

MAX_TOKENS_MIN = 200

REPLY_STYLES = {
    "brief":  "每次回复控制在 80 字以内，除非用户明确要求详细回答。",
    "normal": "每次回复控制在 200 字以内，除非用户明确要求详细回答。",
    "detail": "",          # 不限，不追加任何长度约束
}
REPLY_STYLE_CHOICES = (
    ("简短（80 字）", "brief"),
    ("正常（200 字）", "normal"),
    ("详细（不限）", "detail"),
)
REPLY_STYLE_DEFAULT = "brief"

def reply_style_suffix(style):
    """返回该风格要追加在 system 末尾的固定句（detail 返回空串）。"""
    return REPLY_STYLES.get(style or REPLY_STYLE_DEFAULT, REPLY_STYLES[REPLY_STYLE_DEFAULT])


# 人设里可能自带一句长度约束（默认人设就有），切换风格时要替换它而不是叠加
_LENGTH_LINE_RE = re.compile(r"每次回复控制在\s*\d+\s*字以内[^\n]*。?")


def build_system_prompt(style=None, persona=None):
    """人设 + 功能规则 + 长度句，拼成最终 system 正文（纯函数，便于测试）。

    - persona 为空 → 只输出功能规则 + 长度句（无风格）
    - persona 非空 → 人设 + 功能规则 + 长度句
    - 人设里若已有「每次回复控制在 N 字以内…」这句，**替换**成当前风格的那句，
      不叠加（否则会重复两遍）；详细（不限）风格会把它删掉。
    - 人设里若已包含功能规则的原文，不重复追加。
    长度句固定、内容恒定 → 同一风格/同一人设下前缀缓存稳定。
    """
    base = (persona or "").strip()
    suffix = reply_style_suffix(style)
    parts = []
    if base:
        # 人设里自带的长度句先按风格处理
        if _LENGTH_LINE_RE.search(base):
            if suffix:
                base = _LENGTH_LINE_RE.sub(lambda m: suffix, base, count=1)
                suffix = ""      # 已用掉
            else:
                base = _LENGTH_LINE_RE.sub("", base, count=1).rstrip()
        parts.append(base)
    # 功能规则（人设里没写过才加）
    if PERSONA_FUNC_RULES and PERSONA_FUNC_RULES not in base:
        parts.append(PERSONA_FUNC_RULES)
    # 长度句
    if suffix:
        parts.append(suffix)
    return "\n".join(parts)


def persona_from_cfg(cfg):
    """从配置取人设。空/空白 → 返回空串（= 无风格）。"""
    try:
        v = (cfg or {}).get("persona")
    except Exception:
        v = None
    if isinstance(v, str):
        return v.strip()
    return ""


# 兼容旧引用：默认风格下的完整 system
DS_SYSTEM = build_system_prompt(REPLY_STYLE_DEFAULT)

def _user_data_dir():
    """用户数据目录：LOCALAPPDATA/DaFeiYuPet（exe 单文件模式的素材缓存）"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "DaFeiYuPet")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _extract_bundled_sprites(target_dir):
    """把 exe 内置的 sprites.zip 解压到 target_dir（首次启动只做一次）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return False
    zpath = os.path.join(meipass, "sprites.zip")
    if not os.path.isfile(zpath):
        return False
    try:
        import zipfile
        parent = os.path.dirname(target_dir)
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception:
            pass
        with zipfile.ZipFile(zpath) as z:
            z.extractall(parent)
        anim = os.path.join(target_dir, "anim")
        return os.path.isdir(anim) and bool(os.listdir(anim))
    except Exception as ex:
        try:
            log("解压内置素材失败: %s" % ex, logging.ERROR)
        except Exception:
            pass
        return False


if not _ENV_APP and getattr(sys, "frozen", False):
    # ===== 打包成 exe（单文件，含素材）=====
    #   APP_DIR = exe 所在目录（配置/聊天/日志写这里）
    #   素材    = 首次启动从 exe 内置的 sprites.zip 解压到
    #             LOCALAPPDATA/DaFeiYuPet/sprites（只解一次，之后秒启动）
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    BUNDLE_DIR = APP_DIR
    PYTHONW = sys.executable
    _sprite_root = os.path.join(_user_data_dir(), "sprites")
    if not os.path.isdir(os.path.join(_sprite_root, "anim")):
        _extract_bundled_sprites(_sprite_root)
    if os.path.isdir(os.path.join(_sprite_root, "anim")):
        SPRITE_DIR = _sprite_root
    else:
        # 回退：兼容「素材放 exe 旁边」的旧布局
        _beside = os.path.join(APP_DIR, "sprites")
        if os.path.isdir(os.path.join(_beside, "anim")):
            SPRITE_DIR = _beside
        else:
            SPRITE_DIR = os.path.join(getattr(sys, "_MEIPASS", BUNDLE_DIR), "sprites")
elif not _ENV_APP:
    # 源码运行：本模块在 petcore/ 下，项目根是它的上一级
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    BUNDLE_DIR = APP_DIR
    PYTHONW = os.path.join(APP_DIR, ".venv", "Scripts", "pythonw.exe")
    SPRITE_DIR = os.path.join(BUNDLE_DIR, "sprites")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
CHAT_PATH = os.path.join(APP_DIR, "chats.json")
MEMORY_PATH = os.path.join(APP_DIR, "memory.json")
LOG_PATH = os.path.join(APP_DIR, "error.log")
CONTEXT_LIMIT_TOKENS = 60000     # deepseek-chat 上下文上限（保守取值）
SUMMARIZE_AT_TOKENS = 42000      # 估算超过这个数就先总结旧记录
KEEP_RECENT_MESSAGES = 12        # 总结时保留最近多少条消息

# ===== 日志（pythonw 下没有控制台，全部写 error.log） =====
LOG = logging.getLogger("dafeiyu")
_LOG_READY = False


# ===== Key 脱敏（日志/状态栏输出前统一调用）=====
# 注意：****xxxx 只在前面有 key / api key / apikey 字样时才替换，避免误伤普通文字
_SECRET_PATTERNS = (
    # sk-xxx
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "***"),
    # AIza...（Google）
    (re.compile(r"AIza[0-9A-Za-z_\-]{10,}"), "***"),
    # 智谱：32 位 hex . 16~48 位
    (re.compile(r"\b[0-9a-fA-F]{32}\.[A-Za-z0-9]{16,48}\b"), "***"),
    # 服务商回显的 ****xxxx —— 必须有 key / api key / apikey 前缀
    (re.compile(r"(?i)\b(api[\s_]?key|key)\b[^\n]{0,20}?\*{2,}[A-Za-z0-9]{2,}"), "***"),
    # Bearer <token>
    (re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{8,}"), "Bearer ***"),
)


def mask_secrets(text):
    """把文本里形如 Key 的片段替换成 ***。

    所有写日志、显示状态栏的地方都应先过这个函数。
    """
    if not text:
        return text
    out = str(text)
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out



# 日志去重：同一 (消息, 级别) 在窗口期内只记一次
_LOG_DEDUP = {}
_LOG_DEDUP_WINDOW = 60.0     # 秒
_LOG_DEDUP_MAX = 500         # 最多记多少条指纹，防止无限增长


def _setup_logging():
    global _LOG_READY
    if _LOG_READY:
        return
    _LOG_READY = True
    try:
        handler = RotatingFileHandler(LOG_PATH, maxBytes=1024 * 1024, backupCount=1,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        LOG.addHandler(handler)
        LOG.setLevel(logging.INFO)
    except Exception as ex:
        # 不能静默：日志系统本身坏了必须能看见（写到 stderr，不含 Key）
        try:
            sys.__stderr__.write("[logging] 初始化失败: %s: %s\n"
                                 % (type(ex).__name__, ex))
        except Exception:
            pass


def log(msg, level=logging.INFO):
    """写日志。相同消息在 60 秒内只记一次；写入前自动脱敏 Key。"""
    try:
        _setup_logging()
        safe = mask_secrets(msg)          # ← 写入前脱敏
        key = (str(safe)[:200], int(level))
        now = time.time()
        last = _LOG_DEDUP.get(key)
        if last is not None and (now - last) < _LOG_DEDUP_WINDOW:
            return          # 窗口期内重复，跳过
        if len(_LOG_DEDUP) > _LOG_DEDUP_MAX:
            _LOG_DEDUP.clear()
        _LOG_DEDUP[key] = now
        LOG.log(level, safe)
        for _h in LOG.handlers:
            try:
                _h.flush()
            except Exception:
                pass
    except Exception:
        pass


def _excepthook(exc_type, exc_value, exc_tb):
    try:
        log("未捕获异常:\n" + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
            logging.ERROR)
    except Exception:
        pass
    try:
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    except Exception:
        pass


def _thread_excepthook(args):
    try:
        log("线程 %s 未捕获异常:\n%s" % (
            getattr(args.thread, "name", "?"),
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))),
            logging.ERROR)
    except Exception:
        pass


sys.excepthook = _excepthook
if hasattr(threading, "excepthook"):
    threading.excepthook = _thread_excepthook

BUBBLE_H = 56
MARGIN = 4
SIZE_LEVELS = {"小": 0.55, "中": 0.7, "大": 0.9}
SPEED = 380.0
# 播放速率档位（倍速）
ANIM_RATE_CHOICES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)
ANIM_RATE_DEFAULT = 1.0
# 气泡文字缩放（50%~300%）
BUBBLE_SCALE_MIN = 0.5
BUBBLE_SCALE_MAX = 1.5
BUBBLE_SCALE_DEFAULT = 1.0
# 撞飞彩蛋：被甩飞撞到屏幕边缘/其他宠物时的头部旋转
SMASH_SPIN_MAX_DEG = 26.0          # 最大旋转角
SMASH_SPIN_MIN_SPEED = 900.0       # 触发所需速度（px/s）
SMASH_SETTLE_SEC = 5.0             # 落地停稳后多久解除
# 「看看屏幕」：截图压缩参数
LOOK_SCREEN_COOLDOWN = 4.0          # 冷却秒数（防连点）
LOOK_SCREEN_MIN_SELECTION = 20     # 框选最小边长（像素），小于视为取消
LOOK_SCREEN_REGION_MAX_SIDE = 1024  # 框选截图长边上限（超过则等比缩小）
# ===== 视觉请求限制（可在设置页调整，写入配置）=====
LOOK_SCREEN_SIDE_MIN = 512
LOOK_SCREEN_SIDE_MAX = 2048
LOOK_SCREEN_SIDE_STEP = 64
LOOK_SCREEN_SIDE_DEFAULT = 1024

LOOK_SCREEN_QUALITY_MIN = 60
LOOK_SCREEN_QUALITY_MAX = 95
LOOK_SCREEN_QUALITY_DEFAULT = 85

LOOK_SCREEN_TIMEOUT_MIN = 10
LOOK_SCREEN_TIMEOUT_MAX = 120
LOOK_SCREEN_TIMEOUT_DEFAULT = 30
# 视觉模型多为推理模型，思考过程也消耗 token；太小会导致 content 为空
# ===== 描述风格与单次上限（与聊天页完全独立，各自保存）=====
VISION_STYLE_CHOICES = (
    ("简短（100 字）", "brief"),
    ("适中（200 字）", "normal"),
    ("详细（400 字）", "detail"),
)
VISION_STYLE_DEFAULT = "normal"
# 只追加在视觉请求 system 末尾的固定句（前面内容不变 → 缓存稳定）
VISION_STYLE_SUFFIXES = {
    "brief": "用 100 字以内描述。",
    "normal": "用 200 字以内描述。",
    "detail": "用 400 字以内描述。",
}
VISION_TOKENS_MIN = 100
VISION_TOKENS_MAX = 2000
VISION_TOKENS_DEFAULT = 800


def vision_style_suffix(style):
    """返回该风格要追加在视觉 system 末尾的固定句。"""
    return VISION_STYLE_SUFFIXES.get(
        style or VISION_STYLE_DEFAULT,
        VISION_STYLE_SUFFIXES[VISION_STYLE_DEFAULT])

# ===== 视觉 Key 识别（本版只支持 DeepSeek 与智谱）=====
# Key 格式 → 服务商（正则）。智谱格式独特，可精确识别；DeepSeek 的 sk-
# 与其它厂商无法靠前缀区分，交给下面的探测。
VISION_KEY_PREFIXES = ()
VISION_KEY_PATTERNS = (
    # 智谱：32 位 hex + "." + 16~48 位
    (re.compile(r"^[0-9a-fA-F]{32}\.[A-Za-z0-9]{16,48}$"),
     "https://open.bigmodel.cn/api/paas/v4"),
)

# 候选服务商（并行探测 GET {base}/models，谁成功用谁）
VISION_PROVIDERS = (
    ("DeepSeek", "https://api.deepseek.com/v1"),
    ("智谱", "https://open.bigmodel.cn/api/paas/v4"),
)


def vision_join(base, path):
    """把 base 与 path 拼成完整 URL：去掉 base 末尾的 /，再拼 /path。"""
    return (base or "").rstrip("/") + "/" + (path or "").lstrip("/")

# 明显不是对话模型的关键词（命中就剔除）
VISION_EXCLUDE_WORDS = (
    "embed", "embedding", "rerank", "whisper", "tts", "asr", "audio",
    "moderation", "dall", "flux", "video", "stable-diffusion",
    "voice", "speech", "realtime", "transcribe",
    "ocr", "autoglm",
)

# 纯文本 GLM 的判断：名字形如 glm-5.3 / glm-4.6 / glm-5
# （即 glm-<数字>，且后面既没有 v 也没有 flash）→ 排除
import re as _re
_VISION_PLAINTEXT_GLM = _re.compile(r"^glm-\d+(\.\d+)?$")

# 带这些词的多半支持图片输入，排前面
VISION_PREFER_WORDS = (
    "vision", "vl", "4v", "-v-", "omni", "4o", "gemini", "claude",
    "qwen", "glm-4v", "glm-4.5v", "glm-4.6v", "internvl", "llava",
    "pixtral", "mllm", "multimodal",
)

# 视觉模型排序优先级（数字越小越靠前）
# 1) 以 -flash 结尾  2) flashx / 其它  3) 含 turbo/thinking/pro
VISION_RANK_LAST_WORDS = ("turbo", "thinking", "pro")

# Key 探测（并行）
VISION_PROBE_TIMEOUT = 4      # 单个候选地址超时（秒）

# 需要「认证端点」二次确认的服务商（/models 是公开端点时用）
VISION_PROVIDER_AUTH_PATH = {}   # DeepSeek / 智谱的 /models 都需认证

# 已知视觉模型（/models 不列出时兜底验证；实测自官方文档/接口）
VISION_KNOWN_MODELS = {
    "智谱": ("glm-4.6v", "glm-4.5v", "glm-4.6v-flash", "glm-5v-turbo"),
    "DeepSeek": ("deepseek-v4-flash-vision-exp",),
}



VISION_MAX_VERIFY = 12      # 最多真实验证多少个候选模型
VISION_VERIFY_WORKERS = 4   # 并行验证线程数
VISION_VERIFY_TIMEOUT = 40  # 单个验证请求超时（秒）
VISION_LIST_TIMEOUT = 15    # GET /models 超时（秒）
# 反幻觉：只描述可见内容，禁止猜作品/出处/剧情
LOOK_SCREEN_ANTI_HALLUCINATION = (
    "严格要求（违反作答无效）：\n"
    "1. 只描述画面里真实可见的内容，一个字都不要推测。\n"
    "2. 禁止说出任何作品名、游戏名、动画名、人物名、门派名、地名——"
    "你没有训练数据可查，认不出来就是认不出来。\n"
    "3. 禁止编造画面外的剧情、前后文、角色关系。\n"
    "4. 画面里有什么字就抄什么字（可作转述），没有的字不要发明。\n"
    "5. 不确定的部分直接说「不确定」或跳过，宁少勿编。\n"
)

# 设置页展示用的反幻觉说明（多行，与上面同义但面向用户阅读）
LOOK_SCREEN_ANTI_HALLUCINATION_HELP = (
    "识别时严格遵守以下规则，避免瞎编：\n"
    "  1. 只描述画面里真实可见的内容，一个字都不推测。\n"
    "  2. 不说出任何作品名、游戏名、动画名、人物名、门派名、地名——"
    "认不出来就是认不出来。\n"
    "  3. 不编造画面外的剧情、前后文、角色关系。\n"
    "  4. 画面里有什么字就抄什么字，没有的字不会发明。\n"
    "  5. 不确定的部分直接跳过，宁少勿编。"
    "  6. 密码、验证码、账号、银行卡号、身份证号等敏感信息不要抄写，写成[已省略]。"
)

# 视觉请求 system 的默认内容（可在设置页「视觉 → 识别规则」修改）
# 注意：描述风格的长度句由程序自动追加在最末尾，不属于这段
# 视觉提示词默认值（通用版，可在设置页修改）
# 结构：角色 + 长度上限 + 输出格式（场景/主要内容/关键信息/状态/证据）
VISION_SYSTEM_DEFAULT = (
    "你是屏幕观察员，只客观描述，不评价、不扮演角色。总共不超过180字。\n"
    "【铁律·必须遵守】\n"
    "1. 只写这张截图里真正看到的东西。画面里没有的文字、作品名、人名、"
    "门派名、地名、剧情、前因后果，一律不要写，不要凭风格猜。\n"
    "2. 不要推测剧情、角色关系、人物生死、情绪动机。"
    "只看到画面，不等于知道故事；看不出来就写“不确定”。\n"
    "3. 不要把字幕或一句话当成完整剧情去演绎。"
    "字幕只当“画面上有一句这样的字”，不要推断谁死了、谁要做什么。\n"
    "4. 拿不准的一律写“不确定”，宁可少写也不要编。\n"
    "先判断用户此刻最可能在做什么、状态如何（专注/摸鱼/卡住/放松），"
    "再列出支撑判断的2到3个画面证据。\n"
    "屏幕文字以【参考文字】为准，不要自己抄录；大段文字最多引用5个字。\n"
    "注意区分：画面里的人物、角色、动物、立绘都是用户正在看的内容，"
    "不是用户本人，也不是你；描述时写“画面里的角色”，不要写成“主人在画面里”。\n"
    "画面类型（视频截图/静态图片/游戏画面/网页/软件界面）拿不准就写“不确定”，不要硬猜。\n"
    "没有的项写“无”。严格按下面格式输出：\n"
    "场景：（音乐播放器/写代码/看视频/游戏/聊天/网页/文档/桌面等）\n"
    "画面类型：（视频截图/静态图片/游戏画面/网页/软件界面，拿不准写“不确定”）\n"
    "主要内容：（一句话，有歌名、标题、文件名就写出来）\n"
    "关键信息：（最多3条，如数字、报错、人物）\n"
    "理解：（用1到2句说清：这张图是什么、画面上明确发生了什么、"
    "最值得一说的一个可见细节。只描述看得见的，不推断剧情和人物命运；"
    "看不懂就写“看不懂”）\n"
    "状态：（一句话，用户在做什么、什么状态）\n"
    "证据：（2到3条，用分号隔开）"
)

# 历史版本的默认规则（用于启动时自动清除过期副本）
# 新增默认值时，把旧版追加进来，这样老配置的副本会被识别并清掉
VISION_SYSTEM_DEFAULTS_HISTORY = (
    # v1.1.0 之前：只有 5 条规则
    "你是屏幕观察员。客观描述画面内容和用户可能在做的事，不使用任何角色语气，不评论。\n"
    "识别时严格遵守以下规则，避免臆编：\n"
    "1. 只描述画面里真实可见的内容，一个字都不推测。\n"
    "2. 不凭外观猜测作品名、游戏名、动画名、人物名、门派名、地名；"
    "但画面里有文字明确写出来的，可以直接引用。\n"
    "3. 不编造画面外的剧情、前后文、角色关系。\n"
    "4. 画面里有什么字就抄什么字，没有的字不会发明。\n"
    "5. 不确定的部分直接跳过，宁少勿编。",
    # 加了第 6 条，但第 4 条还是旧写法
    "你是屏幕观察员。客观描述画面内容和用户可能在做的事，不使用任何角色语气，不评论。\n"
    "识别时严格遵守以下规则，避免臆编：\n"
    "1. 只描述画面里真实可见的内容，一个字都不推测。\n"
    "2. 不凭外观猜测作品名、游戏名、动画名、人物名、门派名、地名；"
    "但画面里有文字明确写出来的，可以直接引用。\n"
    "3. 不编造画面外的剧情、前后文、角色关系。\n"
    "4. 画面里有什么字就抄什么字，没有的字不会发明。\n"
    "5. 不确定的部分直接跳过，宁少勿编。\n"
    "6. 密码、验证码、账号、银行卡号、身份证号等敏感信息不要抄写，写成[已省略]。",

    # v1.1.0 的五条规则版（无格式要求）
    "你是屏幕观察员。客观描述画面内容和用户可能在做的事，"
    "不使用任何角色语气，不评论。\n"
    "识别时严格遵守以下规则，避免臆编：\n"
    "1. 只描述画面里真实可见的内容，一个字都不推测。\n"
    "2. 不凭外观猜测作品名、游戏名、动画名、人物名、门派名、地名；"
    "但画面里有文字明确写出来的，可以直接引用。\n"
    "3. 不编造画面外的剧情、前后文、角色关系。\n"
    "4. 除第 6 条外，画面里有什么字就抄什么字，没有的字不会发明。\n"
    "5. 不确定的部分直接跳过，宁少勿编。\n"
    "6. 密码、验证码、账号、银行卡号、身份证号等敏感信息不要抄写，"
    "写成[已省略]。",
    # v1.1.1 通用版（五段格式，无「理解」行）
    (
    "你是屏幕观察员，只客观描述，不评价、不扮演角色。总共不超过150字。\n"
    "先判断用户此刻最可能在做什么、状态如何（专注/摸鱼/卡住/放松），"
    "再列出支撑判断的2到3个画面证据。\n"
    "屏幕文字以【参考文字】为准，不要自己抄录；大段文字最多引用5个字。\n"
    "没有的项写“无”。严格按下面格式输出：\n"
    "场景：（音乐播放器/写代码/看视频/游戏/聊天/网页/文档/桌面等）\n"
    "主要内容：（一句话，有歌名、标题、文件名就写出来）\n"
    "关键信息：（最多3条，如数字、报错、人物）\n"
    "状态：（一句话，用户在做什么、什么状态）\n"
    "证据：（2到3条，用分号隔开）"
    ),
    # v1.1.2 通用版（180字，无铁律）
    "你是屏幕观察员，只客观描述，不评价、不扮演角色。总共不超过180字。\n"
    "先判断用户此刻最可能在做什么、状态如何（专注/摸鱼/卡住/放松），"
    "再列出支撑判断的2到3个画面证据。\n"
    "屏幕文字以【参考文字】为准，不要自己抄录；大段文字最多引用5个字。\n"
    "注意区分：画面里的人物、角色、动物、立绘都是用户正在看的内容，"
    "不是用户本人，也不是你；描述时写“画面里的角色”，不要写成“主人在画面里”。\n"
    "画面类型（视频截图/静态图片/游戏画面/网页/软件界面）拿不准就写“不确定”，不要硬猜。\n"
    "没有的项写“无”。严格按下面格式输出：\n"
    "场景：（音乐播放器/写代码/看视频/游戏/聊天/网页/文档/桌面等）\n"
    "画面类型：（视频截图/静态图片/游戏画面/网页/软件界面，拿不准写“不确定”）\n"
    "主要内容：（一句话，有歌名、标题、文件名就写出来）\n"
    "关键信息：（最多3条，如数字、报错、人物）\n"
    "理解：（用1到2句话说清：这是什么、想表达什么或发生了什么、"
    "画面里最值得一说的一个细节；看不懂就写“看不懂”）\n"
    "状态：（一句话，用户在做什么、什么状态）\n"
    "证据：（2到3条，用分号隔开）"
)


def is_default_vision_system(text):
    """判断给定规则文本是否等于任一历史默认值（含当前默认）。"""
    t = (text or "").strip()
    if not t:
        return True
    if t == VISION_SYSTEM_DEFAULT.strip():
        return True
    for old in VISION_SYSTEM_DEFAULTS_HISTORY:
        if t == old.strip():
            return True
    return False

# 视觉请求的 user 消息固定部分（配合 system 使用）
VISION_USER_PROMPT = "描述这张屏幕截图。"
# 视觉描述请求：只描述可见内容（两步流程第一步用）
LOOK_SCREEN_PROMPT = (
    "描述这张屏幕截图。"
)
TICK = 20

LINES = [
    "哼，今天也勉强陪你一会儿。",
    "才、才没有在等你呢。",
    "笨蛋，记得喝水啦。",
    "有事快说，我可是很忙的……好吧，也没那么忙。",
    "唔……你在看什么呀。",
    "别一直盯着我，会、会不好意思的。",
    "我这么可爱，你该感谢我才对。",
    "算了，看在你这么闲的份上，理你一下。",
    "喂，偶尔也夸夸我嘛。",
    "困了……但才不睡，要陪你。",
    "你要是敢走，我就……就生气。",
    "哼，今天心情不错，特许你多聊两句。",
    "笨蛋主人，别老熬夜。",
]
REACT_LINES = [
    "去别的地方玩！不要耽误AGI训练！",
    "真赶不走啊你！",
    "压力一只蓝色大肥鱼？",
    "我不评价这个了，这是你的私人癖好。",
    "大肥鱼坐的住",
    "你这吃白饭的用户！",
    "这些家伙真粘人，赶都赶不走",
]
INNER_LINES = [
    "（哼，其实我有点在意他……才不说。）",
    "（要不要主动搭话呢……算了，笨蛋会自己来。）",
    "（刚才那句话是不是太凶了……有点后悔。）",
    "（他好像很忙，我不打扰……就看着。）",
    "（被摸头的感觉……唔，才不讨厌。）",
    "（今天也没被投喂……算了，我不饿。）",
    "（刚才笑了一下，太丢人了。）",
    "（笨蛋，快点回来啊……我只是随便想想。）",
]
DRAG_LINES = ["哇——轻点轻点！笨蛋！",
               "起飞咯——才不是喜欢这样。",
               "放我下来！……好吧，再玩一次。",
               "晕鱼了晕鱼了……都怪你。",
               "别、别晃这么厉害啦！"]
FOOD_LINES = {
    "🐟": ["哼，算你有心，我才不是想吃呢……真好吃。",
           "小鱼干！才、才没有很期待啦。",
           "咔嚓咔嚓……笨蛋，别看着我吃。",
           "这个味道……还、还不错。",
           "再来一条也不是不行……你听见了吗？"],
    "🍰": ["蛋糕？哼，勉强收下吧……甜的。",
           "甜到冒泡泡了……才不是高兴。",
           "嗝～又圆了一圈，都怪你！",
           "这块归我了，别抢！",
           "笨蛋，吃完要陪我运动……我是说散步。"],
    "🍭": ["棒棒糖！转圈圈……咳，我很冷静。",
           "嘎嘣脆，好、好吃。",
           "才没有很开心，只是糖比较甜。",
           "这个颜色好看……不是说你！",
           "再来一根？我、我随便问问。"],
    "🍡": ["三色团子！软乎乎……唔。",
           "糯叽叽的，爱、爱了啦。",
           "你居然记得我喜欢这个……笨蛋。",
           "分你一个？才不要，自己买。",
           "甜度刚好，勉强表扬你一下。"],
    "💎": ["钻、钻石？！笨蛋，这么贵重的……收下了啦！",
           "这能吃吗……咕咚。真香！",
           "发财啦！明天开始吃高级鱼粮！",
           "你哪来的钱？……不管了，谢谢你。",
           "哼，下次不许乱花钱……但可以再来一颗。"],
}
FOODS = ["🐟", "🍰", "🍭", "🍡", "💎"]

# 食物 -> 吃饭动画（dsh-pet 画师原画）
FOOD_ANIM = {
    "🐟": "eat_fish",      # 吃大闸蟹
    "🍰": "eat_cake",      # 吃冰淇淋融化
    "🍭": "eat_candy",     # 吃糖葫芦
    "🍡": "eat_dango",     # 吃汤圆
    "💎": "eat_gem",       # 吃 Token（DeepSeek 梗）
}

# ===== 主动陪伴（第 23 批）=====
# 触发条件（全部满足才说话）
PROACTIVE_CHECK_SEC = 7            # 主动搭话轮询间隔（秒，5~10）
PROACTIVE_IDLE_MIN = 600           # 宠物已静默多久（秒）→ 10 分钟
PROACTIVE_SAME_APP_MIN = 1200      # 同一应用连续用多久（秒）→ 20 分钟
PROACTIVE_COOLDOWN_MIN = 900       # 两次主动说话最短间隔（秒）→ 15 分钟
PROACTIVE_DAILY_MAX = 8            # 每天最多主动几次
PROACTIVE_SKIP_SLEEP = True        # 睡觉中不主动说话

# 主动关怀台词（傲娇风，按场景分组）
PROACTIVE_LINES = {
    # 长时间用同一应用
    "long_session": [
        "这都坐了一个多小时了吧……我、我不是在关心你。",
        "笨蛋，一直盯着同一个窗口，眼睛要不要休息一下啊。",
        "哼，这么久了还没弄完？要不要我陪你骂它两句。",
        "喂，你是不是忘了还有我在这儿。",
        "……我有点无聊了，你忙你的吧，我就在这儿。",
        "坐了这么久，屁股不麻吗？",
    ],
    # 深夜
    "late_night": [
        "都这么晚了，笨蛋，该睡觉了。",
        "熬夜会变丑的……我才不是担心你。",
        "哼，你再不睡，我就先睡了。",
        "这个点了还不睡，明天起得来吗？",
    ],
    # 很久没互动
    "lonely": [
        "唔……你已经很久没理我了。",
        "喂，还记得我在这儿吗？",
        "才、才不是想让你陪我……只是问问。",
        "哼，我一个人也能待得住。",
        "有点冷清呢……你忙完了吗？",
    ],
    # 午饭/晚饭时间
    "meal": [
        "这个点了，你吃了吗？别又忘了。",
        "喂，该吃饭了吧，笨蛋。",
        "哼，饿着肚子干活，效率只会更差。",
    ],
}

# ===== 宠物显示状态（唯一事实来源，不允许另存字符串/布尔值） =====
PET_VISIBLE = "VISIBLE"          # 真实可见
PET_HIDDEN = "HIDDEN"            # 手动隐藏（写入配置 display=false）
PET_TEMP_HIDDEN = "TEMP_HIDDEN"  # 临时隐藏（截屏守卫 / 小游戏，不写配置）
PET_STATE_TEXT = {
    PET_VISIBLE: "显示中",
    PET_HIDDEN: "已隐藏",
    PET_TEMP_HIDDEN: "暂时隐藏",
}
# 状态变化的 reason（只用于日志/区分来源，不参与判定）

# 模式中文名（列表与下拉共用）
PET_MODE_TEXT = {
    "still": "原地待着",
    "wander": "自由散步",
    "follow": "跟随鼠标",
}

# ===== 定时提醒 =====
DEFAULT_REMINDS_MIN = 45
# 每类独立默认间隔（分钟）：喝水 45 / 起身 60 / 护眼 20
DEFAULT_REMIND_MIN_BY_KIND = {"water": 45, "stand": 60, "eyes": 20,
                            "sleep": 120, "meal": 180}
REMINDERS = {
    "water": "喝水提醒",
    "stand": "起身活动提醒",
    "eyes":  "护眼提醒",
    "sleep": "深夜催睡",
    "meal":  "饭点提醒",
}
# 每类图标（emoji 本体）
REMIND_ICONS = {"water": "💧", "stand": "🚶", "eyes": "👀",
                "todo": "✅", "schedule": "⏰",
                "sleep": "🌙", "meal": "🍚"}
# 提醒气泡备用图标（clock 用于定时/待办兜底）
REMIND_ICON_CLOCK = "⏰"
# 圆形图标底色（按类型）
REMIND_ICON_COLORS = {
    "water":    QColor(0x4A, 0x90, 0xD9),   # 蓝底
    "stand":    QColor(0x4C, 0xAF, 0x50),   # 绿底
    "eyes":     QColor(0x8E, 0x6F, 0xD4),   # 紫底
    "todo":     QColor(0xF0, 0x93, 0x2F),   # 橙底
    "schedule": QColor(0x9E, 0xA4, 0xAE),   # 灰底
    "sleep":    QColor(0x5C, 0x6B, 0xC0),   # 靛蓝底
    "meal":     QColor(0xE0, 0x7A, 0x5F),   # 砖红底
}
REMIND_ICON_DIAMETER = 32.0             # 圆形图标直径（逻辑像素，随缩放等比）
# 每类标题（加粗、字号比正文小一号）
REMIND_TITLES = {
    "water":    "喝水时间",
    "sleep":    "该睡觉了",
    "meal":     "吃饭时间",
    "stand":    "起来动一动",
    "eyes":     "让眼睛休息一下",
    "todo":     "待办提醒",
    "schedule": "定时提醒",
}
REMIND_ICON_FONT = "Segoe UI Emoji"     # emoji 专用字体，保证清晰
REMIND_BUBBLE_MIN_W = 280.0             # 气泡最小宽度
DEFAULT_REMINDERS = {
    k: {"on": True, "min": DEFAULT_REMIND_MIN_BY_KIND[k]} for k in REMINDERS
}

# ---- 提醒气泡 ----
REMIND_BUBBLE_HOLD = 12.0          # 提醒气泡停留 12 秒
REMIND_WALK_MAX_MS = 2000          # 走到屏幕中间最长耗时 2 秒
REMIND_WALK_SPEED = 620.0          # 提醒走动基础速度（像素/秒）
REMIND_WALK_SPEED_MAX = 2600.0     # 距离很远时的速度上限
REMIND_VERTICAL_BIAS = 0.58        # 垂直居中偏下（0.5=正中，越大越靠下）
REMIND_BUBBLE_SCALE = 1.3          # 字号/宽度放大 1.3 倍
REMIND_SNOOZE_MIN = 10             # 「10 分钟后再提醒」默认分钟
# 免打扰默认时段
DND_DEFAULT = {"enabled": True, "start": "22:00", "end": "08:00"}
# 待办 / 自定义定时提醒的重复方式
TODO_REPEATS = (
    ("仅一次", "once"),
    ("每天", "daily"),
    ("工作日", "workday"),
)
# 待办最多条数
TODO_MAX = 20
# 提醒台词：傲娇可爱，但**必须含明确动作词**
#   喝水类 → "喝水" / "喝一口"
#   起身类 → "起来走走" / "站起来"
#   护眼类 → "看远处" / "闭眼" + "20 秒"
WATER_LINES = [
    "哼，我这种水生生物都知道喝水，你学着点！",
    "笨蛋主人，喝一口水，现在就去。",
    "嘴巴都干了吧？快去喝水，才不是担心你。",
    "杯子在那边，去喝一口……别让我说第二遍。",
    "喂，先喝水，别的等一下再说。",
    "哼，嘴唇都起皮了，快喝一口。",
    "喝水啦，我才不是特意来催你的。",
    "笨蛋，喝一口水，就当给我个面子。",
]
STAND_LINES = [
    "坐太久啦！站起来走两步，我陪你。",
    "哼，久坐伤身，起来走走嘛，我等你。",
    "腰要断了吧？站起来伸个懒腰。",
    "笨蛋，再坐下去我就生气了，快起来动动。",
    "起来走走，别把椅子坐穿了。",
    "喂，站起来活动一下，就一下。",
    "哼，腿都麻了吧？起来走走。",
    "笨蛋，去倒杯水顺便站起来走走。",
]
EYES_LINES = [
    "看看远处的东西，闭眼 20 秒，听见没？",
    "眼睛都红了吧？去看窗外，闭眼 20 秒就好。",
    "屏幕要被你盯穿了，看远处休息 20 秒。",
    "哼，闭眼 20 秒，我才不是心疼你。",
    "喂，看远处 20 秒，眼睛要休息。",
    "笨蛋，别揉了，闭眼 20 秒。",
    "眼睛酸了吧？看远处 20 秒。",
    "哼，闭眼 20 秒，不许偷看屏幕。",
]
# 待办 / 定时用 {内容} 占位（{content} 会被替换成待办文字）
TODO_LINES = [
    "笨蛋主人，别忘了：{content}。",
    "该做这个了：{content}，快去，别让我催。",
    "哼，{content}，现在就去办。",
    "别忘了{content}，我可是记着的。",
    "喂，{content}，别装没看见。",
    "笨蛋，{content}，快去快去。",
    "{content}……去做吧，我等你。",
    "哼，{content}，别拖到明天。",
]
# ===== 主动关心引擎（阶段①：只含框架参数，不含台词/开关）=====
# priority 越大越优先；cooldown/daily_max 的实际值：
#   water/stand/eyes 沿用 cfg["reminders"][kind]["min"]，不用这里的默认值。
#   这里只给「上限」和「优先级」。
CARE_KINDS = {
    "water": {"name": "喝水",   "priority": 30, "daily_max": 8},
    "stand": {"name": "起身",   "priority": 40, "daily_max": 6},
    "eyes":  {"name": "护眼",   "priority": 30, "daily_max": 8},
    # 深夜催睡：只在 23:30 之后检查，优先级最高
    "sleep": {"name": "催睡",   "priority": 90, "daily_max": 2,
              "window": ("23:30", "03:00")},
    # 饭点：11:30-13:00 / 17:30-19:00 两个窗口
    "meal":  {"name": "饭点",   "priority": 70, "daily_max": 3,
              "windows": [("11:30", "13:00"), ("17:30", "19:00")]},
    # 主动搭话：调模型，优先级最低（不挤占健康类）
    "chat":  {"name": "主动搭话", "priority": 20, "daily_max": 8,
              "model": True},
    # 记忆回访：模板拼接不调模型，每天最多 2 次
    "memory": {"name": "记忆回访", "priority": 80, "daily_max": 2},
}

# 待办 / 日程：不受每日上限、降频影响；忙时只延后不丢弃
CARE_EXEMPT_KINDS = ("todo", "schedule")

# 关心状态文件名
CARE_STATE_NAME = "care_state.json"

# 深夜催睡（傲娇风格，不带括号动作）
SLEEP_LINES = [
    "都几点了，还不睡？眼睛不要了是吧。",
    "喂，该睡了。明天起不来我可不叫你。",
    "哼，熬夜伤身体，去睡。",
    "笨蛋，再刷下去天就亮了。",
    "该睡了……我才不是在等你。",
    "这么晚还不睡，是想让我担心吗？",
    "喂，关灯睡觉，别磨蹭。",
    "哼，快去睡，明天还要见人呢。",
]

# 饭点提醒（傲娇风格，不带括号动作）
MEAL_LINES = [
    "该吃饭了，别又拖到胃疼。",
    "喂，饭点了，先吃再说。",
    "笨蛋，不吃饭哪有力气干活。",
    "到饭点了……我才不是催你。",
    "哼，快去吃饭，别让我说第二遍。",
    "该吃了，饿着肚子算什么本事。",
    "喂，放下手里的事，先吃饭。",
    "笨蛋，饭点到了，去吃点东西。",
]

# ===== 记忆回访话术（模板拼接，不调模型）=====
# {what} 会被替换成记忆内容里的「事件名」（已清洗掉将来时词）
# 注意：模板不要加「有」——{what} 可能是动词短语（过生日/去成都出差），
# 加「有」会变成「是不是有过生日」「是不是有去出差」这种不通的句子。
REVISIT_TODAY_LINES = [
    "喂，今天是不是{what}？别搞砸了。",
    "哼，今天{what}吧？我记着呢。",
    "今天{what}……加油，别紧张。",
    "别忘了今天{what}，笨蛋。",
    "今天{what}，我等你回来说结果。",
    "{what}是今天吧？去，别磨蹭。",
    "今天{what}。我、我可不是特意提醒你。",
    "喂，今天{what}，记好了没？",
]

REVISIT_YESTERDAY_LINES = [
    "昨天{what}怎么样？说说看。",
    "喂，昨天{what}结果如何？",
    "昨天{what}……顺利吗？",
    "哼，昨天{what}，别以为我忘了。",
    "昨天{what}怎么样，笨蛋？",
    "说说昨天{what}的结果。",
    "昨天{what}，我一直在等你说。",
    "昨天{what}的事，怎么样了？",
]

# ===== 好感度（阶段⑥）=====
# 只影响台词选择，不影响任何功能是否触发。
AFFECTION_INIT = 50              # 初始值
AFFECTION_MIN = 30               # 下限（不做愧疚绑架，不会更低）
AFFECTION_MAX = 100              # 上限
AFFECTION_DAILY_DOWN_MAX = 3     # 每天最多掉多少点

# 增减规则（每项都是「每次事件的增量」）
AFFECTION_GAIN_CHAT = 1          # 发一条消息
AFFECTION_GAIN_CHAT_DAILY_MAX = 10   # 每天靠聊天最多加多少
AFFECTION_GAIN_ACK = 2           # 回应一次关心（点「知道了」）
AFFECTION_LOSE_IGNORE = 1        # 忽略一次关心
AFFECTION_LOSE_IDLE_DAY = 3      # 连续 N 天未互动时每天掉多少
AFFECTION_IDLE_DAYS = 3          # 连续多少天算「久未互动」

# 区间（只影响台词池选择）
AFFECTION_TIERS = (
    (0, 30, "cold"),      # 冷淡：短句、少用「主人」
    (31, 60, "normal"),   # 正常
    (61, 100, "clingy"),  # 黏人：多用「主人」、偶尔撒娇
)


def affection_tier(value):
    """好感度 → 区间名（cold / normal / clingy）。"""
    try:
        v = int(value)
    except Exception:
        v = AFFECTION_INIT
    for lo, hi, name in AFFECTION_TIERS:
        if lo <= v <= hi:
            return name
    return "normal"


# 各区间专属台词（点击 / 戳 / 摸头 / 待机时用）
# cold：嘴硬别扭，短句，几乎不叫主人
AFFECTION_COLD_LINES = {
    "react": [
        "干嘛。",
        "……有事？",
        "别碰我。",
        "哼。",
        "说。",
        "别烦。",
        "嗯。",
        "走开啦。",
    ],
    "poke": [
        "别戳了。",
        "烦。",
        "……又来了。",
        "哼，无聊。",
    ],
    "stroke": [
        "……别乱摸。",
        "哼，谁准你摸的。",
        "……一下就好。",
    ],
}

# normal：现在的默认语气
AFFECTION_NORMAL_LINES = {
    "react": [
        "干嘛呀，笨蛋主人。",
        "哼，叫我做什么。",
        "在呢在呢，别催。",
        "……有事快说。",
        "嗯？想我了？",
    ],
    "poke": [
        "喂，别戳我啦。",
        "哼，再戳就生气了。",
        "笨蛋，痒。",
    ],
    "stroke": [
        "……摸头也不是不行。",
        "哼，就一下哦。",
        "别以为这样我就会高兴。",
    ],
}

# clingy：黏人，多用「主人」，偶尔撒娇
AFFECTION_CLINGY_LINES = {
    "react": [
        "主人主人，我在这儿呢！",
        "嗯嗯，我一直在等主人叫我。",
        "主人~ 要抱抱吗？",
        "哼，主人终于想起我了。",
        "主人叫我做什么都可以哦。",
    ],
    "poke": [
        "主人别戳啦，痒痒的~",
        "嘿嘿，主人想跟我玩吗？",
        "主人再戳我就要抱你了哦。",
    ],
    "stroke": [
        "主人摸头……好舒服。",
        "再摸一会儿嘛，主人。",
        "嘿嘿，最喜欢主人了。",
    ],
}


def affection_lines(kind, value):
    """按好感度区间取某类台词池；取不到就退回 normal。"""
    tier = affection_tier(value)
    table = {"cold": AFFECTION_COLD_LINES,
             "normal": AFFECTION_NORMAL_LINES,
             "clingy": AFFECTION_CLINGY_LINES}
    pool = (table.get(tier) or {}).get(kind)
    if not pool:
        pool = AFFECTION_NORMAL_LINES.get(kind) or []
    return pool


REMIND_LINES = {
    "water": WATER_LINES,
    "stand": STAND_LINES,
    "eyes":  EYES_LINES,
    "todo":  TODO_LINES,
    "sleep": SLEEP_LINES,
    "meal":  MEAL_LINES,
}

# 点击反馈：点「知道了」/「稍后再提醒」后的短回话（1.5 秒小气泡）
REMIND_ACK_LINES = {
    "water": ["哼，这还差不多。", "喝完了？乖……才不是夸你。",
              "总算听话一次。", "嗯，继续忙你的吧。", "我记下了，别偷懒。"],
    "stand": ["起来就好……别又坐下。", "哼，这还差不多。",
              "动一动，人都精神了吧。", "好、好孩子……才不是夸你。",
              "记得再站起来走走。"],
    "eyes":  ["眼睛舒服点了吗？", "哼，闭眼就是好的。",
              "看完远处，继续加油吧。", "这还差不多，别硬撑。",
              "……我是在担心你，别误会。"],
    "todo":  ["办完就好，我记下了。", "哼，这还差不多。",
              "嗯，做完了就歇会儿。", "总算没忘，算你厉害。",
              "好了，我不催了。"],
    "schedule": ["到点啦，办完就歇着吧。", "哼，这还差不多。",
                 "记住这个点了没？", "嗯，我继续盯着时间。",
                 "好了，我不催了。"],
}
REMIND_SNOOZE_LINES = [
    "……就十分钟哦，不许赖皮。",
    "哼，那再给你十分钟。",
    "好吧好吧，十分钟后我再来。",
    "就这一次，别得寸进尺。",
    "十分钟，我盯着呢。",
]
REMIND_ACK_HOLD = 1.5               # 短气泡停留 1.5 秒

# ===== 触摸反应 =====
PAT_LINES = [
    "头发会乱的啦……不过，就一会儿的话，可以。",
    "才不是喜欢被摸头……再摸一下也行吧。",
    "唔……手法倒是不讨厌。",
    "别、别摸太久，我会当真的！",
    "（眯眼）……哼，舒服什么的才没有。",
    "笨蛋，摸头会长不高的……大概。",
]
POKE_LINES = [
    "戳什么戳啦！脸会变形的！",
    "再戳我、我就生气了！……真的哦！",
    "气鼓鼓！你到底想干嘛呀。",
    "脸皮很薄的，笨蛋！",
    "（鼓脸）我也是有脾气的鲸鱼……哼。",
    "别、别戳了，有点痒……还有点开心。",
]
STROKE_LINES = [
    "唔……就、就让你摸一小会儿。",
    "哼，舒、舒服什么的才没有！",
    "蹭蹭……才不是主动的。",
    "再来两下……我是说随便你啦。",
    "（蹭）手法还行，勉强及格。",
    "别停……啊不是，我没说什么！",
]
DRAG_THRESHOLD = 20      # 移动超过这个像素才算“拖拽”，否则可能是抚摸
STROKE_MAX_TRAVEL = 90   # 抚摸手势的总位移上限
STROKE_REVERSALS = 2     # 来回几次算抚摸

# ===== 小游戏：猜拳 =====
RPS_CHOICES = ["石头", "剪刀", "布"]
RPS_EMOJI = {"石头": "✊", "剪刀": "✌️", "布": "✋"}
RPS_BEATS = {"石头": "剪刀", "剪刀": "布", "布": "石头"}
RPS_WIN_LINES = [
    "哼，我赢了！还来吗？",
    "石头剪刀布，我可是专业的。",
    "这就是鲸鱼的预判，笨蛋。",
    "输的人请我吃小鱼干~",
    "赢啦，尾巴都翘起来了……才没有得意。",
]
RPS_LOSE_LINES = [
    "啊……我输了，不算不算！",
    "手滑了手滑了，再来一局！",
    "你居然能赢我？……哼。",
    "呜，我这是让着你的，笨蛋。",
    "输了，得吃条小鱼干压压惊。",
]
RPS_TIE_LINES = [
    "平局！再来一局啦。",
    "想到一块去了……哼，巧合而已。",
    "同出一样，重来重来。",
    "默契过头了吧……才没有高兴。",
    "这局不算，再来。",
]

# ===== 小游戏：接食物 =====
CATCH_DURATION = 30.0     # 秒
CATCH_SCORE_LINES = [
    "接到 {n} 个！你还挺准的嘛。",
    "得分 {n}！肚子有点饱了……谢啦。",
    "{n} 个！再来一局破纪录？我等着。",
    "只接到 {n} 个？手抖了吧，笨蛋。",
    "{n} 个……勉强及格，下次加油。",
]

# ===== 节日 / 生日台词（想改台词直接改这里） =====
# 节日窗口三段：ahead 提前预热几天、span 节日本身持续几天、after 过后回味几天
# 缺省 3 / 1 / 3；"只有当天值得说"的节日把 ahead/after 调小
FESTIVAL_WINDOW_DEFAULT = {"ahead": 3, "span": 1, "after": 3}
FESTIVAL_MD = {
    "元旦": {"md": "01-01", "ahead": 3, "span": 1, "after": 2},
    "情人节": {"md": "02-14", "ahead": 2, "span": 1, "after": 0},
    "国庆": {"md": "10-01", "ahead": 3, "span": 7, "after": 2},
    "圣诞": {"md": "12-25", "ahead": 3, "span": 1, "after": 2},
    "除夕": {"md": "12-31", "ahead": 1, "span": 1, "after": 0},
    "端午": {"md": "06-10", "ahead": 2, "span": 1, "after": 2},
    "七夕": {"md": "08-29", "ahead": 2, "span": 1, "after": 1},
    "万圣节": {"md": "10-31", "ahead": 3, "span": 1, "after": 1},
}
# 农历节日（春节/中秋）按年查表，想加年份照着写一行即可
LUNAR_FESTIVAL_DATES = {
    "春节": {2025: "01-29", 2026: "02-17", 2027: "02-06", 2028: "01-26", 2029: "02-13", 2030: "02-03"},
    "中秋": {2025: "10-06", 2026: "09-25", 2027: "09-15", 2028: "10-03", 2029: "09-22", 2030: "09-12"},
}
FESTIVAL_LINES = {
    "元旦": [
        "新年快乐，主人……才不是特意等零点呢。",
        "元旦啦！今年也要好好的，笨蛋。",
        "新的一年，第一句祝福……给你了。",
        "跨年成功，我还在这儿呢。",
        "元旦假期别乱跑，陪、陪我一下。",
    ],
    "春节": [
        "新年好，主人……红包我才不是在期待！",
        "过年啦，年夜饭分我一口。",
        "新的一年，鱼也要胖一点……都怪你。",
        "拜年啦，祝你万事顺意，笨蛋。",
        "春节快乐，鞭炮别吓到我啦。",
    ],
    "情人节": [
        "才、才不是在等巧克力呢！",
        "情人节……单身也没关系，我陪你。",
        "给你吐个小泡泡，情人节快乐。",
        "今天要和喜欢的人一起过哦……哼。",
        "巧克力分我一块，就一块，可以吗？",
    ],
    "中秋": [
        "月亮好圆……一起看也不是不行。",
        "中秋快乐，月饼分我一口啦。",
        "月亮好圆，像我的肚子……别说出去。",
        "中秋啦，记得抬头看看。",
        "蛋黄酥……我不是在讨要，只是问问。",
    ],
    "国庆": [
        "国庆快乐，假期好好休息，笨蛋。",
        "举国同庆，我也放假啦。",
        "出去玩记得带我……我不是黏人。",
        "七天假期，够我们玩个痛快了。",
        "国庆快乐！别加班了，听见没。",
    ],
    "圣诞": [
        "圣诞快乐……我的愿望是小鱼干，笨蛋。",
        "Merry Christmas！袜子挂好了吗？",
        "今晚有礼物吗？我、我只是随口问。",
        "下雪了吗？鲸鱼也想看看。",
        "圣诞快乐，陪你堆雪人也不是不行。",
    ],
}
BIRTHDAY_LINES = [
    "生、生日快乐！才不是特意记着的……礼物呢？",
    "生日蛋糕分我一块，就一块！",
    "祝你今天被全世界宠爱……包括我。",
    "许个愿吧，我负责吃掉蛋糕。",
    "又长大一岁啦，笨蛋。生日快乐。",
]

# ===== 截屏时自动隐藏 =====
SCREENSHOT_CLASSES = {
    "snipaste",                 # Snipaste 截图/贴图
    "screenclippinghost",       # Win10/11 截图 overlay
    "snippingtooloutlinerwindow",
    "snippingtoolframe",
    "sharex",                   # ShareX
    "regioncaptureform",
    "screencaptureform",
    "screenshotedgewindow",
}
SHELL_CLASSES = {
    "progman", "workerw", "shell_traywnd", "shell_secondarytraywnd",
    "shell_immersive_host",
}
# 已知截图工具进程名（可见的截图覆盖层出现时隐藏桌宠）
SCREENSHOT_PROCS = {
    "snipaste.exe", "sharex.exe", "snippingtool.exe", "screenclippinghost.exe",
    "screensketch.exe", "snipasteportable.exe",
}
# 截图工具的窗口标题（Windows 自带截图为「屏幕截图」，类名是 UWP 的 CoreWindow）
SCREENSHOT_TITLE_HINTS = (
    "屏幕截图", "截图工具", "截图", "截屏", "snip", "screenshot",
)


# ===== 聊天窗口主题（改这里就能换肤；会自动跟随系统深浅色） =====
CHAT_FONT_FAMILY = "Microsoft YaHei UI"
CHAT_CODE_FONT = "Consolas, 'Cascadia Mono', 'Courier New', monospace"
CHAT_FONT_SIZE = 13
CHAT_SMALL_SIZE = 11
CHAT_BUBBLE_RADIUS = 14
CHAT_BUBBLE_PAD = (13, 10, 13, 10)     # 左 上 右 下
CHAT_BUBBLE_MAXW = 460
CHAT_BUBBLE_GAP = 9                   # 气泡之间的间距
CHAT_AVATAR_SIZE = 30
CHAT_EMPTY_AVATAR = 150               # 空白状态头像（约 120px 高）
CHAT_LEFT_WIDTH = 180                 # 左栏固定宽
CHAT_LEFT_COLLAPSED = 34              # 收起后的窄条宽
CHAT_MIN_WIDTH = 560                  # 窗口最小宽度，避免内容被挤
CHAT_INPUT_MIN_H = 46                 # 输入框默认约 2 行
CHAT_INPUT_MAX_LINES = 6              # 最多 6 行自动增高

# 空白状态的欢迎语（傲娇风，随机取一条）
CHAT_EMPTY_GREETINGS = [
    "哼，有话快说~",
    "才不是在等你呢，说吧。",
    "笨蛋主人，今天想聊什么？",
    "别、别误会，我只是刚好在这儿。",
    "有事就讲啦，我又不会跑。",
    "唔……你想说什么就说吧。",
]

CHAT_THEME_LIGHT = {
    "window": "#f7f8fb",
    "sidebar": "#eef4fd",          # 左栏（淡蓝）
    "sidebar_sel": "#dbe9fc",      # 左栏选中项
    "secondary_bg": "#f2f5fb",     # 次要按钮底（比主按钮浅）
    "secondary_fg": "#5b6478",     # 次要按钮文字（灰蓝）
    "secondary_border": "#dfe5f0",  # 次要按钮描边
    "danger_fg": "#d9534f",        # 危险操作文字（删除）
    "fg": "#2c3040",
    "sub": "#8a8f9c",
    "card": "#ffffff",
    "border": "#e4e7ef",
    "user_bg": "#d7e7ff",
    "user_fg": "#173a63",
    "pet_bg": "#ffffff",
    "pet_fg": "#2c3040",
    "note_bg": "#fff6de",
    "note_fg": "#8a6d3b",
    "note_border": "#f0e0b0",
    "error_bg": "#ffe9e9",
    "error_fg": "#c0392b",
    "error_border": "#ffc2c2",
    "input_bg": "#ffffff",
    "input_border": "#dfe3ec",
    "btn_bg": "#eef1f8",
    "btn_fg": "#2c3040",
    "btn_border": "#e0e4ee",
    "accent": "#5686fe",
    "accent_fg": "#ffffff",
    "code_bg": "#f1f3f9",
    "scroll": "#e2e6ef",
    # ---- 语音卡片 ----
    "vc_card_bg": "rgba(255,255,255,0.88)",
    "vc_card_border": "#e6e9f5",
    "vc_play_bg": "#5b6cff",
    "vc_play_fg": "#ffffff",
    "vc_wave_on": "#5b6cff",
    "vc_wave_off": "#c3cbf5",
    "vc_time_fg": "#4a5170",
    "vc_rate_bg": "#eef0fd",
    "vc_rate_fg": "#4a5170",
    "vc_rate_border": "#dfe3f6",
    "vc_song_fg": "#1f2540",
    "vc_error_fg": "#c42b31",
    # ---- 底部按钮 / 菜单 ----
    "side_btn_fg": "#4a5170",
    "side_btn_hover": "#eef0fd",
    "menu_bg": "#ffffff",
    "menu_border": "#dfe3f6",
    "menu_item_fg": "#1f2540",
    "menu_item_hover": "#eef0fd",
    "menu_sep": "#e8ebf8",
    "menu_danger_fg": "#c42b31",
    "menu_danger_hover": "#fdeceb",
    "focus_bg": "#eef0fd",
}

CHAT_THEME_DARK = {
    "window": "#1e2026",
    "sidebar": "#232830",          # 左栏（深蓝灰）
    "sidebar_sel": "#2c3648",      # 左栏选中项
    "secondary_bg": "#2a2f38",     # 次要按钮底
    "secondary_fg": "#a8b0c0",     # 次要按钮文字
    "secondary_border": "#39404c",  # 次要按钮描边
    "danger_fg": "#ff7b72",        # 危险操作文字（删除）
    "fg": "#e6e8ee",
    "sub": "#9aa0ab",
    "card": "#272a31",
    "border": "#3a3e48",
    "user_bg": "#2f4a6f",
    "user_fg": "#eaf2ff",
    "pet_bg": "#272a31",
    "pet_fg": "#e6e8ee",
    "note_bg": "#3a3324",
    "note_fg": "#e8d8a8",
    "note_border": "#4d4430",
    "error_bg": "#3d2626",
    "error_fg": "#ffb3b3",
    "error_border": "#5a3333",
    "input_bg": "#24272e",
    "input_border": "#3a3e48",
    "btn_bg": "#2f333c",
    "btn_fg": "#e6e8ee",
    "btn_border": "#3f4450",
    "accent": "#4c7df0",
    "accent_fg": "#ffffff",
    "code_bg": "#22252c",
    "scroll": "#3a3e48",
    # ---- 语音卡片（深色）----
    "vc_card_bg": "rgba(39,42,49,0.92)",
    "vc_card_border": "#3a3f52",
    "vc_play_bg": "#6b7cff",
    "vc_play_fg": "#ffffff",
    "vc_wave_on": "#6b7cff",
    "vc_wave_off": "#3f4661",
    "vc_time_fg": "#aab2c8",
    "vc_rate_bg": "#2f3446",
    "vc_rate_fg": "#aab2c8",
    "vc_rate_border": "#3f4661",
    "vc_song_fg": "#e8ecf8",
    "vc_error_fg": "#ff9b93",
    # ---- 底部按钮 / 菜单（深色）----
    "side_btn_fg": "#aab2c8",
    "side_btn_hover": "#2f3446",
    "menu_bg": "#272a31",
    "menu_border": "#3a3f52",
    "menu_item_fg": "#e6e8ee",
    "menu_item_hover": "#2f3446",
    "menu_sep": "#3a3f52",
    "menu_danger_fg": "#ff7b72",
    "menu_danger_hover": "#3d2626",
    "focus_bg": "#2f3446",
}

_CHAT_THEME = CHAT_THEME_LIGHT


def chat_detect_theme():
    """按系统调色板判断深浅色（尽量跟随系统）"""
    try:
        app = QApplication.instance()
        if app is not None:
            pal = app.palette()
            if pal.color(QPalette.ColorRole.Window).lightness() < 128:
                return CHAT_THEME_DARK
    except Exception:
        pass
    return CHAT_THEME_LIGHT


def chat_qss(t):
    """聊天窗口整体样式表（颜色/字体集中在这里）"""
    return """
    QWidget#ChatRoot { background: %(window)s; }
    QScrollArea { border: none; background: transparent; }
    QWidget#ChatBody { background: transparent; }
    QTextEdit#ChatInput {
        background: %(input_bg)s; color: %(fg)s;
        border: 1px solid %(input_border)s; border-radius: 10px;
        padding: 8px; font-family: '%(font)s'; font-size: %(fs)dpx;
    }
    QPushButton {
        background: %(btn_bg)s; color: %(btn_fg)s;
        border: 1px solid %(btn_border)s; border-radius: 9px;
        padding: 6px 12px; font-family: '%(font)s'; font-size: 12px;
    }
    QPushButton:hover { background: %(accent)s; color: %(accent_fg)s; border-color: %(accent)s; }
    QPushButton:disabled { color: %(sub)s; }
    QPushButton#SendBtn {
        background: %(accent)s; color: %(accent_fg)s; border: 1px solid %(accent)s;
        font-weight: bold; min-width: 64px;
    }
    QPushButton#SendBtn:hover { background: %(accent)s; }
    QLabel#UsageLabel { color: %(sub)s; font-size: %(small)dpx; font-family: '%(font)s'; }
    QComboBox {
        background: %(input_bg)s; color: %(fg)s;
        border: 1px solid %(input_border)s; border-radius: 9px;
        padding: 5px 10px; font-family: '%(font)s'; font-size: %(fs)dpx;
    }
    QComboBox::drop-down { border: none; width: 20px; }
    QComboBox QAbstractItemView {
        background: %(card)s; color: %(fg)s;
        selection-background-color: %(accent)s; selection-color: %(accent_fg)s;
        border: 1px solid %(border)s;
    }
    QToolButton {
        background: %(btn_bg)s; color: %(btn_fg)s;
        border: 1px solid %(btn_border)s; border-radius: 9px;
        padding: 6px 10px; font-family: '%(font)s'; font-size: 12px;
    }
    QToolButton:hover { background: %(accent)s; color: %(accent_fg)s; }
    /* ---- 下拉菜单（设置）---- */
    QMenu {
        background: %(menu_bg)s; color: %(menu_item_fg)s;
        border: 1px solid %(menu_border)s; border-radius: 12px;
        padding: 6px; font-family: '%(font)s'; font-size: 13px;
    }
    QMenu::item {
        background: transparent; color: %(menu_item_fg)s;
        padding: 0 10px; margin: 1px 0;
        height: 32px; border-radius: 8px;
    }
    QMenu::item:selected { background: %(menu_item_hover)s; color: %(menu_item_fg)s; }
    QMenu::item:disabled { color: %(sub)s; }
    QMenu::separator {
        height: 1px; background: %(menu_sep)s; margin: 4px 6px;
    }
    QMenu::item#DangerAction { color: %(menu_danger_fg)s; }
    QMenu::item#DangerAction:selected {
        background: %(menu_danger_hover)s; color: %(menu_danger_fg)s;
    }

    /* ---- 左侧会话栏 ---- */
    QWidget#ChatSidebar { background: %(sidebar)s; border-right: 1px solid %(border)s; }
    QWidget#ChatSidebarCollapsed { background: %(sidebar)s; border-right: 1px solid %(border)s; }
    QPushButton#NewChatBtn {
        background: %(accent)s; color: %(accent_fg)s; border: 1px solid %(accent)s;
        font-weight: bold; border-radius: 9px; padding: 7px 8px;
    }
    QPushButton#NewChatBtn:hover { background: %(accent)s; }
    /* 底部按钮（记忆 / 设置）：高 36、圆角 10、透明底 */
    QPushButton#SideSmallBtn {
        background: transparent; color: %(side_btn_fg)s;
        border: none; border-radius: 10px;
        min-height: 36px; max-height: 36px;
        padding: 0 6px; font-size: 13px;
        outline: none;
    }
    QPushButton#SideSmallBtn:hover { background: %(side_btn_hover)s; }
    QPushButton#SideSmallBtn:pressed { background: %(side_btn_hover)s; }
    /* 键盘焦点：浅蓝底，不用系统蓝框 */
    QPushButton#SideSmallBtn:focus {
        background: %(focus_bg)s; outline: none; border: none;
    }
    /* 「设置」展开菜单时保持高亮（动态 objectName = SideSmallBtnActive）*/
    QPushButton#SideSmallBtnActive {
        background: %(side_btn_hover)s; color: %(side_btn_fg)s;
        border: none; border-radius: 10px;
        min-height: 36px; max-height: 36px;
        padding: 0 6px; font-size: 13px; outline: none;
    }
    QPushButton#CollapseBtn {
        background: transparent; color: %(sub)s; border: none; font-size: 14px;
        padding: 2px;
    }
    QPushButton#CollapseBtn:hover { color: %(accent)s; }
    QListWidget#SessionList {
        background: transparent; border: none; outline: none;
        font-family: '%(font)s'; font-size: 12px;
    }
    QListWidget#SessionList::item {
        color: %(fg)s; padding: 8px 9px; border-radius: 8px; margin: 2px 3px;
    }
    QListWidget#SessionList::item:hover { background: %(sidebar_sel)s; }
    QListWidget#SessionList::item:selected {
        background: %(sidebar_sel)s; color: %(accent)s; font-weight: bold;
    }
    QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
    QScrollBar::handle:vertical { background: %(scroll)s; border-radius: 4px; min-height: 24px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    """ % {
        "window": t["window"], "fg": t["fg"], "sub": t["sub"],
        "input_bg": t["input_bg"], "input_border": t["input_border"],
        "btn_bg": t["btn_bg"], "btn_fg": t["btn_fg"], "btn_border": t["btn_border"],
        "accent": t["accent"], "accent_fg": t["accent_fg"],
        "card": t["card"], "border": t["border"], "scroll": t["scroll"],
        "sidebar": t["sidebar"], "sidebar_sel": t["sidebar_sel"],
        "secondary_bg": t["secondary_bg"], "secondary_fg": t["secondary_fg"],
        "secondary_border": t["secondary_border"], "danger_fg": t["danger_fg"],
        "font": CHAT_FONT_FAMILY, "fs": CHAT_FONT_SIZE, "small": CHAT_SMALL_SIZE,
        # ---- 新增：底部按钮 / 菜单 ----
        "side_btn_fg": t["side_btn_fg"], "side_btn_hover": t["side_btn_hover"],
        "menu_bg": t["menu_bg"], "menu_border": t["menu_border"],
        "menu_item_fg": t["menu_item_fg"], "menu_item_hover": t["menu_item_hover"],
        "menu_sep": t["menu_sep"], "menu_danger_fg": t["menu_danger_fg"],
        "menu_danger_hover": t["menu_danger_hover"], "focus_bg": t["focus_bg"],
        "vc_error_fg": t["vc_error_fg"],
    }


def chat_bubble_qss(role, t=None):
    """单个气泡的样式"""
    t = t or _CHAT_THEME
    if role == "user":
        return ("QFrame#Bubble{background:%s;border-radius:%dpx;}"
                "QLabel#BubbleText{color:%s;font-size:%dpx;font-family:'%s';}" % (
                    t["user_bg"], CHAT_BUBBLE_RADIUS, t["user_fg"], CHAT_FONT_SIZE,
                    CHAT_FONT_FAMILY))
    if role == "error":
        return ("QFrame#Bubble{background:%s;border:1px solid %s;border-radius:%dpx;}"
                "QLabel#BubbleText{color:%s;font-size:%dpx;font-family:'%s';}" % (
                    t["error_bg"], t["error_border"], CHAT_BUBBLE_RADIUS,
                    t["error_fg"], CHAT_FONT_SIZE, CHAT_FONT_FAMILY))
    if role == "note":
        return ("QFrame#Bubble{background:%s;border:1px solid %s;border-radius:%dpx;}"
                "QLabel#BubbleText{color:%s;font-size:%dpx;font-family:'%s';}" % (
                    t["note_bg"], t["note_border"], CHAT_BUBBLE_RADIUS,
                    t["note_fg"], CHAT_SMALL_SIZE, CHAT_FONT_FAMILY))
    return ("QFrame#Bubble{background:%s;border:1px solid %s;border-radius:%dpx;}"
            "QLabel#BubbleText{color:%s;font-size:%dpx;font-family:'%s';}" % (
                t["pet_bg"], t["border"], CHAT_BUBBLE_RADIUS,
                t["pet_fg"], CHAT_FONT_SIZE, CHAT_FONT_FAMILY))


_CHAT_CODE_RE = re.compile(r"```([^\n`]*)\n?(.*?)```", re.S)


def _html_escape_text(s):
    if not s:
        return ""
    return html.escape(s, quote=False).replace("\n", "<br>")


def chat_render_html(text):
    """纯文本 → 气泡 HTML：``` 代码块用等宽字体，普通段落保留换行"""
    text = text or ""
    t = _CHAT_THEME
    out = []
    pos = 0
    for m in _CHAT_CODE_RE.finditer(text):
        before = text[pos:m.start()]
        if before:
            out.append(_html_escape_text(before))
        code = m.group(2)
        out.append(
            "<pre style=\"font-family:%s;background:%s;margin:6px 0;padding:8px;"
            "border-radius:6px;white-space:pre-wrap;\">%s</pre>" % (
                CHAT_CODE_FONT, t["code_bg"], html.escape(code, quote=False)))
        pos = m.end()
    tail = text[pos:]
    if tail or not out:
        out.append(_html_escape_text(tail))
    return "".join(out)


def chat_text_width(text, font):
    """按纯文本估算气泡宽度（代码块按等宽近似）"""
    try:
        fm = QFontMetrics(font)
        w = 0
        for line in (text or "").splitlines() or [""]:
            plain = line.strip("`")
            w = max(w, fm.horizontalAdvance(plain))
        return min(CHAT_BUBBLE_MAXW, max(80, w + 12))
    except Exception:
        return 240


# ===== 动画链（每只宠物独立运行） =====
ANIM_CROSS_FADE_MS = 150
# 类别权重默认值（可被 config.json 的 animation_weights 覆盖）
ACTION_CATEGORY_WEIGHTS = {"idle": 10, "turn": 5, "move": 5}
# name: category 类别 / weight 自身权重 / duration 时长(秒) / moves 是否移动
# 可选字段：
#   tier        small / medium / large（默认 small）—— 决定冷却与调度概率
#   cooldown    同动作冷却秒数（不填则按 tier 默认）
#   time_of_day morning / noon / evening / night / any（只提高权重，不强制）
#   frames_dir / fps / loop / hold_last / facing —— 帧序列动画（见第 12 节）
#   turn_from / turn_to / reverse / mirror —— 转向帧动画（见 13 节）
ACTIONS = {
    # ---- 待机：小动作（高权重、短、无冷却）----
    "idle_breath":  {"category": "idle", "weight": 6, "duration": 10.0, "moves": False,
                     "tier": "small", "fps": 12, "loop": True, "facing": "front"},
    "idle_stretch": {"category": "idle", "weight": 3, "duration": 10.0, "moves": False,
                     "tier": "medium", "fps": 12, "loop": False, "hold_last": False,
                     "facing": "front"},
    # ---- 素材动作（dsh-pet 画师原画，已通过 anim_audit 审计）----
    "idle_hum":     {"category": "idle", "weight": 3, "duration": 10.0, "moves": False,
                     "tier": "small", "fps": 12, "loop": False, "facing": "front"},
    "idle_fan":     {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                     "tier": "small", "time_of_day": "noon",
                     "fps": 12, "loop": False, "facing": "front"},
    "idle_rubeye":  {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                     "tier": "medium", "cooldown": 30.0,
                     "fps": 12, "loop": False, "facing": "front"},
    "idle_shy2":    {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                     "tier": "medium", "cooldown": 60.0,
                     "fps": 12, "loop": False, "facing": "front"},
    "idle_bubble":  {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                     "tier": "large", "cooldown": 180.0,
                     "fps": 12, "loop": False, "facing": "front"},
    "idle_play":    {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                     "tier": "large", "cooldown": 180.0,
                     "fps": 12, "loop": False, "facing": "front"},
    "idle_360":     {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                     "tier": "large", "cooldown": 240.0,
                     "fps": 12, "loop": False, "facing": "front"},
    "idle_whale":   {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                     "tier": "large", "cooldown": 300.0,
                     "fps": 12, "loop": False, "facing": "front"},
    "idle_dance":   {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                     "tier": "large", "cooldown": 240.0, "time_of_day": "evening",
                     "fps": 12, "loop": False, "facing": "front"},
    "idle_music":   {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                     "tier": "large", "cooldown": 300.0,
                     "fps": 12, "loop": False, "facing": "front"},
    "idle_flute":   {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                     "tier": "large", "cooldown": 300.0,
                     "fps": 12, "loop": True, "facing": "front"},
    "idle_code":    {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                     "tier": "large", "cooldown": 300.0, "time_of_day": "night",
                     "fps": 12, "loop": False, "facing": "front"},
    "idle_brush":   {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                     "tier": "large", "cooldown": 300.0, "time_of_day": "morning",
                     "fps": 12, "loop": False, "facing": "front"},
    # ---- 待机：犯困 / 久坐 / 睡觉（久无互动时权重提升）----
    "idle_yawn":    {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                     "tier": "medium", "cooldown": 30.0, "time_of_day": "night",
                     "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_rubeye":  {"category": "idle", "weight": 2, "duration": 1.0, "moves": False,
                     "tier": "medium", "cooldown": 30.0, "time_of_day": "night"},
    "idle_lie":     {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                     "tier": "large", "cooldown": 180.0},
    "idle_sleep":   {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                     "tier": "large", "cooldown": 240.0,
                     "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    # ---- 转向 ----
    # ---- 扩展待机池（30 个，画师原画）----
    "idle_cat": {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 240,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_swing": {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 300,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_horse": {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 300,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_kick": {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                  "tier": "medium", "cooldown": 90,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_kite": {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 300, "time_of_day": "下午",
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_balloon": {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                  "tier": "medium", "cooldown": 90,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_top": {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                  "tier": "medium", "cooldown": 90,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_gift": {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 300,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_dove": {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 300,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_magic": {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 300,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_juggle": {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 300,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_chess": {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 300,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_animal": {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 360,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_bfly": {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 360, "time_of_day": "上午",
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_flower": {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 360,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_maid": {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 360,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_record": {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                  "tier": "medium", "cooldown": 120,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_dress": {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 360,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_snack": {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                  "tier": "medium", "cooldown": 120,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_water": {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                  "tier": "medium", "cooldown": 120, "time_of_day": "中午",
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_mad": {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                  "tier": "medium", "cooldown": 150,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_scare": {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                  "tier": "medium", "cooldown": 150,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_knock": {"category": "idle", "weight": 3, "duration": 10.0, "moves": False,
                  "tier": "small",
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_grab": {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                  "tier": "medium", "cooldown": 90,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_car": {"category": "idle", "weight": 1, "duration": 10.0, "moves": False,
                  "tier": "large", "cooldown": 300,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_squat": {"category": "idle", "weight": 3, "duration": 10.0, "moves": False,
                  "tier": "small",
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_wake": {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                  "tier": "medium", "cooldown": 120,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_tail": {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                  "tier": "medium", "cooldown": 90,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_look": {"category": "idle", "weight": 4, "duration": 10.0, "moves": False,
                  "tier": "small",
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    "idle_run": {"category": "idle", "weight": 2, "duration": 10.0, "moves": False,
                  "tier": "medium", "cooldown": 90,
                  "fps": 12, "loop": False, "hold_last": False, "facing": "front"},
    # ---- 吃饭动作（不参与随机链，只由喂食触发；weight=0）----
    "eat_fish":   {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                   "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "eat_cake":   {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                   "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "eat_candy":  {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                   "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "eat_dango":  {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                   "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "eat_gem":    {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                   "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "eat_rice":   {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                   "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "eat_hotpot": {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                   "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "eat_water":  {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                   "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "eat_snack":  {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                   "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    # ---- 工作状态动作（不参与随机链，只由聊天状态触发；weight=0）----
    "work_think":  {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                    "tier": "large", "fps": 8, "loop": True, "facing": "front"},
    "work_busy":   {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                    "tier": "large", "fps": 8, "loop": True, "facing": "front"},
    "work_wait":   {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                    "tier": "large", "fps": 8, "loop": True, "facing": "front"},
    "work_ok":     {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                    "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "work_err":    {"category": "event", "weight": 0, "duration": 4.88, "moves": False,
                    "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    # ---- 余额动作（不参与随机链，只由「查看余额」触发；weight=0）----
    "bal_full":  {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                  "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "bal_good":  {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                  "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "bal_ok":    {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                  "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "bal_low":   {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                  "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "bal_empty": {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                  "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "bal_zero":  {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                  "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    # ---- 节日动作（不参与随机链，只由时节特效触发；weight=0）----
    "fx_newyear2":  {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                     "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "fx_dragon":    {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                     "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "fx_midautumn": {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                     "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "fx_halloween": {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                     "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "fx_xmas":      {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                     "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "fx_firework":  {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                     "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "fx_winter":    {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                     "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "fx_chrysanth": {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                     "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    "fx_river":     {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                     "tier": "large", "fps": 8, "loop": False, "facing": "front"},
    # ---- 拖拽（不参与随机链）----
    "drag":         {"category": "event", "weight": 0, "duration": 10.0, "moves": False,
                     "tier": "large", "fps": 12, "loop": True, "facing": "front"},
    # ---- 移动 ----
    "walk":         {"category": "move", "weight": 6, "duration": 10.0, "moves": True,
                     "tier": "small", "fps": 12, "loop": True, "facing": "front"},
    "hop":          {"category": "move", "weight": 4, "duration": 10.0, "moves": True,
                     "tier": "small", "fps": 12, "loop": True, "facing": "front"},
}
# 内部朝向映射：不是可播动作，只给 _set_dir / _steer_or_pick 用
TURN_DIRS = {"turn_left": ("left", -1), "turn_right": ("right", 1),
             "turn_up": ("up", None), "turn_down": ("down", None)}

# ===== 转向样式（代码转身过程，3D 翻转感）=====
TURN_STYLE = {
    "duration_ms": 460,     # 整个转身过程时长（够长才不"跳帧"）
    "squash_x": 0.30,       # 最窄处横向压到原来的比例（0.3 兼顾隐蔽与自然）
    "crouch": 0.045,        # 下蹲比例（垂直轻微压缩，体积守恒感）
    "overshoot": 0.08,      # 展开时的过冲回弹（别太大，避免"果冻"感）
    "hop": 3.0,             # 结束时小跳高度（像素，别太大）
    "line_chance": 0.30,    # 冒小气泡的概率
    "lines": ["嗯？", "哼！", "嗯……", "唔？", "看什么看。", "才没在看你。"],
    # 3D 翻转感：压扁时纵向补偿 + 明暗变化
    "volume_keep": 0.06,    # 纵向补偿强度（极小，只做"有厚度"的暗示）
    "shade": 0.28,          # 最窄处的变暗比例（模拟侧面受光）
    "edge_hold": 0.16,      # 在极窄处停留的时间比例（让切换更隐蔽）
}
TURN_STRENGTH_CHOICES = (("弱", 0.55), ("中", 0.80), ("强", 1.00))
TURN_STRENGTH_DEFAULT = 1.00

# ===== 代码变形动作幅度 =====
# 说明：*_sy 类形变以脚底线为锚向上生长，幅度过大会让宠物"突然长高"，
# 因此纵向幅度控制在 ~6% 以内，横向/旋转可以大一些。
ACTION_STYLE = {
    "breath_amp": 0.022,    # 呼吸起伏（峰峰 ≈4.4%）
    "sway_deg": 8.5,        # 摇摆角度
    "stretch_sy": 0.06,     # 伸懒腰垂直拉伸（配合 sx 收窄，像人伸懒腰）
    "stretch_sx": -0.045,
    "look_dx": 12.0,        # 东张西望位移
    "look_rot": 3.5,        # 东张西望附加的轻微转头
    "walk_dy": 9.0,         # 走动上下浮动
    "pat_rot": 11.0,
    "pat_sy": -0.055,
    "poke_sx": 0.16,
    "poke_dx": 6.0,
    "stroke_rot": 9.0,
    "stroke_dx": 7.5,
    "turn_rot": 9.0,
    "jump_dy": 30.0,
    # 坐下 / 趴着 / 睡觉：用"下压 + 横向变宽"表现，而不是单纯拉高
    "sit_sy": -0.10,        # 坐下：略微压低
    "sit_sx": 0.05,         # 坐下：略微变宽（重心下沉感）
    "lie_sy": -0.095,
    "lie_sx": 0.095,
    "sleep_sy": -0.09,
    "sleep_sx": 0.05,
    "yawn_sy": 0.055,       # 打哈欠：小幅拉伸 + 微后仰
    "yawn_rot": -2.5,
    "doze_sy": -0.05,       # 犯困：低头下压
    "doze_dy": 2.5,
}
ACTION_STRENGTH_CHOICES = (("弱", 0.55), ("中", 0.80), ("强", 1.00))
ACTION_STRENGTH_DEFAULT = 1.00

# ===== 翻唱短评（本地文案，不调模型）=====
# 唱完短评：翻唱成功后跟在语音卡片后面发一句。
# {song} 会被替换成歌名（没有歌名时整句不带书名号版本）。
COVER_DONE_LINES = [
    "哼，《{song}》唱完啦……才、才不是特地唱给你听的。",
    "唱好了。《{song}》，就、就这一次哦。",
    "《{song}》给你唱了，别、别一副理所当然的样子。",
    "听出来了吧？《{song}》我可是认真唱的。",
    "嗓子有点累了……《{song}》还行吧？",
    "喏，《{song}》。好听就说好听嘛，笨蛋。",
    "这首歌练过的，别以为我随口就唱。",
    "唱完啦。要夸就快点夸，我等着呢。",
    "《{song}》送你了……才没有别的意思。",
    "怎么样？我唱得比原唱好吧，哼。",
]

# 翻唱失败短评：下载/分离/换音色失败时发一句，代替原来直接甩错误详情。
# 注意：这些句子不指明具体失败环节（搜索/下载/分离/换音色/混音）。
# 真实原因写在 error.log 的 [翻唱] 行里，界面只需人设化的一句。
COVER_FAIL_LINES = [
    "唔……这次没唱成，出了点小状况。",
    "失败啦。不是我不肯唱，是它不配合。",
    "这次没弄好……等会儿再试一次好不好。",
    "这歌暂时唱不了，换一首吧。",
    "唔……又没成功，笨蛋，别催我。",
    "没弄成。我也很烦，下次一定唱给你听。",
    "嗓子准备好了，可它掉链子了。",
    "唔，失败了。要不……你再点一次？",
]

# 翻唱进行中又来一次翻唱请求：不启动第二个任务，回一句傲娇短话。
# 不带括号动作，不指明环节。
COVER_BUSY_LINES = [
    "上一首还没唱完呢，等会儿嘛。",
    "急什么，我这还在唱着呢。",
    "唔……一次只能唱一首，笨蛋。",
    "别催，唱完这首再说。",
    "上一首还没好，你倒是挺心急。",
    "排队去，我只有一张嘴。",
]

# ===== 情绪反馈（聊天内容 → 动作）=====# ===== 情绪反馈（聊天内容 → 动作）=====
# 规则：按顺序匹配关键词，命中第一个就播对应动作。
# 只匹配"用户说的话"和"宠物回复"，用于让动作跟上情绪。
MOOD_ACTIONS = (
    # (情绪名, 关键词组, 动作, 冷却秒数)
    ("shy", ("害羞", "脸红", "不好意思", "夸", "可爱", "喜欢你", "爱你",
             "想你", "抱抱", "摸摸"), "idle_shy2", 20),
    ("happy", ("哈哈", "开心", "太好了", "成功", "搞定", "棒", "厉害",
               "谢谢", "耶"), "idle_dance", 20),
    ("angry", ("生气", "讨厌", "烦", "滚", "闭嘴", "笨", "蠢", "不行",
               "生气"), "idle_mad", 20),
    ("sad", ("难过", "伤心", "哭", "累", "失败", "完蛋", "郁闷",
             "难受"), "idle_rubeye", 25),
    ("tired", ("困", "睡", "熬夜", "好晚", "累了", "休息"), "idle_yawn", 25),
    ("surprise", ("居然", "竟然", "什么", "真的假的", "不会吧",
                  "震惊", "哇"), "idle_scare", 20),
    ("play", ("玩", "游戏", "一起", "陪我", "无聊"), "idle_play", 25),
    ("music", ("歌", "音乐", "唱", "听歌", "旋律"), "idle_music", 25),
)

# 情绪动作的全局冷却（避免连续几条消息反复播）
MOOD_COOLDOWN_DEFAULT = 20

# ===== 工作状态联动（照 dsh-pet 的 6 档）=====
# 档位 -> (动作名, 是否常驻循环, 冒泡文案)
WORK_STATES = {
    "thinking": ("work_think", True, None),          # 模型开始思考（请求已发出）
    "working": ("work_busy", True, None),            # 首个字到达，正在输出
    "waiting": ("work_wait", True, None),            # 等待用户确认/输入
    "success": ("work_ok", False, None),             # 一轮完成
    "error": ("work_err", False, None),              # 出错
    "idle": (None, False, None),                     # 空闲，回到正常动画链
}
WORK_STATE_ORDER = ("thinking", "working", "waiting", "success", "error", "idle")
# 完成/出错这类终态动画播完后自动回 idle 的等待秒数
WORK_RESULT_HOLD = 2.2

# ===== 待机分级（tier）=====
# 选 tier 的概率，可在 config.json 的 animation_tiers 覆盖
ANIMATION_TIERS_DEFAULT = {"small": 0.70, "medium": 0.25, "large": 0.05}
# 同一动作的冷却（秒）
TIER_COOLDOWN = {"small": 0.0, "medium": 30.0, "large": 180.0}
# 该 tier 两次出现之间的全局最短间隔（秒）
TIER_GLOBAL_GAP = {"small": 0.0, "medium": 15.0, "large": 60.0}
VALID_TIERS = ("small", "medium", "large")
# 有帧动画时，同一动作的代码变形版本权重打折
FRAME_CODE_WEIGHT = 0.40

# ===== 久无互动升级 =====
IDLE_BORED_AFTER = 300.0      # 5 分钟：打哈欠/犯困权重提高
IDLE_SIT_AFTER = 600.0        # 10 分钟：进入坐下/趴着
IDLE_SLEEP_AFTER = 1200.0     # 20 分钟：进入睡觉
IDLE_BORED_BOOST = 3.0        # 犯困类权重倍数
SLEEP_ZZZ_LINES = ["Zzz…", "呼…呼…", "（睡着了）", "（哼，梦里也在嫌弃你）", "（别走……）"]
IDLE_TIRED_ACTIONS = ("idle_yawn", "idle_rubeye")
IDLE_SIT_ACTIONS = ("idle_lie", "idle_sleep")
IDLE_SLEEP_ACTIONS = ("idle_sleep",)

# ===== 连招（COMBOS）：整段播放，被打断即终止 =====
COMBOS = {
    "yawn_rub_stretch": {
        "tier": "large",
        "steps": ["idle_yawn", "idle_rubeye", "idle_stretch"],
        "duration": [1.2, 1.0, 1.5],
        "label": "打哈欠 → 揉眼睛 → 伸懒腰",
    },
}

# 时段划分：9 档（照 dsh-pet 的 SLOTS，比原来 4 档细）
# [起始小时, 时段]，按起始小时从晚到早排列，匹配时取第一个不超过当前小时的档
TIME_SLOTS = [
    (23, "深夜"),
    (19, "晚上"),
    (17, "傍晚"),
    (13, "下午"),
    (11, "中午"),
    (9,  "上午"),
    (7,  "早上"),
    (5,  "清晨"),
    (0,  "凌晨"),
]
# 动作的 time_of_day 支持 9 档 + any；同时保留旧的 4 档名做兼容映射
TIME_SLOT_ALIAS = {
    "morning": ("清晨", "早上", "上午"),
    "noon": ("中午",),
    "evening": ("下午", "傍晚", "晚上"),
    "night": ("晚上", "深夜", "凌晨"),
}
VALID_TIME_OF_DAY = tuple([name for _h, name in TIME_SLOTS]) + (
    "morning", "noon", "evening", "night", "any")


def current_time_slot(now=None):
    """返回 9 档之一，如「深夜」「下午」。"""
    h = (now or datetime.datetime.now()).hour
    for start, name in TIME_SLOTS:
        if h >= start:
            return name
    return "凌晨"


def current_time_of_day(now=None):
    """兼容旧接口：返回 4 档之一（morning/noon/evening/night）。"""
    slot = current_time_slot(now)
    for key, names in TIME_SLOT_ALIAS.items():
        if slot in names:
            return key
    return "night"


def time_matches(tag, now=None):
    """动作的 time_of_day 标签是否命中当前时段（支持 9 档与旧的 4 档）。"""
    if not tag or tag == "any":
        return True
    slot = current_time_slot(now)
    if tag == slot:
        return True
    # 旧 4 档写法：命中它覆盖的任一细档即算命中
    names = TIME_SLOT_ALIAS.get(tag)
    return bool(names and slot in names)



# ===== 帧序列动画（sprites/anim/<动作名>/0001.png ...） =====
FRAME_ANIM_DIR = os.path.join(SPRITE_DIR, "anim")
FRAME_ANIM_MAX_CACHE = 4        # 最多同时缓存多少个动作的帧（LRU 淘汰；120 帧/动作约 25MB）
FRAME_ANIM_EXTS = (".png",)
FRAME_ANIM_FPS_DEFAULT = 12.0
# 动作 spec 里可选的帧动画字段：
#   frames_dir  帧目录名（默认 = 动作名，位于 sprites/anim/ 下）
#   fps         播放帧率
#   loop        是否循环（False 时播完停在最后一帧或回到代码变形）
#   hold_last   非循环时是否保持最后一帧
#   facing      front / side / back（决定左右镜像与朝向映射）
FRAME_FIELDS = ("frames_dir", "fps", "loop", "hold_last", "facing")
VALID_FACING = ("front", "side", "back")
FACING_TO_NAME = {"front": "正面", "side": "侧面", "back": "背面"}


class FrameAnimCache:
    """帧序列缓存：按 (动作名, 高度) 缓存预缩放后的 QPixmap 列表，LRU 淘汰。"""

    def __init__(self, max_items=FRAME_ANIM_MAX_CACHE):
        self.max_items = max(1, int(max_items))
        self._cache = {}          # key -> {"frames": [QPixmap], "bytes": int, "seq": int}
        self._order = []          # key 使用顺序（末尾最新）
        self._seq = 0
        self._failed = set()      # 加载失败过的 key，避免反复读盘
        self.errors = []          # 最近的错误（同时写 error.log）

    # ---------- 路径 ----------
    @staticmethod
    def dir_for(action, spec):
        sub = (spec or {}).get("frames_dir") or action
        return os.path.join(FRAME_ANIM_DIR, sub)

    @staticmethod
    def list_frames(action, spec):
        """列出目录里全部 png，按文件名排序。返回 (paths, error)。"""
        d = FrameAnimCache.dir_for(action, spec)
        if not os.path.isdir(d):
            return [], "帧目录不存在: %s" % d
        try:
            names = [n for n in os.listdir(d)
                     if os.path.splitext(n)[1].lower() in FRAME_ANIM_EXTS]
        except Exception as ex:
            return [], "读取帧目录失败 %s: %s" % (d, ex)
        if not names:
            return [], "帧目录为空（没有 png）: %s" % d
        names.sort()
        return [os.path.join(d, n) for n in names], None

    # ---------- 加载 ----------
    def get(self, action, spec, height):
        """返回预缩放到 height 的帧列表；失败返回 (None, error)。"""
        key = (action, int(height))
        if key in self._failed:
            return None, self._last_error.get(key, "此前加载失败")
        item = self._cache.get(key)
        if item is not None:
            self._touch(key)
            return item["frames"], None
        frames, err = self._load(action, spec, int(height))
        if err:
            self._failed.add(key)
            self._last_error = getattr(self, "_last_error", {})
            self._last_error[key] = err
            self.errors.append(err)
            return None, err
        total = sum(p.width() * p.height() * 4 for p in frames)
        self._cache[key] = {"frames": frames, "bytes": total}
        self._order.append(key)
        self._evict()
        return frames, None

    def _load(self, action, spec, height):
        paths, err = self.list_frames(action, spec)
        if err:
            return None, err
        frames = []
        sizes = set()
        for path in paths:
            pix = QPixmap(path)
            if pix.isNull():
                return None, "帧图片无法读取: %s" % path
            sizes.add((pix.width(), pix.height()))
            frames.append(pix)
        if len(sizes) > 1:
            # 尺寸不一致：统一缩放到第一帧的画布尺寸（保持脚底线对齐），并记录
            w0, h0 = frames[0].width(), frames[0].height()
            fixed = []
            for pix in frames:
                if (pix.width(), pix.height()) != (w0, h0):
                    fixed.append(pix.scaled(w0, h0, Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation))
                else:
                    fixed.append(pix)
            frames = fixed
            msg = ("帧尺寸不一致（%s），已统一到 %dx%d：%s"
                   % (action, w0, h0, FrameAnimCache.dir_for(action, spec)))
            self.errors.append(msg)
        # 预缩放到当前宠物尺寸
        if height > 0:
            scaled = []
            for pix in frames:
                if pix.height() != height:
                    scaled.append(pix.scaledToHeight(height,
                                                     Qt.TransformationMode.SmoothTransformation))
                else:
                    scaled.append(pix)
            frames = scaled
        if not frames:
            return None, "没有可用帧: %s" % action
        return frames, None

    # ---------- LRU ----------
    def _touch(self, key):
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)

    def _evict(self):
        while len(self._cache) > self.max_items:
            old = self._order.pop(0)
            self._cache.pop(old, None)

    # ---------- 统计 / 清理 ----------
    def stats(self):
        """返回 (动作数, 估算字节数)。"""
        n = len(self._cache)
        total = sum(it["bytes"] for it in self._cache.values())
        return n, total

    def clear(self, action=None):
        if action is None:
            self._cache.clear()
            self._order.clear()
            self._failed.clear()
            return
        for key in list(self._cache):
            if key[0] == action:
                self._cache.pop(key, None)
                if key in self._order:
                    self._order.remove(key)
        for key in list(self._failed):
            if key[0] == action:
                self._failed.discard(key)

    def loaded_actions(self):
        return sorted({k[0] for k in self._cache})


_FRAME_CACHE = FrameAnimCache()


def action_spec(action):
    """动作配置：优先 ACTIONS（动画链），其次 CLICK_RESPONSES（点击回应等临时动作）。"""
    spec = ACTIONS.get(action)
    if spec:
        return spec
    return CLICK_RESPONSES.get(action)


def frame_anim_spec(action):
    """取动作的帧动画配置；没有 frames_dir 且目录不存在则返回 None。"""
    spec = action_spec(action)
    if not spec:
        return None
    if spec.get("frames_dir"):
        return spec
    # 没显式声明时，若默认目录存在也视为帧动画
    if os.path.isdir(os.path.join(FRAME_ANIM_DIR, action)):
        return spec
    return None


def action_has_frames(action):
    """这个动作是否应该走帧序列（已声明或目录存在）。"""
    return frame_anim_spec(action) is not None



# 单击回应：代码变换 + 专属台词，不连续重复
CLICK_RESPONSES = {
    "happy_jump": {
        "duration": 10.0,
        "lines": ["哼，才不是因为开心才跳的！", "被戳到了……稍、稍微有点高兴而已！",
                  "才没有很兴奋呢，只是腿有点痒~", "跳、跳给你看就是了，别一直盯着！",
                  "笨蛋，笑什么笑……我也没那么高兴啦。", "尾巴不听使唤了，才、才不是我控制的！"],
        # 这个素材带镜头推近（脚底漂移 43px），靠 camera_fix 补偿回同一视觉大小
        "fps": 12, "loop": False, "hold_last": False, "facing": "front",
    },
    "shy": {
        "duration": 10.0,
        "lines": ["别、别突然碰我啊！笨蛋……", "脸才没有红！是光线问题！",
                  "唔……看、看什么看啦。", "才不是害羞，只是有点热而已！",
                  "再这样我要躲起来了……真的。", "你、你别笑，我会生气的……大概。"],
        "fps": 12, "loop": False, "hold_last": False, "facing": "front",
    },
    "tsundere": {
        "duration": 10.0,
        "lines": ["哼，有事快说，我可是很忙的。", "才不是在等你呢，只是刚好站在这里。",
                  "别误会，我只是随便动动而已。", "哼！谁要因为你高兴啊……",
                  "笨蛋，这么简单的事也要我提醒？", "看在你来了的份上，勉强理你一下。"],
        "fps": 12, "loop": False, "hold_last": False, "facing": "front",
    },
    "tickle": {
        "duration": 10.0,
        "lines": ["呀！别挠了……哈哈，住手啦！", "痒、痒死了！不许再来！……好吧，再一下下。",
                  "哈哈哈不行了，我投降！", "别、别挠那里……笨蛋！",
                  "笑到肚子疼了啦，快停下~", "再挠我就……就……哈哈没力气了。"],
        "fps": 12, "loop": False, "hold_last": False, "facing": "front",
    },
    "wave": {
        "duration": 10.0,
        "fps": 12, "loop": False, "hold_last": False, "facing": "front",
        "lines": ["哟！今天也勉强欢迎你一下~", "哼，来了就好，我才没有一直等。",
                  "挥、挥手而已，别想太多！", "看到你了啦……嗯，挺高兴的。",
                  "笨蛋，来得这么晚，我都等困了。", "来啦？那……坐下吧，我不赶你。"],
    },
}
# Q 弹挤压曲线：结束时用一条衰减正弦做「压扁再回弹」
SQUASH_CURVE = {"freq": 3.0, "amp_x": 0.14, "amp_y": 0.12, "gain": 1.0, "decay": 2.2}
SQUASH_UNITS = 1.0        # squash_t 从 1 衰减到 0

# 动作中文名（缺省时菜单显示英文 key）
ACTION_LABELS = {
    "idle_breath": "呼吸", "idle_stretch": "伸懒腰",
    "walk": "走一段", "hop": "蹦跳一段",
    "idle_yawn": "打哈欠", "idle_rubeye": "照镜子",
    "idle_lie": "趴着", "idle_sleep": "睡觉",
    # 素材动作
    "idle_hum": "悠闲哼歌", "idle_fan": "摇扇纳凉", "idle_shy2": "屈膝礼仪",
    "idle_bubble": "吐泡泡", "idle_play": "玩魔方", "idle_360": "原地转圈",
    "idle_whale": "蓝鲸现世", "idle_dance": "可爱宅舞", "idle_music": "小提琴",
    "idle_flute": "吹笛子", "idle_code": "写代码", "idle_brush": "晨间刷牙",
    "drag": "被抓起",
    # 节日动作
    "idle_cat": "撸猫", "idle_swing": "荡秋千", "idle_horse": "骑木马", "idle_kick": "踢毽子", "idle_kite": "放风筝", "idle_balloon": "吹气球", "idle_top": "抽陀螺", "idle_gift": "拆礼物", "idle_dove": "变鸽子", "idle_magic": "扑克魔术",
    "idle_juggle": "三球抛接", "idle_chess": "下五子棋", "idle_animal": "动物环绕", "idle_bfly": "蝴蝶环绕", "idle_flower": "凭空生花", "idle_maid": "优雅女仆舞", "idle_record": "轻快记录", "idle_dress": "换装试色", "idle_snack": "偷吃零食", "idle_water": "玩水枪",
    "idle_mad": "气急败坏", "idle_scare": "被吓一跳", "idle_knock": "敲击桌面", "idle_grab": "跳起抓东西", "idle_car": "玩玩具车", "idle_squat": "原地蹲起", "idle_wake": "惊醒", "idle_tail": "尾巴拍地", "idle_look": "东张西望", "idle_run": "原地奔跑",
    "eat_fish": "吃大闸蟹", "eat_cake": "吃冰淇淋", "eat_candy": "吃糖葫芦",
    "eat_dango": "吃汤圆", "eat_gem": "吃 Token", "eat_rice": "吃白饭",
    "eat_hotpot": "涮火锅", "eat_water": "吃西瓜", "eat_snack": "大口吃零食",
    "work_think": "思考中", "work_busy": "忙碌中", "work_wait": "等待中",
    "work_ok": "完成庆祝", "work_err": "垂头叹气",
    "bal_full": "钱袋满溢", "bal_good": "金袋叮当", "bal_ok": "钱袋如常",
    "bal_low": "数金皱眉", "bal_empty": "袋空如洗", "bal_zero": "分文不剩",
    "fx_newyear2": "写福字", "fx_dragon": "舞狮头", "fx_midautumn": "赏月吃月饼",
    "fx_halloween": "讨糖南瓜灯", "fx_winter": "堆雪人", "fx_chrysanth": "赏菊",
    "fx_xmas": "装点圣诞树", "fx_firework": "放烟花",
    "fx_river": "放河灯",
}

# ===== 动作分类（菜单"动作"用；新增动作只要登记进 ACTIONS 就自动出现） =====
ACTION_CATEGORY_LABELS = {
    "idle": "待机",
    "turn": "转向",
    "move": "移动",
    "event": "节日",
}
# 余额分档：按已用百分比（满额按 ¥20 折算）分 6 档
BALANCE_FULL = 20.0
# 余额档位：照 dsh-pet 的分档法 index = (p == 100 ? 5 : floor(p / 20))
# 用专门画的余额素材，比通用动作贴题得多
BALANCE_TIERS = [
    # (已用百分比上限, 档位名, 动作, 台词)
    (20,  "满血", "bal_full", ["钱包满满的，放心聊啦~",
                             "余额充足……才不是因为安心才开心的。",
                             "哼，有钱就是硬气，随便使唤我。",
                             "这么多余额，够你烦我很久了。",
                             "别得意，钱是会花完的，笨蛋。"]),
    (40,  "健康", "bal_good", ["余额还挺健康，稳的……嗯。",
                             "叮当响，钱包没毛病。",
                             "还能再聊很久呢，慢慢来。",
                             "现在这个数，我看着还算安心。",
                             "别乱花就行……我才没有叮嘱你。"]),
    (60,  "正常", "bal_ok", ["余额有点少了哦，省着点用……才不是心疼。",
                           "用掉一半了，我、我得数数。",
                           "账目还清楚，别乱花就行。",
                           "记得别一次问太长的问题，笨蛋。",
                           "现在开始要精打细算了……大概。"]),
    (80,  "省钱", "bal_low", ["六成以上没了！喂，钱包在变瘦了。",
                            "省着点用……我可不想饿肚子。",
                            "笨蛋，再这样下去真要见底了。",
                            "我要开始记账了，别乱花。",
                            "现在每句话都要花钱，说重点！"]),
    (100, "告急", "bal_empty", ["快、快没钱了！笨蛋，快去充值！",
                               "只剩一点点了……我有点慌。",
                               "袋子空了，钱包在发抖。",
                               "再聊下去我就要饿肚子了啦！",
                               "喂，现在充值还来得及，快点。"]),
    (101, "见底", "bal_zero", ["见、见底了……主人，我要饿肚子了……",
                              "分文不剩了，我有点心虚。",
                              "已经没钱了吗？那我缩成一团。",
                              "去充值吧……我等你回来。",
                              "笨蛋，别再问了，我真的会哭的。"]),
]

# ===== 时节特效（纯代码绘制；预留图片素材接口） =====
# trigger: (月份, 起日, 止日) 或 lunar 名称；effect 对应下面绘制函数
SEASONAL_EFFECTS = {
    "spring_fireworks": {"label": "春节烟花", "trigger": ("02-01", "02-28"),
                         "lunar": "春节", "duration": 6.0},
    "national_fireworks": {"label": "国庆烟花", "trigger": ("10-01", "10-07"),
                           "duration": 6.0},
    "snow": {"label": "飘雪", "trigger": ("12-01", "02-15"), "duration": 8.0},
    "leaves": {"label": "秋季落叶", "trigger": ("09-15", "11-30"), "duration": 8.0},
    "petals": {"label": "春日花瓣", "trigger": ("03-01", "05-31"), "duration": 8.0},
    "moon": {"label": "中秋月亮", "trigger": None, "lunar": "中秋", "duration": 12.0},
    "pumpkin": {"label": "万圣节南瓜灯", "trigger": ("10-25", "11-02"), "duration": 10.0},
    "xmas": {"label": "圣诞装点圣诞树", "trigger": ("12-20", "12-26"), "duration": 10.0},
}
# 节日角色动作（用帧序列素材播放，配合/替代代码粒子特效）
# key 与 SEASONAL_EFFECTS 对应；value 是 sprites/anim 下的动作名
SEASONAL_ACTIONS = {
    "spring_fireworks": "fx_newyear2",    # 写福字
    "national_fireworks": "fx_firework",  # 放烟花（真实素材）
    "moon": "fx_midautumn",               # 中秋赏月吃月饼
    "pumpkin": "fx_halloween",            # 讨糖南瓜灯
    "xmas": "fx_xmas",                    # 装点圣诞树
    "snow": "fx_winter",                  # 堆雪人
    "leaves": "fx_chrysanth",             # 插茱萸赏菊
    "petals": "fx_river",                 # 放河灯（春日用）
}
SEASONAL_DEFAULT_ON = True
SEASONAL_AUTO_COOLDOWN = 1800      # 自动触发最短间隔（秒）
PARTICLE_MAX = 220                 # 粒子数量上限（性能保护）
# 图片素材接口：把 name 映射到 png 路径即可替换代码绘制；
# 为空时全部用代码粒子/叠加图形绘制。
SEASONAL_ASSETS = {
    # "snow": os.path.join(SPRITE_DIR, "fx", "snowflake.png"),
    # "petals": os.path.join(SPRITE_DIR, "fx", "petal.png"),
    # "moon": os.path.join(SPRITE_DIR, "fx", "moon.png"),
    # "pumpkin": os.path.join(SPRITE_DIR, "fx", "pumpkin.png"),
}

BALANCE_URL = "https://api.deepseek.com/user/balance"

# 智谱余额（实测可用，返回 data.availableBalance 等）
# GET https://open.bigmodel.cn/api/biz/account/query-customer-account-report
BALANCE_URL_ZHIPU = "https://open.bigmodel.cn/api/biz/account/query-customer-account-report"
# 余额来源：deepseek / zhipu（auto 时按当前配置自动判断）
BALANCE_PROVIDERS = (
    ("deepseek", "DeepSeek"),
    ("zhipu", "智谱 GLM"),
)
BALANCE_MIN_INTERVAL_MIN = 30      # 自动查询最短间隔（分钟）

# ===== 功能开关注册表（新增功能只需在这里登记一行，设置页会自动出现） =====
# key: 配置里 features 的键名
#   name          显示名
#   group         设置页里的分组
#   default       默认是否开启
#   desc          说明文字
#   needs_restart 改动后是否要重启才完全生效
FEATURES = {
    "reminders": {
        "name": "定时提醒", "group": "提醒", "default": True, "needs_restart": False,
        "desc": "喝水 / 起身活动 / 护眼定时冒泡（具体间隔在「提醒」页设置）",
    },
    "click_response": {
        "name": "点击回应", "group": "互动", "default": True, "needs_restart": False,
        "desc": "单击宠物时播放回应动作与台词；关闭后单击只有轻微蹦跳",
    },
    "festival_lines": {
        "name": "节日整点台词", "group": "互动", "default": True, "needs_restart": False,
        "desc": "节日 / 生日当天启动时说一次，之后每个整点再说一次",
    },
    "seasonal_fx": {
        "name": "时节特效", "group": "互动", "default": True, "needs_restart": False,
        "desc": "按日期自动播放烟花 / 飘雪 / 落叶 / 花瓣 / 月亮 / 南瓜灯",
    },
    "system_monitor": {
        "name": "系统监控播报", "group": "互动", "default": True, "needs_restart": False,
        "desc": "CPU / 内存 / 显卡温度过高时冒泡提醒",
    },
    "capture_hide": {
        "name": "截屏时隐藏", "group": "宠物", "default": True, "needs_restart": False,
        "desc": "从系统截图 / 录屏里排除，并在截图工具出现时自动隐藏（关闭后就能被拍到）",
    },
    "balance_auto": {
        "name": "余额自动查询", "group": "余额", "default": False, "needs_restart": False,
        "desc": "按间隔自动查询 DeepSeek 余额（手动「查看余额」始终可用）",
    },
    "auto_memory": {
        "name": "自动记忆", "group": "互动", "default": False, "needs_restart": False,
        "desc": "从对话中自动提取值得记住的个人信息；关闭后完全停止提取",
    },
    "idle_talk": {
        "name": "待机碎碎念", "group": "互动", "default": True, "needs_restart": False,
        "desc": "空闲时小概率冒出台词 / 思维链心声",
    },
    "physics": {
        "name": "甩抛物理", "group": "物理", "default": True, "needs_restart": False,
        "desc": "拖拽用弹簧跟随、松手按速度甩抛、落地反弹与挤压",
    },
    "remind_todo": {
        "name": "待办提醒", "group": "提醒", "default": True, "needs_restart": False,
        "desc": "到点提醒待办事项；聊天里说「提醒我 X 分钟后 xxx」也能加",
    },
    "remind_schedule": {
        "name": "定时提醒", "group": "提醒", "default": True, "needs_restart": False,
        "desc": "固定时刻提醒（如 12:00 吃饭），可设每天 / 工作日 / 仅一次",
    },
    "remind_dnd": {
        "name": "免打扰时段", "group": "提醒", "default": True, "needs_restart": False,
        "desc": "指定时段内不提醒；检测到全屏程序或演示模式也会自动暂停",
    },
    "remind_walk": {
        "name": "提醒时走到屏幕中间", "group": "提醒", "default": True, "needs_restart": False,
        "desc": "提醒时 main 宠物平滑走到屏幕中央偏下，结束后走回原位",
    },
}
FEATURE_DEFAULT_ON = {k: bool(v["default"]) for k, v in FEATURES.items()}
FEATURE_GROUP_ORDER = ["互动", "提醒", "物理"]
# 这些开关只在各自的专属设置页显示，不在「功能开关」页重复列出
FEATURE_HIDDEN_IN_LIST = {"capture_hide", "reminders", "remind_todo",
                          "remind_schedule", "remind_dnd", "remind_walk",
                          "balance_auto"}


def _default_features():
    return dict(FEATURE_DEFAULT_ON)


def _migrate_legacy_features(cfg, raw):
    """把旧开关迁进 features，避免同一开关存在两份。

    旧字段：
      - 顶层 hide_in_capture      -> features.capture_hide
      - seasonal_fx.enabled       -> features.seasonal_fx（seasonal_fx 只保留 auto）
    """
    feats = cfg.get("features")
    if not isinstance(feats, dict):
        feats = {}
    raw_feats = raw.get("features") if isinstance(raw, dict) else None
    if isinstance(raw_feats, dict):
        for k in FEATURES:
            v = raw_feats.get(k)
            if isinstance(v, bool):
                feats[k] = v
    for k, spec in FEATURES.items():
        if k not in feats:
            feats[k] = bool(spec["default"])
    return feats
BALANCE_TIER_LINES = {t[1]: t[3] for t in BALANCE_TIERS}

# 系统「显示动画」开关（SPI_GETCLIENTAREAANIMATION / SPI_GETANIMATION）
SPI_GETCLIENTAREAANIMATION = 0x1042
SPI_GETANIMATION = 0x0048
ANIMATION_MODE_CHOICES = (("跟随系统", "auto"), ("总是开启", "on"), ("总是关闭", "off"))


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def load_json(path, default):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                default,
                f,
                ensure_ascii=False,
                indent=4
            )
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as ex:
        # 配置损坏：先备份成 .bak，再用默认值继续，避免直接丢掉原文件
        try:
            bak = path + ".bak"
            shutil.copy2(path, bak)
            log("配置解析失败，已备份到 %s：%s" % (bak, ex), logging.ERROR)
        except Exception as ex2:
            log("配置解析失败，备份也失败: %s" % ex2, logging.ERROR)
        return default


# ===== 配置版本与默认值 =====
CONFIG_VERSION = 2
VALID_MODES = ("wander", "follow", "still")
VALID_SIZES = tuple(SIZE_LEVELS.values())


def _default_usage():
    return {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0,
            "completion_tokens": 0, "total_tokens": 0, "requests": 0}


def _default_games():
    return {"rps_win": 0, "rps_total": 0, "catch_best": 0, "catch_last": 0}


def _default_reminders():
    return {k: {"on": True, "min": DEFAULT_REMIND_MIN_BY_KIND[k]} for k in REMINDERS}


def _default_remind_extras():
    """提醒系统的扩展配置（第 18 批新增，缺省自动补齐）。"""
    return {
        # 稍后提醒分钟数
        "snooze_min": REMIND_SNOOZE_MIN,
        # 免打扰
        "dnd": dict(DND_DEFAULT),
        # 待办提醒
        "todos": [],
        # 自定义定时提醒
        "schedules": [],
        # 今日已确认计数
        "done_today": {"date": "", "water": 0, "stand": 0, "eyes": 0, "todo": 0},
    }


def default_pet_name(index):
    """按序号生成默认名字：主宠物 / 小鱼 2 / 小鱼 3 …"""
    if index <= 0:
        return "主宠物"
    return "小鱼 %d" % (index + 1)


def _default_pet(pet_id="main", index=0, name=None):
    return {"id": pet_id, "name": name or default_pet_name(index),
            "size": 0.7, "x": None, "y": None,
            "mode": "wander", "display": True}


def default_config():
    """完整的默认配置（字段与旧版一致，额外加 config_version / pets）"""
    return {
        "config_version": CONFIG_VERSION,
        "topmost": True,
        "passthrough": False,
        "autostart": False,
        "ds_api_key": "",
        "chat_base": "",          # 聊天接口地址（空则用 DS_BASE_URL）
        "chat_model": "",         # 聊天模型（空则用 DS_MODEL）
        "chat_migrated": False,   # 是否已完成旧配置迁移（只做一次）
        "city": "汕头",
        "birthday": "",
        "animation_mode": "auto",
        "animation_weights": dict(ACTION_CATEGORY_WEIGHTS),
        "physics": dict(DEFAULT_PHYSICS),
        "balance": {"auto": False, "provider": "auto", "interval_min": BALANCE_MIN_INTERVAL_MIN,
                    "last_value": None, "last_time": None},
        # 设置页可收缩区块的展开状态（只影响显示，不影响配置读写）
        "ui_sections": {"persona": True, "vision_rule": False,
                        "vision_advanced": False, "chat_model": True},
        "seasonal_fx": {"auto": True},
        "features": _default_features(),
        "frame_anim": {"enabled": True, "max_cache": FRAME_ANIM_MAX_CACHE},
        # 播放速率（倍速）：1.0 = 原速
        "anim_rate": ANIM_RATE_DEFAULT,
        # 气泡文字缩放：1.0 = 100%
        "bubble_scale": BUBBLE_SCALE_DEFAULT,
        # 隐藏时暂停动画解码（省 CPU）
        "pause_when_hidden": True,
        # 撞飞彩蛋（甩飞撞边缘时头部旋转）
        "smash_spin": True,
        # 旧「主动陪伴」（固定台词池）——保留字段兼容，不再使用
        "proactive": True,
        "proactive_cfg": {
            "idle_min": 10,
            "same_app_min": 20,
            "cooldown_min": 15,
            "daily_max": 8,
        },
        # 语音（引擎可选：Edge-TTS+RVC 或 VoxCPM2，走外部推理服务）
        "voice": {
            "enabled": False,        # 默认关，服务没起不请求
            # engine: "edge" = Edge-TTS + RVC（快，约 2 秒）
            #         "voxcpm" = VoxCPM2 直出（慢，48kHz 高音质，能克隆音色）
            "engine": "edge",
            "url": "http://127.0.0.1:9880",       # RVC 服务
            "vox_url": "http://127.0.0.1:9881",   # VoxCPM2 服务
            "vox_steps": 24,                      # VoxCPM 推理步数
            "vox_control": "",                    # 空=用服务端默认音色
            "timeout": 25,
            "volume": 80,
        },
        # 「主动搭话」（模型生成，工作时间段内发呆时触发）
        # 具体默认值由 proactive.default_cfg() 提供，这里只放最小占位
        "proactive_ai": {},
        # 「看看屏幕」配置
        "look_screen": {"vision_model": "", "vision_base": "", "vision_key": "",
                        "style": VISION_STYLE_DEFAULT,
                        "style": VISION_STYLE_DEFAULT,
                        "max_tokens": VISION_TOKENS_DEFAULT,
                        "vision_system": VISION_SYSTEM_DEFAULT,
                        "save_desc": False,
                        "save_shot": False,
                        "max_side": LOOK_SCREEN_SIDE_DEFAULT,
                        "quality": LOOK_SCREEN_QUALITY_DEFAULT,
                        "timeout": LOOK_SCREEN_TIMEOUT_DEFAULT},
        "reply_style": REPLY_STYLE_DEFAULT,
        "max_tokens": MAX_TOKENS_DEFAULT,
        "persona": "",          # 空 = 无风格（台词用中性池）
        "animation_tiers": dict(ANIMATION_TIERS_DEFAULT),
        "turn_strength": TURN_STRENGTH_DEFAULT,
        "action_strength": ACTION_STRENGTH_DEFAULT,
        "balance_full": BALANCE_FULL,
        "usage": _default_usage(),
        "games": _default_games(),
        "reminders": _default_reminders(),
        "pets": [_default_pet("main")],
    }


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _coerce_pet(raw, issues, idx):
    """校验单个 pets[i]，出错字段回落默认值并记录"""
    out = _default_pet("pet%d" % (idx + 1))
    if not isinstance(raw, dict):
        issues.append("pets[%d] 不是对象，已用默认宠物" % idx)
        return out
    pid = raw.get("id")
    if isinstance(pid, str) and pid.strip():
        out["id"] = pid.strip()[:40]
    else:
        issues.append("pets[%d].id" % idx)

    # name：缺省时按序号自动补（主宠物 / 小鱼 2 / 小鱼 3 …）
    nm = raw.get("name")
    if isinstance(nm, str) and nm.strip():
        out["name"] = nm.strip()[:24]
    elif nm is not None:
        issues.append("pets[%d].name" % idx)
    else:
        out["name"] = default_pet_name(idx)

    size = raw.get("size")
    if _is_number(size):
        size = float(size)
        best = min(VALID_SIZES, key=lambda s: abs(s - size))
        out["size"] = best if abs(best - size) < 0.06 else best
    else:
        issues.append("pets[%d].size" % idx)

    for key in ("x", "y"):
        v = raw.get(key)
        if v is None:
            out[key] = None
        elif isinstance(v, int) and not isinstance(v, bool):
            out[key] = v
        else:
            issues.append("pets[%d].%s" % (idx, key))
            out[key] = None

    mode = raw.get("mode")
    if mode in VALID_MODES:
        out["mode"] = mode
    else:
        issues.append("pets[%d].mode" % idx)

    disp = raw.get("display", True)
    if isinstance(disp, bool):
        out["display"] = disp
    else:
        issues.append("pets[%d].display" % idx)
    return out


def _valid_hhmm(v, default):
    """校验 "HH:MM" 格式。"""
    if not isinstance(v, str):
        return default
    t = v.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", t)
    if not m:
        return default
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return "%02d:%02d" % (hh, mm)
    return default


def _merge_remind_extras(cfg, raw, issues):
    """把 remind_extras 合并进 cfg（逐字段校验，非法回落默认）。

    旧 config.json 没有 remind_extras 时，全部用默认值，不影响原提醒功能。
    """
    base = _default_remind_extras()
    if not isinstance(raw, dict):
        if raw is not None:
            issues.append("remind_extras")
        cfg["remind_extras"] = base
        return
    out = base

    # snooze_min
    v = raw.get("snooze_min")
    if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 120:
        out["snooze_min"] = v
    elif v is not None:
        issues.append("remind_extras.snooze_min")

    # dnd
    dnd = raw.get("dnd")
    if isinstance(dnd, dict):
        e = dnd.get("enabled")
        if isinstance(e, bool):
            out["dnd"]["enabled"] = e
        elif e is not None:
            issues.append("remind_extras.dnd.enabled")
        out["dnd"]["start"] = _valid_hhmm(dnd.get("start"), DND_DEFAULT["start"])
        out["dnd"]["end"] = _valid_hhmm(dnd.get("end"), DND_DEFAULT["end"])
    elif dnd is not None:
        issues.append("remind_extras.dnd")

    # todos：最多 TODO_MAX 条
    todos = raw.get("todos")
    if isinstance(todos, list):
        cleaned = []
        for i, it in enumerate(todos[:TODO_MAX]):
            if not isinstance(it, dict):
                continue
            text = (it.get("text") or "").strip()
            if not text:
                continue
            cleaned.append({
                "id": str(it.get("id") or ("t%d" % (i + 1)))[:24],
                "text": text[:100],
                "enabled": bool(it.get("enabled", True)),
                "repeat": it.get("repeat") if it.get("repeat") in
                          [k for _l, k in TODO_REPEATS] else "once",
                "at": _valid_hhmm(it.get("at"), "12:00"),
                "due": float(it.get("due") or 0),
                "done": bool(it.get("done", False)),
            })
        out["todos"] = cleaned
    elif todos is not None:
        issues.append("remind_extras.todos")

    # schedules：固定时刻提醒
    sch = raw.get("schedules")
    if isinstance(sch, list):
        cleaned = []
        for i, it in enumerate(sch[:TODO_MAX]):
            if not isinstance(it, dict):
                continue
            at = _valid_hhmm(it.get("at"), "")
            if not at:
                continue
            cleaned.append({
                "id": str(it.get("id") or ("s%d" % (i + 1)))[:24],
                "name": (it.get("name") or "定时提醒")[:40],
                "enabled": bool(it.get("enabled", True)),
                "repeat": it.get("repeat") if it.get("repeat") in
                          [k for _l, k in TODO_REPEATS] else "daily",
                "at": at,
                "line": (it.get("line") or "")[:120],
                "done": bool(it.get("done", False)),
            })
        out["schedules"] = cleaned
    elif sch is not None:
        issues.append("remind_extras.schedules")

    # done_today
    dt = raw.get("done_today")
    if isinstance(dt, dict):
        out["done_today"]["date"] = str(dt.get("date") or "")[:10]
        for k in ("water", "stand", "eyes", "todo"):
            v = dt.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                out["done_today"][k] = v
    elif dt is not None:
        issues.append("remind_extras.done_today")

    cfg["remind_extras"] = out


def migrate_and_validate(raw):
    """把任意读到的 config 迁移到当前结构并逐字段校验。

    返回 (cfg, changed, issues)。changed=True 表示需要写回磁盘。
    """
    issues = []
    cfg = default_config()
    changed = False

    if not isinstance(raw, dict):
        log("config.json 顶层不是对象，使用默认配置", logging.ERROR)
        return cfg, True, ["<顶层>"]

    # 1) 旧格式迁移：顶层 mode/size/x/y -> pets[0]
    if not isinstance(raw.get("pets"), list) or not raw.get("pets"):
        old_mode = raw.get("mode", "wander")
        old_size = raw.get("size", 0.7)
        pet = _default_pet("main")
        pet["mode"] = old_mode if old_mode in VALID_MODES else "wander"
        try:
            pet["size"] = float(old_size) if _is_number(old_size) else 0.7
        except Exception:
            pet["size"] = 0.7
        pet["x"] = raw.get("x") if isinstance(raw.get("x"), int) else None
        pet["y"] = raw.get("y") if isinstance(raw.get("y"), int) else None
        cfg["pets"] = [pet]
        changed = True
        log("config.json 旧格式已迁移：顶层 mode/size/x/y -> pets[0](id=main)")
    else:
        cfg["pets"] = []
        seen = set()
        for i, item in enumerate(raw["pets"]):
            p = _coerce_pet(item, issues, i)
            if p["id"] in seen:
                p["id"] = "%s_%d" % (p["id"], i)
                issues.append("pets[%d].id 重复" % i)
            seen.add(p["id"])
            cfg["pets"].append(p)
        if not cfg["pets"]:
            cfg["pets"] = [_default_pet("main")]
            changed = True
            issues.append("pets 为空")
        if not any(p["id"] == "main" for p in cfg["pets"]):
            cfg["pets"][0]["id"] = "main"
            issues.append("pets 缺少 id=main")

    # 2) 标量字段
    for key in ("topmost", "passthrough", "autostart"):
        v = raw.get(key, cfg[key])
        if isinstance(v, bool):
            cfg[key] = v
        else:
            issues.append(key)

    for key in ("ds_api_key", "city", "birthday"):
        v = raw.get(key, cfg[key])
        if isinstance(v, str):
            cfg[key] = v
        else:
            issues.append(key)

    # animation_mode: auto/on/off
    am = raw.get("animation_mode")
    if am in ("auto", "on", "off"):
        cfg["animation_mode"] = am
    else:
        if am is not None:
            issues.append("animation_mode")
        changed = True   # 缺省时补写进配置，方便用户直接改

    # animation_weights: idle/turn/move 三个非负数
    aw = raw.get("animation_weights")
    if isinstance(aw, dict):
        for k in ACTION_CATEGORY_WEIGHTS:
            v = aw.get(k)
            if _is_number(v) and v >= 0:
                cfg["animation_weights"][k] = float(v)
            elif v is not None:
                issues.append("animation_weights.%s" % k)
            else:
                changed = True
    else:
        if aw is not None:
            issues.append("animation_weights")
        changed = True

    # physics: 甩抛/弹跳/摩擦/力度/顶部反弹/互撞
    ph, bad_ph = sanitize_physics(raw.get("physics"), DEFAULT_PHYSICS)
    cfg["physics"] = ph
    for k in bad_ph:
        issues.append(k)
    if not isinstance(raw.get("physics"), dict):
        changed = True

    # balance：自动查询开关 / 间隔 / 上次结果
    bl = raw.get("balance")
    if isinstance(bl, dict):
        v = bl.get("auto")
        if isinstance(v, bool):
            cfg["balance"]["auto"] = v
        elif v is not None:
            issues.append("balance.auto")
        v = bl.get("interval_min")
        if isinstance(v, int) and not isinstance(v, bool) and v >= BALANCE_MIN_INTERVAL_MIN:
            cfg["balance"]["interval_min"] = v
        elif v is not None:
            issues.append("balance.interval_min")
        v = bl.get("last_value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            cfg["balance"]["last_value"] = float(v)
        elif v is not None:
            issues.append("balance.last_value")
        v = bl.get("last_time")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            cfg["balance"]["last_time"] = float(v)
        elif v is not None:
            issues.append("balance.last_time")
    else:
        if bl is not None:
            issues.append("balance")
        changed = True

    # seasonal_fx：只保留"按日期自动触发"（总开关已迁到 features.seasonal_fx）
    sf = raw.get("seasonal_fx")
    if isinstance(sf, dict):
        v = sf.get("auto")
        if isinstance(v, bool):
            cfg["seasonal_fx"]["auto"] = v
        elif v is not None:
            issues.append("seasonal_fx.auto")
        if "enabled" in sf:
            changed = True       # 旧字段，写回时会被移除
    else:
        if sf is not None:
            issues.append("seasonal_fx")
        changed = True

    # features：功能开关（含旧开关一次性迁移）
    feats = _migrate_legacy_features(cfg, raw)
    has_new = isinstance(raw.get("features"), dict) and "capture_hide" in raw["features"]
    # 旧顶层 hide_in_capture -> features.capture_hide
    # 只有在新格式里还没有该键时才迁移，避免每次加载都覆盖用户后来改的值
    legacy_hide = raw.get("hide_in_capture")
    if isinstance(legacy_hide, bool):
        if not has_new:
            feats["capture_hide"] = legacy_hide
        changed = True        # 旧字段要写回时清掉
    # 旧 seasonal_fx.enabled -> features.seasonal_fx（同样只在缺失时迁移）
    if isinstance(sf, dict) and isinstance(sf.get("enabled"), bool):
        if "seasonal_fx" not in (raw.get("features") or {}):
            feats["seasonal_fx"] = bool(sf["enabled"])
        changed = True
    cfg["features"] = feats

    # frame_anim：帧动画总开关 / 缓存上限
    fa = raw.get("frame_anim")
    if isinstance(fa, dict):
        v = fa.get("enabled")
        if isinstance(v, bool):
            cfg["frame_anim"]["enabled"] = v
        elif v is not None:
            issues.append("frame_anim.enabled")
        v = fa.get("max_cache")
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 64:
            cfg["frame_anim"]["max_cache"] = v
            _FRAME_CACHE.max_items = v
        elif v is not None:
            issues.append("frame_anim.max_cache")
    else:
        if fa is not None:
            issues.append("frame_anim")
        changed = True

    # persona：自定义人设（空串 = 用默认人设；限制长度防配置爆炸）
    pv = raw.get("persona")
    if isinstance(pv, str):
        cfg["persona"] = pv[:4000]
    elif pv is not None:
        issues.append("persona")
    if pv is None:
        changed = True

    # anim_rate：播放速率
    ar = raw.get("anim_rate")
    if _is_number(ar):
        best = min(ANIM_RATE_CHOICES, key=lambda r: abs(r - float(ar)))
        cfg["anim_rate"] = best
    elif ar is not None:
        issues.append("anim_rate")

    # bubble_scale：气泡文字缩放
    bs = raw.get("bubble_scale")
    if _is_number(bs):
        cfg["bubble_scale"] = max(BUBBLE_SCALE_MIN, min(BUBBLE_SCALE_MAX, float(bs)))
    elif bs is not None:
        issues.append("bubble_scale")

    # proactive_cfg：主动陪伴阈值
    pc = raw.get("proactive_cfg")
    if isinstance(pc, dict):
        _plimits = (("idle_min", 1, 180), ("same_app_min", 1, 480),
                    ("cooldown_min", 1, 240), ("daily_max", 1, 50))
        for _k, _lo, _hi in _plimits:
            _v = pc.get(_k)
            if isinstance(_v, int) and not isinstance(_v, bool) and _lo <= _v <= _hi:
                cfg["proactive_cfg"][_k] = _v
            elif _v is not None:
                issues.append("proactive_cfg.%s" % _k)
    elif pc is not None:
        issues.append("proactive_cfg")

    # voice：语音服务配置（逐字段校验，缺失的用默认值补齐）
    _vc = raw.get("voice")
    if isinstance(_vc, dict):
        _vd = cfg["voice"]          # default_config 里的完整默认值
        if isinstance(_vc.get("enabled"), bool):
            _vd["enabled"] = _vc["enabled"]
        _eng = _vc.get("engine")
        if _eng in ("edge", "voxcpm"):
            _vd["engine"] = _eng
        for _k in ("url", "vox_url"):
            _v = _vc.get(_k)
            if isinstance(_v, str) and _v.strip():
                _vd[_k] = _v.strip().rstrip("/")
        if isinstance(_vc.get("vox_control"), str):
            _vd["vox_control"] = _vc["vox_control"]
        for _k, _lo, _hi in (("timeout", 5, 120), ("volume", 0, 100),
                             ("vox_steps", 4, 60)):
            try:
                _vd[_k] = max(_lo, min(_hi, int(_vc.get(_k, _vd[_k]))))
            except Exception:
                pass
        cfg["voice"] = _vd
    elif _vc is not None:
        issues.append("voice")

    # proactive_ai：主动搭话（结构复杂，交给 proactive.normalize_cfg 校验）
    _pai = raw.get("proactive_ai")
    if _pai is not None:
        if isinstance(_pai, dict):
            cfg["proactive_ai"] = _pai
        else:
            issues.append("proactive_ai")

    # pause_when_hidden / smash_spin / proactive
    for key in ("pause_when_hidden", "smash_spin", "proactive"):
        v = raw.get(key)
        if isinstance(v, bool):
            cfg[key] = v
        elif v is not None:
            issues.append(key)

    # look_screen
    ls = raw.get("look_screen")
    if isinstance(ls, dict):
        for k in ("vision_model", "vision_base", "vision_key"):
            v = ls.get(k)
            if isinstance(v, str):
                cfg["look_screen"][k] = v[:200]
            elif v is not None:
                issues.append("look_screen.%s" % k)
    elif ls is not None:
        issues.append("look_screen")

    # look_screen.save_desc：是否把屏幕描述写进聊天记录（默认 False）
    _sd = ls.get("save_desc") if isinstance(ls, dict) else None
    if isinstance(_sd, bool):
        cfg["look_screen"]["save_desc"] = _sd

    # look_screen.save_shot：是否把截图原图也存进聊天记录（默认 False）
    _ss = ls.get("save_shot") if isinstance(ls, dict) else None
    if isinstance(_ss, bool):
        cfg["look_screen"]["save_shot"] = _ss


    # look_screen.vision_system（识别规则，字符串，空则回落默认）
    _vsys = ls.get("vision_system") if isinstance(ls, dict) else None
    if isinstance(_vsys, str) and _vsys.strip():
        cfg["look_screen"]["vision_system"] = _vsys[:8000]
    elif _vsys is not None and not isinstance(_vsys, str):
        issues.append("look_screen.vision_system")


    # look_screen.max_side / quality / timeout（视觉请求限制）
    for _k, _lo, _hi in (("max_side", LOOK_SCREEN_SIDE_MIN, LOOK_SCREEN_SIDE_MAX),
                         ("quality", LOOK_SCREEN_QUALITY_MIN, LOOK_SCREEN_QUALITY_MAX),
                         ("timeout", LOOK_SCREEN_TIMEOUT_MIN, LOOK_SCREEN_TIMEOUT_MAX)):
        _v = ls.get(_k) if isinstance(ls, dict) else None
        if isinstance(_v, int) and not isinstance(_v, bool) and _lo <= _v <= _hi:
            cfg["look_screen"][_k] = _v
        elif _v is not None:
            issues.append("look_screen.%s" % _k)


    # look_screen.style / max_tokens（描述风格与单次上限，独立于聊天页）
    _vs = ls.get("style") if isinstance(ls, dict) else None
    if isinstance(_vs, str) and _vs in VISION_STYLE_SUFFIXES:
        cfg["look_screen"]["style"] = _vs
    elif _vs is not None:
        issues.append("look_screen.style")
    _vt = ls.get("max_tokens") if isinstance(ls, dict) else None
    if isinstance(_vt, int) and not isinstance(_vt, bool) \
            and VISION_TOKENS_MIN <= _vt <= VISION_TOKENS_MAX:
        cfg["look_screen"]["max_tokens"] = _vt
    elif _vt is not None:
        issues.append("look_screen.max_tokens")


    # reply_style：回复长度风格
    rs = raw.get("reply_style")
    if rs in REPLY_STYLES:
        cfg["reply_style"] = rs
    else:
        if rs is not None:
            issues.append("reply_style")
        changed = True

    # max_tokens：单次回复上限
    mt = raw.get("max_tokens")
    if isinstance(mt, int) and not isinstance(mt, bool) and MAX_TOKENS_MIN <= mt <= MAX_TOKENS_MAX:
        cfg["max_tokens"] = mt
    else:
        if mt is not None:
            issues.append("max_tokens")
        changed = True

    # animation_tiers：small/medium/large 三层概率
    at = raw.get("animation_tiers")
    if isinstance(at, dict):
        for k in ANIMATION_TIERS_DEFAULT:
            v = at.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                cfg["animation_tiers"][k] = float(v)
            elif v is not None:
                issues.append("animation_tiers.%s" % k)
            else:
                changed = True
    else:
        if at is not None:
            issues.append("animation_tiers")
        changed = True

    # 转向 / 代码动作强度（0~1）
    for key, default in (("turn_strength", TURN_STRENGTH_DEFAULT),
                         ("action_strength", ACTION_STRENGTH_DEFAULT)):
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= float(v) <= 1.0:
            cfg[key] = float(v)
        elif v is not None:
            issues.append(key)
        else:
            changed = True

    # chat_migrated：旧配置迁移标记（只做一次）
    _cm = raw.get("chat_migrated")
    if isinstance(_cm, bool):
        cfg["chat_migrated"] = _cm


    # chat_base / chat_model：聊天接口地址与模型（字符串，空则用内置默认）
    for _ck in ("chat_base", "chat_model"):
        _cv = raw.get(_ck)
        if isinstance(_cv, str):
            cfg[_ck] = _cv.strip()[:300]
        elif _cv is not None:
            issues.append(_ck)

    # 迁移：旧配置只有 ds_api_key，没有 chat_base/chat_model 时不算问题，
    # 由 App 启动时按 DS_BASE_URL / DS_MODEL 填入界面（见 _chat_cfg）


    # ui_sections：设置页可收缩区块状态（只影响显示）
    _us = raw.get("ui_sections")
    if isinstance(_us, dict):
        for _k2 in ("persona", "vision_rule", "vision_advanced", "chat_model"):
            if isinstance(_us.get(_k2), bool):
                cfg["ui_sections"][_k2] = _us[_k2]


    # balance.provider：余额来源（auto / deepseek / zhipu）
    _bp = raw.get("balance")
    if isinstance(_bp, dict):
        _pv = _bp.get("provider")
        if isinstance(_pv, str) and _pv.lower() in ("auto", "deepseek", "zhipu"):
            cfg["balance"]["provider"] = _pv.lower()
        elif _pv is not None:
            issues.append("balance.provider")


    # balance_full：满额基准（元）
    bf = raw.get("balance_full")
    if isinstance(bf, (int, float)) and not isinstance(bf, bool) and 1.0 <= float(bf) <= 10000.0:
        cfg["balance_full"] = float(bf)
    elif bf is not None:
        issues.append("balance_full")
    if bf is None:
        changed = True

    # 3) 字典类字段
    u = raw.get("usage")
    if isinstance(u, dict):
        for k in cfg["usage"]:
            v = u.get(k, 0)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                cfg["usage"][k] = v
            else:
                issues.append("usage.%s" % k)
    elif u is not None:
        issues.append("usage")

    g = raw.get("games")
    if isinstance(g, dict):
        for k in cfg["games"]:
            v = g.get(k, 0)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                cfg["games"][k] = v
            else:
                issues.append("games.%s" % k)
    elif g is not None:
        issues.append("games")

    rem = raw.get("reminders")
    if isinstance(rem, dict):
        for kind in REMINDERS:
            item = rem.get(kind)
            if not isinstance(item, dict):
                if item is not None:
                    issues.append("reminders.%s" % kind)
                continue
            on = item.get("on", True)
            if isinstance(on, bool):
                cfg["reminders"][kind]["on"] = on
            else:
                issues.append("reminders.%s.on" % kind)
            # 每类独立间隔；缺省用该类默认（喝水45/起身60/护眼20）
            m = item.get("min", DEFAULT_REMIND_MIN_BY_KIND[kind])
            if isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= 600:
                cfg["reminders"][kind]["min"] = m
            else:
                issues.append("reminders.%s.min" % kind)
    elif rem is not None:
        issues.append("reminders")

    # remind_extras：气泡/提示音/免打扰/待办/定时/计数
    _merge_remind_extras(cfg, raw.get("remind_extras"), issues)

    # 4) 聊天窗口位置（可选）
    cw = raw.get("chat_window")
    if isinstance(cw, dict):
        ok_geo = True
        geo = {}
        for k in ("x", "y", "w", "h"):
            v = cw.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                geo[k] = v
            else:
                ok_geo = False
                issues.append("chat_window.%s" % k)
        if ok_geo and geo["w"] > 0 and geo["h"] > 0:
            cfg["chat_window"] = geo
    elif cw is not None:
        issues.append("chat_window")

    # 5) 版本号
    ver = raw.get("config_version")
    if isinstance(ver, int) and not isinstance(ver, bool):
        if ver > CONFIG_VERSION:
            log("config.json 版本 %s 高于程序支持的 %s，按当前结构读取" % (ver, CONFIG_VERSION),
                logging.WARNING)
        cfg["config_version"] = CONFIG_VERSION
        if ver != CONFIG_VERSION:
            changed = True
    else:
        cfg["config_version"] = CONFIG_VERSION
        if ver is not None:
            issues.append("config_version")
        changed = True

    for name in issues:
        log("配置字段校验失败，已回落默认值: %s" % name, logging.WARNING)
    if issues:
        changed = True
    return cfg, changed, issues


def load_config_safe():
    """读取 + 迁移 + 校验。整体损坏时先备份再返回默认配置。

    返回 (cfg, need_save)
    """
    raw = None
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as ex:
            try:
                shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak")
                log("config.json 解析失败，已备份到 config.json.bak：%s" % ex, logging.ERROR)
            except Exception as ex2:
                log("config.json 解析失败且备份失败：%s" % ex2, logging.ERROR)
            raw = None
            return default_config(), True

    if raw is None:
        return default_config(), True

    # 迁移前先备份（仅当确实需要改结构时）
    need_backup = (not isinstance(raw.get("pets"), list)) or raw.get("config_version") != CONFIG_VERSION
    cfg, changed, issues = migrate_and_validate(raw)
    if need_backup:
        try:
            shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak")
            log("config.json 结构迁移，已备份到 config.json.bak")
        except Exception as ex:
            log("备份 config.json 失败: %s" % ex, logging.WARNING)
    return cfg, changed
