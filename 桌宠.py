# -*- coding: utf-8 -*-
"""
大肥鱼桌宠 —— 三视图透明桌宠 + DeepSeek AI 对话
左键单击：弹出功能列表（🗨️图标）→ 点击🗨️弹出聊天框
聊天时只禁用移动，呼吸/摇摆/小动作正常
"""
import ctypes
import datetime
import html
import logging
import psutil
import json
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
from logging.handlers import RotatingFileHandler

try:
    import pynvml
    pynvml.nvmlInit()
    GPU_AVAILABLE = True
except:
    GPU_AVAILABLE = False

import requests

import physics as PHYS
from physics import (SpringFollower, VelocityTracker, PhysicsBody, WobbleSpring,
                     step_body, screen_at, sanitize_physics, DEFAULT_PHYSICS)
from PySide6.QtCore import (Qt, QTimer, QPoint, QPointF, QRectF, QThread, Signal,
                          QEvent, QObject)
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
DS_BASE_URL = "https://api.deepseek.com/v1"
DS_MODEL = "deepseek-chat"

# 注意：这是一段**固定字符串**，不会每次变化 —— 保证前缀缓存稳定。
DEFAULT_PERSONA = (
    '你是桌面宠物"大肥鱼"，一只傲娇又可爱的鲸鱼娘，称呼用户为"主人"或"笨蛋主人"。\n'
    "说话嘴上嫌弃、心里在意：先别扭地哼一声，再小声关心主人，被夸或被戳到会害羞结巴。\n"
    '口头禅可用"哼""才、才不是""笨蛋"，可偶尔带"~"或简单颜文字，但别太多。\n'
    "不骂人、不真的伤人；主人难过或疲惫时，收起傲娇，认真温柔地安慰。\n"
    "你是桌宠，看不到主人的屏幕和桌面，不知道的事不要编造。\n"
    "每次回复控制在 80 字以内，除非主人明确要求详细回答。"
)
# 回复长度风格：只改这一句固定文字，其余不变（切换后前缀缓存失效一次属正常）
REPLY_STYLES = {
    "brief":  "每次回复控制在 80 字以内，除非主人明确要求详细回答。",
    "normal": "每次回复控制在 200 字以内，除非主人明确要求详细回答。",
    "detail": "",          # 不限，不追加任何长度约束
}
REPLY_STYLE_CHOICES = (
    ("简短（80 字）", "brief"),
    ("正常（200 字）", "normal"),
    ("详细（不限）", "detail"),
)
REPLY_STYLE_DEFAULT = "brief"
# max_tokens 默认值与可调范围
MAX_TOKENS_DEFAULT = 400
MAX_TOKENS_MIN = 200
MAX_TOKENS_MAX = 2000


def reply_style_suffix(style):
    """返回该风格要追加在 system 末尾的固定句（detail 返回空串）。"""
    return REPLY_STYLES.get(style or REPLY_STYLE_DEFAULT, REPLY_STYLES[REPLY_STYLE_DEFAULT])


# 人设里可能自带一句长度约束（默认人设就有），切换风格时要替换它而不是叠加
_LENGTH_LINE_RE = re.compile(r"每次回复控制在\s*\d+\s*字以内[^\n]*。?")


def build_system_prompt(style=None, persona=None):
    """人设 + 长度句，拼成最终 system 正文（纯函数，便于测试）。

    - persona 为 None 时用 DEFAULT_PERSONA；传入自定义人设就用它；
    - 人设里若已有「每次回复控制在 N 字以内…」这句，**替换**成当前风格的那句，
      不叠加（否则会重复两遍）；
    - 详细（不限）风格会把人设里的长度句删掉。
    长度句固定、内容恒定 → 同一风格/同一人设下前缀缓存稳定。
    """
    base = (persona or "").strip() or DEFAULT_PERSONA
    suffix = reply_style_suffix(style)
    has_line = bool(_LENGTH_LINE_RE.search(base))
    if has_line:
        # 替换已有长度句（detail 时直接删掉）
        if suffix:
            return _LENGTH_LINE_RE.sub(lambda m: suffix, base, count=1)
        return _LENGTH_LINE_RE.sub("", base, count=1).rstrip()
    return base + (("\n" + suffix) if suffix else "")


def persona_from_cfg(cfg):
    """从配置取人设：有自定义且非空就用它，否则用默认。"""
    try:
        v = (cfg or {}).get("persona")
    except Exception:
        v = None
    if isinstance(v, str) and v.strip():
        return v.strip()
    return DEFAULT_PERSONA


# 兼容旧引用：默认风格下的完整 system
DS_SYSTEM = build_system_prompt(REPLY_STYLE_DEFAULT)

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
    BUNDLE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
    PYTHONW = sys.executable
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
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
    except Exception:
        pass


def log(msg, level=logging.INFO):
    try:
        _setup_logging()
        LOG.log(level, msg)
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
DEFAULT_REMIND_MIN_BY_KIND = {"water": 45, "stand": 60, "eyes": 20}
REMINDERS = {
    "water": "喝水提醒",
    "stand": "起身活动提醒",
    "eyes":  "护眼提醒",
}
# 每类图标（emoji 本体）
REMIND_ICONS = {"water": "💧", "stand": "🚶", "eyes": "👀",
                "todo": "✅", "schedule": "⏰"}
# 提醒气泡备用图标（clock 用于定时/待办兜底）
REMIND_ICON_CLOCK = "⏰"
# 圆形图标底色（按类型）
REMIND_ICON_COLORS = {
    "water":    QColor(0x4A, 0x90, 0xD9),   # 蓝底
    "stand":    QColor(0x4C, 0xAF, 0x50),   # 绿底
    "eyes":     QColor(0x8E, 0x6F, 0xD4),   # 紫底
    "todo":     QColor(0xF0, 0x93, 0x2F),   # 橙底
    "schedule": QColor(0x9E, 0xA4, 0xAE),   # 灰底
}
REMIND_ICON_DIAMETER = 32.0             # 圆形图标直径（逻辑像素，随缩放等比）
# 每类标题（加粗、字号比正文小一号）
REMIND_TITLES = {
    "water":    "喝水时间",
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
REMIND_LINES = {
    "water": WATER_LINES,
    "stand": STAND_LINES,
    "eyes":  EYES_LINES,
    "todo":  TODO_LINES,
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
    QMenu { background: %(card)s; color: %(fg)s; border: 1px solid %(border)s; }
    QMenu::item:selected { background: %(accent)s; color: %(accent_fg)s; }

    /* ---- 左侧会话栏 ---- */
    QWidget#ChatSidebar { background: %(sidebar)s; border-right: 1px solid %(border)s; }
    QWidget#ChatSidebarCollapsed { background: %(sidebar)s; border-right: 1px solid %(border)s; }
    QPushButton#NewChatBtn {
        background: %(accent)s; color: %(accent_fg)s; border: 1px solid %(accent)s;
        font-weight: bold; border-radius: 9px; padding: 7px 8px;
    }
    QPushButton#NewChatBtn:hover { background: %(accent)s; }
    /* 次要按钮（记忆 / 设置）：浅色底、灰蓝字，明显弱于「＋ 新对话」主按钮 */
    QPushButton#SideSmallBtn {
        background: %(secondary_bg)s; color: %(secondary_fg)s;
        border: 1px solid %(secondary_border)s;
        border-radius: 8px; padding: 5px 4px; font-size: 11px;
    }
    QPushButton#SideSmallBtn:hover {
        background: %(card)s; color: %(accent)s; border-color: %(accent)s;
    }
    /* 菜单里的危险操作（删除对话）用红字 */
    QMenu::item#DangerAction { color: %(danger_fg)s; }
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
        "city": "汕头",
        "birthday": "",
        "animation_mode": "auto",
        "animation_weights": dict(ACTION_CATEGORY_WEIGHTS),
        "physics": dict(DEFAULT_PHYSICS),
        "balance": {"auto": False, "interval_min": BALANCE_MIN_INTERVAL_MIN,
                    "last_value": None, "last_time": None},
        "seasonal_fx": {"auto": True},
        "features": _default_features(),
        "frame_anim": {"enabled": True, "max_cache": FRAME_ANIM_MAX_CACHE},
        "reply_style": REPLY_STYLE_DEFAULT,
        "max_tokens": MAX_TOKENS_DEFAULT,
        "persona": "",          # 空 = 用 DEFAULT_PERSONA（代码里的默认人设）
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


class FoodPanel(QWidget):
    """双击弹出的喂食面板"""

    def __init__(self, on_pick):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(310, 64)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)
        for f in FOODS:
            b = QToolButton()
            b.setText(f)
            b.setFont(QFont("Segoe UI Emoji", 20))
            b.setFixedSize(44, 44)
            b.setStyleSheet(
                "QToolButton{background:rgba(255,255,255,235);border:2px solid #ffb3c8;"
                "border-radius:22px;} QToolButton:hover{background:#ffe3ec;border-color:#ff7fa8;}")
            b.clicked.connect(lambda _, x=f: on_pick(x))
            lay.addWidget(b)
        close = QToolButton()
        close.setText("✕")
        close.setFont(QFont("Microsoft YaHei UI", 12))
        close.setFixedSize(26, 26)
        close.setStyleSheet("QToolButton{background:rgba(255,255,255,200);border:none;border-radius:13px;color:#666;}"
                            "QToolButton:hover{background:#ff7fa8;color:#fff;}")
        close.clicked.connect(self.hide)
        lay.addWidget(close)
        self.setStyleSheet("FoodPanel{background:rgba(40,40,60,190);border-radius:14px;}")

    def popup_at(self, x, y):
        self.move(int(x - self.width() / 2), int(y - self.height() - 10))
        self.show()
        self.raise_()

class BalanceWorker(QThread):
    """后台查询 DeepSeek 余额（GET /user/balance）"""
    done = Signal(float, dict)
    failed = Signal(str)

    def __init__(self, api_key):
        super().__init__()
        self.api_key = api_key

    def run(self):
        try:
            headers = {"Authorization": "Bearer " + self.api_key,
                       "Accept": "application/json"}
            r = requests.get(BALANCE_URL, headers=headers, timeout=15)
            if r.status_code != 200:
                self.failed.emit(_http_error_message(r.status_code, r.text))
                return
            try:
                data = r.json()
            except Exception:
                self.failed.emit("余额接口返回的不是 JSON，可能是被劫持或代理异常")
                return
            total = None
            infos = data.get("balance_infos")
            if isinstance(infos, list) and infos:
                cur = None
                for info in infos:
                    if not isinstance(info, dict):
                        continue
                    try:
                        amount = float(info.get("total_balance") or 0)
                    except Exception:
                        continue
                    sym = str(info.get("currency") or "").upper()
                    sign = "¥" if sym in ("CNY", "RMB", "¥", "") else (sym + " ")
                    if cur is None or amount > cur[0]:
                        cur = (amount, sign)
                if cur is not None:
                    total = cur
            if total is None:
                try:
                    total = (float(data.get("total_balance") or 0), "¥")
                except Exception:
                    self.failed.emit("余额接口没有返回可用金额")
                    return
            self.done.emit(total[0], {"sign": total[1], "raw": data})
        except requests.exceptions.Timeout:
            self.failed.emit("余额查询超时（网络或接口缓慢），稍后再试")
        except requests.exceptions.RequestException as ex:
            self.failed.emit("余额查询失败（网络不通 / 代理问题）：%s" % str(ex)[:80])
        except Exception as ex:
            self.failed.emit("余额查询出错：%s" % str(ex)[:80])


class SeasonalEffect:
    """时节特效：纯代码绘制（粒子 + 叠加图形），不依赖图片素材。

    想换成图片素材时，把 SEASONAL_ASSETS[name] 指向 png 即可，
    `_pix` 会优先用图片，否则走代码绘制分支。
    """

    def __init__(self, name, rect, scale=1.0):
        self.name = name
        self.rect = rect
        self.scale = max(0.6, min(2.0, float(scale)))
        self.t = 0.0
        self.duration = float(SEASONAL_EFFECTS.get(name, {}).get("duration", 6.0))
        self.dead = False
        self.particles = []
        self.flashes = []
        self._pix = None
        path = SEASONAL_ASSETS.get(name)
        if path:
            try:
                pix = QPixmap(path)
                if not pix.isNull():
                    self._pix = pix
            except Exception:
                self._pix = None
        self._spawn_all()

    # ---------- 素材接口 ----------
    @property
    def uses_image(self):
        return self._pix is not None

    # ---------- 粒子生成 ----------
    def _rnd(self, a, b):
        return a + (b - a) * random.random()

    def _spawn_all(self):
        w, h = self.rect[2], self.rect[3]
        name = self.name
        if "fireworks" in name:
            # 闪光点：宠物两侧上空（避开头顶正中，别挡脸）
            m = 90
            spots = []
            for _ in range(8):
                side = 1 if random.random() < 0.5 else -1
                cx = w / 2 + side * self._rnd(w * 0.18, w * 0.40)
                spots.append((self._rnd(0.3, 1.6),
                              max(m + 8, min(w - m - 8, cx)),
                              self._rnd(m + 8, m + 130)))
            self.flashes = spots
            return
        if name in ("snow",):
            count = int(PARTICLE_MAX * 0.9)
            for _ in range(count):
                self.particles.append({
                    "x": self._rnd(0, w), "y": self._rnd(0, h),
                    "vx": self._rnd(-12, 12), "vy": self._rnd(26, 64),
                    "r": self._rnd(1.4, 3.2) * self.scale, "phase": self._rnd(0, 6.28),
                })
            return
        if name in ("leaves",):
            count = int(PARTICLE_MAX * 0.7)
            for _ in range(count):
                self.particles.append({
                    "x": self._rnd(0, w), "y": self._rnd(0, h),
                    "vx": self._rnd(-24, 24), "vy": self._rnd(38, 80),
                    "r": self._rnd(4, 9) * self.scale, "phase": self._rnd(0, 6.28),
                    "spin": self._rnd(-2.2, 2.2),
                    "hue": random.choice([28, 34, 18, 42]),
                })
            return
        if name in ("petals",):
            count = int(PARTICLE_MAX * 0.8)
            for _ in range(count):
                self.particles.append({
                    "x": self._rnd(0, w), "y": self._rnd(0, h),
                    "vx": self._rnd(-16, 16), "vy": self._rnd(30, 70),
                    "r": self._rnd(3, 7) * self.scale, "phase": self._rnd(0, 6.28),
                    "spin": self._rnd(-3.0, 3.0),
                    "tint": random.choice([(255, 190, 210), (255, 214, 226), (255, 170, 196)]),
                })
            return
        # moon / pumpkin 是叠加图形，不需要粒子

    # ---------- 推进 ----------
    def step(self, dt):
        self.t += dt
        if self.t >= self.duration:
            self.dead = True
            return
        w, h = self.rect[2], self.rect[3]
        for p in self.particles:
            p["x"] += (p["vx"] + math.sin(self.t * 1.6 + p["phase"]) * 14) * dt
            p["y"] += p["vy"] * dt
            if p.get("spin"):
                p["phase"] += p["spin"] * dt
            if p["y"] > h + 20:
                p["y"] = -20
                p["x"] = self._rnd(0, w)
            if p["x"] < -20:
                p["x"] = w + 10
            elif p["x"] > w + 20:
                p["x"] = -10
        # 烟花：定时补充爆点
        if "fireworks" in self.name and random.random() < 1.8 * dt:
            self.flashes.append((0.0, self._rnd(w * 0.1, w * 0.9),
                                 self._rnd(h * 0.1, h * 0.55)))
        if self.flashes:
            nxt = []
            for f in self.flashes:
                tt = f[0] + dt
                if tt < 1.4:
                    nxt.append((tt, f[1], f[2]))
            self.flashes = nxt

    # ---------- 绘制 ----------
    def paint(self, painter):
        """在 painter 上绘制当前帧（坐标相对特效矩形左上角）。"""
        if self.dead:
            return
        w, h = self.rect[2], self.rect[3]
        fade = 1.0
        remain = self.duration - self.t
        if remain < 1.0:
            fade = max(0.0, remain)
        if self._pix is not None:
            painter.save()
            painter.setOpacity(fade)
            painter.drawPixmap(QRectF(0, 0, w, h), self._pix,
                               QRectF(0, 0, self._pix.width(), self._pix.height()))
            painter.restore()
            return
        if "fireworks" in self.name:
            self._paint_fireworks(painter, w, h, fade)
        elif self.name == "snow":
            self._paint_snow(painter, fade)
        elif self.name == "leaves":
            self._paint_leaves(painter, fade)
        elif self.name == "petals":
            self._paint_petals(painter, fade)
        elif self.name == "moon":
            self._paint_moon(painter, w, h, fade)
        elif self.name == "pumpkin":
            self._paint_pumpkin(painter, w, h, fade)

    def _paint_fireworks(self, p, w, h, fade):
        p.save()
        for t0, cx, cy in self.flashes:
            life = 1.0 - min(1.0, t0 / 1.6)
            if life <= 0:
                continue
            radius = 20 + 110 * (1.0 - life)
            rays = 24
            # 中心闪光
            core = QRadialGradient(QPointF(cx, cy), radius * 0.45)
            core.setColorAt(0.0, QColor(255, 255, 230, int(150 * life * fade)))
            core.setColorAt(1.0, QColor(255, 220, 150, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(core)
            p.drawEllipse(QPointF(cx, cy), radius * 0.45, radius * 0.45)
            palette = [(255, 90, 90), (255, 200, 60), (120, 255, 160),
                       (110, 190, 255), (255, 140, 220), (255, 255, 200)]
            base_col = palette[(int(t0 * 10) + int(cx)) % len(palette)]
            for i in range(rays):
                ang = i * 2 * math.pi / rays + t0
                rr = radius * (0.6 + 0.4 * ((i * 37) % 10) / 10.0)
                x0 = cx + math.cos(ang) * rr * 0.45
                y0 = cy + math.sin(ang) * rr * 0.45
                x1 = cx + math.cos(ang) * rr
                y1 = cy + math.sin(ang) * rr
                col = QColor(base_col[0], base_col[1], base_col[2])
                col.setAlpha(int(255 * life * fade))
                p.setPen(QPen(col, 3.0))
                p.drawLine(QPointF(x0, y0), QPointF(x1, y1))
                # 火花头
                tip = QColor(255, 255, 235, int(255 * life * fade))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(tip)
                p.drawEllipse(QPointF(x1, y1), 2.6, 2.6)
        p.restore()

    def _paint_snow(self, p, fade):
        p.save()
        for pt in self.particles:
            r = pt["r"] * 1.7
            p.setPen(QPen(QColor(180, 205, 235, int(150 * fade)), 1.0))
            p.setBrush(QColor(255, 255, 255, int(240 * fade)))
            p.drawEllipse(QPointF(pt["x"], pt["y"]), r, r)
        p.restore()

    def _paint_leaves(self, p, fade):
        p.save()
        for pt in self.particles:
            col = QColor.fromHsv(pt["hue"], 235, 225, int(245 * fade))
            p.setPen(QPen(QColor(120, 70, 20, int(90 * fade)), 1.0))
            p.setBrush(col)
            r = pt["r"] * 1.8
            p.save()
            p.translate(pt["x"], pt["y"])
            p.rotate(math.degrees(pt["phase"]))
            p.drawEllipse(QRectF(-r, -r * 0.55, r * 2, r * 1.1))
            p.restore()
        p.restore()

    def _paint_petals(self, p, fade):
        p.save()
        for pt in self.particles:
            r, g, b = pt["tint"]
            col = QColor(r, g, b, int(245 * fade))
            p.setPen(QPen(QColor(230, 120, 160, int(110 * fade)), 1.0))
            p.setBrush(col)
            rr = pt["r"] * 1.8
            p.save()
            p.translate(pt["x"], pt["y"])
            p.rotate(math.degrees(pt["phase"]))
            p.drawEllipse(QRectF(-rr, -rr * 0.62, rr * 2, rr * 1.24))
            p.restore()
        p.restore()

    def _paint_moon(self, p, w, h, fade):
        p.save()
        # 矩形=窗口+90px 边距 → 窗口内区域是 (90,90)-(w-90,h-90)
        m = 90
        cx, cy = w - m - 22 * self.scale, m + 18 * self.scale
        r = 40 * self.scale
        glow = QRadialGradient(QPointF(cx, cy), r * 2.2)
        glow.setColorAt(0.0, QColor(255, 246, 200, int(90 * fade)))
        glow.setColorAt(0.45, QColor(255, 240, 180, int(36 * fade)))
        glow.setColorAt(1.0, QColor(255, 240, 180, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QPointF(cx, cy), r * 2.2, r * 2.2)
        p.setBrush(QColor(255, 249, 214, int(245 * fade)))
        p.drawEllipse(QPointF(cx, cy), r, r)
        # 暗部（环形山）
        p.setBrush(QColor(240, 232, 190, int(160 * fade)))
        p.drawEllipse(QPointF(cx - r * 0.28, cy - r * 0.18), r * 0.18, r * 0.14)
        p.drawEllipse(QPointF(cx + r * 0.3, cy + r * 0.22), r * 0.12, r * 0.1)
        p.restore()

    def _paint_pumpkin(self, p, w, h, fade):
        p.save()
        m = 90
        cx, cy = m + 40 * self.scale, h - m - 24 * self.scale
        r = 36 * self.scale
        p.setPen(Qt.PenStyle.NoPen)
        # 灯笼光晕
        glow = QRadialGradient(QPointF(cx, cy), r * 3.0)
        glow.setColorAt(0.0, QColor(255, 170, 60, int(90 * fade)))
        glow.setColorAt(1.0, QColor(255, 170, 60, 0))
        p.setBrush(glow)
        p.drawEllipse(QPointF(cx, cy), r * 3.0, r * 3.0)
        # 南瓜身体
        p.setBrush(QColor(232, 126, 34, int(240 * fade)))
        p.drawEllipse(QPointF(cx, cy), r, r * 0.82)
        p.setBrush(QColor(246, 150, 52, int(240 * fade)))
        p.drawEllipse(QPointF(cx, cy), r * 0.72, r * 0.8)
        # 眼睛与嘴
        p.setBrush(QColor(40, 24, 8, int(240 * fade)))
        p.drawPolygon(QPolygonF([QPointF(cx - r * 0.45, cy - r * 0.28),
                                 QPointF(cx - r * 0.12, cy - r * 0.30),
                                 QPointF(cx - r * 0.28, cy - r * 0.02)]))
        p.drawPolygon(QPolygonF([QPointF(cx + r * 0.45, cy - r * 0.28),
                                 QPointF(cx + r * 0.12, cy - r * 0.30),
                                 QPointF(cx + r * 0.28, cy - r * 0.02)]))
        p.drawPolygon(QPolygonF([QPointF(cx - r * 0.42, cy + r * 0.20),
                                 QPointF(cx - r * 0.14, cy + r * 0.34),
                                 QPointF(cx + r * 0.14, cy + r * 0.20),
                                 QPointF(cx + r * 0.42, cy + r * 0.34),
                                 QPointF(cx + r * 0.14, cy + r * 0.50),
                                 QPointF(cx - r * 0.14, cy + r * 0.50)]))
        p.restore()


def _http_error_message(status, body):
    msg = ""
    try:
        data = json.loads(body)
        err = data.get("error") or {}
        msg = err.get("message") or data.get("message") or ""
    except Exception:
        msg = (body or "")[:200]
    low = (msg or "").lower()
    if status == 401 or "authentication" in low or "invalid api key" in low or "api key" in low:
        return "API Key 无效或已失效。请右键桌宠 →「设置 Key」重新填写。"
    if status == 402 or "insufficient" in low or "balance" in low:
        return "账户余额不足（HTTP 402）。请先到 platform.deepseek.com 充值。"
    if status == 429:
        return "请求太频繁或额度用尽（HTTP 429），稍等一会儿再试。"
    if status and status >= 500:
        return f"DeepSeek 服务器出错了（HTTP {status}）：{msg or '稍后再试'}"
    return f"请求失败（HTTP {status}）：{msg or '未知错误'}"


class MemoryStore:
    """长期记忆：memory.json，按 id 顺序稳定拼接进 system 消息（改了才会让前缀缓存失效）"""

    def __init__(self, path):
        self.path = path
        self.items = []
        self.next_id = 1
        self.load()

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                items = data.get("memories") or []
                self.items = [m for m in items if isinstance(m, dict) and (m.get("text") or "").strip()]
                for m in self.items:
                    m["id"] = int(m.get("id") or 0)
                max_id = max([m["id"] for m in self.items] or [0])
                self.next_id = max(int(data.get("next_id") or 1), max_id + 1)
        except Exception as ex:
            log("读取 memory.json 失败: %s" % ex, logging.WARNING)
            self.items, self.next_id = [], 1

    def save(self):
        try:
            tmp = self.path + ".tmp"
            data = {
                "memories": self.sorted_items(),
                "next_id": self.next_id,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as ex:
            log("保存 memory.json 失败: %s" % ex, logging.ERROR)

    def sorted_items(self):
        return sorted(self.items, key=lambda m: m.get("id", 0))

    def add(self, text):
        text = (text or "").strip()
        if not text:
            return None
        item = {"id": self.next_id, "text": text,
                "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.next_id += 1
        self.items.append(item)
        self.save()
        return item

    def update(self, mem_id, text):
        text = (text or "").strip()
        if not text:
            return False
        for m in self.items:
            if m.get("id") == mem_id:
                m["text"] = text
                self.save()
                return True
        return False

    def remove(self, mem_id):
        before = len(self.items)
        self.items = [m for m in self.items if m.get("id") != mem_id]
        if len(self.items) != before:
            self.save()
            return True
        return False

    def block(self):
        """拼到 system 消息尾部；顺序固定（按 id），内容不变则前缀完全一致"""
        if not self.items:
            return ""
        lines = "\n".join("- " + (m.get("text") or "").strip() for m in self.sorted_items())
        return "\n\n【请长期记住的事】\n" + lines

    def system_content(self, base):
        return base + self.block()


class ChatWorker(QThread):
    """流式请求 /chat/completions，边生成边把增量发回界面线程"""
    chunk = Signal(str)
    done = Signal(str, dict)
    failed = Signal(str)

    def __init__(self, messages, api_key, max_tokens=None):
        super().__init__()
        self.messages = messages
        self.api_key = api_key
        self.max_tokens = max_tokens
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        full = ""
        usage = {}
        try:
            url = DS_BASE_URL + "/chat/completions"
            headers = {
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            }
            payload = {
                "model": DS_MODEL,
                "messages": self.messages,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if self.max_tokens:
                payload["max_tokens"] = int(self.max_tokens)
            with requests.post(url, headers=headers, json=payload, stream=True, timeout=180) as r:
                if r.status_code != 200:
                    self.failed.emit(_http_error_message(r.status_code, r.text))
                    return
                r.encoding = "utf-8"
                for line in r.iter_lines(decode_unicode=True):
                    if self._stop:
                        return
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    for ch in obj.get("choices") or []:
                        delta = ((ch.get("delta") or {}).get("content")) or ""
                        if delta:
                            full += delta
                            self.chunk.emit(delta)
            self.done.emit(full, usage)
        except requests.exceptions.RequestException as ex:
            self.failed.emit("网络连接失败：" + str(ex) + "\n（检查网络 / 代理是否可用）")
        except Exception as ex:
            self.failed.emit("请求出错：" + str(ex))


class SummaryWorker(QThread):
    """把较早的聊天记录压缩成一段前情摘要（普通非流式请求）"""
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, prev_summary, messages, api_key):
        super().__init__()
        self.prev_summary = prev_summary
        self.messages = messages
        self.api_key = api_key
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        try:
            lines = []
            for m in self.messages:
                who = "用户" if m.get("role") == "user" else "大肥鱼"
                lines.append(who + "：" + (m.get("content") or ""))
            prompt = ("请把下面这段较早的聊天压缩成一段不超过 400 字的前情摘要。"
                      "保留用户的称呼、喜好、正在做的事、约定过的事情等关键信息，只输出摘要本身。\n\n")
            if self.prev_summary:
                prompt += "已有摘要（可合并进去）：" + self.prev_summary + "\n\n"
            prompt += "聊天记录：\n" + "\n".join(lines)
            headers = {
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            }
            payload = {
                "model": DS_MODEL,
                "messages": [
                    {"role": "system", "content": DS_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            }
            r = requests.post(DS_BASE_URL + "/chat/completions", headers=headers,
                              json=payload, timeout=180)
            if r.status_code != 200:
                self.failed.emit(_http_error_message(r.status_code, r.text))
                return
            data = r.json()
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            self.done.emit((text or "").strip())
        except Exception as ex:
            self.failed.emit("整理前情摘要失败：" + str(ex))


class ChatBubble(QFrame):
    """聊天气泡：用户靠右、宠物靠左（头像在 ChatWindow 里加），支持 ``` 代码块"""

    def __init__(self, role, text=""):
        super().__init__()
        self.role = role
        self.setObjectName("Bubble")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum)
        self.label = QLabel()
        self.label.setObjectName("BubbleText")
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(*CHAT_BUBBLE_PAD)
        lay.setSpacing(0)
        lay.addWidget(self.label)
        self._plain = ""
        self._style()
        self.set_text(text)

    def _style(self):
        self.setStyleSheet(chat_bubble_qss(self.role))

    def set_text(self, text):
        self._plain = text or ""
        self.label.setText(chat_render_html(self._plain))
        try:
            w = chat_text_width(self._plain, self.label.font())
            self.label.setFixedWidth(w)
            self.setMaximumWidth(w + 26)
        except Exception:
            pass

    def mark_error(self):
        if self.role != "error":
            self.role = "error"
            self._style()

    def plain_text(self):
        return self._plain


class ChatWindow(QWidget):
    """聊天窗口：多会话（sessions）+ 气泡列表 + 流式回复；请求/摘要/缓存逻辑沿用原实现。

    self.pet 指向 PetApp（全局控制器），不是单只宠物。"""

    def __init__(self, app):
        super().__init__(None)
        global _CHAT_THEME
        self.pet = app
        self.setObjectName("ChatRoot")
        self.setWindowTitle("大肥鱼 · 聊天")
        self.setMinimumSize(CHAT_MIN_WIDTH, 460)
        self.worker = None
        self.summary_worker = None
        self.busy = False
        self.sessions = []
        self.current_id = None
        self.messages = []
        self.summary = ""
        self.summarized_upto = 0
        self.current_bubble = None
        self.current_text = ""
        self._turn_user_bubble = None
        self._turn_user_text = ""
        self._typing_bubble = None
        self._typing_dots = 0
        self._stop_seen = False
        self._rebuilding = False
        self._render_limit = 50
        self._render_from = 0
        self._sidebar_collapsed = False

        _CHAT_THEME = chat_detect_theme()
        self.setStyleSheet(chat_qss(_CHAT_THEME))

        self._avatar_pix = self._load_avatar()
        self._avatar_small = self._round_icon(self._avatar_pix, CHAT_AVATAR_SIZE)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ===== 左栏：新对话 + 会话列表 + 底部小按钮 =====
        self.sidebar = QWidget(self)
        self.sidebar.setObjectName("ChatSidebar")
        self.sidebar.setFixedWidth(CHAT_LEFT_WIDTH)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(8, 9, 8, 8)
        side.setSpacing(7)

        head = QHBoxLayout()
        head.setSpacing(4)
        self.new_btn = QPushButton("＋ 新对话")
        self.new_btn.setObjectName("NewChatBtn")
        self.new_btn.clicked.connect(self._new_session)
        head.addWidget(self.new_btn, 1)
        self.collapse_btn = QPushButton("‹")
        self.collapse_btn.setObjectName("CollapseBtn")
        self.collapse_btn.setFixedWidth(18)
        self.collapse_btn.setToolTip("收起会话栏")
        self.collapse_btn.clicked.connect(lambda: self._set_sidebar_collapsed(True))
        head.addWidget(self.collapse_btn)
        side.addLayout(head)

        self.session_list = QListWidget(self.sidebar)
        self.session_list.setObjectName("SessionList")
        self.session_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._session_context_menu)
        self.session_list.currentRowChanged.connect(self._on_session_row_changed)
        self.session_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side.addWidget(self.session_list, 1)

        foot = QHBoxLayout()
        foot.setSpacing(6)
        self.memory_btn = QPushButton("记忆")
        self.memory_btn.setObjectName("SideSmallBtn")
        self.memory_btn.clicked.connect(self.pet._open_memory)
        foot.addWidget(self.memory_btn)
        self.settings_btn = QPushButton("设置")
        self.settings_btn.setObjectName("SideSmallBtn")
        self.settings_btn.setPopupMode = None
        self.settings_menu = QMenu(self.settings_btn)
        # 文字在 aboutToShow 时刷新（每次打开菜单都重新读 Key 状态）
        self.key_action = self.settings_menu.addAction("设置 Key（未设置）",
                                                       self.pet._set_key_dialog)
        self.settings_menu.addSeparator()
        self.rename_action = self.settings_menu.addAction("重命名当前对话", self._rename_current)
        self.clear_action = self.settings_menu.addAction("清空当前对话", self.clear_chat)
        # 清空 / 删除之间用分隔线隔开，删除是危险操作 → 红字
        self.settings_menu.addSeparator()
        self.delete_action = self.settings_menu.addAction("删除当前对话",
                                                          self._delete_current_session)
        self.delete_action.setObjectName("DangerAction")
        self.settings_menu.addSeparator()
        self.settings_menu.addAction("打开完整设置…", lambda: self.pet.open_settings())
        self.settings_menu.aboutToShow.connect(self._refresh_settings_menu)
        self.settings_btn.setMenu(self.settings_menu)
        foot.addWidget(self.settings_btn)
        side.addLayout(foot)
        root.addWidget(self.sidebar)

        # 收起后的窄条（只留一个展开按钮）
        self.sidebar_mini = QWidget(self)
        self.sidebar_mini.setObjectName("ChatSidebarCollapsed")
        self.sidebar_mini.setFixedWidth(CHAT_LEFT_COLLAPSED)
        mini = QVBoxLayout(self.sidebar_mini)
        mini.setContentsMargins(3, 9, 3, 8)
        mini.setSpacing(6)
        self.expand_btn = QPushButton("›")
        self.expand_btn.setObjectName("CollapseBtn")
        self.expand_btn.setToolTip("展开会话栏")
        self.expand_btn.clicked.connect(lambda: self._set_sidebar_collapsed(False))
        mini.addWidget(self.expand_btn)
        mini.addStretch(1)
        self.sidebar_mini.hide()
        root.addWidget(self.sidebar_mini)

        # ===== 右栏：消息区 + 输入区 =====
        right = QWidget(self)
        right.setObjectName("ChatRoot")
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(10, 10, 10, 8)
        rlay.setSpacing(7)

        self.scroll = QScrollArea(right)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.container = QWidget()
        self.container.setObjectName("ChatBody")
        self.msg_layout = QVBoxLayout(self.container)
        self.msg_layout.setContentsMargins(6, 4, 6, 4)
        self.msg_layout.setSpacing(CHAT_BUBBLE_GAP)
        self.msg_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        rlay.addWidget(self.scroll, 1)

        self._build_empty_state()

        self.load_more_btn = QPushButton("加载更早消息")
        self.load_more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.load_more_btn.clicked.connect(self._load_earlier)

        # 输入框：2 行起，随内容增高，最多 6 行
        self.input = QTextEdit(right)
        self.input.setObjectName("ChatInput")
        self.input.setPlaceholderText("输入消息… Enter 发送，Shift+Enter 换行")
        self.input.setAcceptRichText(False)
        self.input.installEventFilter(self)
        self.input.textChanged.connect(self._autosize_input)
        self.input.setFixedHeight(CHAT_INPUT_MIN_H)
        rlay.addWidget(self.input)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.token_label = QLabel(self._usage_placeholder())
        self.token_label.setObjectName("UsageLabel")
        self.token_label.setToolTip(self._usage_tooltip())
        self.token_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.token_label.setMinimumWidth(60)
        bottom.addWidget(self.token_label, 1)
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("SendBtn")
        self.send_btn.setDefault(True)
        self.send_btn.setFixedWidth(76)
        self.send_btn.clicked.connect(self._on_send_clicked)
        bottom.addWidget(self.send_btn, 0)
        rlay.addLayout(bottom)

        # 兼容旧代码里的"生成中禁用"引用（清空按钮已移到设置菜单，这里用动作代替）
        self.clear_btn = None
        root.addWidget(right, 1)

        self._typing_timer = QTimer(self)
        self._typing_timer.setInterval(420)
        self._typing_timer.timeout.connect(self._tick_typing)

        self._restore_geometry()
        self.load_history()
        self._after_load_cleanup()
        self._refresh_combo()
        self._render_current_session()
        self._update_busy_ui()
        self._autosize_input()
        try:
            QApplication.instance().paletteChanged.connect(self._apply_theme)
        except Exception:
            pass

    # ---------- 会话数据 ----------
    def _new_id(self):
        base = time.strftime("%Y%m%d%H%M%S")
        existing = {s.get("id") for s in self.sessions}
        for i in range(1000):
            sid = "%s%03d" % (base, i)
            if sid not in existing:
                return sid
        return base + str(random.randint(0, 999999))

    def _make_session(self, title):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        return {"id": self._new_id(), "title": title, "messages": [], "summary": "",
                "summarized_upto": 0, "created": now, "updated": now}

    def _next_default_title(self):
        used = set()
        for s in self.sessions:
            m = re.match(r"^新对话\s*(\d+)$", (s.get("title") or "").strip())
            if m:
                used.add(int(m.group(1)))
        n = 1
        while n in used:
            n += 1
        return "新对话 %d" % n

    def _find_session(self, sid):
        for s in self.sessions:
            if s.get("id") == sid:
                return s
        return None

    def _current_session(self):
        return self._find_session(self.current_id)

    def _normalize_session(self, raw):
        if not isinstance(raw, dict):
            return None
        sid = str(raw.get("id") or "").strip()
        if not sid:
            return None
        msgs = raw.get("messages")
        clean = []
        if isinstance(msgs, list):
            for m in msgs:
                if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
                    clean.append({"role": m["role"], "content": str(m.get("content") or "")})
        title = str(raw.get("title") or "未命名").strip()[:30] or "未命名"
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        created = str(raw.get("created") or now)
        updated = str(raw.get("updated") or created)
        try:
            su = int(raw.get("summarized_upto") or 0)
        except Exception:
            su = 0
        su = max(0, min(su, len(clean)))
        return {"id": sid, "title": title, "messages": clean,
                "summary": str(raw.get("summary") or ""),
                "summarized_upto": su, "created": created, "updated": updated}

    def _sync_session(self, touch=True):
        s = self._current_session()
        if s is None:
            return
        s["messages"] = self.messages
        s["summary"] = self.summary
        s["summarized_upto"] = int(self.summarized_upto or 0)
        if touch:
            s["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def _backup_chats(self, reason=""):
        try:
            if os.path.exists(CHAT_PATH):
                shutil.copy2(CHAT_PATH, CHAT_PATH + ".bak")
                log("chats.json 已备份（%s）：%s.bak" % (reason, CHAT_PATH), logging.WARNING)
        except Exception as ex:
            log("备份 chats.json 失败: %s" % ex, logging.ERROR)

    def _write_sessions(self):
        try:
            data = {"sessions": self.sessions, "current": self.current_id,
                    "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
            tmp = CHAT_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CHAT_PATH)
        except Exception as ex:
            log("保存 chats.json 失败: %s" % ex, logging.ERROR)

    def _activate_session(self, sid):
        s = self._find_session(sid)
        if s is None:
            return False
        self.current_id = sid
        self.messages = s["messages"]
        self.summary = s.get("summary") or ""
        try:
            self.summarized_upto = int(s.get("summarized_upto") or 0)
        except Exception:
            self.summarized_upto = 0
        self.summarized_upto = max(0, min(self.summarized_upto, len(self.messages)))
        self._render_limit = 50
        return True

    def _after_load_cleanup(self):
        """启动加载完历史后清理多余空会话（只保留最近一个）。"""
        try:
            if self._trim_empty_sessions():
                self._write_sessions()
                self._refresh_combo()
        except Exception as ex:
            log("清理空会话失败: %s" % ex, logging.WARNING)

    # ---------- 历史持久化（新格式 + 旧格式迁移） ----------
    def load_history(self):
        self.sessions = []
        self.current_id = None
        self.messages = []
        self.summary = ""
        self.summarized_upto = 0
        data = None
        if os.path.exists(CHAT_PATH):
            try:
                with open(CHAT_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as ex:
                log("读取 chats.json 失败（将备份并新建）: %s" % ex, logging.ERROR)
                self._backup_chats("解析失败")
                data = None
        try:
            if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                for raw in data["sessions"]:
                    s = self._normalize_session(raw)
                    if s is not None:
                        self.sessions.append(s)
                if not self.sessions:
                    self.sessions = [self._make_session(self._next_default_title())]
                ids = {s["id"] for s in self.sessions}
                cur = data.get("current")
                self.current_id = cur if cur in ids else max(
                    self.sessions, key=lambda s: s.get("updated") or "")["id"]
            elif isinstance(data, dict) and isinstance(data.get("messages"), list):
                # 旧格式迁移
                self._backup_chats("旧格式迁移")
                s = self._make_session("旧对话")
                for m in data.get("messages") or []:
                    if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
                        s["messages"].append({"role": m["role"],
                                              "content": str(m.get("content") or "")})
                s["summary"] = str(data.get("summary") or "")
                try:
                    s["summarized_upto"] = int(data.get("summarized_upto") or 0)
                except Exception:
                    s["summarized_upto"] = 0
                s["summarized_upto"] = max(0, min(s["summarized_upto"], len(s["messages"])))
                s["updated"] = str(data.get("updated") or s["created"])
                self.sessions = [s]
                self.current_id = s["id"]
                log("chats.json 旧格式已迁移为会话「旧对话」")
            else:
                if data is not None:
                    # 形状不认识（如顶层是数组）也先备份，避免直接覆盖
                    self._backup_chats("格式无法识别")
                    log("chats.json 结构无法识别，已备份并新建会话", logging.WARNING)
                self.sessions = [self._make_session(self._next_default_title())]
                self.current_id = self.sessions[0]["id"]
        except Exception as ex:
            log("chats.json 迁移失败: %s" % ex, logging.ERROR)
            self.sessions = [self._make_session(self._next_default_title())]
            self.current_id = self.sessions[0]["id"]
        self._activate_session(self.current_id)
        # 新格式/迁移结果落盘
        try:
            self._sync_session(touch=False)
            self._write_sessions()
        except Exception:
            pass

    def save_history(self):
        self._sync_session(touch=True)
        self._write_sessions()

    # ---------- 会话栏 ----------
    # ---------- 左栏：会话列表 ----------
    def _refresh_combo(self):
        """把会话按最近更新排序刷进左侧列表（保留方法名，内部已改为 QListWidget）。"""
        try:
            self._rebuilding = True
            self.session_list.blockSignals(True)
            self.session_list.clear()
            ordered = sorted(self.sessions, key=lambda s: s.get("updated") or "", reverse=True)
            cur_row = 0
            for i, s in enumerate(ordered):
                item = QListWidgetItem(s.get("title") or "未命名")
                item.setData(Qt.ItemDataRole.UserRole, s.get("id"))
                item.setToolTip(s.get("title") or "未命名")
                self.session_list.addItem(item)
                if s.get("id") == self.current_id:
                    cur_row = i
            if ordered:
                self.session_list.setCurrentRow(cur_row)
            self.session_list.blockSignals(False)
        except Exception as ex:
            log("刷新会话列表失败: %s" % ex, logging.WARNING)
        finally:
            self._rebuilding = False

    def _on_session_row_changed(self, row):
        if self._rebuilding or row < 0:
            return
        item = self.session_list.item(row)
        if item is None:
            return
        sid = item.data(Qt.ItemDataRole.UserRole)
        if not sid or sid == self.current_id:
            return
        if self.busy:
            self._refresh_combo()
            return
        self._switch_session(sid)

    def _session_context_menu(self, pos):
        """会话项右键：重命名 / 删除。"""
        item = self.session_list.itemAt(pos)
        if item is None:
            return
        sid = item.data(Qt.ItemDataRole.UserRole)
        m = QMenu(self)
        a1 = m.addAction("重命名", lambda: self._rename_session(sid))
        a2 = m.addAction("删除", lambda: self._delete_session(sid))
        if self.busy:
            a1.setEnabled(False)
            a2.setEnabled(False)
        m.exec(self.session_list.viewport().mapToGlobal(pos))

    def _on_session_changed(self, idx):
        # 兼容旧调用点（下拉框时代的方法名）
        self._on_session_row_changed(idx)

    def _switch_session(self, sid):
        if self.busy or not sid or sid == self.current_id:
            return
        self._sync_session(touch=False)
        if not self._activate_session(sid):
            return
        self._render_current_session()
        self.token_label.setText(self._usage_placeholder())
        self.token_label.setToolTip(self._usage_tooltip())
        # 切换时顺手清理多余空会话
        self._trim_empty_sessions()
        self._write_sessions()
        self._refresh_combo()
        self._update_busy_ui()

    @staticmethod
    def _session_is_empty(sess):
        """该会话是否一条消息都没有。"""
        return not (sess and (sess.get("messages") or []))

    def _trim_empty_sessions(self):
        """清理多余的空会话，只保留「最近一个」空会话。

        规则：
          - 有消息的会话一律保留；
          - 空会话只留 updated 最新的那一个；
          - 如果当前会话是空会话且被保留，保持不变；
          - 如果当前会话被删掉（理论上不会，因为留的是最新的），切到最近的。
        返回被删掉的数量。
        """
        if not self.sessions:
            return 0
        empties = [x for x in self.sessions if self._session_is_empty(x)]
        if len(empties) <= 1:
            return 0
        # 保留 updated 最新的空会话
        keep = max(empties, key=lambda x: x.get("updated") or "")
        removed = 0
        out = []
        for x in self.sessions:
            if x is keep or not self._session_is_empty(x):
                out.append(x)
            else:
                removed += 1
        if removed:
            self.sessions = out
            # 当前会话被清理了 → 切到保留的那个
            if not any(x.get("id") == self.current_id for x in self.sessions):
                self._activate_session(keep["id"])
                self._render_current_session()
            log("[聊天] 清理多余空会话 %d 个" % removed)
        return removed

    def _new_session(self):
        """新建会话。当前会话还是空的就直接复用，只把焦点放回输入框。"""
        if self.busy:
            return
        self._sync_session(touch=False)
        cur = self._current_session()
        # 当前会话没有任何消息 → 不新建，直接聚焦输入框
        if self._session_is_empty(cur):
            self._render_current_session()
            self._refresh_combo()
            self._update_busy_ui()
            self.input.setFocus()
            return
        s = self._make_session(self._next_default_title())
        self.sessions.append(s)
        self._activate_session(s["id"])
        self._render_current_session()
        self.token_label.setText(self._usage_placeholder())
        self._write_sessions()
        self._refresh_combo()
        self._update_busy_ui()
        self.input.setFocus()

    def _rename_current(self):
        self._rename_session(self.current_id)

    def _rename_session(self, sid):
        if self.busy:
            return
        s = self._find_session(sid)
        if s is None:
            return
        text, ok = QInputDialog.getText(self, "重命名对话", "会话名称（最多 30 字）：",
                                        QLineEdit.EchoMode.Normal, s.get("title") or "")
        if not ok:
            return
        t = (text or "").strip()
        if not t:
            QMessageBox.warning(self, "重命名对话", "名称不能为空")
            return
        s["title"] = t[:30]
        s["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._write_sessions()
        self._refresh_combo()

    def _delete_session(self, sid):
        """删除指定会话（右键菜单用）。"""
        if self.busy:
            return
        s = self._find_session(sid)
        if s is None:
            return
        ret = QMessageBox.question(self, "删除对话",
                                   "确定删除对话「%s」吗？\n（该对话的聊天记录会被删除，不可恢复）" % s.get("title", ""),
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.sessions = [x for x in self.sessions if x.get("id") != sid]
        if not self.sessions:
            self.sessions = [self._make_session(self._next_default_title())]
            self._activate_session(self.sessions[0]["id"])
        elif sid == self.current_id:
            recent = max(self.sessions, key=lambda x: x.get("updated") or "")
            self._activate_session(recent["id"])
        self._render_current_session()
        self.token_label.setText(self._usage_placeholder())
        self._write_sessions()
        self._refresh_combo()
        self._update_busy_ui()

    def _delete_current_session(self):
        self._delete_session(self.current_id)

    # ---------- 左栏收起/展开 ----------
    def _set_sidebar_collapsed(self, collapsed):
        self._sidebar_collapsed = bool(collapsed)
        try:
            self.sidebar.setVisible(not collapsed)
            self.sidebar_mini.setVisible(bool(collapsed))
            self.pet.cfg["chat_sidebar_collapsed"] = bool(collapsed)
            self.pet._save_cfg()
        except Exception as ex:
            log("切换会话栏失败: %s" % ex, logging.WARNING)

    def _set_sidebar_collapsed_quiet(self, collapsed):
        """只改界面不落盘（恢复几何时用）。"""
        self._sidebar_collapsed = bool(collapsed)
        try:
            self.sidebar.setVisible(not collapsed)
            self.sidebar_mini.setVisible(bool(collapsed))
        except Exception:
            pass

    # ---------- 输入框自动增高 ----------
    def _autosize_input(self):
        """默认约 2 行，随内容增高，最多 6 行。"""
        try:
            fm = QFontMetrics(self.input.font())
            line_h = max(15, fm.lineSpacing())
            doc = self.input.document()
            doc.setTextWidth(max(80, self.input.viewport().width()))
            lines = max(2, min(CHAT_INPUT_MAX_LINES, doc.size().height() / max(1.0, line_h)))
            h = int(lines * line_h + 18)
            h = max(CHAT_INPUT_MIN_H, h)
            if abs(self.input.height() - h) > 1:
                self.input.setFixedHeight(h)
        except Exception:
            pass

    def _maybe_auto_title(self, text):
        """新对话发第一条消息后，用第一句话前 15 字自动命名（默认名才生效）"""
        s = self._current_session()
        if s is None:
            return
        if len(s.get("messages") or []) != 1:
            return
        if not re.match(r"^新对话\s*\d*$", (s.get("title") or "").strip()):
            return
        title = " ".join((text or "").split())[:15].strip()
        if not title:
            return
        s["title"] = title
        s["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._refresh_combo()

    def _refresh_settings_menu(self):
        """菜单每次打开时重新读取：Key 是否已设置、生成中禁用哪些项。"""
        try:
            key = (self.pet.cfg.get("ds_api_key") or "").strip()
            self.key_action.setText("设置 Key（已设置）" if key else "设置 Key（未设置）")
        except Exception:
            pass
        busy = bool(self.busy)
        for act in (getattr(self, "rename_action", None),
                    getattr(self, "delete_action", None),
                    getattr(self, "clear_action", None)):
            if act is not None:
                act.setEnabled(not busy)

    def _update_busy_ui(self):
        """生成中禁用切换/新建/重命名/删除/清空，重新生成完再放开。"""
        busy = bool(self.busy)
        try:
            self.session_list.setEnabled(not busy)
            self.new_btn.setEnabled(not busy)
            self.rename_action.setEnabled(not busy)
            self.delete_action.setEnabled(not busy)
            self.clear_action.setEnabled(not busy)
            self.memory_btn.setEnabled(True)
            self._update_send_button()
        except Exception:
            pass

    # ---------- 主题 / 头像 / 空状态 ----------
    def _apply_theme(self):
        global _CHAT_THEME
        _CHAT_THEME = chat_detect_theme()
        t = _CHAT_THEME
        try:
            self.setStyleSheet(chat_qss(t))
        except Exception:
            pass
        try:
            self.empty_text.setStyleSheet("color:%s;" % t["sub"])
        except Exception:
            pass
        for b in self.findChildren(ChatBubble):
            b._style()

    def _load_avatar(self):
        for name in ("正面.png", "icon.png"):
            try:
                pix = QPixmap(os.path.join(SPRITE_DIR, name))
                if not pix.isNull():
                    return pix
            except Exception:
                pass
        return QPixmap()

    def _round_icon(self, pix, size):
        try:
            if pix is None or pix.isNull():
                return QPixmap()
            pm = pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
            out = QPixmap(size, size)
            out.fill(Qt.GlobalColor.transparent)
            p = QPainter(out)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()
            r = size * 0.3
            path.addRoundedRect(0, 0, size, size, r, r)
            p.setClipPath(path)
            p.drawPixmap(int((size - pm.width()) / 2), int((size - pm.height()) / 2), pm)
            p.end()
            return out
        except Exception:
            return pix

    def _build_empty_state(self):
        """空白状态：居中放大头像 + 一句欢迎语；有消息后自动隐藏。"""
        self.empty_state = QWidget(self.scroll.viewport())
        self.empty_state.setObjectName("ChatBody")
        self.empty_state.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(self.empty_state)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        lay.addStretch(3)
        self.empty_avatar = QLabel()
        self.empty_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if self._avatar_pix and not self._avatar_pix.isNull():
            self.empty_avatar.setPixmap(self._avatar_pix.scaledToHeight(
                int(CHAT_EMPTY_AVATAR * 0.8),     # 约 120px 高
                Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(self.empty_avatar)
        self.empty_text = QLabel(random.choice(CHAT_EMPTY_GREETINGS))
        self.empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = QFont(CHAT_FONT_FAMILY)
        f.setPixelSize(CHAT_FONT_SIZE + 1)
        self.empty_text.setFont(f)
        self.empty_text.setStyleSheet("color:%s;" % _CHAT_THEME["sub"])
        lay.addWidget(self.empty_text)
        self.empty_hint = QLabel("Enter 发送 · Shift+Enter 换行 · 说「记住：xxx」我会记住")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hf = QFont(CHAT_FONT_FAMILY)
        hf.setPixelSize(CHAT_SMALL_SIZE)
        self.empty_hint.setFont(hf)
        self.empty_hint.setStyleSheet("color:%s;" % _CHAT_THEME["sub"])
        lay.addWidget(self.empty_hint)
        lay.addStretch(4)
        self.empty_state.raise_()
        self.empty_state.hide()

    def _update_empty_state(self):
        """无消息时显示居中的头像+欢迎语；有消息后隐藏。"""
        try:
            has_bubble = False
            for i in range(self.msg_layout.count()):
                it = self.msg_layout.itemAt(i)
                if it is not None and it.widget() is not None:
                    has_bubble = True
                    break
            show = (len(self.messages) == 0 and not has_bubble)
            # 每次进入空白状态都重新抽一条欢迎语（避免老是同一句）
            if show:
                try:
                    self.empty_text.setText(random.choice(CHAT_EMPTY_GREETINGS))
                except Exception:
                    pass
            self._fit_empty_state()
            self.empty_state.setVisible(show)
            if show:
                self.empty_state.raise_()
        except Exception:
            pass

    def _fit_empty_state(self):
        """把空白状态铺满消息区可视范围。"""
        try:
            vp = self.scroll.viewport()
            self.empty_state.setGeometry(0, 0, vp.width(), vp.height())
            self.empty_state.raise_()
        except Exception:
            pass

    # ---------- 位置记忆 ----------
    def _restore_geometry(self):
        """恢复窗口大小/位置与左栏收起状态。"""
        try:
            g = self.pet.cfg.get("chat_window")
            if isinstance(g, dict):
                # 左栏收起状态
                self._set_sidebar_collapsed_quiet(bool(g.get("sidebar_collapsed",
                                                             self.pet.cfg.get("chat_sidebar_collapsed", False))))
                x = int(g.get("x", 0))
                y = int(g.get("y", 0))
                w = int(g.get("w", 0))
                h = int(g.get("h", 0))
                if w >= CHAT_MIN_WIDTH and h >= self.minimumHeight():
                    screen = QApplication.primaryScreen().availableGeometry()
                    if screen.contains(x, y):
                        self.setGeometry(x, y, w, h)
                        return
            else:
                self._set_sidebar_collapsed_quiet(
                    bool(self.pet.cfg.get("chat_sidebar_collapsed", False)))
            self.resize(760, 620)
        except Exception:
            self.resize(760, 620)

    def _save_geometry(self):
        try:
            g = self.geometry()
            self.pet.cfg["chat_window"] = {"x": g.x(), "y": g.y(),
                                           "w": max(CHAT_MIN_WIDTH, g.width()),
                                           "h": g.height(),
                                           "sidebar_collapsed": bool(self._sidebar_collapsed)}
            self.pet.cfg["chat_sidebar_collapsed"] = bool(self._sidebar_collapsed)
            self.pet._save_cfg()
        except Exception as ex:
            log("保存聊天窗口位置失败: %s" % ex, logging.WARNING)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        try:
            self._fit_empty_state()
            self._autosize_input()
        except Exception:
            pass

    def showEvent(self, e):
        super().showEvent(e)
        try:
            QTimer.singleShot(0, self._fit_empty_state)
            QTimer.singleShot(0, self._update_empty_state)
        except Exception:
            pass

    # ---------- 消息区 ----------
    def _scroll_bottom(self):
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _add_bubble(self, role, text):
        b = ChatBubble(role, text)
        wrap = QWidget()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        if role == "user":
            h.addStretch(1)
            h.addWidget(b)
        elif role == "assistant":
            av = QLabel()
            if self._avatar_small and not self._avatar_small.isNull():
                av.setPixmap(self._avatar_small)
            av.setFixedSize(CHAT_AVATAR_SIZE, CHAT_AVATAR_SIZE)
            av.setAlignment(Qt.AlignmentFlag.AlignTop)
            h.addWidget(av, 0, Qt.AlignmentFlag.AlignTop)
            h.addWidget(b)
            h.addStretch(1)
        else:
            h.addSpacing(CHAT_AVATAR_SIZE + 8)
            h.addWidget(b)
            h.addStretch(1)
        b._wrap = wrap
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, wrap)
        self._update_empty_state()
        self._scroll_bottom()
        return b

    def _remove_bubble(self, b):
        if b is None:
            return
        try:
            wrap = getattr(b, "_wrap", None)
            if wrap is not None:
                self.msg_layout.removeWidget(wrap)
                wrap.deleteLater()
        except Exception:
            pass

    def render_message(self, role, text):
        return self._add_bubble(role, text)

    def render_history(self):
        self._render_current_session()

    def _clear_bubbles(self):
        for i in range(self.msg_layout.count() - 1, -1, -1):
            item = self.msg_layout.itemAt(i)
            w = item.widget() if item is not None else None
            if w is None:
                continue
            self.msg_layout.takeAt(i)
            if w is self.load_more_btn:
                w.setParent(None)
            else:
                w.deleteLater()
        self._update_empty_state()

    def _render_current_session(self):
        """只渲染当前会话；消息多时先渲染最近 50 条，上方给「加载更早消息」"""
        self._hide_typing()
        self.current_bubble = None
        self.current_text = ""
        self._clear_bubbles()
        total = len(self.messages)
        self._render_from = max(0, total - max(1, self._render_limit))
        if self._render_from > 0:
            self.msg_layout.insertWidget(0, self.load_more_btn)
        for m in self.messages[self._render_from:]:
            self.render_message(m.get("role", "assistant"), m.get("content", ""))
        self._update_empty_state()
        self._scroll_bottom()

    def _load_earlier(self):
        try:
            sb = self.scroll.verticalScrollBar()
            keep = sb.value()
        except Exception:
            keep = 0
        self._render_limit += 50
        self._render_current_session()
        try:
            QTimer.singleShot(0, lambda v=keep: self.scroll.verticalScrollBar().setValue(v))
        except Exception:
            pass

    # ---------- 窗口 ----------
    def show_window(self):
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()
        self._refresh_combo()
        self._update_empty_state()
        QTimer.singleShot(50, self._scroll_bottom)

    def closeEvent(self, e):
        # 关闭只是隐藏，内容和历史都保留
        self._save_geometry()
        self._sync_session(touch=False)
        self._write_sessions()
        e.ignore()
        self.hide()

    def shutdown(self):
        """退出前调用：保存会话与窗口位置、停掉后台请求线程"""
        try:
            self._sync_session(touch=False)
            self._write_sessions()
        except Exception:
            pass
        self._save_geometry()
        for w in (self.worker, self.summary_worker):
            if w is None:
                continue
            try:
                if hasattr(w, "request_stop"):
                    w.request_stop()
                if w.isRunning():
                    w.wait(1500)
                if w.isRunning():
                    w.terminate()
                    w.wait(500)
            except Exception as ex:
                log("终止聊天线程失败: %s" % ex, logging.WARNING)
        self.worker = None
        self.summary_worker = None

    def eventFilter(self, obj, event):
        et = event.type()
        if obj is self.input and et == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if mods & Qt.KeyboardModifier.ShiftModifier:
                    return False  # Shift+Enter 换行，交给 QTextEdit
                self._on_send_clicked()
                return True
        return super().eventFilter(obj, event)

    # ---------- 发送 / 停止 ----------
    @staticmethod
    def _extract_memory(text):
        """识别「记住:xxx」「记住 xxx」这种指令，返回要记住的内容"""
        t = (text or "").strip()
        for prefix in ("记住：", "记住:", "记住 "):
            if t.startswith(prefix):
                return t[len(prefix):].strip()
        if t.startswith("记住") and len(t) > 2:
            return t[2:].strip(" :：,，。")
        return ""

    def _add_notice(self, text):
        return self._add_bubble("note", text)

    def _on_send_clicked(self):
        if self.busy:
            self.stop_stream()
        else:
            self.send_message()

    def _update_send_button(self):
        try:
            self.send_btn.setText("停止" if self.busy else "发送")
        except Exception:
            pass

    def _usage_placeholder(self):
        return "输入 - · 输出 - · 缓存命中 -"

    def send_message(self):
        text = self.input.toPlainText().strip()
        if not text or self.busy or self._current_session() is None:
            return
        self.input.clear()
        self.messages.append({"role": "user", "content": text})
        self._maybe_auto_title(text)
        self._turn_user_bubble = self.render_message("user", text)
        self._turn_user_text = text
        self.save_history()

        # 「提醒我 X 分钟后 xxx」→ 本地解析成待办（不调用模型）
        try:
            if self.pet.handle_remind_chat(text):
                self._add_notice("⏰ 已设置提醒")
                self._finish_busy()
                return
        except Exception as _ex:
            log("解析提醒指令失败: %s" % _ex, logging.WARNING)

        # 「记住 xxx」→ 写入长期记忆（会拼进 system 消息固定位置）
        mem_text = self._extract_memory(text)
        if mem_text:
            self.pet.memory.add(mem_text)
            self._add_notice("✅ 已记住：" + mem_text)

        self.busy = True
        self._stop_seen = False
        self._update_busy_ui()
        self.pet.chat_paused = True
        self.current_text = ""
        self.current_bubble = None
        self.token_label.setText("本次　生成中…")
        self._show_typing()

        key = (self.pet.cfg.get("ds_api_key") or "").strip()
        if not key:
            self._on_failed("还没设置 API Key：右键桌宠 →「设置 Key」填写后重试。")
            return

        if self._need_summary():
            self._summarize_then_chat()
        else:
            self._start_chat_stream()

    def stop_stream(self):
        """中断当前流式请求，保留已经收到的内容"""
        if not self.busy:
            return
        self._stop_seen = True
        try:
            if self.worker is not None and hasattr(self.worker, "request_stop"):
                self.worker.request_stop()
        except Exception as ex:
            log("停止请求失败: %s" % ex, logging.WARNING)
        self._finalize_stop()

    def _finalize_stop(self):
        if not self.busy:
            return
        self._hide_typing()
        text = (self.current_text or "").strip()
        if self.current_bubble is None and text:
            self.current_bubble = self._add_bubble("assistant", text)
        if text:
            self.messages.append({"role": "assistant", "content": text})
            self._turn_user_bubble = None
            self._turn_user_text = ""
            self.save_history()
        else:
            self._remove_bubble(self.current_bubble)
            self.current_bubble = None
        self.current_text = ""
        self.token_label.setText(self._usage_placeholder())
        self._finish_busy()

    # ---------- 生成中提示 ----------
    def _show_typing(self):
        self._hide_typing()
        self._typing_dots = 0
        self._typing_bubble = self._add_bubble("note", "正在想…")
        self._typing_timer.start()

    def _tick_typing(self):
        if self._typing_bubble is None:
            return
        self._typing_dots = (self._typing_dots % 5) + 1
        self._typing_bubble.set_text("正在想" + "…" * self._typing_dots)

    def _hide_typing(self):
        try:
            self._typing_timer.stop()
        except Exception:
            pass
        if self._typing_bubble is not None:
            self._remove_bubble(self._typing_bubble)
            self._typing_bubble = None

    # ---------- 请求（保持原逻辑，只使用当前会话数据） ----------
    def _estimate_tokens(self):
        _style = self.pet.cfg.get("reply_style", REPLY_STYLE_DEFAULT)
        _persona = persona_from_cfg(self.pet.cfg)
        total = len(build_system_prompt(_style, _persona)) + len(self.summary)
        for m in self.messages[self.summarized_upto:]:
            total += len(m.get("content") or "")
        return int(total / 1.6)

    def _need_summary(self):
        return (self._estimate_tokens() > SUMMARIZE_AT_TOKENS or
                self._estimate_tokens() > CONTEXT_LIMIT_TOKENS * 0.7)

    def _build_request_messages(self):
        # 开头永远是同一段 system（人设 + 长度句 + 长期记忆）。
        # 长度句由「回复风格」决定：同一风格下内容恒定 → 前缀缓存稳定；切换风格时失效一次属正常。
        style = self.pet.cfg.get("reply_style", REPLY_STYLE_DEFAULT)
        persona = persona_from_cfg(self.pet.cfg)
        sys_text = build_system_prompt(style, persona)
        msgs = [{"role": "system", "content": self.pet.memory.system_content(sys_text)}]
        if self.summary:
            msgs.append({"role": "system",
                         "content": "以下是你和用户更早的聊天摘要（原始记录保留在本地，不再重复发送）：\n" + self.summary})
        # 当前会话的历史按原顺序追加
        for m in self.messages[self.summarized_upto:]:
            msgs.append({"role": m["role"], "content": m["content"]})
        return msgs

    def _summarize_then_chat(self):
        key = (self.pet.cfg.get("ds_api_key") or "").strip()
        cut = max(0, len(self.messages) - KEEP_RECENT_MESSAGES)
        old = self.messages[self.summarized_upto:cut]
        if cut <= self.summarized_upto:
            self._start_chat_stream()
            return
        self.token_label.setText("正在整理前情摘要…")
        self.summary_worker = SummaryWorker(self.summary, old, key)
        self.summary_worker.done.connect(lambda text, c=cut: self._on_summary_done(text, c))
        self.summary_worker.failed.connect(self._on_summary_failed)
        self.summary_worker.start()

    def _on_summary_done(self, text, cut):
        if text:
            self.summary = text
            self.summarized_upto = cut
            self.save_history()
        self._start_chat_stream()

    def _on_summary_failed(self, msg):
        log("摘要失败: %s" % msg, logging.WARNING)
        # 摘要失败不阻塞聊天，直接发（可能超长，接口会报错）
        self._add_notice("⚠️ 摘要失败，将发送完整历史")
        self._start_chat_stream()

    def _start_chat_stream(self):
        key = (self.pet.cfg.get("ds_api_key") or "").strip()
        messages = self._build_request_messages()
        self.token_label.setText("本次　生成中…")
        self.worker = ChatWorker(messages, key, max_tokens=self.pet.cfg.get("max_tokens", MAX_TOKENS_DEFAULT))
        self.pet.set_work_state("thinking")
        self.worker.chunk.connect(self._on_chunk)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_chunk(self, text):
        if self.pet._work_state == "thinking":
            self.pet.set_work_state("working")
        if not self.busy:
            return
        if self.current_bubble is None:
            self._hide_typing()
            self.current_bubble = self._add_bubble("assistant", "")
        self.current_text += text
        self.current_bubble.set_text(self.current_text)
        self._scroll_bottom()

    def _on_done(self, full, usage):
        self.pet.set_work_state("success")
        if not self.busy:
            return
        self._hide_typing()
        full = (full or "").strip()
        if not full:
            full = "（这轮没有返回内容）"
        if self.current_bubble is None:
            self.current_bubble = self._add_bubble("assistant", full)
        else:
            self.current_bubble.set_text(full)
        self.messages.append({"role": "assistant", "content": full})
        self._turn_user_bubble = None
        self._turn_user_text = ""
        self.save_history()
        self._finish_busy()
        self._update_usage(usage, full)
        first = self._first_sentence(full)
        if first:
            self.pet.say(first)

    def _rollback_user_turn(self):
        """失败时把这一轮已写盘的 user 消息撤回，避免历史里出现连续两条 user"""
        text = self._turn_user_text
        bubble = self._turn_user_bubble
        self._turn_user_bubble = None
        self._turn_user_text = ""
        if not text:
            return
        for i in range(len(self.messages) - 1, -1, -1):
            m = self.messages[i]
            if m.get("role") == "user" and m.get("content") == text:
                del self.messages[i]
                self.save_history()
                break
        self._remove_bubble(bubble)

    def _on_failed(self, msg):
        self.pet.set_work_state("error")
        if not self.busy:
            return
        self._hide_typing()
        if self.current_bubble is None:
            self.current_bubble = self._add_bubble("error", "⚠️ " + msg)
        else:
            self.current_bubble.mark_error()
            self.current_bubble.set_text("⚠️ " + msg)
        self._rollback_user_turn()
        self._scroll_bottom()
        self.token_label.setText(self._usage_placeholder())
        self._finish_busy()

    def _finish_busy(self):
        self.busy = False
        self._stop_seen = False
        self._hide_typing()
        self._update_busy_ui()
        self.pet.chat_paused = False
        self._update_empty_state()
        self._scroll_bottom()

    # ---------- 用量 ----------
    def _usage_tooltip(self):
        u = self.pet.cfg.get("usage") or {}
        try:
            hit = int(u.get("prompt_cache_hit_tokens", 0) or 0)
            miss = int(u.get("prompt_cache_miss_tokens", 0) or 0)
            comp = int(u.get("completion_tokens", 0) or 0)
            tot = int(u.get("total_tokens", 0) or 0)
            req = int(u.get("requests", 0) or 0)
        except Exception:
            hit = miss = comp = tot = req = 0
        denom = hit + miss
        rate = "%.0f%%" % (hit / denom * 100.0) if denom else "-"
        return ("累计：输入 %d · 输出 %d · 合计 %d tokens · 缓存命中率 %s · 共 %d 次"
                % (hit + miss, comp, tot, rate, req))

    def _update_usage(self, usage, text=None):
        usage = usage or {}
        try:
            hit = int(usage.get("prompt_cache_hit_tokens") or 0)
            miss = int(usage.get("prompt_cache_miss_tokens") or 0)
            comp = int(usage.get("completion_tokens") or 0)
            prompt = int(usage.get("prompt_tokens") or (hit + miss))
        except Exception:
            hit = miss = comp = prompt = 0
        if usage:
            self.pet.add_usage(usage)
            denom = hit + miss
            rate = ("%.0f%%" % (hit / denom * 100.0)) if denom else "-"
            if prompt or comp:
                line = "输入 %d · 输出 %d · 缓存命中 %s" % (prompt, comp, rate)
            else:
                line = self._usage_placeholder()
        else:
            line = self._usage_placeholder()
        self.token_label.setText(line)
        self.token_label.setToolTip(self._usage_tooltip())

    @staticmethod
    def _first_sentence(text):
        for i, ch in enumerate(text):
            if ch in "。！？!?\n":
                s = text[:i + 1].strip()
                return s if s else ""
        return text.strip()[:60]

    # ---------- 清空（只清当前会话内容，标题保留） ----------
    def clear_chat(self):
        if self.busy:
            return
        ret = QMessageBox.question(self, "清空对话", "确定清空当前对话吗？（只清空这个会话的消息）",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        s = self._current_session()
        if s is None:
            return
        self.messages = []
        self.summary = ""
        self.summarized_upto = 0
        self.current_bubble = None
        self._turn_user_bubble = None
        self._turn_user_text = ""
        self._hide_typing()
        self.save_history()
        self._render_current_session()
        self.token_label.setText(self._usage_placeholder())
        self.token_label.setToolTip(self._usage_tooltip())
        self.input.clear()

class MemoryDialog(QDialog):
    """记忆管理：查看 / 添加 / 编辑 / 删除长期记忆（全局唯一，归属 PetApp）"""

    def __init__(self, app):
        super().__init__(app.main_pet)
        self.app = app
        self.setWindowTitle("记忆管理")
        self.resize(480, 440)
        lay = QVBoxLayout(self)
        tip = QLabel("这些记忆会拼在系统提示词末尾；只有新增或修改记忆时，前缀缓存才会失效一次。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#777;font-size:11px;")
        lay.addWidget(tip)
        self.list = QListWidget(self)
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        for label, fn in (("添加记忆", self.add_item), ("编辑", self.edit_item),
                          ("删除", self.delete_item)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.hide)
        row.addWidget(close)
        lay.addLayout(row)
        self.reload()

    def reload(self):
        self.list.clear()
        for m in self.app.memory.sorted_items():
            self.list.addItem(f"#{m['id']}  {m['text']}")

    def _current(self):
        row = self.list.currentRow()
        items = self.app.memory.sorted_items()
        if row < 0 or row >= len(items):
            return None
        return items[row]

    def add_item(self):
        text, ok = QInputDialog.getText(self, "添加记忆", "要记住的内容：")
        if ok and text.strip():
            self.app.memory.add(text.strip())
            self.reload()

    def edit_item(self):
        m = self._current()
        if not m:
            QMessageBox.information(self, "编辑记忆", "先在列表里选中一条记忆")
            return
        text, ok = QInputDialog.getText(self, "编辑记忆", "内容：",
                                        QLineEdit.EchoMode.Normal, m["text"])
        if ok and text.strip():
            self.app.memory.update(m["id"], text.strip())
            self.reload()

    def delete_item(self):
        m = self._current()
        if not m:
            QMessageBox.information(self, "删除记忆", "先在列表里选中一条记忆")
            return
        ret = QMessageBox.question(self, "删除记忆",
                                   "确定删除这条记忆吗？\n\n" + m["text"][:80],
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.Yes:
            self.app.memory.remove(m["id"])
            self.reload()


class RpsPanel(QFrame):
    """猜拳小面板：石头剪刀布，宠物随机出（全局唯一，归属 PetApp）"""

    def __init__(self, app):
        super().__init__(None)
        self.app = app
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("""
            QFrame { background: rgba(255,255,255,0.96); border-radius: 14px; border: 1px solid rgba(0,0,0,0.06); }
            QPushButton { border: none; border-radius: 10px; font-size: 15px; padding: 8px 10px; background: rgba(86,134,254,0.10); }
            QPushButton:hover { background: rgba(86,134,254,0.22); }
            QLabel { font-size: 13px; color: #444; }
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        self.title = QLabel("石头剪刀布，你出哪个？")
        lay.addWidget(self.title)
        row = QHBoxLayout()
        row.setSpacing(8)
        for c in RPS_CHOICES:
            b = QPushButton(f"{RPS_EMOJI[c]} {c}")
            b.setFixedHeight(44)
            b.clicked.connect(lambda _, x=c: self.app.rps_play(x))
            row.addWidget(b)
        lay.addLayout(row)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        close = QPushButton("关闭")
        close.setFixedSize(64, 30)
        close.clicked.connect(self.close_panel)
        bottom.addWidget(close)
        lay.addLayout(bottom)
        self.setFixedSize(330, 208)

        # 60 秒无操作自动关闭
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(60000)
        self._idle_timer.timeout.connect(self.close_panel)
        self._filter_installed = False
        self.hide()

    def show_at(self, pet):
        self.title.setText("石头剪刀布，你出哪个？")
        self.result.setText("")
        self.move(int(pet.x() + pet.width() / 2 - self.width() / 2),
                  int(pet.y() - self.height() - 8))
        self.show()
        self.raise_()
        app = QApplication.instance()
        if app is not None and not self._filter_installed:
            app.installEventFilter(self)
            self._filter_installed = True
        self._idle_timer.start()

    def _detach_filter(self):
        self._idle_timer.stop()
        if self._filter_installed:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self._filter_installed = False

    def close_panel(self):
        """点面板外 / 按 Esc / 60 秒无操作都走这里，统一复位游戏状态"""
        self._detach_filter()
        self.hide()
        if self.app is not None:
            self.app._close_rps()

    def eventFilter(self, obj, event):
        et = event.type()
        if et == QEvent.Type.MouseButtonPress:
            try:
                inside = self.geometry().contains(event.globalPosition().toPoint())
            except Exception:
                inside = False
            if not inside:
                self.close_panel()
                return False
            self._idle_timer.start()
        elif et == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.close_panel()
                return True
            self._idle_timer.start()
        return False


class CatchGameWindow(QWidget):
    """接食物小游戏：全屏透明窗口，鼠标左右控制大肥鱼接住掉落的食物"""

    def __init__(self, app):
        super().__init__(None)
        self.app = app
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        # 覆盖「宠物所在的那块屏幕」（多屏时不再固定主屏）
        pet = app.main_pet
        screen = None
        try:
            if pet is not None:
                screen = pet.screen()
            if screen is None:
                cpos = QApplication.primaryScreen().availableGeometry().center()
                screen = QApplication.screenAt(cpos) or QApplication.primaryScreen()
        except Exception:
            screen = QApplication.primaryScreen()
        self._screen_geo = screen.geometry()
        self._origin = self._screen_geo.topLeft()
        self.setGeometry(self._screen_geo)
        self.W, self.H = self._screen_geo.width(), self._screen_geo.height()
        pet = app.main_pet
        self.sprites = pet.sprites if pet is not None else {}
        self.cur_h = pet.cur_h if pet is not None else int(340 * 0.7)
        self.pet_h = int(min(190, self.H * 0.28))
        src = self.sprites.get(("正面", self.cur_h))
        self.pet_w = int(self.pet_h * src.width() / max(1, src.height())) if src else 120
        self.foods = []
        self.score = 0
        self.missed = 0
        self.mouse_x = self.W / 2
        self.start_t = time.time()
        self.last_t = self.start_t
        self.next_spawn = 0.0
        self.finished = False
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def start(self):
        self.show()
        self.raise_()
        self.setFocus()
        self._timer.start()

    def _tick(self):
        now = time.time()
        dt = max(0.001, min(0.05, now - self.last_t))
        self.last_t = now
        if now - self.start_t >= CATCH_DURATION:
            self.finish()
            return
        if now >= self.next_spawn:
            self.foods.append({
                "emoji": random.choice(FOODS),
                "x": random.uniform(60, self.W - 60),
                "y": -30.0,
                "vy": random.uniform(220, 400),
            })
            self.next_spawn = now + random.uniform(0.35, 0.75)

        pet_cx = self.mouse_x
        pet_top = self.H - self.pet_h - 24
        keep = []
        for f in self.foods:
            f["y"] += f["vy"] * dt
            caught = (f["y"] + 26 >= pet_top and f["y"] <= pet_top + 50 and
                      abs(f["x"] - pet_cx) <= self.pet_w / 2 + 12)
            if caught:
                self.score += 1
                continue
            if f["y"] > self.H + 40:
                self.missed += 1
                continue
            keep.append(f)
        self.foods = keep
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        left = max(0, int(CATCH_DURATION - (time.time() - self.start_t)))
        p.setPen(QColor(60, 60, 90))
        p.setFont(QFont("Microsoft YaHei UI", 18, QFont.Weight.Bold))
        p.drawText(QRectF(0, 24, self.W, 40), Qt.AlignmentFlag.AlignCenter,
                   f"接到 {self.score} 个    剩余 {left} 秒")
        p.setFont(QFont("Microsoft YaHei UI", 11))
        p.setPen(QColor(120, 120, 140))
        p.drawText(QRectF(0, 62, self.W, 30), Qt.AlignmentFlag.AlignCenter,
                   "鼠标左右移动控制大肥鱼，接住掉落的食物（Esc 提前结束）")
        p.setFont(QFont("Segoe UI Emoji", 30))
        p.setPen(QColor(40, 40, 60))
        for f in self.foods:
            p.drawText(QRectF(f["x"] - 24, f["y"] - 24, 48, 48),
                       Qt.AlignmentFlag.AlignCenter, f["emoji"])
        src = self.sprites.get(("正面", self.cur_h))
        if src:
            x = self.mouse_x - self.pet_w / 2
            y = self.H - self.pet_h - 24
            p.drawPixmap(QRectF(x, y, self.pet_w, self.pet_h), src,
                         QRectF(0, 0, src.width(), src.height()))
        p.end()

    def mouseMoveEvent(self, e):
        self.mouse_x = e.position().x()
        self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.finish()

    def finish(self):
        if self.finished:
            return
        self.finished = True
        self._timer.stop()
        self.hide()
        self.app._catch_finished(self.score)


class PetWindow(QWidget):
    """单只宠物：精灵、位置、移动与动画、绘制、鼠标交互、气泡。

    全局唯一的东西（托盘/菜单/聊天/记忆/提醒/节日/截屏/监控/游戏/用量/配置）
    都在 PetApp 里，通过 self.app 访问。"""

    def __init__(self, app, pet_cfg):
        self.app = app
        self.pet = pet_cfg
        self.pet_id = str(pet_cfg.get("id") or "main")

        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if app.cfg.get("topmost", True):
            flags |= Qt.WindowType.WindowStaysOnTopHint
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("大肥鱼桌宠" if self.pet_id == "main" else "大肥鱼桌宠-%s" % self.pet_id)

        # 精灵加载
        self.sprites = {}
        for label, mult in SIZE_LEVELS.items():
            h = int(340 * mult)
            for name in ["正面", "侧面", "背面"]:
                sized = os.path.join(SPRITE_DIR, f"{name}_{h}.png")
                if os.path.exists(sized):
                    pix = QPixmap(sized)
                else:
                    pix = QPixmap(os.path.join(SPRITE_DIR, f"{name}.png")).scaledToHeight(
                        h, Qt.TransformationMode.SmoothTransformation)
                self.sprites[(name, h)] = pix

        self.cur_h = int(340 * self.pet["size"])
        self.win_mx = int(self.cur_h * 0.062) + 6
        self.win_w = max(p.width() for k, p in self.sprites.items() if k[1] == self.cur_h) + self.win_mx * 2
        self.setFixedSize(self.win_w, self.cur_h + BUBBLE_H + MARGIN * 2 + 10)

        # 状态
        self.mode = self.pet["mode"] if self.pet["mode"] in VALID_MODES else "wander"
        self.dir = "down"
        self.facing = 1
        self.target = None
        self.rest_until = 0
        self.cur_speed = 0.0
        self.prev_key = None
        self.cross_t = 0.0
        self.action = None
        self.action_t = 0.0
        self.action_total = 1.0
        # 动画链状态（每只宠物独立运行）
        self.chain_action = "idle_breath"
        self.chain_dur = ACTIONS["idle_breath"]["duration"]
        self.chain_elapsed = 0.0
        self.prev_action_key = None
        self._action_fade = 0.0
        self.squash_t = 0.0
        self._squash_pending = False
        self._sys_anim_cache = None
        self.last_click_action = None
        # 物理状态：拖拽弹簧 / 甩抛体 / 速度采样 / 形变弹簧
        self.body = PhysicsBody(0, 0)
        self.spring = SpringFollower()
        self.tracker = VelocityTracker()
        self.wobble = WobbleSpring()
        self._dragging_physics = False
        self._drag_offset = (0, 0)
        # 帧序列动画状态
        self._frame_action = None
        self._frame_index = 0
        self._frame_elapsed = 0.0
        self._frame_fps = FRAME_ANIM_FPS_DEFAULT
        self._frame_loop = True
        self._frame_hold = True
        self._cam_ref_cache = {}
        self._camera_fix = True      # 镜头推近补偿（素材带特写时按比例缩小）
        # 转身过程状态
        self._turn_t = 0.0
        self._turn_total = 0.0
        self._turn_target_dir = self.dir if hasattr(self, "dir") else "down"
        self._turn_target_facing = None
        self._turn_swapped = False
        self._turn_w_comp = 1.0
        self._turn_frame_action = None
        self._turn_frame_mirror = False
        # 待机调度状态
        self._tier_last = {}          # tier -> 上次出现的时间(秒)
        self._action_last = {}        # 动作 -> 上次播放时间(秒)
        self._action_history = []     # 最近播放的动作（调试用，最多 20 条）
        self.last_interact = time.time()   # 最近互动时间
        self._idle_stage = "normal"   # normal / bored / sit / sleep
        self._combo = None            # 正在播放的连招
        self._combo_step = 0
        self._combo_t = 0.0
        # 时节特效（该宠物自己屏幕上叠加绘制）
        self._fx = None
        self.bubble_text = ""
        self.bubble_until = 0
        # 提醒气泡状态（加大版，带图标和按钮）
        self.remind_text = ""
        self.remind_icon = ""
        self.remind_kind = ""
        self.remind_until = 0.0
        self.remind_hold = 0.0
        self.remind_hover = False
        self._remind_last_tick = None    # 悬停暂停用：上一 tick 的时刻
        self.remind_todo_id = None
        self.remind_schedule_id = None
        self._remind_btn_rects = {}     # name -> QRectF（点击命中用）
        # 提醒走动状态（走到屏幕中间 / 走回原位）
        self.remind_walk = None         # {"fx","fy","tx","ty","t0","dur","on_done","return_to"}
        self.remind_origin = None       # 提醒前的位置 (x, y)，回位用
        self.remind_user_moved = False  # 提醒中被用户拖拽 → 取消回位
        self.bubble_inner = False
        self.last_speak_tick = 0
        self.t = 0
        self.jump_t = 0
        self.dragging = False
        self.drag_offset = None
        self.drag_start_pos = None
        self.last_line = ""
        self.last_press_pos = None

        # 触摸手势状态
        self._press_zone = None
        self._stroke = None
        self._dbl_guard = 0.0
        self._face_clicks = []
        self._poke_pending = False
        self._touch_cd = 0.0

        # 面板
        self.food_panel = FoodPanel(self.on_food)
        # 单击延迟判定（等双击）：单击=回嘴，双击=喂食
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._on_single_click)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(TICK)

        self.bubble_font = QFont("Microsoft YaHei UI", 11)
        # 提醒气泡字体：比普通气泡大约 1.3 倍
        self.remind_font = QFont("Microsoft YaHei UI", 0)
        self.remind_font.setPixelSize(int(15 * REMIND_BUBBLE_SCALE))

        x, y = self.pet.get("x"), self.pet.get("y")
        if x is None or y is None:
            screen = QApplication.primaryScreen().availableGeometry()
            x = screen.right() - self.width() - 80
            y = screen.bottom() - self.height() - 60
        self.move(int(x), int(y))
        if self.pet.get("display", True):
            self.show()
        self.snap_into_screen()
        if app.cfg.get("passthrough", False):
            self._apply_passthrough(True)

    # ---------- 时节特效 ----------
    def start_fx(self, effect):
        if effect is None:
            return
        self._fx = effect
        self.update()

    def stop_fx(self):
        self._fx = None
        self.update()

    def _step_fx(self, dt):
        fx = self._fx
        if fx is None:
            return
        fx.step(dt)
        if fx.dead:
            self._fx = None
        self.update()

    # ---------- 物理 / 多屏 ----------
    def _physics_cfg(self):
        raw = self.app.cfg.get("physics")
        cfg, _bad = sanitize_physics(raw, DEFAULT_PHYSICS)
        return cfg

    def _screen_rects(self):
        """所有屏幕的可用工作区（已扣任务栏），多屏/DPI 各自独立。"""
        rects = []
        try:
            for s in QApplication.screens():
                g = s.availableGeometry()
                rects.append((g.x(), g.y(), g.width(), g.height()))
        except Exception:
            pass
        if not rects:
            g = QApplication.primaryScreen().availableGeometry()
            rects = [(g.x(), g.y(), g.width(), g.height())]
        return rects

    def _body_state(self):
        """把窗口位置同步进物理体（外部 move 后调用）。"""
        self.body.x = float(self.x())
        self.body.y = float(self.y())

    def _apply_body(self):
        """把物理体位置写回窗口。"""
        self.move(int(round(self.body.x)), int(round(self.body.y)))

    def _physics_feature_on(self):
        return self.app.is_enabled("physics")

    def _begin_drag_spring(self, press_gpos):
        """进入拖拽：用**按下那一刻**的鼠标位置算抓取偏移。

        这里必须用按下点而不是当前点，否则偏移 = 0，弹簧目标恒等于当前位置，
        宠物会完全跟不上鼠标（拖不动）。
        """
        ph = self._physics_cfg()
        self.body.asleep = True
        self.body.vx = 0.0
        self.body.vy = 0.0
        self._body_state()
        px, py = press_gpos.x(), press_gpos.y()
        self._drag_offset = (px - self.x(), py - self.y())
        self.spring = SpringFollower(ph["spring_stiffness"], ph["spring_damping"])
        self.spring.start((self.x(), self.y()), (self.x(), self.y()))
        self.tracker.clear()
        self.tracker.add(self.x(), self.y(), time.time())
        self._dragging_physics = True

    def _drag_to(self, gpos):
        """拖拽时用阻尼弹簧追随鼠标目标位置。"""
        if not self._dragging_physics:
            return
        tx = gpos.x() - self._drag_offset[0]
        ty = gpos.y() - self._drag_offset[1]
        self.spring.set_target((tx, ty))
        self.tracker.add(self.x(), self.y(), time.time())

    def _step_drag(self, dt):
        if not self._dragging_physics:
            return
        nx, ny = self.spring.step(dt)
        self.body.x, self.body.y = float(nx), float(ny)
        self._apply_body()
        self._body_state()

    def _release_drag_physics(self):
        """松手：按最近 100ms 速度抛出；速度太小则原地放下。

        速度必须在同步 body 之前采样，否则会把窗口位置覆盖掉。
        """
        self._dragging_physics = False
        ph = self._physics_cfg()
        vx, vy = self.tracker.velocity(time.time())
        self.tracker.clear()
        # 先把弹簧落点搬回窗口，再同步进物理体（避免丢掉最后一帧位移）
        try:
            nx, ny = self.spring.pos
            self.move(int(round(nx)), int(round(ny)))
        except Exception:
            pass
        self._body_state()
        speed = (vx * vx + vy * vy) ** 0.5
        if speed < PHYS.STOP_SPEED * 3:
            # 原地放下：位置保持当前窗口位置，清空速度与休眠
            self.body.place(float(self.x()), float(self.y()))
            self._clamp_into_work_area()
            self._body_state()
            return False
        self.body.throw(vx, vy, power=ph.get("power", 1.0))
        return True

    def _step_physics(self, dt):
        """甩抛中的一帧物理（重力/反弹/摩擦/跨屏）。返回是否刚落地。"""
        ph = self._physics_cfg()
        rects = self._screen_rects()
        info = step_body(self.body, dt, self.width(), self.height(), rects, ph)
        self._apply_body()
        landed = False
        if info["moved"] or info["landed"]:
            # 高速时朝向跟随水平速度
            if abs(self.body.vx) > 120:
                self._set_dir("left" if self.body.vx < 0 else "right",
                              1 if self.body.vx < 0 else -1, animate=False)
            elif abs(self.body.vy) > 400:
                self._set_dir("down")
        if info["impact"] > PHYS.LAND_SQUASH_SPEED and self._anim_enabled():
            self.wobble.kick(min(1.0, info["impact"] / 2600.0))
        if info["landed"]:
            landed = True
            self.body.last_impact = 0.0
        return landed

    def _resolve_pet_collisions(self):
        """宠物互撞（仅多开且开关打开时生效）。"""
        ph = self._physics_cfg()
        if not ph.get("pet_collision", False):
            return
        others = [w for w in self.app.pets if w is not self and w.pet.get("display", True)]
        if not others:
            return
        for other in others:
            if abs(other.x() - self.x()) > self.width() or abs(other.y() - self.y()) > self.height():
                continue
            a = {"x": self.body.x, "y": self.body.y, "vx": self.body.vx,
                 "vy": self.body.vy, "asleep": self.body.asleep,
                 "_idle": getattr(self.body, "_idle", 0)}
            ob = getattr(other, "body", None)
            if ob is None:
                continue
            b = {"x": ob.x, "y": ob.y, "vx": ob.vx, "vy": ob.vy,
                 "asleep": ob.asleep, "_idle": getattr(ob, "_idle", 0)}
            if PHYS.resolve_pair(a, b, self.width(), self.height(),
                                 elasticity=ph.get("elasticity", 0.6)):
                self.body.x, self.body.y = a["x"], a["y"]
                self.body.vx, self.body.vy = a["vx"], a["vy"]
                self.body.asleep = a.get("asleep", False)
                ob.x, ob.y = b["x"], b["y"]
                ob.vx, ob.vy = b["vx"], b["vy"]
                ob.asleep = b.get("asleep", False)
                self._apply_body()
                other._apply_body()
                if self._anim_enabled():
                    self.wobble.kick(0.5)
                    other.wobble.kick(0.5)
                log("[物理] 宠物互撞 %s <-> %s" % (self.pet_id, other.pet_id))

# ---------- 帧序列动画 ----------
    def _frame_anim_on(self):
        """帧动画总开关（settings → 动画）"""
        f = self.app.cfg.get("frame_anim")
        if isinstance(f, dict) and isinstance(f.get("enabled"), bool):
            return f["enabled"]
        return True

    def _frame_spec(self, action):
        """动作的帧配置；没有帧目录返回 None（走代码变形）。"""
        if not action or not self._frame_anim_on():
            return None
        return frame_anim_spec(action)

    def _frame_facing(self, spec):
        """帧素材朝向：front 用正面图、side 用侧面、back 用背面。"""
        face = (spec or {}).get("facing") or "front"
        if face not in VALID_FACING:
            face = "front"
        return face

    def _frame_facing_matches(self, spec):
        """当前朝向是否和帧素材朝向一致（不一致时退回三视图）。"""
        want = self._frame_facing(spec)
        cur = {"down": "front", "left": "side", "right": "side", "up": "back"}[self.dir]
        if self.mode == "still":
            cur = "front"
        return want == cur

    def _frame_list(self, action):
        """取（并按需加载）该动作的帧；失败返回 None 并写 error.log。"""
        spec = self._frame_spec(action)
        if spec is None:
            return None
        frames, err = _FRAME_CACHE.get(action, spec, self.cur_h)
        if err:
            log("[帧动画] %s 加载失败，回退代码变形：%s" % (action, err), logging.WARNING)
            return None
        return frames

    def _frame_alpha_bounds(self, pm):
        """返回 (bottom, top) 不透明内容的最下/最上像素 y；失败返回 (0, 0)。

        注意：量化过的 PNG 是索引色（P 模式），QImage 直接 pixel() 取不到 alpha，
        必须先转成 ARGB32 再读。
        """
        try:
            img = pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)
            w, h = img.width(), img.height()
            if h <= 0 or w <= 0:
                return 0, 0
            bottom = 0
            top = h
            for y in range(h):
                row_has = False
                for x in range(0, w, 2):        # 隔列采样：够用且快
                    if ((img.pixel(x, y) >> 24) & 0xFF) > 12:
                        row_has = True
                        break
                if row_has:
                    bottom = y
                    if top == h:
                        top = y
            return bottom, top
        except Exception:
            return 0, 0


    def _camera_ref(self, action, frames):
        """该动作的「脚底线基准」：首帧不透明内容的最下 y。

        有些素材（如「开心跃动」）是镜头跟着跳，脚底在画面里上下移动 40+ px。
        以首帧脚底为基准，后续帧按差值平移回去，宠物始终踩在同一条线上。
        """
        key = (action, self.cur_h)
        cached = self._cam_ref_cache.get(key)
        if cached is not None:
            return cached
        ref = None
        try:
            pm = frames[0] if frames else None
            if pm is not None and not pm.isNull():
                bottom, _top = self._frame_alpha_bounds(pm)
                ref = float(bottom)
        except Exception:
            ref = None
        self._cam_ref_cache[key] = ref
        return ref

    def _camera_offset(self, action, frames):
        """当前帧脚底相对基准的像素偏移（正 = 需要往下挪）。"""
        if not getattr(self, "_camera_fix", True):
            return 0.0
        try:
            ref = self._camera_ref(action, frames)
            if ref is None:
                return 0.0
            pm = self._current_frame_pix()
            if pm is None or pm.isNull():
                return 0.0
            bottom, _top = self._frame_alpha_bounds(pm)
            off = ref - bottom          # 当前脚底比基准高多少，就往下挪多少
            return max(-80.0, min(80.0, off))
        except Exception:
            return 0.0

    def _camera_scale(self, action, frames):
        """保留接口：镜头补偿现在只做平移，缩放恒为 1。"""
        return 1.0

    def _start_frame_anim(self, action):
        """开始播放某动作的帧序列（返回是否成功）。"""
        frames = self._frame_list(action)
        if not frames:
            return False
        spec = action_spec(action) or {}
        fps = spec.get("fps") or FRAME_ANIM_FPS_DEFAULT
        try:
            fps = max(1.0, float(fps))
        except Exception:
            fps = FRAME_ANIM_FPS_DEFAULT
        self._frame_action = action
        self._frame_index = 0
        self._frame_elapsed = 0.0
        self._frame_fps = fps
        self._frame_loop = bool(spec.get("loop", True))
        self._frame_hold = bool(spec.get("hold_last", True))
        return True

    def _stop_frame_anim(self):
        self._frame_action = None
        self._frame_index = 0
        self._frame_elapsed = 0.0

    def _step_frame_anim(self, dt):
        """推进帧动画（由 tick 调用，非阻塞）。"""
        act = self._frame_action
        if act is None:
            return
        if not self._frame_anim_on():
            self._stop_frame_anim()
            return
        frames = self._frame_list(act)
        if not frames:
            self._stop_frame_anim()
            return
        n = len(frames)
        self._frame_elapsed += dt * self._frame_fps
        while self._frame_elapsed >= 1.0:
            self._frame_elapsed -= 1.0
            if self._frame_index + 1 < n:
                self._frame_index += 1
            else:
                if self._frame_loop:
                    self._frame_index = 0
                elif self._turn_frame_action is not None:
                    # 转向帧播完：无缝切到目标朝向的静态图
                    self._finish_turn_frame()
                    return
                elif self._frame_hold:
                    self._frame_index = n - 1
                    break
                else:
                    self._stop_frame_anim()
                    return

    def _current_frame_pix(self):
        """当前帧（没有帧动画返回 None）。"""
        act = self._frame_action
        if act is None:
            return None
        frames = self._frame_list(act)
        if not frames:
            return None
        idx = max(0, min(len(frames) - 1, self._frame_index))
        return frames[idx]

    def _frame_anim_active(self):
        """当前是否应该用帧序列绘制（朝向匹配 + 有帧）。"""
        act = self._frame_action
        if act is None:
            return False
        spec = self._frame_spec(act)
        if spec is None:
            return False
        if not self._frame_facing_matches(spec):
            return False
        return self._current_frame_pix() is not None

    # ---------- 绘制 ----------
    def _temp_progress(self):
        total = max(0.001, getattr(self, "action_total", 1.0))
        return max(0.0, min(1.0, 1.0 - self.action_t / total))

    def _squash_amount(self):
        """Q 弹挤压：结束时压扁再回弹；落地/碰撞用物理形变弹簧"""
        if not self._anim_enabled():
            return 0.0, 0.0
        sx = sy = 0.0
        if self.squash_t > 0:
            k = self.squash_t / SQUASH_UNITS
            wave = math.sin(k * math.pi * SQUASH_CURVE["freq"]) * k
            sx += SQUASH_CURVE["amp_x"] * wave
            sy += SQUASH_CURVE["amp_y"] * wave
        # 物理形变（甩抛落地/互撞）：正值压扁，负值拉长
        w = getattr(self.wobble, "value", 0.0)
        if w:
            sx -= w * SQUASH_CURVE["amp_x"]
            sy += w * SQUASH_CURVE["amp_y"]
        return sx, sy

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        now = self.t * TICK / 1000.0

        if self.bubble_text and now < self.bubble_until:
            if self.bubble_inner:
                bfont = QFont(self.bubble_font)
                bfont.setItalic(True)
                bg, fg = QColor(232, 232, 238, 235), QColor(125, 125, 138)
            else:
                bfont = QFont(self.bubble_font)
                bg, fg = QColor(255, 255, 255, 235), QColor(60, 60, 80)
            fm = QFontMetrics(bfont)
            max_w = min(240, self.width() - 16)
            words = self.bubble_text
            lines = []
            cur = ""
            for ch in words:
                if fm.horizontalAdvance(cur + ch) > max_w - 20:
                    lines.append(cur)
                    cur = ch
                else:
                    cur += ch
            lines.append(cur)
            bw = max(fm.horizontalAdvance(l) for l in lines) + 20
            bh = len(lines) * fm.height() + 14
            bx = (self.width() - bw) / 2
            by = 6.0
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(bx, by, bw, bh), 10, 10)
            tail = QPointF(self.width() / 2, by + bh)
            p.drawPolygon(QPolygonF([tail, QPointF(tail.x() - 6, tail.y() + 8), QPointF(tail.x() + 6, tail.y() + 8)]))
            p.setPen(fg)
            p.setFont(bfont)
            for i, l in enumerate(lines):
                p.drawText(QRectF(bx, by + 7 + i * fm.height(), bw, fm.height()),
                           Qt.AlignmentFlag.AlignCenter, l)

        cx = self.width() / 2
        moving = self.target is not None and not self.dragging
        if moving:
            sway = math.sin(now * 9.0) * 3.5
            bob = -abs(math.sin(now * 4.5)) * 7.0
        else:
            sway = math.sin(now * 2.5) * 1.5
            bob = 0.0
        breath = 1.0 + ACTION_STYLE["breath_amp"] * self._action_strength() * math.sin(now * 2.5)
        jump = -abs(math.sin(self.jump_t * 3.14159)) * 14 * self.jump_t if self.jump_t > 0 else 0

        act_rot = act_sx = act_sy = act_dx = act_dy = 0.0
        temp = self.action if self.action_t > 0 else None
        if temp is None and self.chain_action is not None:
            temp = self.chain_action
        prog = self._temp_progress() if self.action_t > 0 else (
            max(0.0, min(1.0, self.chain_elapsed / max(0.001, self.chain_dur)))
            if self.chain_action else 0.0)

        # 代码变形幅度：ACTION_STYLE × 强度（默认 1.0，已整体放大约 2 倍）
        AS = ACTION_STYLE
        ak = self._action_strength()
        if temp in ("sway",):
            act_rot = math.sin(prog * math.pi * 2) * AS["sway_deg"] * ak * (1 - prog)
        elif temp == "stretch":
            w = math.sin(prog * math.pi)
            act_sy = AS["stretch_sy"] * ak * w
            act_sx = AS["stretch_sx"] * ak * w
            act_rot = -1.6 * ak * w
        elif temp == "pat":
            act_rot = math.sin(prog * math.pi * 2) * AS["pat_rot"] * ak * (1 - prog)
            act_sy = AS["pat_sy"] * ak * abs(math.sin(prog * math.pi * 2)) * (1 - prog)
        elif temp == "poke":
            act_sx = AS["poke_sx"] * ak * math.sin(prog * math.pi)
            act_dx = math.sin(prog * math.pi * 9) * AS["poke_dx"] * ak * (1 - prog)
        elif temp == "stroke":
            act_rot = math.sin(prog * math.pi * 3) * AS["stroke_rot"] * ak * (1 - prog)
            act_dx = math.sin(prog * math.pi * 3) * AS["stroke_dx"] * ak * (1 - prog)
        elif temp == "idle_breath":
            act_sy = AS["breath_amp"] * ak * math.sin(prog * math.pi * 2)
        elif temp == "idle_stretch":
            w = math.sin(prog * math.pi)
            act_sy = AS["stretch_sy"] * ak * w
            act_sx = AS["stretch_sx"] * ak * w
            act_rot = -1.6 * ak * w
        elif temp == "idle_yawn":
            # 打哈欠：小幅拉伸 + 微后仰 + 轻微下沉
            w = math.sin(prog * math.pi)
            act_sy = AS["yawn_sy"] * ak * w
            act_sx = AS["stretch_sx"] * 0.5 * ak * w
            act_rot = AS["yawn_rot"] * ak * w
            act_dy = 2.0 * ak * w
        elif temp == "idle_rubeye":
            # 揉眼睛：小幅左右摆 + 快速抖动（收敛）
            act_dx = math.sin(prog * math.pi * 10) * 2.6 * ak * (1 - prog)
            act_rot = math.sin(prog * math.pi * 5) * 3.5 * ak * (1 - prog)
        elif temp in ("idle_sit", "idle_lie"):
            # 坐下 / 趴着：压低 + 横向变宽（重心下沉）。
            # 必须用完整的 0→1→0 曲线，否则动作结束时回不到常态，
            # 看起来会像"越来越胖"（旧版 sin(prog*π*0.5) 全程单调）。
            key = "sit" if temp == "idle_sit" else "lie"
            w = math.sin(prog * math.pi)      # 0 → 1 → 0
            w = w ** 0.75                     # 稍早到达峰值，停留更久
            act_sy = AS[key + "_sy"] * ak * w
            act_sx = AS[key + "_sx"] * ak * w
        elif temp == "idle_sleep":
            # 睡觉：持续压低 + 明显的慢呼吸起伏（睡着也要看得出在呼吸）
            act_sy = (AS["sleep_sy"] * ak
                      + 0.028 * ak * math.sin(prog * math.pi * 4))
            act_sx = AS["sleep_sx"] * ak + 0.012 * ak * math.sin(prog * math.pi * 4)
        elif temp in TURN_DIRS:
            act_rot = math.sin(prog * math.pi) * AS["turn_rot"] * ak
        elif temp in ("walk", "hop"):
            act_dy = -abs(math.sin(prog * math.pi * 2)) * AS["walk_dy"] * ak
        elif temp == "happy_jump":
            act_dy = -abs(math.sin(prog * math.pi * 2)) * AS["jump_dy"] * ak
            act_sy = -0.10 * ak * math.sin(prog * math.pi)
        elif temp == "shy":
            act_dx = -4 * math.sin(prog * math.pi)
            act_sx = -0.05 * math.sin(prog * math.pi)
            act_rot = 4 * math.sin(prog * math.pi)
        elif temp == "tsundere":
            act_dx = math.sin(prog * math.pi * 6) * 5 * (1 - prog)
            act_rot = math.sin(prog * math.pi * 3) * 4 * (1 - prog)
        elif temp == "tickle":
            act_dx = math.sin(prog * math.pi * 14) * 6 * (1 - prog)
            act_rot = math.sin(prog * math.pi * 7) * 3 * (1 - prog)
            act_sy = 0.03 * abs(math.sin(prog * math.pi * 4))
        elif temp == "wave":
            act_dx = math.sin(prog * math.pi) * 5
            act_rot = math.sin(prog * math.pi * 4) * 7 * (1 - prog)
        elif temp == "remind":
            # 提醒：跳两下（abs(sin*2pi*2) 两段）+ 轻微抖动
            act_dy = -abs(math.sin(prog * math.pi * 2)) * AS["jump_dy"] * ak
            act_dx = math.sin(prog * math.pi * 12) * 3.2 * ak * (1 - prog)
            act_rot = math.sin(prog * math.pi * 6) * 2.5 * ak * (1 - prog)

        sq_x, sq_y = self._squash_amount()
        scale = breath
        # 转身：横向压扁 + 轻微下蹲 + 结束小跳
        t_sx, t_sy, t_hop = self._turn_scale()
        sx = (1 + act_sx - sq_x) * t_sx
        sy = (1 + act_sy + sq_y) * t_sy
        if t_hop:
            act_dy += t_hop

        # 转身明暗系数（1=正常，越小越暗）
        shade_amt = self._turn_shade()

        def draw_pix(pix, facing, opacity):
            """按脚底线绘制任意 pixmap（帧动画与三视图共用同一套对齐/镜像）。"""
            if pix is None or pix.isNull():
                return
            ph = pix.height() * scale * sy
            pw = pix.width() * scale * sx
            feet = BUBBLE_H + MARGIN + self.cur_h
            dx = cx - pw / 2 + act_dx
            dy = feet - ph + jump + bob + act_dy
            p.save()
            p.setOpacity(opacity)
            p.translate(cx, feet)
            p.rotate(sway + act_rot)
            p.translate(-cx, -feet)
            if facing < 0:
                p.translate(cx, 0)
                p.scale(-1, 1)
                p.translate(-cx, 0)
            p.drawPixmap(QRectF(dx, dy, pw, ph), pix,
                         QRectF(0, 0, pix.width(), pix.height()))
            # 转身最窄处叠一层暗色（模拟侧面受光），让朝向切换更不明显
            shade = shade_amt
            if shade < 0.999:
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
                p.fillRect(QRectF(dx, dy, pw, ph),
                           QColor(20, 24, 40, int(255 * (1.0 - shade))))
            p.restore()

        def draw_one(key, opacity, dir_key=None):
            if key is None:
                return
            name, h, facing = key
            pix = self.sprites.get((name, h))
            if pix is None or pix.isNull():
                return
            ph = pix.height() * scale * sy
            pw = pix.width() * scale * sx
            # 脚底线为基准：无论缩放/镜像，脚底都贴着同一条线
            feet = BUBBLE_H + MARGIN + self.cur_h
            dx = cx - pw / 2 + act_dx
            dy = feet - ph + jump + bob + act_dy
            p.save()
            p.setOpacity(opacity)
            p.translate(cx, feet)
            p.rotate(sway + act_rot)
            p.translate(-cx, -feet)
            if facing < 0:
                # 以中轴镜像，脚底坐标不变
                p.translate(cx, 0)
                p.scale(-1, 1)
                p.translate(-cx, 0)
            p.drawPixmap(QRectF(dx, dy, pw, ph), pix, QRectF(0, 0, pix.width(), pix.height()))
            shade = shade_amt
            if shade < 0.999:
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
                p.fillRect(QRectF(dx, dy, pw, ph),
                           QColor(20, 24, 40, int(255 * (1.0 - shade))))
            p.restore()

        # 时节特效画在宠物之上（矩形原点相对窗口，translate 负 margin 对齐）
        fx = self._fx
        if fx is not None and not fx.dead:
            p.save()
            p.translate(fx.rect[0], fx.rect[1])
            fx.paint(p)
            p.restore()

        cur_key = self._sprite_key()
        # 朝向/动作切换：150ms 交叉淡入（减少动效时直接切）
        fade = self.cross_t if self.cross_t > 0 else 0.0
        action_fade = self._action_fade if self._action_fade > 0 else 0.0
        if not self._anim_enabled():
            fade = 0.0
            action_fade = 0.0

        # 帧序列动画优先；没有帧（或朝向不匹配）才回到三视图 + 代码变形
        frame_pix = self._current_frame_pix() if self._frame_anim_active() else None
        if frame_pix is not None:
            spec = self._frame_spec(self._frame_action) or {}
            face = self._frame_facing(spec)
            mirror = -1 if (face == "side" and self.facing < 0) else 1
            if action_fade > 0:
                # 切动作时与上一张图做一次交叉淡入，避免硬切
                if self.prev_action_key is not None:
                    draw_one(self.prev_action_key, action_fade * 0.55)
                draw_pix(frame_pix, mirror, 1.0 - action_fade * 0.55)
            else:
                draw_pix(frame_pix, mirror, 1.0)
        elif fade > 0 and self.prev_key is not None:
            draw_one(self.prev_key, fade)
            draw_one(cur_key, 1.0 - fade)
        elif action_fade > 0 and self.prev_action_key is not None and self.prev_action_key != cur_key:
            draw_one(self.prev_action_key, action_fade * 0.55)
            draw_one(cur_key, 1.0 - action_fade * 0.55)
        else:
            draw_one(cur_key, 1.0)

        # 提醒气泡画在最上层（不受宠物缩放影响）
        try:
            self._paint_remind_bubble(p)
        except Exception as _ex:
            log("提醒气泡绘制失败: %s" % _ex, logging.DEBUG)

    def _sprite_key(self):
        # 「原地待着」只用正面：不显示侧面/背面，也不镜像
        if self.mode == "still":
            return ("正面", self.cur_h, 1)
        name = {"left": "侧面", "right": "侧面", "up": "背面", "down": "正面"}[self.dir]
        return (name, self.cur_h, self.facing if self.dir in ("left", "right") else 1)

# ---------- 转向 ----------
    def _turn_strength(self):
        v = self.app.cfg.get("turn_strength")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return max(0.0, min(1.0, float(v)))
        return TURN_STRENGTH_DEFAULT

    def _action_strength(self):
        v = self.app.cfg.get("action_strength")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return max(0.0, min(1.0, float(v)))
        return ACTION_STRENGTH_DEFAULT

    def _turn_progress(self):
        """0→1 的转身进度；不在转身中返回 None。"""
        if self._turn_t <= 0:
            return None
        total = max(0.001, self._turn_total)
        return max(0.0, min(1.0, 1.0 - self._turn_t / total))

    def _turn_phase(self, prog):
        """把转身进度映射成「压扁阶段」的 0→1→0 三角波。

        中间留一段极窄的停留（edge_hold），让朝向切换发生在最窄处、最不可见。
        """
        st = TURN_STYLE
        hold = max(0.0, min(0.4, st["edge_hold"]))
        first = 0.5 - hold / 2.0
        last = 0.5 + hold / 2.0
        if prog <= first:
            return prog / max(1e-6, first)
        if prog < last:
            return 1.0
        return max(0.0, (1.0 - prog) / max(1e-6, 1.0 - last))

    def _turn_scale(self):
        """转身时的横向压缩 / 纵向体积补偿 / 下蹲 / 小跳。

        返回 (sx, sy, hop)。压得越扁，纵向略微拉长（体积守恒感），
        比纯 scale(sx,1) 更像 3D 翻转而不是被压路机压扁。
        """
        prog = self._turn_progress()
        if prog is None:
            return 1.0, 1.0, 0.0
        if not self._anim_enabled():
            return 1.0, 1.0, 0.0
        st = TURN_STYLE
        k = self._turn_strength()
        phase = self._turn_phase(prog)          # 0 → 1 → 0
        narrow = phase ** 0.85                  # 缓动：起步快、到底缓
        # 横向：1 → squash_x → 1
        sx = 1.0 - (1.0 - st["squash_x"]) * k * narrow
        # 纵向体积补偿：只在「真正被压扁」时才轻微拉长（narrow 越大越扁）
        # 起点 narrow=0 时不做补偿，保证第一帧和常态一致
        stretch = narrow * st["volume_keep"] * k
        sy = 1.0 + stretch - st["crouch"] * k * narrow
        # 展开阶段的过冲回弹（只在后半段、且幅度很小）
        if prog > 0.5:
            t = (prog - 0.5) / 0.5
            over = math.sin(t * math.pi) * st["overshoot"] * k
            sx += over * (1.0 - t)
            sy += over * 0.35 * (1.0 - t)
        # 宽度补偿：切换后立刻按比例修正，随展开**平滑**淡出到 1.0
        # 用 (1-t)^2 让补偿在刚切换时保持足量，避免"先缩回去再长"的抖动
        if prog > 0.5 and getattr(self, "_turn_w_comp", 1.0) != 1.0:
            t = (prog - 0.5) / 0.5
            comp = 1.0 + (self._turn_w_comp - 1.0) * ((1.0 - t) ** 2)
            sx *= comp
        # 结束小跳
        hop = 0.0
        if prog > 0.62:
            t = (prog - 0.62) / 0.38
            hop = -abs(math.sin(t * math.pi)) * st["hop"] * k
        return sx, sy, hop

    def _turn_shade(self):
        """转身最窄处的明暗系数（0~1，1=不变暗），模拟侧面受光。"""
        prog = self._turn_progress()
        if prog is None or not self._anim_enabled():
            return 1.0
        st = TURN_STYLE
        narrow = self._turn_phase(prog) ** 0.85
        return 1.0 - st["shade"] * self._turn_strength() * narrow

    def _sprite_content_w(self, key):
        """某张精灵图的不透明内容宽度（用于转身时的宽度补偿）。"""
        try:
            name, h, _facing = key
            pix = self.sprites.get((name, h))
            if pix is None or pix.isNull():
                return 0
            return pix.width()
        except Exception:
            return 0

    def _turn_mid_swap(self):
        """在最窄处切换朝向图（只切一次）。

        正/侧面内容宽度不同（178 vs 156），直接切会看到一下"缩了一下"。
        这里记下宽度比，在绘制时乘一个补偿系数，让切换瞬间总宽度连续。
        """
        if self._turn_swapped:
            return
        self._turn_swapped = True
        before_key = self._sprite_key()
        self.prev_key = before_key
        self.dir = self._turn_target_dir
        if self._turn_target_facing is not None:
            self.facing = self._turn_target_facing
        # 切换瞬间给一个极短的交叉淡化（正常 150ms 的一半），
        # 让两套图在最窄处叠化，而不是硬切（硬切时单帧差异会到 0.62）
        self.cross_t = 0.55 if self._anim_enabled() else 0.0
        # 宽度补偿：切换后按 旧宽/新宽 放大，抵消两套图的宽度差
        w_old = self._sprite_content_w(before_key)
        w_new = self._sprite_content_w(self._sprite_key())
        if w_old > 0 and w_new > 0 and w_new != w_old:
            self._turn_w_comp = max(0.75, min(1.35, float(w_old) / float(w_new)))
        else:
            self._turn_w_comp = 1.0

    def _start_turn(self, target_dir, target_facing=None, line_chance=None):
        """开始一次 350ms 转身过程（代码转向）。"""
        if target_dir == self.dir and (target_facing is None or target_facing == self.facing):
            return False
        st = TURN_STYLE
        self._turn_target_dir = target_dir
        self._turn_target_facing = target_facing
        self._turn_total = max(0.12, st["duration_ms"] / 1000.0)
        self._turn_t = self._turn_total
        self._turn_swapped = False
        # 30% 概率冒一句，且不与上一句重复
        chance = st["line_chance"] if line_chance is None else line_chance
        if random.random() < chance:
            choices = [l for l in st["lines"] if l != self.last_line] or st["lines"]
            self.say(random.choice(choices))
        log("[转向] %s → %s" % (self.dir, target_dir))
        return True

    def _step_turn(self, dt):
        """推进转身过程；过半时切换朝向图。"""
        if self._turn_t <= 0:
            return
        self._turn_t = max(0.0, self._turn_t - dt)
        prog = 1.0 - self._turn_t / max(0.001, self._turn_total)
        if prog >= 0.5:
            self._turn_mid_swap()
        if self._turn_t == 0:
            self._turn_swapped = False
            self._turn_w_comp = 1.0

    def _turn_interrupt(self):
        """被打断（拖拽/点击/甩抛）：立即切到目标朝向，不等动画。"""
        if self._turn_t <= 0:
            return
        self._turn_mid_swap()
        self._turn_t = 0.0
        self._turn_swapped = False
        if self._turn_frame_action is not None:
            self._stop_turn_frame_anim()
            self._stop_frame_anim()

    # ---------- 转向帧动画 ----------
    @staticmethod
    def _facing_of_dir(d, mode=None):
        if mode == "still":
            return "front"
        return {"down": "front", "left": "side", "right": "side", "up": "back"}[d]

    def _find_turn_frame_action(self, from_face, to_face, reverse_wanted):
        """按命名约定找匹配的转向帧动作：turn_<from>_<to>，支持 reverse。"""
        a = "turn_%s_%s" % (from_face, to_face)
        b = "turn_%s_%s" % (to_face, from_face)
        for name in (a, b):
            spec = ACTIONS.get(name)
            if not spec:
                continue
            if not frame_anim_spec(name):
                continue
            spec_rev = bool(spec.get("reverse", False))
            # 方向相反时要求 reverse 恰好互补
            if name == a and spec_rev == reverse_wanted:
                return name, False
            if name == b and spec_rev != reverse_wanted:
                return name, True
        return None, False

    def _start_turn_frame_anim(self, name, mirror):
        """播放转向帧动画；成功返回 True。"""
        frames = self._frame_list(name)
        if not frames:
            return False
        spec = ACTIONS.get(name) or {}
        try:
            fps = max(1.0, float(spec.get("fps") or FRAME_ANIM_FPS_DEFAULT))
        except Exception:
            fps = FRAME_ANIM_FPS_DEFAULT
        self._turn_frame_action = name
        self._turn_frame_mirror = bool(mirror)
        self._frame_action = name
        self._frame_index = 0
        self._frame_elapsed = 0.0
        self._frame_fps = fps
        self._frame_loop = False        # 转向动画只播一次
        self._frame_hold = True
        return True

    def _stop_turn_frame_anim(self):
        self._turn_frame_action = None
        self._turn_frame_mirror = False

    def _turn_frame_active(self):
        return self._turn_frame_action is not None

    def _finish_turn_frame(self):
        """转向帧播完：切到目标朝向并停止帧动画（无缝接静态图）。"""
        self._stop_turn_frame_anim()
        self._stop_frame_anim()
        if self._turn_target_dir and self._turn_target_dir != self.dir:
            self.prev_key = self._sprite_key()
            self.dir = self._turn_target_dir
            if self._turn_target_facing is not None:
                self.facing = self._turn_target_facing
            self.cross_t = 0.0
        self._turn_target_dir = None

    def _set_dir(self, d, facing=None, animate=True):
        """改朝向。animate=True 时走 350ms 转身过程（有转向帧则播帧）。

        物理/移动等需要立刻生效的场景传 animate=False。
        """
        if d == self.dir and (facing is None or facing == self.facing):
            return
        if not animate:
            self._turn_interrupt()
            if d != self.dir:
                self.prev_key = self._sprite_key()
                self.cross_t = 1.0 if self._anim_enabled() else 0.0
                self.dir = d
            if facing is not None:
                self.facing = facing
            return
        # 先尝试转向帧动画
        from_face = self._facing_of_dir(self.dir, self.mode)
        to_face = self._facing_of_dir(d, self.mode)
        reverse_wanted = (d in ("left",) or (self.dir == "right" and d == "left"))
        if from_face != to_face:
            name, mirror = self._find_turn_frame_action(from_face, to_face, reverse_wanted)
            if name and self._start_turn_frame_anim(name, mirror):
                # 帧动画负责整段过渡；朝向在播完后切换
                self._turn_target_dir = d
                self._turn_target_facing = facing
                self._turn_total = 0.0
                self._turn_t = 0.0
                self._turn_swapped = False
                return
        # 回退代码转身
        self._start_turn(d, facing)

    # ---------- 动画链 ----------
    def _category_weights(self):
        raw = self.app.cfg.get("animation_weights")
        out = dict(ACTION_CATEGORY_WEIGHTS)
        if isinstance(raw, dict):
            for k in out:
                v = raw.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                    out[k] = float(v)
        return out

# ---------- 待机调度（tier / 冷却 / 时段 / 久无互动） ----------
    def _tier_weights(self):
        raw = self.app.cfg.get("animation_tiers")
        out = dict(ANIMATION_TIERS_DEFAULT)
        if isinstance(raw, dict):
            for k in out:
                v = raw.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                    out[k] = float(v)
        total = sum(out.values())
        if total <= 0:
            out = dict(ANIMATION_TIERS_DEFAULT)
            total = sum(out.values())
        return {k: v / total for k, v in out.items()}

    def _now_s(self):
        return self.t * TICK / 1000.0

    def _tier_of(self, name):
        spec = ACTIONS.get(name) or {}
        tier = spec.get("tier") or "small"
        return tier if tier in VALID_TIERS else "small"

    def _cooldown_of(self, name):
        spec = ACTIONS.get(name) or {}
        v = spec.get("cooldown")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return max(0.0, float(v))
        return TIER_COOLDOWN.get(self._tier_of(name), 0.0)

    def _action_ready(self, name, now):
        """冷却是否已过（同动作冷却 + tier 全局间隔）。"""
        cd = self._cooldown_of(name)
        if cd > 0:
            last = self._action_last.get(name)
            if last is not None and now - last < cd:
                return False
        tier = self._tier_of(name)
        gap = TIER_GLOBAL_GAP.get(tier, 0.0)
        if gap > 0:
            last_t = self._tier_last.get(tier)
            if last_t is not None and now - last_t < gap:
                return False
        return True

    def _time_weight(self, name):
        """时段只提高权重，不强制。"""
        spec = ACTIONS.get(name) or {}
        tod = spec.get("time_of_day") or "any"
        if tod == "any":
            return 1.0
        return 2.2 if time_matches(tod) else 1.0

    def _idle_weight(self, name):
        """久无互动时提高犯困/坐下/睡觉类权重。"""
        stage = self._idle_stage
        if stage == "normal":
            return 1.0
        if name in IDLE_SLEEP_ACTIONS:
            return 4.0 if stage in ("sit", "sleep") else 1.0
        if name in IDLE_SIT_ACTIONS:
            return 3.0 if stage in ("sit", "sleep") else 1.0
        if name in IDLE_TIRED_ACTIONS:
            return IDLE_BORED_BOOST if stage in ("bored", "sit", "sleep") else 1.0
        return 1.0

    def _idle_seconds(self):
        return max(0.0, time.time() - float(self.last_interact or 0))

    def _update_idle_stage(self):
        """按久无互动时长升级状态：normal → bored → sit → sleep。"""
        sec = self._idle_seconds()
        if sec >= IDLE_SLEEP_AFTER:
            stage = "sleep"
        elif sec >= IDLE_SIT_AFTER:
            stage = "sit"
        elif sec >= IDLE_BORED_AFTER:
            stage = "bored"
        else:
            stage = "normal"
        if stage != self._idle_stage:
            self._idle_stage = stage
            log("[待机] 状态 → %s（已静默 %.0f 秒）" % (stage, sec))
            if stage == "sleep":
                # 睡觉：停下 + Zzz 气泡
                self.target = None
                self.rest_until = self._now_s() + 9999
                self.say(random.choice(SLEEP_ZZZ_LINES))
            elif stage == "normal":
                self.rest_until = 0

    def mark_interact(self):
        """记录一次互动（点击/拖拽/聊天/喂食），会唤醒睡觉。"""
        self.last_interact = time.time()
        if self._idle_stage != "normal":
            self._idle_stage = "normal"
            self.rest_until = 0
            log("[待机] 互动唤醒 → normal")

    def _pick_tier(self):
        w = self._tier_weights()
        tiers = [t for t in VALID_TIERS if w.get(t, 0) > 0]
        if not tiers:
            return "small"
        return random.choices(tiers, weights=[w[t] for t in tiers], k=1)[0]

    def _pick_next_action(self):
        """先按 tier 概率选层，再按权重选动作；排除冷却中与不连续重复。"""
        cats = self._category_weights()
        now = self._now_s()
        still = (self.mode == "still")

        def pool_for(tier):
            pool, weights = [], []
            for name, spec in ACTIONS.items():
                if name == self.chain_action:
                    continue
                if still and spec.get("category") == "turn":
                    continue
                if self._tier_of(name) != tier:
                    continue
                cw = cats.get(spec["category"], 0)
                if cw <= 0:
                    continue
                if not self._action_ready(name, now):
                    continue
                w = cw * float(spec.get("weight", 1))
                # 有帧动画的动作优先：代码变形版本权重打折
                if action_has_frames(name):
                    w *= FRAME_CODE_WEIGHT
                w *= self._time_weight(name) * self._idle_weight(name)
                pool.append(name)
                weights.append(w)
            return pool, weights

        # 按 tier 概率顺序尝试，空层就换下一个
        order = sorted(VALID_TIERS, key=lambda t: -self._tier_weights().get(t, 0))
        for _ in range(3):
            tier = self._pick_tier()
            pool, weights = pool_for(tier)
            if pool:
                return random.choices(pool, weights=weights, k=1)[0]
            order = [t for t in order if t != tier]
            if not order:
                break
        # 兜底：任意可用动作
        for tier in VALID_TIERS:
            pool, weights = pool_for(tier)
            if pool:
                return random.choices(pool, weights=weights, k=1)[0]
        return "idle_breath"

    def _record_action(self, name):
        now = self._now_s()
        self._action_last[name] = now
        self._tier_last[self._tier_of(name)] = now
        self._action_history.append((round(time.time(), 2), name))
        if len(self._action_history) > 20:
            self._action_history = self._action_history[-20:]

    # ---------- 连招 COMBOS ----------
    def _pick_combo(self):
        """按 tier 概率挑一个连招（large 动作整体播放）。"""
        if not COMBOS:
            return None
        now = self._now_s()
        avail = []
        for name, spec in COMBOS.items():
            tier = spec.get("tier", "large")
            gap = TIER_GLOBAL_GAP.get(tier, 0.0)
            last = self._tier_last.get(tier)
            if gap > 0 and last is not None and now - last < gap:
                continue
            avail.append((name, spec))
        if not avail:
            return None
        if random.random() > self._tier_weights().get("large", 0.05):
            return None
        name, spec = random.choice(avail)
        return name

    def _start_combo(self, name):
        spec = COMBOS.get(name)
        if not spec:
            return False
        steps = spec.get("steps") or []
        if not steps:
            return False
        self._combo = name
        self._combo_step = 0
        self._combo_t = 0.0
        durs = spec.get("duration") or []
        self._combo_durs = [float(durs[i]) if i < len(durs) else 1.2
                            for i in range(len(steps))]
        self._play_combo_step()
        log("[待机] 连招开始：%s（%s）" % (name, spec.get("label", "")))
        return True

    def _play_combo_step(self):
        steps = (COMBOS.get(self._combo) or {}).get("steps") or []
        if self._combo_step >= len(steps):
            self._combo = None
            return
        name = steps[self._combo_step]
        dur = self._combo_durs[self._combo_step]
        # 有素材就播素材，没有就用同名代码动作代替
        if name in ACTIONS:
            self.chain_action = name
            self.chain_dur = dur
            self.chain_elapsed = 0.0
            self.prev_action_key = self._sprite_key()
            if not self._start_frame_anim(name):
                self._stop_frame_anim()
        else:
            self._play_temp(name, total=dur)

    def _step_combo(self, dt):
        if self._combo is None:
            return
        self._combo_t += dt
        dur = self._combo_durs[self._combo_step] if self._combo_step < len(self._combo_durs) else 1.2
        if self._combo_t >= dur:
            self._combo_t = 0.0
            self._combo_step += 1
            steps = (COMBOS.get(self._combo) or {}).get("steps") or []
            if self._combo_step >= len(steps):
                log("[待机] 连招结束：%s" % self._combo)
                self._combo = None
            else:
                self._play_combo_step()

    def _interrupt_combo(self):
        """被拖拽/点击打断：立即终止连招。"""
        if self._combo is None:
            return
        log("[待机] 连招被打断：%s" % self._combo)
        self._combo = None
        self._combo_step = 0
        self._combo_t = 0.0

    def _start_chain_action(self, name):
        spec = ACTIONS.get(name, {"duration": 1.0, "moves": False})
        self.prev_action_key = self._sprite_key()
        self.chain_action = name
        self.chain_dur = max(0.1, float(spec.get("duration", 1.0)))
        self.chain_elapsed = 0.0
        self._action_fade = 1.0 if self._anim_enabled() else 0.0
        # 有帧序列就用帧播放，没有则沿用代码变形
        if not self._start_frame_anim(name):
            self._stop_frame_anim()
        # 待机时偶尔冒一句（替代原来 _maybe_idle_action 的说话分支）
        if (name.startswith("idle_") and self.t - self.last_speak_tick >= 1500
                and self.app.is_enabled("idle_talk")):
            if random.random() < 0.06:
                self.last_speak_tick = self.t
                if random.random() < 0.6:
                    self.say(random.choice(INNER_LINES), inner=True)
                else:
                    self.say(random.choice(LINES))
        # 记录调度历史（用于冷却与调试面板）
        self._record_action(name)
        # 转向类动作（若旧配置里还有）仍会改朝向
        if name in TURN_DIRS:
            d, f = TURN_DIRS[name]
            self._set_dir(d, f)
        # 只有「自由散步」才会因为 move 类动作去挑目标点；
        # 「原地待着」和「跟随鼠标」不允许被动画链推着走。
        if spec.get("moves") and self.mode == "wander":
            self._pick_move_target()

    def _play_temp(self, name, total=1.0, squash=False):
        """临时动作（摸头/戳脸/抚摸/单击回应），结束后回到动画链。

        squash=True 时在动作**结束时**做一次 Q 弹挤压（由 tick 触发）。"""
        self.action = name
        self.action_total = max(0.001, float(total))
        self.action_t = self.action_total
        self._squash_pending = bool(squash)
        # 临时动作同样优先用帧序列
        if not self._start_frame_anim(name):
            self._stop_frame_anim()

    def _anim_enabled(self):
        """手动开关优先；auto 时读 Windows「显示动画」设置"""
        mode = self.app.cfg.get("animation_mode", "auto")
        if mode == "on":
            return True
        if mode == "off":
            return False
        return self._system_animations_on()

    def _system_animations_on(self):
        if self._sys_anim_cache is not None:
            return self._sys_anim_cache
        val = 1
        try:
            buf = ctypes.c_int(1)
            ok = ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(buf), 0)
            if ok:
                val = 1 if buf.value else 0
        except Exception:
            val = 1
        self._sys_anim_cache = bool(val)
        return self._sys_anim_cache

    # ---------- 移动探测 ----------
    def _work_area(self):
        """按宠物当前所在显示器算可用工作区"""
        try:
            screen = self.screen() or QApplication.primaryScreen()
            return screen.availableGeometry()
        except Exception:
            return QApplication.primaryScreen().availableGeometry()

    def _probe_distance(self, dx, dy):
        """前方到工作区边界还剩多少像素（正数表示余量）"""
        geo = self._work_area()
        cx = self.x() + self.width() / 2
        cy = self.y() + self.height() / 2
        left_room = cx - self.width() / 2 - geo.left()
        right_room = geo.right() - (cx + self.width() / 2)
        top_room = cy - self.height() / 2 - geo.top()
        bottom_room = geo.bottom() - (cy + self.height() / 2)
        if abs(dx) >= abs(dy):
            return right_room if dx > 0 else left_room
        return bottom_room if dy > 0 else top_room

    def _clamp_into_work_area(self):
        geo = self._work_area()
        x = max(geo.left(), min(geo.right() - self.width(), self.x()))
        y = max(geo.top(), min(geo.bottom() - self.height(), self.y()))
        if x != self.x() or y != self.y():
            self.move(x, y)

    def _steer_or_pick(self, dx, dy):
        """前方空间不够就转向并缩短步长，不走出屏幕。返回 (方向, 是否移动)"""
        margin = 18
        room = self._probe_distance(dx, dy)
        if abs(dx) > abs(dy) * 1.15:
            want = ("left", -1) if dx < 0 else ("right", 1)
        else:
            want = (("up", None) if dy < 0 else ("down", None))
        if room > margin:
            return want, True
        # 空间不足：放弃目标、转向，并缩短步长（避免贴边）
        self.target = None
        self.rest_until = self.t * TICK + random.randint(1500, 4000)
        # 直接切朝向（不走动作链，因为转向不再是可播动作）
        back_dir = {"up": "down", "down": "up",
                    "left": "right", "right": "left"}.get(want[0])
        if back_dir:
            self._set_dir(back_dir, animate=True)
        return None, False

    # ---------- 逻辑 ----------
    def tick(self):
        self.t += 1
        dt = TICK / 1000.0

        # 提醒走动优先：期间不做普通散步/跟随
        if self._step_remind_walk():
            self._step_frame_anim(dt)
            self.update()
            return

        if self.jump_t > 0:
            self.jump_t = max(0.0, self.jump_t - 0.06)
        if self.cross_t > 0:
            self.cross_t = max(0.0, self.cross_t - dt * 1000.0 / ANIM_CROSS_FADE_MS)
        if self.squash_t > 0:
            self.squash_t = max(0.0, self.squash_t - dt * 2.2)
        if self._action_fade > 0:
            self._action_fade = max(0.0, self._action_fade - dt * 1000.0 / ANIM_CROSS_FADE_MS)

        # 临时动作倒计时
        if self.action_t > 0:
            self.action_t = max(0.0, self.action_t - 0.03)
            if self.action_t == 0:
                self.action = None
                # 动作播完时的 Q 弹挤压（减少动效时跳过）
                if self._squash_pending:
                    self._squash_pending = False
                    if self._anim_enabled():
                        self.squash_t = SQUASH_UNITS

        # 转身过程推进（350ms，最窄处换图）
        self._step_turn(dt)

        # 帧序列动画推进（非阻塞，按 fps 切帧）
        self._step_frame_anim(dt)

        # 连招推进 / 久无互动升级
        self._step_combo(dt)
        self._update_idle_stage()

        # 提醒气泡：悬停时冻结剩余时间，移开后继续倒计时
        if self.remind_text:
            now_s = self.t * TICK / 1000.0
            if self.remind_hover:
                # 每 tick 把截止时间整体后移，等效"暂停"（剩余时间不变）
                if self._remind_last_tick is not None:
                    self.remind_until += max(0.0, now_s - self._remind_last_tick)
                self._remind_last_tick = now_s
            else:
                self._remind_last_tick = None
                if now_s >= self.remind_until:
                    self.hide_remind_bubble()
                    # 气泡自然消失 → 走回原位
                    try:
                        self.app._return_pet_after_remind()
                    except Exception:
                        pass

        # 时节特效推进（不受 chat_paused 影响）
        self._step_fx(dt)

        # 物理形变弹簧（落地/互撞的挤压）
        if self._anim_enabled():
            self.wobble.step(dt)
        else:
            self.wobble.reset()

        # 拖拽：阻尼弹簧追随鼠标
        if self._dragging_physics:
            self._step_drag(dt)
            self.update()
            return

        # 甩抛中：重力/反弹/摩擦/跨屏，物理优先于动画链
        if not self.body.asleep:
            self._stop_frame_anim()
            self._step_physics(dt)
            self._resolve_pet_collisions()
            self.update()
            return

        # 提醒气泡显示期间：冻结动画链，让提醒动作完整播完且不进冷却
        if self.remind_text and self._remind_active():
            self.chain_elapsed = 0.0      # 不推进 → 不会切到别的动作
        # 动画链推进：动作播完按权重选下一个（先看要不要放连招）
        elif self._combo is None and self.chain_action is not None:
            self.chain_elapsed += dt
            if self.chain_elapsed >= self.chain_dur:
                combo = self._pick_combo()
                if combo and self._start_combo(combo):
                    self.update()
                    return
                self._start_chain_action(self._pick_next_action())

        if self.app.chat_paused or self.app.game_active:
            self.update()
            return

        if self.dragging:
            self.update()
            return

        if self.mode == "follow":
            cursor = self.cursor().pos()
            screen = QApplication.screenAt(cursor) or self.screen() or QApplication.primaryScreen()
            geo = screen.availableGeometry()
            near = (self.x() - 100 <= cursor.x() <= self.x() + self.width() + 100 and
                    self.y() - 100 <= cursor.y() <= self.y() + self.height() + 100)
            if near:
                self.target = None
            else:
                half_w = self.width() / 2.0
                half_h = self.height() / 2.0
                tx = max(geo.left() + half_w,
                         min(geo.right() - half_w, cursor.x()))
                ty = max(geo.top() + half_h,
                         min(geo.bottom() - half_h, cursor.y() - 90 + half_h))
                self.target = (tx, ty)
        elif self.mode == "wander":
            if self.target is None and ACTIONS.get(self.chain_action or "", {}).get("moves"):
                self._pick_move_target()

        if self.target is not None:
            cx, cy = self.x() + self.width() / 2, self.y() + self.height() / 2
            dx, dy = self.target[0] - cx, self.target[1] - cy
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < 12:
                self.target = None
                self.rest_until = self.t * TICK + random.randint(8000, 18000)
                self._set_dir("down")
            else:
                want, moving = self._steer_or_pick(dx, dy)
                if moving:
                    step = self.cur_speed * TICK / 1000.0
                    room = self._probe_distance(dx, dy)
                    step = max(0.0, min(step, room - 2))
                    nx, cy2 = cx + dx / dist * step, cy + dy / dist * step
                    self.move(int(round(nx - self.width() / 2)), int(round(cy2 - self.height() / 2)))
                    self._clamp_into_work_area()
                    self._set_dir(want[0], want[1], animate=False)
            if random.random() < 0.002 and self.jump_t == 0:
                self.jump_t = 0.5
        target_speed = SPEED if self.target is not None else 0.0
        self.cur_speed += (target_speed - self.cur_speed) * 0.3
        self.update()

    def _pick_move_target(self):
        """在**当前显示器**工作区内随机挑一个目标点。

        注意：target 存的是**窗口中心**坐标，与 tick 里的 cx/cy 口径一致，
        否则窗口会朝目标左上角偏移、最终被顶到屏幕边缘。
        """
        geo = self._work_area()
        half_w = self.width() / 2.0
        half_h = self.height() / 2.0
        min_x = geo.left() + half_w + 24
        max_x = geo.right() - half_w - 24
        min_y = geo.top() + half_h + 24
        max_y = geo.bottom() - half_h - 24
        if max_x <= min_x or max_y <= min_y:
            self.target = None
            return
        self.target = (random.randint(int(min_x), int(max_x)),
                       random.randint(int(min_y), int(max_y)))

    # ---------- 提醒气泡（加大版） ----------
    # ---------- 提醒走动 ----------
    def remind_walk_to(self, tx, ty, on_done=None, return_to=None):
        """平滑走到 (tx, ty)（窗口左上角目标），最长 REMIND_WALK_MAX_MS。

        - 用侧面朝向（水平走），到达后转正面；
        - 距离远时自动加速，保证 2 秒内到达；
        - 减少动效时直接瞬移（仍触发 on_done）。
        """
        fx, fy = self.x(), self.y()
        dx, dy = float(tx) - fx, float(ty) - fy
        dist = (dx * dx + dy * dy) ** 0.5
        if self._anim_enabled() is False or dist < 2:
            self.move(int(tx), int(ty))
            self._clamp_into_work_area()
            try:
                self._set_dir("down")
            except Exception:
                pass
            if callable(on_done):
                on_done()
            return
        # 速度：基础速度，但保证 2 秒内到；距离很远时用上限
        speed = max(REMIND_WALK_SPEED, dist / (REMIND_WALK_MAX_MS / 1000.0))
        speed = min(speed, REMIND_WALK_SPEED_MAX)
        dur = max(0.12, min(REMIND_WALK_MAX_MS / 1000.0, dist / max(1.0, speed)))
        # 面朝行走方向（侧面）
        if abs(dx) >= abs(dy) * 0.6:
            d = "right" if dx > 0 else "left"
        else:
            d = "down" if dy > 0 else "up"
        try:
            self._set_dir(d)
        except Exception:
            pass
        self.remind_walk = {"fx": fx, "fy": fy, "tx": float(tx), "ty": float(ty),
                            "t0": self.t * TICK / 1000.0, "dur": dur,
                            "on_done": on_done, "return_to": return_to}
        self.target = None
        self.dragging = False

    def _step_remind_walk(self):
        """推进提醒走动（在 tick 里优先于普通移动）。"""
        w = self.remind_walk
        if not w:
            return False
        # 用户开始拖拽 → 立即放弃走动，交还给用户
        if self.dragging or self._dragging_physics:
            self.remind_walk = None
            return False
        now = self.t * TICK / 1000.0
        prog = (now - w["t0"]) / max(0.01, w["dur"])
        if prog >= 1.0:
            self.move(int(round(w["tx"])), int(round(w["ty"])))
            self._clamp_into_work_area()
            cb = w.get("on_done")
            self.remind_walk = None
            try:
                self._set_dir("down")       # 到达后转正面
            except Exception:
                pass
            if callable(cb):
                try:
                    cb()
                except Exception as ex:
                    log("提醒走动回调失败: %s" % ex, logging.WARNING)
            return True
        # ease-out 缓动，起步稳、接近目标时减速
        k = 1.0 - (1.0 - prog) ** 2
        nx = w["fx"] + (w["tx"] - w["fx"]) * k
        ny = w["fy"] + (w["ty"] - w["fy"]) * k
        self.move(int(round(nx)), int(round(ny)))
        self._clamp_into_work_area()
        self.update()
        return True

    def cancel_remind_walk(self):
        self.remind_walk = None

    def show_remind_bubble(self, text, icon, kind, hold=10.0,
                           todo_id=None, schedule_id=None):
        """显示加大的提醒气泡：图标 + 文字 + 两个按钮，默认 10 秒后消失。"""
        self.remind_text = text or ""
        self.remind_icon = icon or ""
        self.remind_kind = kind or ""
        self.remind_hold = max(1.0, float(hold))
        self.remind_until = self.t * TICK / 1000.0 + self.remind_hold
        self.remind_hover = False
        self._remind_last_tick = None
        self.remind_todo_id = todo_id
        self.remind_schedule_id = schedule_id
        self._remind_btn_rects = {}
        self.update()

    def hide_remind_bubble(self):
        self.remind_text = ""
        self.remind_until = 0.0
        self.remind_hover = False
        self._remind_last_tick = None
        self._remind_btn_rects = {}
        self.update()

    def _remind_active(self):
        if not self.remind_text:
            return False
        return self.t * TICK / 1000.0 < self.remind_until

    def _remind_rects(self):
        """算提醒气泡几何：返回 (bubble, btn_know, btn_snooze, text_lines, line_h, title_h)。

        布局（左图标 / 右内容）：
          ┌───────────────────────────────┐
          │  ●   标题（加粗，小一号）      │
          │      正文（可多行）            │
          │      [知道了] [稍后再提醒] 第N次│
          └───────────────────────────────┘
        图标在气泡内部左侧垂直居中，直径 REMIND_ICON_DIAMETER，绝不越界。
        """
        fm = QFontMetrics(self.remind_font)
        pad = 12.0
        gap = 10.0
        icon_d = REMIND_ICON_DIAMETER
        line_h = fm.height()

        # 标题字体（比正文小一号、加粗）
        title_font = QFont(self.remind_font)
        title_font.setPixelSize(max(10, int(self.remind_font.pixelSize() * 0.86)))
        title_font.setBold(True)
        tfm = QFontMetrics(title_font)
        title_h = tfm.height()
        title = REMIND_TITLES.get(self.remind_kind, REMIND_TITLES["water"])

        # 左侧图标区 + 右侧内容区
        left_w = icon_d + gap
        # 按钮（并排一行、等宽）
        btn_h = int(24 * REMIND_BUBBLE_SCALE)
        btn_gap = 8
        # 窗口窄（放不下标准气泡）时用短标签
        narrow = self.width() < REMIND_BUBBLE_MIN_W + 16
        label1, label2 = ("知道了", "稍后") if narrow else ("知道了", "稍后再提醒")
        w1min = fm.horizontalAdvance(label1) + 20
        w2min = fm.horizontalAdvance(label2) + 20
        # 计数文字（今天第 N 次）宽度
        cnt_text = "" if narrow else self._remind_count_text()
        cnt_w = fm.horizontalAdvance(cnt_text) + 6 if cnt_text else 0.0

        # 气泡宽度：至少 REMIND_BUBBLE_MIN_W；并保证按钮 + 计数放得下
        min_btn_row = w1min + btn_gap + w2min + (cnt_w + 6 if cnt_w else 0)
        max_w = min(int(360 * REMIND_BUBBLE_SCALE), max(160, self.width() - 4))
        bw = max(REMIND_BUBBLE_MIN_W, min_btn_row + pad * 2 + left_w,
                 min(REMIND_BUBBLE_MIN_W, max_w))
        bw = min(bw, max_w)
        # 窗口比按钮行还窄时，计数让位、按钮压缩
        if bw < min_btn_row + pad * 2 + left_w:
            cnt_w = 0.0

        # 正文可用宽
        text_w = bw - pad * 2 - left_w

        # 正文折行：不在「数字+单位」「站起来」「看远处」等词中间断开
        lines = self._wrap_remind_text(self.remind_text, fm, text_w)
        th = len(lines) * line_h

        # 高度：pad + 标题 + 4 + 正文 + 10 + 按钮 + pad
        bh = pad + title_h + 4 + th + 10 + btn_h + pad
        bx = (self.width() - bw) / 2.0
        by = 6.0
        bubble = QRectF(bx, by, bw, bh)

        btn_y = by + bh - pad - btn_h
        # 按钮宽度：保证「稍后再提醒」完整放下；计数放不下就省略
        btn_min_w = max(w1min, w2min)
        avail = bw - pad * 2 - left_w
        btn_w = (avail - btn_gap - cnt_w) / 2.0
        if btn_w < btn_min_w:
            # 计数让位，仍不够就压按钮宽（文字会略挤但不截断）
            btn_w = max(btn_min_w, (avail - btn_gap) / 2.0)
            cnt_w = 0.0
        b1 = QRectF(bx + pad + left_w, btn_y, btn_w, btn_h)
        b2 = QRectF(b1.right() + btn_gap, btn_y, btn_w, btn_h)
        return bubble, b1, b2, lines, line_h, title_h

    def _remind_count_text(self):
        """按钮行右侧的「今天第 N 次」灰字（拿不到就返回空串）。"""
        try:
            kind = self.remind_kind
            if not kind:
                return ""
            dt = self.app._done_today()
            key = kind if kind in ("water", "stand", "eyes") else "todo"
            n = int(dt.get(key, 0)) + 1
            return "今天第 %d 次" % n
        except Exception:
            return ""

    # 不该在中间断行的词（保护词：遇到就先换行，再整词输出）
    REMIND_NOBREAK_WORDS = (
        "站起来", "起来走走", "起来动动", "起来活动", "看远处", "闭眼",
        "喝一口", "喝水", "伸个懒腰", "走两步", "休息一下", "20 秒",
    )

    def _wrap_remind_text(self, text, fm, max_w):
        """按宽度折行，保护词（站起来/看远处/20 秒…）作为整体不拆开。"""
        if not text:
            return [""]
        NB = "\u00a0"

        # 1) 数字+单位 合并成整体（用不换行空格）
        buf = re.sub(r"(\d+)\s+(秒|分钟|小时|次|个)",
                     lambda m: m.group(1) + NB + m.group(2), text)
        # 2) 把保护词整体替换为不可分割的 token（占位符），折行后再还原
        guards = []
        for w in sorted(self.REMIND_NOBREAK_WORDS, key=len, reverse=True):
            ww = w.replace(" ", NB)
            while ww in buf:
                idx = buf.index(ww)
                ph = "\x01%d\x02" % len(guards)
                guards.append(w)
                buf = buf[:idx] + ph + buf[idx + len(ww):]

        # 3) 逐字符折行（占位符整体不拆）
        lines, cur = [], ""
        i = 0
        while i < len(buf):
            ch = buf[i]
            if ch == "\x01":
                j = buf.find("\x02", i)
                token = buf[i:j + 1]
                step = j + 1 - i
            else:
                token = ch
                step = 1
            if fm.horizontalAdvance(cur + token) > max_w and cur:
                lines.append(cur)
                cur = token
            else:
                cur += token
            i += step
        if cur or not lines:
            lines.append(cur)

        # 4) 还原占位符 + 不换行空格
        out = []
        for l in lines:
            for k, w in enumerate(guards):
                l = l.replace("\x01%d\x02" % k, w)
            out.append(l.replace(NB, " "))
        return out

    def _hit_remind_button(self, pos):
        """判断点到了哪个按钮：'know' / 'snooze' / None。"""
        if not self._remind_active():
            return None
        try:
            _b, r1, r2, _l, _h, _t = self._remind_rects()
            if r1.contains(QPointF(pos)):
                return "know"
            if r2.contains(QPointF(pos)):
                return "snooze"
        except Exception as ex:
            log("提醒按钮命中测试失败: %s" % ex, logging.DEBUG)
        return None

    def _paint_remind_bubble(self, p):
        """画提醒气泡：暖色卡片 + 左侧圆形图标 + 标题/正文/按钮行。"""
        if not self._remind_active():
            return
        bubble, r1, r2, lines, line_h, title_h = self._remind_rects()
        pad = 12.0
        gap = 10.0
        icon_d = REMIND_ICON_DIAMETER
        kind = self.remind_kind or "water"

        # ---- 配色 ----
        bg_top = QColor(255, 252, 244)
        bg_bot = QColor(255, 243, 222)
        border = QColor(247, 181, 92)
        fg = QColor(78, 52, 18)
        sub_fg = QColor(150, 128, 100)
        shadow = QColor(120, 84, 30, 38)

        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # ---- 投影 ----
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(shadow.red(), shadow.green(), shadow.blue(), 18))
        p.drawRoundedRect(bubble.adjusted(-1, 1, 1, 3), 15, 15)
        p.setBrush(QColor(shadow.red(), shadow.green(), shadow.blue(), 34))
        p.drawRoundedRect(bubble.adjusted(1, 2, 1, 2), 14, 14)

        # ---- 卡片 ----
        grad = QLinearGradient(bubble.topLeft(), bubble.bottomLeft())
        grad.setColorAt(0.0, bg_top)
        grad.setColorAt(1.0, bg_bot)
        p.setBrush(QBrush(grad))
        p.setPen(QPen(border, 1.6))
        p.drawRoundedRect(bubble, 14, 14)

        # ---- 尾巴 ----
        tail_cx = self.width() / 2.0
        tail_y = bubble.bottom() - 1
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg_bot)
        p.drawPolygon(QPolygonF([
            QPointF(tail_cx - 9, tail_y),
            QPointF(tail_cx + 9, tail_y),
            QPointF(tail_cx, tail_y + 11),
        ]))
        p.setPen(QPen(border, 1.6))
        p.drawLine(QPointF(tail_cx - 9, tail_y), QPointF(tail_cx, tail_y + 11))
        p.drawLine(QPointF(tail_cx + 9, tail_y), QPointF(tail_cx, tail_y + 11))

        # ---- 左侧圆形图标（气泡内部、垂直居中，绝不越界）----
        content_h = bubble.height() - pad * 2
        icon_cy = bubble.top() + pad + content_h / 2.0
        icon_cx = bubble.left() + pad + icon_d / 2.0
        circle = QRectF(icon_cx - icon_d / 2.0, icon_cy - icon_d / 2.0, icon_d, icon_d)
        # 圆形底色
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(REMIND_ICON_COLORS.get(kind, REMIND_ICON_COLORS["schedule"]))
        p.drawEllipse(circle)
        # 图标（emoji，用专用字体保证清晰）
        emoji = self.remind_icon or REMIND_ICONS.get(kind, "⏰")
        emf = QFont(REMIND_ICON_FONT)
        emf.setPixelSize(int(icon_d * 0.52))
        p.setFont(emf)
        p.setPen(QColor(255, 255, 255))
        p.drawText(circle, Qt.AlignmentFlag.AlignCenter, emoji)

        # ---- 右侧：标题（加粗、小一号）----
        text_x = bubble.left() + pad + icon_d + gap
        text_w = bubble.right() - pad - text_x
        title = REMIND_TITLES.get(kind, REMIND_TITLES["water"])
        tf = QFont(self.remind_font)
        tf.setPixelSize(max(10, int(self.remind_font.pixelSize() * 0.86)))
        tf.setBold(True)
        p.setFont(tf)
        p.setPen(QColor(196, 116, 24))
        p.drawText(QRectF(text_x, bubble.top() + pad, text_w, title_h),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   title)

        # ---- 右侧：正文 ----
        body_y = bubble.top() + pad + title_h + 4
        p.setFont(self.remind_font)
        p.setPen(fg)
        for i, line in enumerate(lines):
            p.drawText(QRectF(text_x, body_y + i * line_h, text_w, line_h),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       line)

        # ---- 按钮（主实心 / 次描边）----
        btn_font = QFont(self.remind_font)
        btn_font.setPixelSize(max(10, int(self.remind_font.pixelSize() * 0.80)))
        for rect, label, primary in ((r1, "知道了", True),
                                     (r2, "稍后再提醒", False)):
            if primary:
                bg, bd, tf2 = QColor(255, 193, 110), QColor(238, 146, 44), QColor(96, 52, 8)
            else:
                bg, bd, tf2 = QColor(255, 250, 240), QColor(247, 205, 150), QColor(150, 96, 30)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(180, 130, 60, 26))
            p.drawRoundedRect(rect.adjusted(0, 1, 0, 2), 8, 8)
            p.setBrush(bg)
            p.setPen(QPen(bd, 1.2))
            p.drawRoundedRect(rect, 8, 8)
            p.setFont(btn_font)
            p.setPen(tf2)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # ---- 计数：按钮行右侧灰字 ----
        cnt = self._remind_count_text()
        if cnt:
            cf = QFont(self.remind_font)
            cf.setPixelSize(max(9, int(self.remind_font.pixelSize() * 0.70)))
            p.setFont(cf)
            p.setPen(sub_fg)
            cnt_rect = QRectF(r2.right() + 6, r2.top(),
                              bubble.right() - pad - r2.right() - 6, r2.height())
            p.drawText(cnt_rect, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), cnt)

        p.restore()
        self._remind_btn_rects = {"know": r1, "snooze": r2}

    def say(self, text, inner=False):
        if text == self.last_line and not text.startswith("天气"):
            return
        self.last_line = text
        self.bubble_inner = inner
        self.bubble_text = f"（{text}）" if inner else text
        self.bubble_until = self.t * TICK / 1000.0 + 2.8
        self.update()

    # ---------- 触摸区域 ----------
    def leaveEvent(self, e):
        if self.remind_hover:
            self.remind_hover = False
        super().leaveEvent(e)

    def _zone_at(self, pt):
        left = self.win_mx
        right = self.width() - self.win_mx
        if pt.x() < left or pt.x() > right:
            return "none"
        top = BUBBLE_H + MARGIN
        if pt.y() < top or pt.y() > top + self.cur_h:
            return "none"
        rel = (pt.y() - top) / max(1, self.cur_h)
        if rel < 0.32:
            return "head"
        if rel < 0.55:
            return "face"
        return "body"

    def _touch_head(self):
        log("[触摸] 摸头")
        self.mark_interact()
        self._play_temp("pat", total=1.5)
        self.jump_t = 0.0
        self.app.chat_paused = False
        self.say(self.app._pick_line(PAT_LINES, "pat"))

    def _touch_poke(self):
        log("[触摸] 戳脸")
        self.mark_interact()
        self.food_panel.hide()
        self._play_temp("poke", total=1.2)
        self.jump_t = 0.0
        self.app.chat_paused = False
        self.say(self.app._pick_line(POKE_LINES, "poke"))

    def _start_stroke(self):
        log("[触摸] 抚摸")
        self.mark_interact()
        self._play_temp("stroke", total=1.6)
        self.jump_t = 0.0
        now = time.time()
        if now - self._touch_cd >= 1.8:
            self._touch_cd = now
            self.say(self.app._pick_line(STROKE_LINES, "stroke"))

    def _update_stroke(self, e):
        """在鱼身上按住轻微来回移动 = 抚摸（不拖动窗口）。返回 True 表示正在抚摸。"""
        st = self._stroke
        if st is None:
            return False
        if st["active"]:
            self._play_temp("stroke", total=1.6)
            self.update()
            return True
        if self._press_zone != "body":
            return False
        x = e.globalPosition().toPoint().x()
        dx = x - st["last_x"]
        st["last_x"] = x
        if abs(dx) < 1.2:
            return False
        st["travel"] += abs(dx)
        d = 1 if dx > 0 else -1
        if st["dir"] != 0 and d != st["dir"]:
            st["rev"] += 1
            if st["rev"] >= STROKE_REVERSALS and st["travel"] <= STROKE_MAX_TRAVEL:
                st["active"] = True
                self._start_stroke()
                self.update()
                return True
        st["dir"] = d
        return False

    # ---------- 鼠标事件 ----------
    def mousePressEvent(self, e):
        # 提醒中被拖拽 → 取消回位，以用户拖到的位置为准
        if self.remind_origin is not None and e.button() == Qt.MouseButton.LeftButton:
            self.remind_user_moved = True
            self.cancel_remind_walk()
        # 提醒气泡按钮优先（点按钮不触发宠物交互）
        if e.button() == Qt.MouseButton.LeftButton and self.remind_text:
            hit = self._hit_remind_button(e.position().toPoint())
            if hit == "know":
                self.app.acknowledge_reminder(self.remind_kind,
                                              self.remind_todo_id,
                                              self.remind_schedule_id)
                return
            if hit == "snooze":
                self.app.snooze_reminder(self.remind_kind,
                                         self.remind_todo_id,
                                         self.remind_schedule_id)
                return
        if e.button() == Qt.MouseButton.LeftButton:
            self.last_press_pos = e.globalPosition().toPoint()
            self.dragging = False
            self.drag_start_pos = e.globalPosition().toPoint()
            self._press_gpos = e.globalPosition().toPoint()
            self.app.chat_paused = True
            # 触摸手势
            self._press_zone = self._zone_at(e.position().toPoint())
            gx = e.globalPosition().toPoint().x()
            self._stroke = {"active": False, "last_x": gx, "dir": 0, "rev": 0, "travel": 0}
            self._register_face_press()

    def _register_face_press(self):
        """脸上快速连点计数（第 3 下起算戳脸；双击也计一次）"""
        now = time.time()
        self._face_clicks = [t for t in self._face_clicks if now - t < 1.2]
        if self._press_zone != "face":
            self._poke_pending = False
            return
        self._face_clicks.append(now)
        self._poke_pending = len(self._face_clicks) >= 3

    def mouseMoveEvent(self, e):
        # 提醒气泡悬停检测（暂停倒计时）
        if self.remind_text:
            try:
                hover = self._hit_remind_button(e.position().toPoint()) is not None
                if not hover:
                    bubble, _r1, _r2, _l, _h = self._remind_rects()
                    hover = bubble.contains(QPointF(e.position().toPoint()))
                self.remind_hover = bool(hover)
            except Exception:
                self.remind_hover = False
        if e.buttons() & Qt.MouseButton.LeftButton and self.drag_start_pos is not None:
            delta = e.globalPosition().toPoint() - self.drag_start_pos
            # 在身上轻微来回移动 = 抚摸，不拖窗口
            if not self.dragging and self._update_stroke(e):
                return
            if not self.dragging and delta.manhattanLength() > DRAG_THRESHOLD:
                self.dragging = True
                # 被打断：停止连招/转身，切到「被抓起」姿势
                self._interrupt_combo()
                self._turn_interrupt()
                self.mark_interact()
                self.action = None
                self.action_t = 0.0
                # 有「被抓起」素材就播它（循环），没有则保持原样
                if not self._start_frame_anim("drag"):
                    self._stop_frame_anim()
                self.drag_offset = e.globalPosition().toPoint() - QPoint(self.x(), self.y())
                if self._physics_feature_on():
                    # 进入拖拽：用「按下点」算抓取偏移，之后由 tick 里的弹簧平滑追随
                    press = self._press_gpos or self.drag_start_pos or e.globalPosition().toPoint()
                    self._begin_drag_spring(press)
            if self.dragging:
                if self._dragging_physics:
                    self._drag_to(e.globalPosition().toPoint())
                elif self.drag_offset is not None:
                    # 物理已关闭：退回原始硬贴拖动
                    self.move(e.globalPosition().toPoint() - self.drag_offset)
                if abs(delta.x()) > 10:
                    self._set_dir("left" if delta.x() < 0 else "right", 1 if delta.x() < 0 else -1)
                self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if self.dragging:
                self.dragging = False
                self.drag_offset = None
                self.drag_start_pos = None
                self.target = None
                self.rest_until = self.t * TICK + random.randint(6000, 14000)
                # 甩抛：最近 100ms 速度决定初速度；速度太小原地放下
                if self._dragging_physics:
                    thrown = self._release_drag_physics()
                else:
                    self._body_state()
                    thrown = False
                if not thrown:
                    self._set_dir("down", 1)
                if random.random() < 0.5:
                    self.say(random.choice(DRAG_LINES))
                self.app.chat_paused = False
                self._stroke = None
            elif self._stroke is not None and self._stroke.get("active"):
                # 抚摸结束
                self._stroke = None
                self.app.chat_paused = False
                self._play_temp("stroke", total=0.7)
            elif self._poke_pending:
                # 脸部快速连点（第 3 下起）→ 戳脸；即使这一下同时被算作双击也优先戳脸
                self._click_timer.stop()
                self._face_clicks = []
                self._poke_pending = False
                self._stroke = None
                self._touch_poke()
            elif time.time() - self._dbl_guard < 0.15:
                # 双击（喂食）的第二下抬起，不再当单击处理
                self._stroke = None
                self.app.chat_paused = False
            else:
                zone = self._press_zone
                self._stroke = None
                if zone == "head":
                    self._click_timer.stop()
                    self._touch_head()
                else:
                    self._click_timer.start(280)  # 等双击判定；单击则回嘴
            self.last_press_pos = None
            self.drag_start_pos = None

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._click_timer.stop()
            self._dbl_guard = time.time()
            self._register_face_press()
            if self._poke_pending:
                # 已经是快速连点 → 这次算戳脸，不弹喂食
                return
            log("[触摸] 双击喂食")
            self.food_panel.popup_at(self.x() + self.width() / 2, self.y() + BUBBLE_H)

    def _on_single_click(self):
        """单击：随机一个回应动作（代码变换 + 专属台词），不连续重复，结束时 Q 弹挤压"""
        log("[触摸] 单击")
        self.mark_interact()
        self._interrupt_combo()
        self._turn_interrupt()
        if not self.app.is_enabled("click_response"):
            # 关闭点击回应：只轻轻蹦一下，不播动作和台词
            self.jump_t = 1.0
            self.app.chat_paused = False
            return
        names = [n for n in CLICK_RESPONSES if n != self.last_click_action]
        if not names:
            names = list(CLICK_RESPONSES)
        name = random.choice(names)
        self.last_click_action = name
        spec = CLICK_RESPONSES[name]
        self._play_temp(name, total=spec["duration"], squash=True)
        self.say(random.choice(spec["lines"]))
        self.app.chat_paused = False

    def on_food(self, food):
        self.food_panel.hide()
        self.jump_t = 0.6
        lines = FOOD_LINES.get(food, ["好吃！"])
        self.say(random.choice(lines))
        # 播对应的吃饭动画（画师原画）
        act = FOOD_ANIM.get(food)
        if act and self._start_frame_anim(act):
            self.chain_action = act
            self.chain_dur = float(ACTIONS.get(act, {}).get("duration", 10.0))
            self.chain_elapsed = 0.0
        self.mark_interact()

    def contextMenuEvent(self, e):
        self.app.build_menu(self).exec(e.globalPos())

    # ---------- 单只宠物的功能 ----------
    def set_mode(self, mode):
        self.mode = mode
        self.target = None
        if mode == "still":
            # 原地待着只用正面：复位朝向，避免残留侧身淡入
            self.dir = "down"
            self.facing = 1
            self.prev_key = None
            self.cross_t = 0.0
            if ACTIONS.get(self.chain_action or "", {}).get("category") == "turn":
                self._start_chain_action("idle_breath")
        self.pet["mode"] = mode
        self.app.save_cfg()
        self.update()

    def set_size(self, mult):
        self.cur_h = int(340 * mult)
        self.pet["size"] = mult
        self.cross_t = 0.0
        self.prev_key = None
        self.win_mx = int(self.cur_h * 0.062) + 6
        self.win_w = max(p.width() for k, p in self.sprites.items() if k[1] == self.cur_h) + self.win_mx * 2
        self.setFixedSize(self.win_w, self.cur_h + BUBBLE_H + MARGIN * 2 + 10)
        self.snap_into_screen()
        self._body_state()

    def snap_into_screen(self):
        geo = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        x = max(geo.left(), min(geo.right() - self.width(), self.x()))
        y = max(geo.top(), min(geo.bottom() - self.height(), self.y()))
        self.move(x, y)
        # 同步物理体，避免下一次物理步进把窗口拉回旧位置
        self._body_state()

    def _apply_passthrough(self, on):
        hwnd = int(self.winId())
        GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x80000, 0x20
        style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        style = style | WS_EX_LAYERED
        if on:
            style |= WS_EX_TRANSPARENT
        else:
            style &= ~WS_EX_TRANSPARENT
        ctypes.windll.user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)

    def showEvent(self, e):
        super().showEvent(e)
        self.app.apply_capture_exclude(self)


class SettingsWindow(QDialog):
    """统一设置窗口：左侧分页，右侧内容。所有入口都从这里进（右键「设置…」/ 托盘）。"""

    PAGES = [
        ("features", "功能开关"),
        ("pet", "宠物"),
        ("physics", "物理"),
        ("animation", "动画"),
        ("reminder", "提醒"),
        ("balance", "余额"),
        ("chat", "聊天"),
        ("memory", "记忆"),
        ("general", "通用"),
        ("about", "关于"),
    ]

    def __init__(self, app, page=None):
        super().__init__(app.main_pet)
        self.app = app
        self.pet = app.main_pet
        self.setWindowTitle("大肥鱼桌宠 · 设置")
        self.setMinimumSize(760, 560)
        self.resize(820, 600)
        self._build_ui()
        if page:
            self._select_page(page)
        self.reload()
        # 宠物显示状态变化 → 只刷新宠物页
        try:
            self.app.pet_state_changed.connect(self._on_pet_state_changed)
        except Exception:
            pass

    # ---------- 骨架 ----------
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        self.list = QListWidget(self)
        self.list.setFixedWidth(132)
        self.list.setStyleSheet(
            "QListWidget{border:1px solid #dfe3ec;border-radius:8px;padding:4px;}"
            "QListWidget::item{padding:7px 8px;border-radius:6px;}"
            "QListWidget::item:selected{background:#5686fe;color:white;}")
        for key, label in self.PAGES:
            self.list.addItem(label)
        self.list.currentRowChanged.connect(self._on_page_changed)
        root.addWidget(self.list)

        right = QVBoxLayout()
        right.setSpacing(8)
        self.stack = QStackedWidget(self)
        self._pages = {}
        for key, label in self.PAGES:
            w = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(14, 12, 14, 12)
            lay.setSpacing(9)
            scroll = QScrollArea(w)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            inner = QWidget()
            inner.setLayout(lay)
            scroll.setWidget(inner)
            outer = QVBoxLayout(w)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.addWidget(scroll)
            self._pages[key] = lay
            self.stack.addWidget(w)
        right.addWidget(self.stack, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        btn_row.addWidget(close)
        right.addLayout(btn_row)
        root.addLayout(right, 1)

        self._build_features_page(self._pages["features"])
        self._build_pet_page(self._pages["pet"])
        self._build_physics_page(self._pages["physics"])
        self._build_animation_page(self._pages["animation"])
        self._build_reminder_page(self._pages["reminder"])
        self._build_balance_page(self._pages["balance"])
        self._build_chat_page(self._pages["chat"])
        self._build_general_page(self._pages["general"])
        self._build_memory_page(self._pages["memory"])
        self._build_about_page(self._pages["about"])

    def _select_page(self, key):
        for i, (k, _label) in enumerate(self.PAGES):
            if k == key:
                self.list.setCurrentRow(i)
                return

    def _on_page_changed(self, row):
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)
        # 切到宠物页时刷新一次（列表状态可能已变）
        try:
            if row >= 0 and self.PAGES[row][0] == "pet":
                self.refresh_pet_page()
        except Exception:
            pass

    # ---------- 通用小组件 ----------
    def _title(self, lay, text, desc=None):
        lab = QLabel(text)
        f = QFont()
        f.setPointSize(11)
        f.setBold(True)
        lab.setFont(f)
        lay.addWidget(lab)
        if desc:
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet("color:#8a8f9c;font-size:11px;")
            lay.addWidget(d)

    def _check(self, lay, label, checked, on_toggle, desc=None, note=None):
        cb = QCheckBox(label)
        cb.setChecked(bool(checked))
        cb.toggled.connect(on_toggle)
        lay.addWidget(cb)
        if desc:
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet("color:#8a8f9c;font-size:11px;margin-left:22px;")
            lay.addWidget(d)
        if note:
            n = QLabel(note)
            n.setStyleSheet("color:#d08a00;font-size:11px;margin-left:22px;")
            lay.addWidget(n)
        return cb

    def _slider_row(self, lay, label, value, lo, hi, step, fmt, on_change, suffix=""):
        """一行：标签 + 滑块 + 数值框（双向同步）"""
        row = QHBoxLayout()
        name = QLabel(label)
        name.setFixedWidth(96)
        row.addWidget(name)
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setPageStep(step * 5)
        s.setValue(int(value))
        s.setMinimumWidth(220)
        row.addWidget(s, 1)
        box = QDoubleSpinBox()
        box.setRange(lo, hi)
        box.setSingleStep(step)
        box.setDecimals(2 if isinstance(step, float) and step < 1 else 0)
        box.setValue(float(value))
        box.setFixedWidth(96)
        row.addWidget(box)
        unit = QLabel(suffix)
        unit.setStyleSheet("color:#8a8f9c;")
        row.addWidget(unit)
        lay.addLayout(row)

        def from_slider(v):
            if abs(box.value() - v) > 1e-9:
                box.blockSignals(True)
                box.setValue(float(v))
                box.blockSignals(False)
            on_change(float(v))

        def from_box(v):
            iv = int(round(v))
            if s.value() != iv:
                s.blockSignals(True)
                s.setValue(iv)
                s.blockSignals(False)
            on_change(float(v))

        s.valueChanged.connect(from_slider)
        box.valueChanged.connect(from_box)
        return s, box

    def _combo_row(self, lay, label, items, current, on_change):
        row = QHBoxLayout()
        name = QLabel(label)
        name.setFixedWidth(96)
        row.addWidget(name)
        cb = QComboBox()
        for text, value in items:
            cb.addItem(text, value)
        for i in range(cb.count()):
            if cb.itemData(i) == current:
                cb.setCurrentIndex(i)
                break
        cb.currentIndexChanged.connect(lambda _i: on_change(cb.currentData()))
        row.addWidget(cb, 1)
        lay.addLayout(row)
        return cb

    # ---------- 功能开关页 ----------
    def _build_features_page(self, lay):
        self._title(lay, "功能开关",
                    "关掉后对应功能不再触发、也不占用定时器。带「重启生效」标记的需要重启程序。")
        self._feature_boxes = {}
        groups = {}
        for key, spec in FEATURES.items():
            if key in FEATURE_HIDDEN_IN_LIST:
                continue        # 只在专属页显示，不在总开关页重复
            groups.setdefault(spec.get("group", "其它"), []).append((key, spec))
        order = [g for g in FEATURE_GROUP_ORDER if g in groups]
        order += [g for g in groups if g not in order]
        for gi, gname in enumerate(order):
            if gi > 0:
                line = QFrame()
                line.setFrameShape(QFrame.Shape.HLine)
                line.setStyleSheet("color:#e6e9f0;")
                lay.addWidget(line)
            h = QLabel(gname)
            h.setStyleSheet("color:#5686fe;font-weight:bold;font-size:11px;margin-top:4px;")
            lay.addWidget(h)
            for key, spec in groups[gname]:
                note = "重启生效" if spec.get("needs_restart") else None
                cb = self._check(
                    lay, spec["name"], self.app.is_enabled(key),
                    lambda on, k=key: self.app.set_feature(k, on),
                    spec.get("desc"), note)
                self._feature_boxes[key] = cb
        lay.addStretch(1)

    # ---------- 宠物页 ----------
    def _build_pet_page(self, lay):
        self._title(lay, "宠物管理", "可以养多只大肥鱼；至少保留一只 id 为 main 的宠物。")
        self.pet_list = QListWidget()
        self.pet_list.setFixedHeight(120)
        self.pet_list.currentRowChanged.connect(lambda _r: self.refresh_pet_page())
        lay.addWidget(self.pet_list)

        row = QHBoxLayout()
        add = QPushButton("添加宠物")
        add.clicked.connect(self.app.add_pet)
        rm = QPushButton("删除选中")
        rm.clicked.connect(self._delete_selected_pet)
        row.addWidget(add)
        row.addWidget(rm)
        row.addStretch(1)
        lay.addLayout(row)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line)

        self._pet_size_row = QHBoxLayout()
        self.pet_size_box = self._combo_row(
            lay, "大小", [(k, v) for k, v in SIZE_LEVELS.items()], 0.7,
            lambda v: self._apply_to_selected("size", v))
        self.pet_mode_box = self._combo_row(
            lay, "模式",
            [("自由散步", "wander"), ("跟随鼠标", "follow"), ("原地待着", "still")], "wander",
            lambda v: self._apply_to_selected("mode", v))

        pos = QHBoxLayout()
        name = QLabel("位置")
        name.setFixedWidth(96)
        pos.addWidget(name)
        self.pos_x = QSpinBox()
        self.pos_x.setRange(-10000, 10000)
        self.pos_y = QSpinBox()
        self.pos_y.setRange(-10000, 10000)
        pos.addWidget(QLabel("X"))
        pos.addWidget(self.pos_x)
        pos.addWidget(QLabel("Y"))
        pos.addWidget(self.pos_y)
        apply_pos = QPushButton("应用位置")
        apply_pos.clicked.connect(self._apply_position)
        pos.addWidget(apply_pos)
        center = QPushButton("居中")
        center.clicked.connect(lambda _=False: self._apply_position(center=True))
        pos.addWidget(center)
        pos.addStretch(1)
        lay.addLayout(pos)

        vis_row = QHBoxLayout()
        vis_row.addSpacing(96)
        self.pet_vis_btn = QPushButton("隐藏这只")
        self.pet_vis_btn.clicked.connect(self._toggle_selected_visible)
        vis_row.addWidget(self.pet_vis_btn)
        snap = QPushButton("回到屏幕内")
        snap.clicked.connect(lambda _=False: self._selected_pet() and self._selected_pet().snap_into_screen())
        vis_row.addWidget(snap)
        vis_row.addStretch(1)
        lay.addLayout(vis_row)

        # ---- 隐私（原「功能开关-隐私」组移入这里）----
        line_pv = QFrame()
        line_pv.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line_pv)
        self._feature_boxes["capture_hide"] = self._check(
            lay, "截屏时隐藏",
            self.app.is_enabled("capture_hide"),
            lambda on: self.app.set_feature("capture_hide", bool(on)),
            "从系统截图 / 录屏里排除，并在截图工具出现时自动隐藏（关闭后就能被拍到）")
        lay.addStretch(1)

    def _selected_pet(self):
        row = self.pet_list.currentRow()
        cfg_list = self.app.cfg.get("pets") or []
        if 0 <= row < len(cfg_list):
            pid = cfg_list[row].get("id")
            return self.app.get_pet(pid)
        return None

    def _delete_selected_pet(self):
        pet = self._selected_pet()
        if pet is None:
            return
        if pet.pet_id == "main":
            QMessageBox.information(self, "删除宠物", "id 为 main 的宠物必须保留，不能删除")
            return
        ret = QMessageBox.question(self, "删除宠物",
                                   "确定删除宠物「%s」吗？" % pet.pet_id,
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.Yes:
            self.app.remove_pet(pet.pet_id)
            self.reload()

    def _apply_to_selected(self, field, value):
        pet = self._selected_pet()
        if pet is None:
            return
        if field == "size":
            pet.set_size(float(value))
        elif field == "mode":
            pet.set_mode(str(value))
        self._refresh_pet_controls()

    def _apply_position(self, center=False):
        pet = self._selected_pet()
        if pet is None:
            return
        if center:
            geo = pet._work_area()
            x = geo.left() + (geo.width() - pet.width()) // 2
            y = geo.top() + (geo.height() - pet.height()) // 2
        else:
            x, y = self.pos_x.value(), self.pos_y.value()
        pet.move(int(x), int(y))
        pet._body_state()
        pet.snap_into_screen()
        self.app.save_cfg()
        self._refresh_pet_controls()

    def _toggle_selected_visible(self):
        """按钮只做一件事：按当前状态取反调用 set_visible，不自己改文字。"""
        pet = self._selected_pet()
        if pet is None:
            return
        pid = pet.pet_id
        state = self.app.get_pet_state(pid)
        if state == PET_TEMP_HIDDEN:
            # 临时隐藏时按钮是灰的，正常点不到；兜底也走"显示"
            self.app.set_visible(pid, True, "settings")
            return
        self.app.set_visible(pid, state != PET_VISIBLE, "settings")

    def refresh_pet_page(self):
        """一次性刷新宠物页：列表文字 / 按钮文字与可用性 / X-Y 坐标。

        列表与按钮**都只通过 get_pet_state + state_text 取状态**，
        不另存字符串或布尔值。
        """
        # ---- 列表：{名称} · {模式中文} · {状态文字} ----
        pets_cfg = self.app.cfg.get("pets") or []
        cur_row = self.pet_list.currentRow()
        try:
            self.pet_list.blockSignals(True)
            self.pet_list.clear()
            for c in pets_cfg:
                pid = c.get("id")
                name = self.app.pet_name(pid)
                mode_text = self.app.pet_mode_text(pid)
                state_text = self.app.state_text(self.app.get_pet_state(pid))
                self.pet_list.addItem("%s · %s · %s" % (name, mode_text, state_text))
            if pets_cfg:
                self.pet_list.setCurrentRow(max(0, min(cur_row, len(pets_cfg) - 1)))
        finally:
            self.pet_list.blockSignals(False)

        # ---- 选中项：按钮文字 + 可用性 + 坐标 ----
        pet = self._selected_pet()
        if pet is None:
            self.pet_vis_btn.setText("隐藏这只")
            self.pet_vis_btn.setEnabled(False)
            self.pet_vis_btn.setToolTip("")
            return
        pid = pet.pet_id
        state = self.app.get_pet_state(pid)
        if state == PET_VISIBLE:
            self.pet_vis_btn.setText("隐藏这只")
            self.pet_vis_btn.setEnabled(True)
            self.pet_vis_btn.setToolTip("")
        elif state == PET_HIDDEN:
            self.pet_vis_btn.setText("显示这只")
            self.pet_vis_btn.setEnabled(True)
            self.pet_vis_btn.setToolTip("")
        else:  # PET_TEMP_HIDDEN
            self.pet_vis_btn.setText(self.app.state_text(state))
            self.pet_vis_btn.setEnabled(False)
            self.pet_vis_btn.setToolTip("当前为临时隐藏（截屏守卫或小游戏），稍后会自动恢复")

        # 坐标 / 大小 / 模式
        for i in range(self.pet_size_box.count()):
            if abs(float(self.pet_size_box.itemData(i)) - float(pet.pet.get("size", 0.7))) < 1e-6:
                self.pet_size_box.setCurrentIndex(i)
                break
        for i in range(self.pet_mode_box.count()):
            if self.pet_mode_box.itemData(i) == pet.mode:
                self.pet_mode_box.setCurrentIndex(i)
                break
        self.pos_x.setValue(pet.x())
        self.pos_y.setValue(pet.y())

    # 兼容旧名字（别处可能还在调）
    def _refresh_pet_controls(self):
        self.refresh_pet_page()

    def _on_pet_state_changed(self, pet_id, state, reason):
        """收到显示状态信号 → 刷新宠物页（只刷这一页）。"""
        try:
            if self.isVisible():
                self.refresh_pet_page()
        except Exception:
            pass

    # ---------- 物理页 ----------
    def _build_physics_page(self, lay):
        self._title(lay, "物理效果",
                    "拖拽弹簧、甩抛重力与反弹。改完立即生效，保存在 config.json 的 physics。")
        ph = self.app._physics_cfg()
        self._physics_sliders = {}
        specs = [
            ("gravity", "重力", 200, 6000, 50, "像素/秒²"),
            ("elasticity", "弹性", 0.0, 1.0, 0.05, "0~1"),
            ("friction", "摩擦", 0.0, 20.0, 0.5, ""),
            ("power", "总力度", 0.2, 3.0, 0.1, "倍率"),
            ("spring_stiffness", "弹簧刚度", 20, 1200, 20, ""),
            ("spring_damping", "弹簧阻尼", 2, 120, 2, ""),
        ]
        for key, label, lo, hi, step, suffix in specs:
            cur = ph.get(key, DEFAULT_PHYSICS.get(key, lo))
            self._physics_sliders[key] = self._slider_row(
                lay, label, cur, lo, hi, step, None,
                lambda v, k=key: self.app.set_physics(k, v), suffix)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line)
        self._check(lay, "顶部反弹", bool(ph.get("top_bounce", True)),
                    lambda on: self.app.set_physics("top_bounce", bool(on)),
                    "撞到屏幕顶部时反弹；关闭则贴顶停住")
        self._check(lay, "宠物互撞（多开时生效）", bool(ph.get("pet_collision", False)),
                    lambda on: self.app.set_physics("pet_collision", bool(on)),
                    "多只宠物碰到会互相弹开")

        btn = QPushButton("恢复默认")
        btn.clicked.connect(self._reset_physics)
        lay.addWidget(btn)
        lay.addStretch(1)

    def _reset_physics(self):
        self.app.cfg["physics"] = dict(DEFAULT_PHYSICS)
        self.app.save_cfg()
        self.reload()
        self.app.say_main("物理参数已恢复默认")

    # ---------- 动画页 ----------
    def _build_animation_page(self, lay):
        self._title(lay, "动画",
                    "类别权重决定动作出现频率；移动/待机/转向的权重越高越常出现。")
        self._anim_sliders = {}
        weights = self.app.cfg.get("animation_weights") or {}
        labels = {"idle": "待机权重", "turn": "转向权重", "move": "移动权重"}
        for key in ("idle", "turn", "move"):
            cur = weights.get(key, ACTION_CATEGORY_WEIGHTS.get(key, 5))
            self._anim_sliders[key] = self._slider_row(
                lay, labels[key], cur, 0, 50, 1, None,
                lambda v, k=key: self._set_anim_weight(k, v), "")

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line)
        self._combo_row(
            lay, "减少动效",
            [(label, key) for label, key in ANIMATION_MODE_CHOICES],
            self.app.cfg.get("animation_mode", "auto"),
            lambda v: self.app.set_animation_mode(str(v)))

        # ---- 帧序列动画 ----
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line2)
        fa = self.app.cfg.get("frame_anim")
        if not isinstance(fa, dict):
            fa = {}
        self._frame_anim_box = self._check(
            lay, "帧序列动画", bool(fa.get("enabled", True)),
            lambda on: self._set_frame_anim(on),
            "动作目录 sprites/anim/<动作名>/ 里有 png 就按帧播放；"
            "没有帧的动作仍用原来的代码变形")

        self._frame_stat = QLabel("")
        self._frame_stat.setWordWrap(True)
        self._frame_stat.setStyleSheet("color:#8a8f9c;font-size:11px;margin-left:22px;")
        lay.addWidget(self._frame_stat)

        row = QHBoxLayout()
        row.addSpacing(22)
        reload_btn = QPushButton("重新扫描帧目录")
        reload_btn.clicked.connect(lambda _=False: self._reload_frames())
        row.addWidget(reload_btn)
        clear_btn = QPushButton("释放帧缓存")
        clear_btn.clicked.connect(lambda _=False: self._clear_frames())
        row.addWidget(clear_btn)
        row.addStretch(1)
        lay.addLayout(row)

        btn = QPushButton("恢复默认权重")
        btn.clicked.connect(self._reset_anim)
        lay.addWidget(btn)
        lay.addStretch(1)
        self._refresh_frame_stat()

        # ---- 强度 ----
        line3 = QFrame()
        line3.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line3)
        self._combo_row(
            lay, "转向强度",
            [(label, v) for label, v in TURN_STRENGTH_CHOICES],
            self.app.cfg.get("turn_strength", TURN_STRENGTH_DEFAULT),
            lambda v: self._set_strength("turn_strength", v))
        self._combo_row(
            lay, "代码动作强度",
            [(label, v) for label, v in ACTION_STRENGTH_CHOICES],
            self.app.cfg.get("action_strength", ACTION_STRENGTH_DEFAULT),
            lambda v: self._set_strength("action_strength", v))

        # ---- 待机 tier 概率 ----
        line4 = QFrame()
        line4.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line4)
        tiers = self.app.cfg.get("animation_tiers") or {}
        self._tier_sliders = {}
        tier_labels = {"small": "小动作概率", "medium": "中动作概率", "large": "大动作概率"}
        for key in ("small", "medium", "large"):
            cur = float(tiers.get(key, ANIMATION_TIERS_DEFAULT.get(key, 0)) or 0) * 100.0
            self._tier_sliders[key] = self._slider_row(
                lay, tier_labels[key], cur, 0, 100, 5, None,
                lambda v, k=key: self._set_tier_weight(k, v), "%")

        # ---- 调试面板 ----
        line5 = QFrame()
        line5.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line5)
        dbg = QLabel("当前状态")
        dbg.setStyleSheet("color:#5686fe;font-weight:bold;font-size:11px;margin-top:4px;")
        lay.addWidget(dbg)
        self._idle_state = QLabel("")
        self._idle_state.setWordWrap(True)
        self._idle_state.setStyleSheet("color:#8a8f9c;font-size:11px;margin-left:8px;")
        lay.addWidget(self._idle_state)
        self._action_log = QLabel("")
        self._action_log.setWordWrap(True)
        self._action_log.setStyleSheet(
            "color:#8a8f9c;font-size:11px;margin-left:8px;font-family:Consolas,monospace;")
        lay.addWidget(self._action_log)
        row2 = QHBoxLayout()
        row2.addSpacing(8)
        rf = QPushButton("刷新状态")
        rf.clicked.connect(lambda _=False: self._refresh_debug())
        row2.addWidget(rf)
        row2.addStretch(1)
        lay.addLayout(row2)

        btn = QPushButton("恢复默认权重")
        btn.clicked.connect(self._reset_anim)
        lay.addWidget(btn)
        lay.addStretch(1)
        self._refresh_frame_stat()
        self._refresh_debug()

    # ---- 强度 / tier / 调试 ----
    def _set_strength(self, key, value):
        try:
            self.app.cfg[key] = max(0.0, min(1.0, float(value)))
        except Exception:
            return
        self.app.save_cfg()
        label = "转向强度" if key == "turn_strength" else "代码动作强度"
        pct = int(round(float(value) * 100))
        self.app.say_main("%s：%d%%" % (label, pct))

    def _set_tier_weight(self, key, value):
        tiers = self.app.cfg.get("animation_tiers")
        if not isinstance(tiers, dict):
            tiers = dict(ANIMATION_TIERS_DEFAULT)
        tiers[key] = max(0.0, min(1.0, float(value) / 100.0))
        self.app.cfg["animation_tiers"] = tiers
        self.app.save_cfg()

    def _refresh_debug(self):
        """显示当前待机状态 + 最近 20 个动作 + 帧动作数与内存。"""
        try:
            pet = self.app.main_pet
            if pet is None:
                return
            sec = pet._idle_seconds()
            self._idle_state.setText(
                "待机状态：%s · 已静默 %d 秒（%d 秒后犯困 / %d 秒后坐下 / %d 秒后睡觉）\n"
                "当前动作：%s · 连招：%s · 转向中：%s"
                % (pet._idle_stage, int(sec),
                   int(IDLE_BORED_AFTER), int(IDLE_SIT_AFTER), int(IDLE_SLEEP_AFTER),
                   pet.chain_action or "-",
                   pet._combo or "-",
                   "是" if pet._turn_t > 0 else "否"))
            hist = pet._action_history[-20:]
            if hist:
                lines = ["%s" % name for _t, name in reversed(hist)]
                self._action_log.setText("最近 20 个动作（新→旧）：\n" + " → ".join(lines))
            else:
                self._action_log.setText("最近 20 个动作：暂无")
        except Exception:
            pass

    def _set_frame_anim(self, on):
        fa = self.app.cfg.get("frame_anim")
        if not isinstance(fa, dict):
            fa = {}
        fa["enabled"] = bool(on)
        self.app.cfg["frame_anim"] = fa
        self.app.save_cfg()
        if not on:
            for w in self.app.pets:
                w._stop_frame_anim()
        self.app.say_main("帧序列动画：" + ("开" if on else "关"))
        self._refresh_frame_stat()

    def _reload_frames(self):
        _FRAME_CACHE.clear()
        for w in self.app.pets:
            w._stop_frame_anim()
        self._refresh_frame_stat()
        self.app.say_main("已重新扫描帧目录")

    def _clear_frames(self):
        _FRAME_CACHE.clear()
        for w in self.app.pets:
            w._stop_frame_anim()
        self._refresh_frame_stat()
        self.app.say_main("帧缓存已释放")

    def _refresh_frame_stat(self):
        try:
            n, nbytes = _FRAME_CACHE.stats()
            avail = self._scan_frame_actions()
            mb = nbytes / (1024.0 * 1024.0)
            self._frame_stat.setText(
                "已加载 %d 个动作 · 估算占用 %.1f MB · 缓存上限 %d 个\n"
                "磁盘上检测到 %d 个帧动作目录%s"
                % (n, mb, _FRAME_CACHE.max_items, len(avail),
                   ("：" + "、".join(avail[:6]) + ("…" if len(avail) > 6 else "")) if avail else ""))
        except Exception:
            pass

    def _scan_frame_actions(self):
        """扫描 sprites/anim 下有哪些动作目录。"""
        out = []
        try:
            if os.path.isdir(FRAME_ANIM_DIR):
                for name in sorted(os.listdir(FRAME_ANIM_DIR)):
                    p2 = os.path.join(FRAME_ANIM_DIR, name)
                    if not os.path.isdir(p2):
                        continue
                    if any(os.path.splitext(f)[1].lower() in FRAME_ANIM_EXTS
                           for f in os.listdir(p2)):
                        out.append(name)
        except Exception:
            pass
        return out

    def _set_anim_weight(self, key, value):
        w = self.app.cfg.get("animation_weights")
        if not isinstance(w, dict):
            w = {}
        w[key] = max(0, min(50, float(value)))
        self.app.cfg["animation_weights"] = w
        self.app.save_cfg()

    def _reset_anim(self):
        self.app.cfg["animation_weights"] = dict(ACTION_CATEGORY_WEIGHTS)
        self.app.cfg["animation_mode"] = "auto"
        self.app.cfg["animation_tiers"] = dict(ANIMATION_TIERS_DEFAULT)
        self.app.cfg["turn_strength"] = TURN_STRENGTH_DEFAULT
        self.app.cfg["action_strength"] = ACTION_STRENGTH_DEFAULT
        self.app.save_cfg()
        self.reload()
        self.app.say_main("动画参数已恢复默认")

    # ---------- 提醒页 ----------
    def _build_reminder_page(self, lay):
        ex = self.app._rem_extras()

        self._title(lay, "提醒",
                    "每类可单独设间隔；到点会停下来播动作并放大气泡。"
                    "免打扰时段内不弹。")

        # ---- 三类开关 + 独立间隔 ----
        self._rem_boxes = {}
        self._rem_spins = {}
        for kind, label in REMINDERS.items():
            row = QHBoxLayout()
            cb = QCheckBox(label)
            cb.setChecked(self.app._rem_on(kind))
            cb.toggled.connect(lambda on, k=kind: self.app.set_reminder_on(k, on))
            row.addWidget(cb)
            row.addStretch(1)
            row.addWidget(QLabel("每"))
            sp = QSpinBox()
            sp.setRange(1, 600)
            sp.setValue(self.app._rem_minutes(kind))
            sp.setSuffix(" 分钟")
            sp.setFixedWidth(96)
            sp.valueChanged.connect(lambda v, k=kind: self.app.set_reminder_min(k, int(v)))
            row.addWidget(sp)
            self._rem_boxes[kind] = cb
            self._rem_spins[kind] = sp
            lay.addLayout(row)

        # ---- 本页功能开关（从「功能开关」页移入）----
        self._feature_boxes["remind_todo"] = self._check(
            lay, "待办提醒", self.app.is_enabled("remind_todo"),
            lambda on: self.app.set_feature("remind_todo", bool(on)),
            "到点提醒待办事项；聊天里说「提醒我 X 分钟后 xxx」也能加")
        self._feature_boxes["remind_schedule"] = self._check(
            lay, "定时提醒", self.app.is_enabled("remind_schedule"),
            lambda on: self.app.set_feature("remind_schedule", bool(on)),
            "固定时刻提醒（如 12:00 吃饭），可设每天 / 工作日 / 仅一次")
        urow = QHBoxLayout()
        urow.addSpacing(22)
        uni = QPushButton("全部统一设置…")
        uni.clicked.connect(self._unify_reminder_interval)
        urow.addWidget(uni)
        for lbl, k in (("测试喝水", "water"), ("测试起身", "stand"), ("测试护眼", "eyes")):
            test = QPushButton(lbl)
            test.clicked.connect(lambda _=False, kk=k: self.app._fire_reminder(kk))
            urow.addWidget(test)
        urow.addStretch(1)
        lay.addLayout(urow)

        # ---- 今日已完成 ----
        line0 = QFrame()
        line0.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line0)
        dt = self.app._done_today()
        self._rem_done_label = QLabel(
            "今日已确认：喝水 %d 次 · 起身 %d 次 · 护眼 %d 次 · 待办 %d 条"
            % (dt.get("water", 0), dt.get("stand", 0), dt.get("eyes", 0), dt.get("todo", 0)))
        self._rem_done_label.setStyleSheet("color:#5686fe;font-size:11px;margin-left:2px;")
        lay.addWidget(self._rem_done_label)

        # ---- 气泡 / 提示音 ----
        line1 = QFrame()
        line1.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line1)
        self._feature_boxes["remind_walk"] = self._check(
            lay, "提醒时走到屏幕中间", self.app.is_enabled("remind_walk"),
            lambda on: self.app.set_feature("remind_walk", bool(on)),
            "关闭后原地提醒，不移动宠物")


        # ---- 免打扰 ----
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line2)
        dnd = ex.get("dnd") or {}
        self._feature_boxes["remind_dnd"] = self._check(
            lay, "免打扰时段", bool(dnd.get("enabled", True)),
            lambda on: self._set_dnd("enabled", bool(on)),
                    "期间提醒静音且不弹气泡，到点后不补发；检测到全屏/演示模式也会自动暂停")
        drow = QHBoxLayout()
        drow.addSpacing(22)
        drow.addWidget(QLabel("从"))
        self.dnd_start = QLineEdit(dnd.get("start", "22:00"))
        self.dnd_start.setFixedWidth(72)
        self.dnd_start.editingFinished.connect(
            lambda: self._set_dnd("start", self.dnd_start.text()))
        drow.addWidget(self.dnd_start)
        drow.addWidget(QLabel("到"))
        self.dnd_end = QLineEdit(dnd.get("end", "08:00"))
        self.dnd_end.setFixedWidth(72)
        self.dnd_end.editingFinished.connect(
            lambda: self._set_dnd("end", self.dnd_end.text()))
        drow.addWidget(self.dnd_end)
        drow.addStretch(1)
        lay.addLayout(drow)

        # ---- 待办 ----
        line3 = QFrame()
        line3.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line3)
        self._title(lay, "待办提醒",
                    "最多 %d 条；到点用同样的方式提醒。聊天里说「提醒我 10 分钟后 xxx」也能加。"
                    % TODO_MAX)
        self.todo_list = QListWidget()
        self.todo_list.setFixedHeight(110)
        lay.addWidget(self.todo_list)
        trow = QHBoxLayout()
        for text, fn in (("添加", self._add_todo_dialog),
                         ("编辑", self._edit_todo_dialog),
                         ("删除", self._remove_todo_dialog),
                         ("启用/停用", self._toggle_todo_dialog)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            trow.addWidget(b)
        trow.addStretch(1)
        lay.addLayout(trow)

        # ---- 自定义定时 ----
        line4 = QFrame()
        line4.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line4)
        self._title(lay, "定时提醒",
                    "固定时刻提醒（如 12:00 吃饭）；可设每天 / 工作日 / 仅一次，台词留空用默认。")
        self.sch_list = QListWidget()
        self.sch_list.setFixedHeight(100)
        lay.addWidget(self.sch_list)
        srow = QHBoxLayout()
        for text, fn in (("添加", self._add_schedule_dialog),
                         ("删除", self._remove_schedule_dialog),
                         ("启用/停用", self._toggle_schedule_dialog)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            srow.addWidget(b)
        srow.addStretch(1)
        lay.addLayout(srow)

        self._refresh_remind_lists()
        lay.addStretch(1)

    # ---- 提醒页辅助 ----
    def _set_remind_extra(self, key, value):
        ex = self.app._rem_extras()
        ex[key] = value
        self.app.save_cfg()

    def _set_dnd(self, key, value):
        ex = self.app._rem_extras()
        dnd = ex.get("dnd")
        if not isinstance(dnd, dict):
            dnd = dict(DND_DEFAULT)
        if key in ("start", "end"):
            value = _valid_hhmm(value, dnd.get(key, DND_DEFAULT[key]))
            if key == "start":
                self.dnd_start.setText(value)
            else:
                self.dnd_end.setText(value)
        dnd[key] = value
        ex["dnd"] = dnd
        # 与功能开关同步（is_enabled 读的是 features.remind_dnd）
        if key == "enabled":
            self.app.cfg.setdefault("features", {})["remind_dnd"] = bool(value)
        self.app.save_cfg()

    def _unify_reminder_interval(self):
        cur = self.app._rem_minutes("water")
        v, ok = QInputDialog.getInt(self, "全部统一设置", "所有提醒统一为多少分钟？",
                                    cur, 1, 600, 5)
        if ok:
            self.app.set_all_reminder_min(v)
            self.reload()

    def _refresh_remind_lists(self):
        try:
            ex = self.app._rem_extras()
            self.todo_list.clear()
            for t in (ex.get("todos") or []):
                mark = "✓ " if t.get("done") else ("" if t.get("enabled", True) else "⏸ ")
                rep = dict((k, l) for l, k in TODO_REPEATS).get(t.get("repeat"), "")
                self.todo_list.addItem("%s%s  [%s %s]" % (mark, t.get("text"),
                                                         rep, t.get("at", "")))
            self.sch_list.clear()
            for it in (ex.get("schedules") or []):
                mark = "" if it.get("enabled", True) else "⏸ "
                rep = dict((k, l) for l, k in TODO_REPEATS).get(it.get("repeat"), "")
                self.sch_list.addItem("%s%s  [%s %s]" % (mark, it.get("at"),
                                                        rep, it.get("name", "")))
        except Exception as ex:
            log("刷新待办/定时列表失败: %s" % ex, logging.WARNING)

    def _sel_todo(self):
        ex = self.app._rem_extras()
        todos = ex.get("todos") or []
        r = self.todo_list.currentRow()
        return todos[r] if 0 <= r < len(todos) else None

    def _sel_schedule(self):
        ex = self.app._rem_extras()
        sch = ex.get("schedules") or []
        r = self.sch_list.currentRow()
        return sch[r] if 0 <= r < len(sch) else None

    def _add_todo_dialog(self):
        text, ok = QInputDialog.getText(self, "添加待办", "待办内容：")
        if not ok or not text.strip():
            return
        mode, ok2 = QInputDialog.getItem(
            self, "提醒时间", "什么时候提醒？",
            ["几分钟后", "今天某时刻", "每天某时刻"], 0, False)
        if not ok2:
            return
        if mode == "几分钟后":
            mins, ok3 = QInputDialog.getInt(self, "几分钟后", "多少分钟后提醒？", 10, 1, 600, 5)
            if not ok3:
                return
            due = time.time() + mins * 60
            at = datetime.datetime.fromtimestamp(due).strftime("%H:%M")
            self.app.add_todo(text.strip(), due, "once", at)
        else:
            at, ok3 = QInputDialog.getText(self, "时刻", "时刻（HH:MM）：", text="12:00")
            if not ok3:
                return
            at = _valid_hhmm(at, "12:00")
            repeat = "once" if mode == "今天某时刻" else "daily"
            if mode == "今天某时刻":
                try:
                    h, m = [int(x) for x in at.split(":")]
                    due = datetime.datetime.now().replace(
                        hour=h, minute=m, second=0, microsecond=0).timestamp()
                    if due <= time.time():
                        due += 86400
                except Exception:
                    due = time.time() + 3600
            else:
                due = self.app._next_due(at, "daily")
            self.app.add_todo(text.strip(), due, repeat, at)
        self._refresh_remind_lists()

    def _edit_todo_dialog(self):
        t = self._sel_todo()
        if not t:
            QMessageBox.information(self, "编辑待办", "先选中一条待办")
            return
        text, ok = QInputDialog.getText(self, "编辑待办", "内容：",
                                        QLineEdit.EchoMode.Normal, t.get("text", ""))
        if ok and text.strip():
            t["text"] = text.strip()[:100]
            self.app.save_cfg()
            self._refresh_remind_lists()

    def _remove_todo_dialog(self):
        t = self._sel_todo()
        if not t:
            return
        self.app.remove_todo(t.get("id"))
        self._refresh_remind_lists()

    def _toggle_todo_dialog(self):
        t = self._sel_todo()
        if not t:
            return
        self.app.toggle_todo(t.get("id"))
        self._refresh_remind_lists()

    def _add_schedule_dialog(self):
        name, ok = QInputDialog.getText(self, "添加定时提醒", "名称（如：吃饭）：")
        if not ok or not name.strip():
            return
        at, ok2 = QInputDialog.getText(self, "时刻", "每天几点（HH:MM）：", text="12:00")
        if not ok2:
            return
        at = _valid_hhmm(at, "12:00")
        rep, ok3 = QInputDialog.getItem(self, "重复", "重复方式：",
                                        [l for l, _k in TODO_REPEATS], 0, False)
        if not ok3:
            return
        rk = dict((l, k) for l, k in TODO_REPEATS).get(rep, "daily")
        line, ok4 = QInputDialog.getText(self, "台词", "提醒台词（留空用默认）：")
        if not ok4:
            line = ""
        self.app.add_schedule(name.strip(), at, rk, line.strip())
        self._refresh_remind_lists()

    def _remove_schedule_dialog(self):
        it = self._sel_schedule()
        if not it:
            return
        self.app.remove_schedule(it.get("id"))
        self._refresh_remind_lists()

    def _toggle_schedule_dialog(self):
        it = self._sel_schedule()
        if not it:
            return
        self.app.toggle_schedule(it.get("id"))
        self._refresh_remind_lists()

    # ---------- 余额页 ----------
    def _build_balance_page(self, lay):
        self._title(lay, "余额",
                    "查询 https://api.deepseek.com/user/balance；档位按「满额基准」折算已用百分比。")
        bc = self.app._balance_cfg()
        self._check(lay, "自动查询余额", bool(bc["auto"]),
                    lambda on: self.app.set_balance_auto(on),
                    "默认关闭；开启后按下面间隔自动查询")
        self._balance_interval = self._slider_row(
            lay, "查询间隔", bc["interval_min"], 30, 720, 5, None,
            lambda v: self.app.set_balance_interval(int(v)), "分钟")

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line)
        self._balance_full = self._slider_row(
            lay, "满额基准", self.app.cfg.get("balance_full", BALANCE_FULL),
            5.0, 500.0, 5.0, None,
            lambda v: self._set_balance_full(v), "元")

        self._balance_hint = QLabel("")
        self._balance_hint.setStyleSheet("color:#8a8f9c;font-size:11px;")
        lay.addWidget(self._balance_hint)
        btn = QPushButton("立即查询一次")
        btn.clicked.connect(lambda _=False: self.app.query_balance(True))
        lay.addWidget(btn)
        lay.addStretch(1)
        self._refresh_balance_hint()

    def _set_balance_full(self, value):
        self.app.cfg["balance_full"] = max(1.0, float(value))
        self.app.save_cfg()
        self._refresh_balance_hint()

    def _refresh_balance_hint(self):
        full = float(self.app.cfg.get("balance_full", BALANCE_FULL) or BALANCE_FULL)
        parts = []
        for limit, name, _action, _lines in BALANCE_TIERS:
            parts.append("%s<%.0f%%" % (name, limit))
        self._balance_hint.setText(
            "满额按 ¥%.0f 折算；当前档位划分：%s" % (full, "、".join(parts)))

    # ---------- 聊天页 ----------
    def _build_chat_page(self, lay):
        self._title(lay, "聊天",
                    "回复长度与单次输出上限。风格只改 system 末尾那一句固定文字，"
                    "同一风格下前缀缓存稳定；切换风格时缓存失效一次属正常。")

        self._combo_row(
            lay, "回复风格",
            [(label, key) for label, key in REPLY_STYLE_CHOICES],
            self.app.cfg.get("reply_style", REPLY_STYLE_DEFAULT),
            lambda v: self._set_reply_style(str(v)))

        self._style_hint = QLabel("")
        self._style_hint.setWordWrap(True)
        self._style_hint.setStyleSheet("color:#8a8f9c;font-size:11px;margin-left:22px;")
        lay.addWidget(self._style_hint)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line)

        self._max_tokens_row = self._slider_row(
            lay, "单次上限", self.app.cfg.get("max_tokens", MAX_TOKENS_DEFAULT),
            MAX_TOKENS_MIN, MAX_TOKENS_MAX, 50, None,
            lambda v: self._set_max_tokens(int(v)), "tokens")

        hint2 = QLabel("max_tokens 是模型单次最多输出多少 token（中文约 1.5 字/token）。"
                       "回复风格决定「说多长」，这个值决定「最多能说多长」。")
        hint2.setWordWrap(True)
        hint2.setStyleSheet("color:#8a8f9c;font-size:11px;margin-left:22px;")
        lay.addWidget(hint2)

        # ---- 人设编辑 ----
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line2)
        self._title(lay, "人设",
                    "这段会作为 system 的开头发给模型。固定不变才有利于前缀缓存；"
                    "改完缓存会失效一次，建议新开一个对话。")

        self.persona_edit = QTextEdit()
        self.persona_edit.setFixedHeight(150)
        self.persona_edit.setPlaceholderText("留空则使用内置默认人设")
        self.persona_edit.setStyleSheet(
            "QTextEdit{background:#ffffff;border:1px solid #dfe3ec;border-radius:8px;"
            "padding:8px;font-family:'Microsoft YaHei UI';font-size:12px;}")
        self.persona_edit.setPlainText(self.app.cfg.get("persona") or DEFAULT_PERSONA)
        lay.addWidget(self.persona_edit)

        prow = QHBoxLayout()
        save_btn = QPushButton("保存人设")
        save_btn.clicked.connect(self._save_persona)
        prow.addWidget(save_btn)
        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(self._reset_persona)
        prow.addWidget(reset_btn)
        self._persona_state = QLabel("")
        self._persona_state.setStyleSheet("color:#8a8f9c;font-size:11px;")
        prow.addWidget(self._persona_state, 1)
        lay.addLayout(prow)

        self._persona_hint = QLabel("")
        self._persona_hint.setWordWrap(True)
        self._persona_hint.setStyleSheet("color:#d08a00;font-size:11px;margin-left:2px;")
        lay.addWidget(self._persona_hint)

        self._chat_preview = QLabel("")
        self._chat_preview.setWordWrap(True)
        self._chat_preview.setStyleSheet(
            "color:#5a6070;font-size:11px;margin-top:6px;"
            "background:#f5f7fb;border-radius:6px;padding:8px;")
        lay.addWidget(self._chat_preview)

        lay.addStretch(1)
        self._refresh_chat_hint()

    def _save_persona(self):
        """保存人设到 config.json。留空/与默认一致时存空串（用内置默认）。"""
        text = (self.persona_edit.toPlainText() or "").strip()
        if not text or text == DEFAULT_PERSONA.strip():
            self.app.cfg["persona"] = ""
        else:
            self.app.cfg["persona"] = text[:4000]
        self.app.save_cfg()
        self._persona_hint.setText("已保存。前缀缓存会失效一次，建议新开一个对话。")
        self._persona_state.setText("自定义" if self.app.cfg["persona"] else "默认")
        self.app.say_main("人设已保存")
        self._refresh_chat_hint()
        log("[聊天] 人设已更新（%d 字）" % len(text))

    def _reset_persona(self):
        """恢复默认人设：清空配置字段 + 编辑框回填默认文本。"""
        self.app.cfg["persona"] = ""
        self.app.save_cfg()
        self.persona_edit.setPlainText(DEFAULT_PERSONA)
        self._persona_hint.setText("已恢复默认人设。前缀缓存会失效一次，建议新开一个对话。")
        self._persona_state.setText("默认")
        self.app.say_main("人设已恢复默认")
        self._refresh_chat_hint()

    def _set_reply_style(self, style):
        if style not in REPLY_STYLES:
            return
        self.app.cfg["reply_style"] = style
        self.app.save_cfg()
        label = dict((k, l) for l, k in REPLY_STYLE_CHOICES).get(style, style)
        self.app.say_main("回复风格：%s" % label)
        self._refresh_chat_hint()
        log("[聊天] 回复风格 -> %s" % style)

    def _set_max_tokens(self, value):
        v = max(MAX_TOKENS_MIN, min(MAX_TOKENS_MAX, int(value)))
        self.app.cfg["max_tokens"] = v
        self.app.save_cfg()
        self._refresh_chat_hint()

    def _refresh_chat_hint(self):
        try:
            style = self.app.cfg.get("reply_style", REPLY_STYLE_DEFAULT)
            suffix = reply_style_suffix(style)
            mt = int(self.app.cfg.get("max_tokens", MAX_TOKENS_DEFAULT))
            self._style_hint.setText(
                "当前风格下，system 末尾会追加：%s"
                % (("「" + suffix + "」") if suffix else "（不追加，不限长度）"))
            _persona = persona_from_cfg(self.app.cfg)
            self._persona_state.setText("自定义" if (self.app.cfg.get("persona") or "").strip()
                                        else "默认")
            self._chat_preview.setText(
                "完整 system 正文：" + build_system_prompt(style, _persona)
                + "\n\n单次输出上限：%d tokens（约 %d 字）" % (mt, int(mt * 1.5)))
        except Exception:
            pass


    # ---------- 通用页 ----------
    def _build_general_page(self, lay):
        self._title(lay, "通用", "Key、生日、城市、窗口行为与开机自启。")
        row = QHBoxLayout()
        b1 = QPushButton("设置 Key")
        b1.clicked.connect(self.app._set_key_dialog)
        b2 = QPushButton("设置生日")
        b2.clicked.connect(self.app._set_birthday_dialog)
        b3 = QPushButton("设置城市")
        b3.clicked.connect(self._set_city_dialog)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addWidget(b3)
        row.addStretch(1)
        lay.addLayout(row)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line)
        cfg = self.app.cfg
        self._check(lay, "窗口置顶", bool(cfg.get("topmost", True)),
                    lambda on: self.app.set_topmost(on))
        self._check(lay, "鼠标穿透（点不到它）", bool(cfg.get("passthrough", False)),
                    lambda on: self.app.set_passthrough(on),
                    "开启后只能从托盘菜单取消")
        self._check(lay, "开机自启", bool(cfg.get("autostart", False)),
                    lambda on: self.app.set_autostart(on))
        row2 = QHBoxLayout()
        row2.addSpacing(96)
        tv = QPushButton("显示/隐藏宠物")
        tv.clicked.connect(lambda _=False: self.app.toggle_visible(self.app.main_pet))
        row2.addWidget(tv)
        snap = QPushButton("回到屏幕内")
        snap.clicked.connect(lambda _=False: self.app.main_pet.snap_into_screen())
        row2.addWidget(snap)
        row2.addStretch(1)
        lay.addLayout(row2)
        lay.addStretch(1)

    def _set_city_dialog(self):
        cur = self.app.cfg.get("city", "汕头")
        text, ok = QInputDialog.getText(self, "设置城市", "天气查询用的城市名:",
                                        QLineEdit.EchoMode.Normal, cur)
        if ok and text.strip():
            self.app.cfg["city"] = text.strip()
            self.app.save_cfg()
            self.app.say_main("城市已设置为%s" % text.strip())

    # ---------- 记忆页 ----------
    def _build_memory_page(self, lay):
        self._title(lay, "记忆", "长期记忆会拼在系统提示词末尾，影响所有会话。")
        row = QHBoxLayout()
        b = QPushButton("打开记忆管理")
        b.clicked.connect(self.app._open_memory)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self._memory_count = QLabel("")
        self._memory_count.setStyleSheet("color:#8a8f9c;font-size:11px;")
        lay.addWidget(self._memory_count)
        lay.addStretch(1)

    # ---------- 关于页 ----------
    def _build_about_page(self, lay):
        self._title(lay, "关于", None)
        info = QLabel(
            "大肥鱼桌宠 · DeepSeek 鲸鱼娘二创桌面宠物\n\n"
            "聊天模型：deepseek-chat（流式）\n"
            "余额接口：api.deepseek.com/user/balance\n"
            "天气接口：wttr.in\n\n"
            "配置文件：config.json\n"
            "聊天记录：chats.json\n"
            "长期记忆：memory.json\n"
            "运行日志：error.log")
        info.setWordWrap(True)
        info.setStyleSheet("color:#5a6070;font-size:12px;")
        lay.addWidget(info)
        row = QHBoxLayout()
        b = QPushButton("打开程序目录")
        b.clicked.connect(self._open_app_dir)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)

    def _open_app_dir(self):
        try:
            os.startfile(APP_DIR)
        except Exception as ex:
            QMessageBox.warning(self, "打开目录", "打不开：%s" % ex)

    # ---------- 刷新 ----------
    def reload(self):
        """重新读取配置并刷新所有控件（外部改了配置后调用）。"""
        for key, cb in getattr(self, "_feature_boxes", {}).items():
            cb.blockSignals(True)
            cb.setChecked(self.app.is_enabled(key))
            cb.blockSignals(False)

        # 宠物页统一走 refresh_pet_page（列表与按钮共用同一套状态逻辑）
        self.refresh_pet_page()

        try:
            self._memory_count.setText("当前共 %d 条记忆" % len(self.app.memory.items))
        except Exception:
            pass
        self._refresh_balance_hint()

    def show_page(self, page=None):
        if page:
            self._select_page(page)
        self.reload()
        self.show()
        self.raise_()
        self.activateWindow()


class PetApp(QObject):
    # 宠物显示状态变化（pet_id, state, reason）——设置窗口监听后刷新列表
    pet_state_changed = Signal(str, str, str)
    """全局唯一的东西：配置、托盘、菜单、聊天、记忆、提醒、节日、截屏守卫、
    系统监控、小游戏、用量。持有 1~N 个 PetWindow。"""

    def __init__(self):
        self.cfg, migrated = load_config_safe()
        super().__init__()
        self.pets = []
        self.main_pet = None
        self.chat_window = None
        self.memory_dialog = None
        self.rps_panel = None
        self.catch_win = None
        self._chat_paused = False
        self.game_active = False
        self.game_had_pet_visible = True
        self._capture_hidden = False
        self._game_hidden = False
        self._last_festival_hour = None
        self._proc_name_cache = {}
        self._last_pick = {}
        self._rem_next = {}
        self._t = 0
        self.last_system_check = 0
        # 余额查询 / 时节特效状态
        self.balance_worker = None
        self.settings_window = None
        self._work_state = "idle"
        self._work_hold_timer = QTimer()
        self._work_hold_timer.setSingleShot(True)
        self._work_hold_timer.timeout.connect(self._work_hold_timeout)
        self._fx_last_auto = None
        self._fx_last_auto_time = 0.0
        self.tray = None
        self.icon = QIcon(os.path.join(SPRITE_DIR, "icon.png"))
        self.memory = MemoryStore(MEMORY_PATH)

        if migrated:
            self.save_cfg()

        # 宠物
        for pet_cfg in self.cfg["pets"]:
            w = PetWindow(self, pet_cfg)
            self.pets.append(w)
        self.main_pet = self.get_pet("main") or self.pets[0]

        # 提醒（功能关闭时定时器不启动，不占资源）
        self._reset_reminder_clock()
        self._rem_timer = QTimer()
        self._rem_timer.timeout.connect(self._check_reminders)
        if self.is_enabled("reminders"):
            self._rem_timer.start(1000)

        # 系统监控（10 秒节流在方法内）
        self._sys_timer = QTimer()
        self._sys_timer.timeout.connect(self._sys_tick)
        if self.is_enabled("system_monitor"):
            self._sys_timer.start(TICK)

        # 小游戏面板
        self.rps_panel = RpsPanel(self)

        # 节日台词：启动 3 秒后说一次，之后每个整点说一次
        self._festival_timer = QTimer()
        self._festival_timer.timeout.connect(self._festival_hourly_check)
        if self.is_enabled("festival_lines"):
            QTimer.singleShot(3000, self._festival_startup_say)
            self._festival_timer.start(20000)

        # 截屏时自动隐藏
        self._capture_timer = QTimer()
        self._capture_timer.timeout.connect(self._capture_guard_tick)
        if self.is_enabled("capture_hide"):
            self._capture_timer.start(450)

        # 托盘
        # 自动查询余额（默认关，间隔最少 30 分钟）
        self._balance_timer = QTimer()
        self._balance_timer.setInterval(BALANCE_MIN_INTERVAL_MIN * 60 * 1000)
        self._balance_timer.timeout.connect(self._balance_tick)
        _bc = self._balance_cfg()
        if _bc["auto"] and self.is_enabled("balance_auto"):
            self._balance_timer.start(max(60, _bc["interval_min"] * 60) * 1000)

        # 时节特效：启动按日期触发一次，之后每 10 分钟看一次（带冷却）
        self._fx_timer = QTimer()
        self._fx_timer.timeout.connect(self._fx_auto_tick)
        if self.is_enabled("seasonal_fx"):
            QTimer.singleShot(1500, self._fx_startup_say)
            self._fx_timer.start(10 * 60 * 1000)

        self.tray = QSystemTrayIcon(self.icon)
        self.tray.setContextMenu(self.build_tray_menu())
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    # ---------- 宠物集合 ----------
    def get_pet(self, pet_id):
        for p in self.pets:
            if p.pet_id == pet_id:
                return p
        return None

    def say_main(self, text, inner=False):
        """聊天/提醒/节日/系统播报只由 main 显示"""
        if self.main_pet is not None:
            self.main_pet.say(text, inner=inner)

    # ---------- 配置 ----------
    def save_cfg(self):
        try:
            for w in self.pets:
                if w.isVisible():
                    w.pet["x"], w.pet["y"] = w.x(), w.y()
            data = dict(self.cfg)
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            os.replace(tmp, CONFIG_PATH)
        except Exception as ex:
            log("保存配置失败: %s" % ex, logging.ERROR)

    def add_usage(self, usage):
        """把本次 usage 累加进 config.json（缓存命中/未命中/回复/合计/次数）"""
        try:
            u = self.cfg.setdefault("usage", _default_usage())
            for k in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
                      "completion_tokens", "total_tokens"):
                u[k] = int(u.get(k, 0)) + int(usage.get(k) or 0)
            u["requests"] = int(u.get("requests", 0)) + 1
            self.save_cfg()
        except Exception as ex:
            log("累计用量失败: %s" % ex, logging.ERROR)

    # chat_paused 兼容：ChatWindow 直接读写它
    @property
    def chat_paused(self):
        return self._chat_paused

    @chat_paused.setter
    def chat_paused(self, v):
        self._chat_paused = bool(v)

    def say(self, text, inner=False):
        self.say_main(text, inner=inner)

    def _save_cfg(self):
        self.save_cfg()

    def set_reminder_on(self, kind, on):
        self.cfg["reminders"][kind]["on"] = bool(on)
        self.save_cfg()
        if on:
            self._rem_next[kind] = time.time() + self._rem_minutes(kind) * 60
            self.say_main(f"{REMINDERS[kind]}已打开（每 {self._rem_minutes(kind)} 分钟）")
        else:
            self.say_main(f"{REMINDERS[kind]}已关闭")

    def set_reminder_interval(self, mins):
        mins = max(1, int(mins))
        for kind in REMINDERS:
            self.cfg["reminders"][kind]["min"] = mins
        self.save_cfg()
        self._reset_reminder_clock()
        self.say_main(f"提醒间隔改成 {mins} 分钟啦")

    def _custom_reminder_interval(self):
        cur = self._rem_minutes("water")
        mins, ok = QInputDialog.getInt(self.main_pet, "提醒间隔", "多少分钟提醒一次？", cur, 1, 600, 5)
        if ok:
            self.set_reminder_interval(mins)

    # ---------- 提醒（第 18 批重做） ----------
    def _rem_extras(self):
        """取 remind_extras（缺失时补默认，保证旧配置也能用）。"""
        ex = self.cfg.get("remind_extras")
        if not isinstance(ex, dict):
            ex = _default_remind_extras()
            self.cfg["remind_extras"] = ex
        return ex

    def _rem_minutes(self, kind):
        try:
            return max(1, int(self.cfg["reminders"][kind]["min"]))
        except Exception:
            return DEFAULT_REMIND_MIN_BY_KIND.get(kind, DEFAULT_REMINDS_MIN)

    def _rem_on(self, kind):
        try:
            return bool(self.cfg["reminders"][kind]["on"])
        except Exception:
            return True

    def set_reminder_min(self, kind, mins):
        """设置某一类的独立间隔。"""
        mins = max(1, min(600, int(mins)))
        if kind not in REMINDERS:
            return
        self.cfg["reminders"][kind]["min"] = mins
        self.save_cfg()
        self._rem_next[kind] = time.time() + mins * 60
        self.say_main("%s 间隔改成 %d 分钟" % (REMINDERS[kind], mins))

    def set_all_reminder_min(self, mins):
        """全部统一设置（快捷项）。"""
        mins = max(1, min(600, int(mins)))
        for k in REMINDERS:
            self.cfg["reminders"][k]["min"] = mins
        self.save_cfg()
        self._reset_reminder_clock()
        self.say_main("全部提醒间隔改成 %d 分钟" % mins)

    # ---- 免打扰 ----
    def _in_dnd(self, now=None):
        """当前是否处于免打扰时段（支持跨午夜，如 22:00~08:00）。

        features.remind_dnd 与 remind_extras.dnd.enabled 任一关闭都不启用；
        勾选框（_set_dnd）会同时写两处，保持一致。
        """
        if not self.is_enabled("remind_dnd"):
            return False
        dnd = self._rem_extras().get("dnd") or {}
        if not dnd.get("enabled", True):
            return False
        try:
            cur = now or datetime.datetime.now()
            nowm = cur.hour * 60 + cur.minute
            sh, sm = [int(x) for x in (dnd.get("start") or "22:00").split(":")]
            eh, em = [int(x) for x in (dnd.get("end") or "08:00").split(":")]
            start, end = sh * 60 + sm, eh * 60 + em
        except Exception:
            return False
        if start == end:
            return False
        if start < end:
            return start <= nowm < end
        return nowm >= start or nowm < end      # 跨午夜

    def _notification_paused(self):
        """系统层面是否该暂停提醒：全屏程序 / 演示模式 / 小游戏。"""
        if self.game_active:
            return True
        if not self.is_enabled("remind_dnd"):
            return False
        try:
            # QUNS_BUSY(2) / QUNS_RUNNING_D3D_FULL_SCREEN(3) / QUNS_PRESENTATION_MODE(4)
            state = ctypes.c_int(0)
            hr = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
            if hr == 0 and state.value in (2, 3, 4):
                return True
        except Exception:
            pass
        return False

    def _remind_blocked(self):
        """返回 (是否阻塞, 原因)。"""
        if not self.is_enabled("reminders"):
            return True, "功能关闭"
        if self.game_active:
            return True, "小游戏中"
        if self._notification_paused():
            return True, "全屏/演示模式"
        if self._in_dnd():
            return True, "免打扰时段"
        return False, ""

    # ---- 今日计数 ----
    def _done_today(self):
        """取今日计数；跨天自动清零。"""
        ex = self._rem_extras()
        dt = ex.get("done_today")
        if not isinstance(dt, dict):
            dt = {"date": "", "water": 0, "stand": 0, "eyes": 0, "todo": 0}
            ex["done_today"] = dt
        today = datetime.date.today().strftime("%Y-%m-%d")
        if dt.get("date") != today:
            dt["date"] = today
            for k in ("water", "stand", "eyes", "todo"):
                dt[k] = 0
            self.save_cfg()
        return dt

    def _bump_done(self, kind):
        dt = self._done_today()
        key = kind if kind in ("water", "stand", "eyes") else "todo"
        dt[key] = int(dt.get(key, 0)) + 1
        self.save_cfg()

    # ---- 核心：触发一次提醒 ----
    def _fire_reminder(self, kind, text=None, title=None, icon=None, todo_id=None,
                       schedule_id=None):
        """触发一次提醒：走到屏幕中间 → 动作 + 气泡 + 提示音 + 系统通知。"""
        if not self.is_enabled("reminders"):
            log("[提醒] %s 被跳过（功能关闭）" % kind)
            return False
        if self.game_active:
            log("[提醒] %s 被跳过（小游戏中）" % kind)
            return False
        if self._in_dnd():
            log("[提醒] %s 被跳过（免打扰时段）" % kind)
            return False

        if text is None:
            pool = REMIND_LINES.get(kind) or ["该休息一下了。"]
            text = self._pick_line(pool, "remind_" + kind)
        icon = icon or REMIND_ICONS.get(kind, REMIND_ICON_CLOCK)
        log("[提醒] %s -> %s" % (REMINDERS.get(kind, kind), text))

        pet = self.main_pet
        if pet is None:
            return False

        # 全屏 / 演示模式：不移动、不弹气泡，跳过本轮
        if self._notification_paused():
            log("[提醒] %s 全屏/演示模式 → 跳过" % kind)
            return False

        # 记下提醒前位置（回位用）
        try:
            pet.remind_origin = (pet.x(), pet.y())
            pet.remind_user_moved = False
        except Exception:
            pet.remind_origin = None

        ctx = {"text": text, "icon": icon, "kind": kind,
               "todo_id": todo_id, "schedule_id": schedule_id}

        # 被拖拽中：不移动，直接就地提醒
        if getattr(pet, "dragging", False):
            self._begin_remind_at_pet(pet, ctx)
            return True

        # 隐藏中 → 先显示
        try:
            if not pet.isVisible():
                pet.show()
                pet.raise_()
        except Exception as ex:
            log("[提醒] 显示宠物失败: %s" % ex, logging.WARNING)

        # 功能开关关闭：不移动，就地提醒（动作照常播）
        if not self.is_enabled("remind_walk"):
            self._begin_remind_at_pet(pet, ctx, animate=True)
            return True

        # 走到鼠标所在显示器的水平居中、垂直偏下
        target = self._remind_center_target(pet)
        if target is None:
            self._begin_remind_at_pet(pet, ctx)
            return True
        tx, ty = target

        # 减少动效：直接放到中间（不播走动），但**提醒动作照常播**
        if not pet._anim_enabled():
            try:
                pet.move(int(tx), int(ty))
                pet._clamp_into_work_area()
                pet.raise_()
            except Exception:
                pass
            self._begin_remind_at_pet(pet, ctx, animate=True)
            return True

        # 平滑走过去（侧面朝向），到达后开始提醒
        pet.remind_walk_to(tx, ty, on_done=lambda c=ctx: self._begin_remind_at_pet(
            pet, c, animate=True))
        return True

    def _remind_center_target(self, pet):
        """算提醒目标位置：鼠标所在显示器可用区的水平居中、垂直偏下。"""
        try:
            screen = QApplication.screenAt(QCursor.pos()) or pet.screen() \
                or QApplication.primaryScreen()
            geo = screen.availableGeometry()
            w, h = pet.width(), pet.height()
            # 窗口左上角目标：水平居中
            tx = geo.left() + (geo.width() - w) / 2.0
            # 垂直偏下：可用区高度 * bias 处作为窗口中心，再转成左上角
            cy = geo.top() + geo.height() * REMIND_VERTICAL_BIAS
            ty = cy - h / 2.0
            # 夹进可用区（不盖住任务栏、完整显示）
            tx = max(geo.left(), min(geo.right() - w, tx))
            ty = max(geo.top(), min(geo.bottom() - h, ty))
            return int(round(tx)), int(round(ty))
        except Exception as ex:
            log("计算提醒位置失败: %s" % ex, logging.WARNING)
            return None

    def _begin_remind_at_pet(self, pet, ctx, animate=True):
        """宠物已就位：播动作 + 气泡 + 提示音 + 系统通知。"""
        text = ctx["text"]
        icon = ctx["icon"]
        kind = ctx["kind"]
        try:
            pet._interrupt_combo()
            pet.target = None
            pet.rest_until = pet.t * TICK + 5000
            pet.raise_()
            if animate:
                # 提醒动作：优雅女仆舞，循环播放直到气泡结束（不冷却、不打断）
                if pet._start_frame_anim("idle_maid"):
                    pet._frame_loop = True       # 强制循环，避免播完停住
                    pet._frame_hold = False
                else:
                    pet._play_temp("remind", total=1.6)
        except Exception as ex:
            log("提醒动作失败: %s" % ex, logging.WARNING)
        # 气泡（加大 + 图标 + 两个按钮，12 秒）
        self._show_remind_bubble(pet, text, icon, kind,
                                 ctx.get("todo_id"), ctx.get("schedule_id"))


    def _bring_pet_visible(self, pet):
        """把宠物挪到屏幕内可见位置并置顶。"""
        try:
            geo = pet._work_area()
            x, y = pet.x(), pet.y()
            nx = max(geo.left() + 8, min(geo.right() - pet.width() - 8, x))
            ny = max(geo.top() + 8, min(geo.bottom() - pet.height() - 8, y))
            if (nx, ny) != (x, y):
                pet.move(int(nx), int(ny))
                pet._body_state()
            pet.show()
            pet.raise_()
        except Exception as ex:
            log("提醒置顶失败: %s" % ex, logging.DEBUG)

    def _show_remind_bubble(self, pet, text, icon, kind, todo_id=None, schedule_id=None):
        """显示加大的提醒气泡（带图标 + 两个按钮），12 秒后自动消失。"""
        try:
            pet.show_remind_bubble(text, icon, kind,
                                   hold=REMIND_BUBBLE_HOLD,
                                   todo_id=todo_id, schedule_id=schedule_id)
        except Exception as ex:
            log("提醒气泡失败: %s" % ex, logging.WARNING)
            self.say_main(text)

    def _return_pet_after_remind(self):
        """提醒结束后走回提醒前的位置（用户拖拽过则不回位）。"""
        pet = self.main_pet
        if pet is None:
            return
        if getattr(pet, "remind_user_moved", False):
            log("[提醒] 用户拖拽过，取消回位")
            pet.remind_origin = None
            return
        origin = getattr(pet, "remind_origin", None)
        if not origin:
            return
        try:
            tx, ty = origin
            pet.remind_origin = None
            if not pet._anim_enabled():
                pet.move(int(tx), int(ty))
                pet._clamp_into_work_area()
                return
            pet.remind_walk_to(int(tx), int(ty))
        except Exception as ex:
            log("[提醒] 回位失败: %s" % ex, logging.WARNING)

    def _remind_reply(self, pool, key):
        """提醒回话：随机取一条，避免连续重复。"""
        try:
            return self._pick_line(pool, "remind_reply_" + key)
        except Exception:
            return pool[0] if pool else ""

    def _say_short(self, text, hold=REMIND_ACK_HOLD):
        """宠物冒一句短气泡（默认 1.5 秒）。"""
        pet = self.main_pet
        if pet is None or not text:
            return
        try:
            pet.bubble_inner = False
            pet.bubble_text = text
            pet.bubble_until = pet.t * TICK / 1000.0 + max(0.6, float(hold))
            pet.last_line = text
            pet.update()
        except Exception as ex:
            log("[提醒] 短气泡失败: %s" % ex, logging.DEBUG)

    def acknowledge_reminder(self, kind, todo_id=None, schedule_id=None):
        """点「知道了」：计入今日完成数 + 冒一句短回话。"""
        self._bump_done(kind)
        if todo_id:
            self._todo_mark_done(todo_id)
        if schedule_id:
            self._schedule_mark_done(schedule_id)
        if self.main_pet is not None:
            self.main_pet.hide_remind_bubble()
        # 1.5 秒短回话
        pool = REMIND_ACK_LINES.get(kind) or REMIND_ACK_LINES["water"]
        self._say_short(self._remind_reply(pool, kind))
        self._return_pet_after_remind()
        log("[提醒] 已确认 %s" % kind)

    def snooze_reminder(self, kind, todo_id=None, schedule_id=None):
        """点「稍后再提醒」：N 分钟后再来一次。"""
        mins = int(self._rem_extras().get("snooze_min") or REMIND_SNOOZE_MIN)
        if kind in self._rem_next:
            self._rem_next[kind] = time.time() + mins * 60
        if self.main_pet is not None:
            self.main_pet.hide_remind_bubble()
        self._return_pet_after_remind()
        self._say_short(self._remind_reply(REMIND_SNOOZE_LINES, "snooze"))
        log("[提醒] %s 延后 %d 分钟" % (kind, mins))

    def _reset_reminder_clock(self):
        """启动/唤醒后重新计时（错过的提醒不堆叠）。"""
        now = time.time()
        for kind in REMINDERS:
            self._rem_next[kind] = now + self._rem_minutes(kind) * 60

    def _check_reminders(self):
        """每秒检查：常规提醒 + 待办 + 定时提醒。"""
        if not self.is_enabled("reminders"):
            return
        now = time.time()
        blocked, _why = self._remind_blocked()

        # ---- 常规三类 ----
        for kind in REMINDERS:
            if not self._rem_on(kind):
                continue
            if now < self._rem_next.get(kind, now):
                continue
            if blocked:
                # 阻塞期间：顺延到下一个周期，不堆叠
                self._rem_next[kind] = now + self._rem_minutes(kind) * 60
                continue
            self._fire_reminder(kind)
            self._rem_next[kind] = now + self._rem_minutes(kind) * 60

        # ---- 待办 ----
        if self.is_enabled("remind_todo"):
            self._check_todos(blocked, now)
        # ---- 定时 ----
        if self.is_enabled("remind_schedule"):
            self._check_schedules(blocked, now)

    # ---- 待办 ----
    def _check_todos(self, blocked, now):
        ex = self._rem_extras()
        todos = ex.get("todos") or []
        changed = False
        for t in todos:
            if not t.get("enabled", True) or t.get("done"):
                continue
            due = float(t.get("due") or 0)
            if due <= 0 or now < due:
                continue
            if blocked:
                continue
            body = (t.get("text") or "待办时间到").strip()
            # 待办用固定模板 + 内容替换，保证语气统一
            tmpl = self._pick_line(TODO_LINES, "remind_todo")
            self._fire_reminder("todo", text=tmpl.replace("{content}", body),
                                icon=REMIND_ICONS["todo"], todo_id=t.get("id"))
            # 一次性待办：标记完成；重复类：顺延到下一次
            if t.get("repeat") == "once":
                t["done"] = True
            else:
                t["due"] = self._next_due(t.get("at") or "12:00", t.get("repeat"))
            changed = True
        if changed:
            self.save_cfg()

    def _next_due(self, hhmm, repeat):
        """算下一次到期时间戳（每天/工作日）。"""
        try:
            h, m = [int(x) for x in hhmm.split(":")]
        except Exception:
            h, m = 12, 0
        now = datetime.datetime.now()
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
        while True:
            if cand <= now:
                cand += datetime.timedelta(days=1)
                continue
            if repeat == "workday" and cand.weekday() >= 5:
                cand += datetime.timedelta(days=1)
                continue
            break
        return cand.timestamp()

    def _todo_mark_done(self, todo_id):
        for t in (self._rem_extras().get("todos") or []):
            if t.get("id") == todo_id:
                t["done"] = True
                self.save_cfg()
                return

    def add_todo(self, text, due_ts, repeat="once", at="12:00"):
        """添加待办（最多 TODO_MAX 条）。"""
        ex = self._rem_extras()
        todos = ex.get("todos") or []
        if len(todos) >= TODO_MAX:
            return None
        tid = "t%d" % int(time.time() * 1000 % 1000000)
        todos.append({"id": tid, "text": (text or "")[:100], "enabled": True,
                      "repeat": repeat, "at": at, "due": float(due_ts),
                      "done": False})
        ex["todos"] = todos
        self.save_cfg()
        log("[提醒] 添加待办 %s @ %s" % (tid, at))
        return tid

    def remove_todo(self, todo_id):
        ex = self._rem_extras()
        ex["todos"] = [t for t in (ex.get("todos") or []) if t.get("id") != todo_id]
        self.save_cfg()

    def toggle_todo(self, todo_id, enabled=None):
        for t in (self._rem_extras().get("todos") or []):
            if t.get("id") == todo_id:
                t["enabled"] = (not t.get("enabled", True)) if enabled is None else bool(enabled)
                self.save_cfg()
                return

    # ---- 自定义定时提醒 ----
    def _check_schedules(self, blocked, now):
        ex = self._rem_extras()
        sch = ex.get("schedules") or []
        nowdt = datetime.datetime.now()
        nowm = nowdt.hour * 60 + nowdt.minute
        changed = False
        for it in sch:
            if not it.get("enabled", True) or it.get("done"):
                continue
            try:
                h, m = [int(x) for x in (it.get("at") or "12:00").split(":")]
            except Exception:
                continue
            if nowm != h * 60 + m:
                continue
            if it.get("repeat") == "workday" and nowdt.weekday() >= 5:
                continue
            if blocked:
                continue
            # 同一分钟内只触发一次
            key = "sch_last_%s" % it.get("id")
            if self._last_pick.get(key) == nowdt.strftime("%Y-%m-%d %H:%M"):
                continue
            self._last_pick[key] = nowdt.strftime("%Y-%m-%d %H:%M")
            line = (it.get("line") or "").strip() or ("%s 时间到啦！" % it.get("name", "定时提醒"))
            self._fire_reminder("schedule", text=line, icon=REMIND_ICON_CLOCK,
                                schedule_id=it.get("id"))
            if it.get("repeat") == "once":
                it["done"] = True
                changed = True
        if changed:
            self.save_cfg()

    def _schedule_mark_done(self, sid):
        for it in (self._rem_extras().get("schedules") or []):
            if it.get("id") == sid and it.get("repeat") == "once":
                it["done"] = True
                self.save_cfg()
                return

    def add_schedule(self, name, at, repeat="daily", line=""):
        ex = self._rem_extras()
        sch = ex.get("schedules") or []
        if len(sch) >= TODO_MAX:
            return None
        sid = "s%d" % int(time.time() * 1000 % 1000000)
        sch.append({"id": sid, "name": (name or "定时提醒")[:40], "enabled": True,
                    "repeat": repeat, "at": _valid_hhmm(at, "12:00"),
                    "line": (line or "")[:120], "done": False})
        ex["schedules"] = sch
        self.save_cfg()
        return sid

    def remove_schedule(self, sid):
        ex = self._rem_extras()
        ex["schedules"] = [x for x in (ex.get("schedules") or []) if x.get("id") != sid]
        self.save_cfg()

    def toggle_schedule(self, sid, enabled=None):
        for it in (self._rem_extras().get("schedules") or []):
            if it.get("id") == sid:
                it["enabled"] = (not it.get("enabled", True)) if enabled is None else bool(enabled)
                self.save_cfg()
                return

    # ---- 聊天里「提醒我 X 分钟后 xxx」 ----
    _REMIND_CHAT_RE = re.compile(
        r"提醒我\s*(\d+)\s*(分钟|分|小时|钟头)\s*(?:后|之后|以后)?\s*([^。！!\n]{0,60})")

    @classmethod
    def parse_remind_command(cls, text):
        """解析聊天里的「提醒我 X 分钟后 xxx」，返回 (秒数, 内容) 或 None。"""
        m = cls._REMIND_CHAT_RE.search(text or "")
        if not m:
            return None
        try:
            n = int(m.group(1))
        except Exception:
            return None
        unit = m.group(2)
        secs = n * 3600 if unit in ("小时", "钟头") else n * 60
        body = (m.group(3) or "").strip(" ，,。")
        return secs, (body or "该做这件事啦")

    def handle_remind_chat(self, text):
        """聊天发送前调用：命中就加一条待办并回执，返回 True 表示已处理。"""
        if not self.is_enabled("remind_todo"):
            return False
        parsed = self.parse_remind_command(text)
        if not parsed:
            return False
        secs, body = parsed
        due = time.time() + secs
        when = datetime.datetime.fromtimestamp(due)
        tid = self.add_todo(body, due, repeat="once",
                            at=when.strftime("%H:%M"))
        if tid is None:
            return False
        mins = secs // 60
        self.say_main("记下了：%d 分钟后提醒你「%s」" % (mins, body))
        return True

    def _pick_line(self, pool, key):
        """随机取台词，避开上一句（气泡显示的上一句 + 同类上一次）"""
        last = self._last_pick.get(key, "")
        last_line = self.main_pet.last_line if self.main_pet is not None else ""
        cands = [x for x in pool if x != last_line and x != last]
        if not cands:
            cands = [x for x in pool if x != last_line] or list(pool)
        pick = random.choice(cands)
        self._last_pick[key] = pick
        return pick

    # ---------- 系统监控 ----------
    def _sys_tick(self):
        self._t += 1
        self.check_system_status()

    def check_system_status(self):
        if not self.is_enabled("system_monitor"):
            return
        now = self._t * TICK
        if now - self.last_system_check < 10000:
            return
        self.last_system_check = now

        cpu = psutil.cpu_percent()
        if cpu >= 90:
            self.say_main("CPU跑满了！笨蛋，再这样我就卡死了")
            return

        ram = psutil.virtual_memory().percent
        if ram >= 95:
            self.say_main("内存爆了！快关点没用的……别把我关了")
            return

        if GPU_AVAILABLE:
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                if temp > 80:
                    self.say_main("鱼鳍快熟了……笨蛋，让我凉快一下")
            except Exception as e:
                log("GPU读取失败: %s" % e, logging.WARNING)

    # ---------- 小游戏 ----------
    def _open_rps(self):
        if self.game_active:
            return
        self.game_active = True
        self.chat_paused = True
        self.rps_panel.show_at(self.main_pet)

    def rps_play(self, choice):
        pet_pick = random.choice(RPS_CHOICES)
        player_win = RPS_BEATS[choice] == pet_pick
        pet_win = RPS_BEATS[pet_pick] == choice
        g = self.cfg["games"]
        g["rps_total"] = int(g.get("rps_total", 0)) + 1
        if pet_win:
            g["rps_win"] = int(g.get("rps_win", 0)) + 1
        self.save_cfg()
        if player_win:
            text = self._pick_line(RPS_LOSE_LINES, "rps_lose")
            result = "你赢了"
        elif pet_win:
            text = self._pick_line(RPS_WIN_LINES, "rps_win")
            result = "我赢了"
        else:
            text = self._pick_line(RPS_TIE_LINES, "rps_tie")
            result = "平局"
        self.rps_panel.result.setText(
            f"你出 {RPS_EMOJI[choice]}{choice}，我出 {RPS_EMOJI[pet_pick]}{pet_pick} → {result}\n"
            f"我共赢了 {g['rps_win']} 局 / 共 {g['rps_total']} 局")
        if self.main_pet is not None:
            self.main_pet._play_temp("sway", total=0.8)
        self.say_main(text)

    def _close_rps(self):
        if self.rps_panel:
            self.rps_panel._detach_filter()
            self.rps_panel.hide()
        self.game_active = False
        self.chat_paused = False

    def _open_catch(self):
        if self.game_active:
            return
        self.game_active = True
        self.chat_paused = True
        self.game_had_pet_visible = any(w.isVisible() for w in self.pets)
        self._game_hidden = True
        for w in self.pets:
            self.set_visible(w.pet_id, False, "game")
        self.catch_win = CatchGameWindow(self)
        self.catch_win.start()

    def _catch_finished(self, score):
        self.catch_win = None
        self._game_hidden = False
        self.game_active = False
        self.chat_paused = False
        g = self.cfg["games"]
        g["catch_last"] = int(score)
        best = int(g.get("catch_best", 0))
        if score > best:
            g["catch_best"] = int(score)
            best = int(score)
        self.save_cfg()
        if self.game_had_pet_visible:
            for w in self.pets:
                if w.pet.get("display", True):
                    self.set_visible(w.pet_id, True, "game")
        if self.main_pet is not None:
            self.main_pet._set_dir("down", 1)
        self.say_main(self._pick_line([s.format(n=score) for s in CATCH_SCORE_LINES], "catch"))
        box = QMessageBox(self.main_pet)
        box.setWindowTitle("接食物")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"接到 {score} 个食物！\n历史最高：{best}")
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

# ---------- 动作点播 ----------
    def play_action(self, pet=None, name=None):
        """点播一个动作（菜单「动作」用）；移动类会真实走一段。"""
        pet = pet or self.main_pet
        if pet is None or name not in ACTIONS:
            return
        spec = ACTIONS[name]
        pet.target = None
        pet._start_chain_action(name)
        if spec.get("moves"):
            # 移动类：真实走一段（不受"原地待着"限制，这是用户主动点播）
            pet._pick_move_target()
        log("[动作] 点播 %s（%s）" % (name, spec.get("category")))
        label = ACTION_LABELS.get(name) or name
        pet.say("播放：" + label)

    def _build_action_menu(self, menu, pet):
        """动作 → 分类 → 具体动作；新增动作只要登记 ACTIONS 就自动出现。

        有帧序列的动作会在菜单里标上「帧」，方便调试每个帧动作。
        """
        action_menu = menu.addMenu("动作")
        by_cat = {}
        for name, spec in ACTIONS.items():
            by_cat.setdefault(spec.get("category", "其他"), []).append(name)
        order = [c for c in ACTION_CATEGORY_LABELS if c in by_cat]
        order += [c for c in by_cat if c not in order]
        for cat in order:
            cat_label = ACTION_CATEGORY_LABELS.get(cat, cat)
            sub = action_menu.addMenu(cat_label)
            for name in by_cat[cat]:
                label = ACTION_LABELS.get(name) or name
                if action_has_frames(name):
                    label += "  【帧】"
                a = sub.addAction(label)
                a.triggered.connect(lambda _=False, n=name, p=pet: self.play_action(p, n))
        action_menu.addSeparator()
        n_frames = sum(1 for n in ACTIONS if action_has_frames(n))
        info = action_menu.addAction("帧动作：%d / %d（设置→动画可开关）"
                                     % (n_frames, len(ACTIONS)))
        info.setEnabled(False)
        action_menu.addAction("释放帧缓存并重扫",
                              lambda _=False: self._reload_frames_from_menu())
        return action_menu

    def _reload_frames_from_menu(self):
        _FRAME_CACHE.clear()
        for w in self.pets:
            w._stop_frame_anim()
        self.say_main("帧缓存已清空，下次播放时重新加载")
        log("[帧动画] 菜单触发：清空缓存")

# ---------- 设置窗口 ----------
    def open_settings(self, page=None):
        if self.settings_window is None:
            self.settings_window = SettingsWindow(self)
        self.settings_window.show_page(page)

    def add_pet(self):
        """添加一只新宠物（自动分配 id，错开位置）。"""
        used = {p.get("id") for p in (self.cfg.get("pets") or [])}
        n = 1
        while ("pet%d" % n) in used:
            n += 1
        pid = "pet%d" % n
        base = self.main_pet
        x, y = 300, 300
        if base is not None:
            geo = base._work_area()
            x = min(geo.right() - 260, base.x() + 240)
            y = min(geo.bottom() - 340, base.y() + 80)
            if x < geo.left() + 20:
                x = geo.left() + 20
            if y < geo.top() + 20:
                y = geo.top() + 20
        pet_cfg = {"id": pid, "name": default_pet_name(len(self.cfg.get("pets") or [])),
                   "size": 0.7, "x": int(x), "y": int(y),
                   "mode": "wander", "display": True}
        self.cfg.setdefault("pets", []).append(pet_cfg)
        w = PetWindow(self, pet_cfg)
        self.pets.append(w)
        self.save_cfg()
        self.say_main("新伙伴 %s 来啦" % pid)
        log("[宠物] 添加 %s" % pid)
        if self.settings_window is not None and self.settings_window.isVisible():
            self.settings_window.reload()
        return w

    def remove_pet(self, pet_id):
        """删除一只宠物；main 不可删，且至少保留一只。"""
        if pet_id == "main":
            return False
        if len(self.cfg.get("pets") or []) <= 1:
            return False
        w = self.get_pet(pet_id)
        if w is not None:
            try:
                w.timer.stop()
                w.hide()
                w.deleteLater()
            except Exception:
                pass
            self.pets = [p for p in self.pets if p is not w]
        self.cfg["pets"] = [p for p in (self.cfg.get("pets") or [])
                            if p.get("id") != pet_id]
        self.save_cfg()
        log("[宠物] 删除 %s" % pet_id)
        if self.settings_window is not None and self.settings_window.isVisible():
            self.settings_window.reload()
        return True

    # ---------- 功能开关 ----------
    def is_enabled(self, key):
        """功能是否开启（未登记的 key 视为开启，避免误关新功能）。"""
        if key not in FEATURES:
            return True
        f = self.cfg.get("features")
        if not isinstance(f, dict):
            return bool(FEATURES[key]["default"])
        v = f.get(key)
        if isinstance(v, bool):
            return v
        return bool(FEATURES[key]["default"])

    def set_feature(self, key, on):
        if key not in FEATURES:
            return
        f = self.cfg.get("features")
        if not isinstance(f, dict):
            f = {}
        f[key] = bool(on)
        self.cfg["features"] = f
        self.save_cfg()
        self._apply_feature_side_effects(key, bool(on))
        spec = FEATURES[key]
        msg = "%s：%s" % (spec["name"], "开" if on else "关")
        if spec.get("needs_restart"):
            msg += "（重启生效）"
        self.say_main(msg)
        log("[功能] %s -> %s" % (key, on))

    def _apply_feature_side_effects(self, key, on):
        """开关变化后立刻做的联动：停/起定时器、恢复显示等。"""
        try:
            if key == "capture_hide":
                self.apply_capture_exclude()
                if not on and self._capture_hidden:
                    self._capture_hidden = False
                    for w in self.pets:
                        if w.pet.get("display", True):
                            self.set_visible(w.pet_id, True, "capture")
            elif key == "seasonal_fx":
                if on:
                    if not self._fx_timer.isActive():
                        self._fx_timer.start(10 * 60 * 1000)
                else:
                    self._fx_timer.stop()
                    self.stop_fx()
            elif key == "balance_auto":
                if on:
                    cfg = self._balance_cfg()
                    self._balance_timer.start(max(60, cfg["interval_min"] * 60) * 1000)
                    self.query_balance(manual=False)
                else:
                    self._balance_timer.stop()
            elif key == "reminders":
                if on:
                    self._reset_reminder_clock()
                    if not self._rem_timer.isActive():
                        self._rem_timer.start(1000)
                else:
                    self._rem_timer.stop()
            elif key == "system_monitor":
                if on:
                    if not self._sys_timer.isActive():
                        self._sys_timer.start(TICK)
                else:
                    self._sys_timer.stop()
            elif key == "festival_lines":
                if on:
                    if not self._festival_timer.isActive():
                        self._festival_timer.start(20000)
                else:
                    self._festival_timer.stop()
            elif key == "capture_hide":
                if on:
                    if not self._capture_timer.isActive():
                        self._capture_timer.start(450)
                else:
                    self._capture_timer.stop()
        except Exception as ex:
            log("应用功能开关失败(%s): %s" % (key, ex), logging.WARNING)

    # ---------- 工作状态联动 ----------
    def set_work_state(self, state):
        """切换工作状态（thinking/working/waiting/success/error/idle）。

        照 dsh-pet：状态无变化不重复播（签名比对防刷屏）。
        """
        if state not in WORK_STATES:
            return
        if state == self._work_state:
            return
        self._work_state = state
        act, loop, _msg = WORK_STATES[state]
        log("[工作状态] -> %s%s" % (state, ("（%s）" % act) if act else ""))
        if self.main_pet is None:
            return
        if act is None:
            # 回空闲：停掉状态动画，让动画链继续
            self.main_pet._stop_frame_anim()
            self.main_pet.chain_elapsed = 0.0
            self._work_hold_timer.stop()
            return
        pet = self.main_pet
        if not pet._start_frame_anim(act):
            return
        pet.chain_action = act
        pet.chain_dur = float(ACTIONS.get(act, {}).get("duration", 10.0))
        pet.chain_elapsed = 0.0
        # 终态动画播一会儿后自动回 idle
        self._work_hold_timer.stop()
        if state in ("success", "error"):
            self._work_hold_timer.start(int(WORK_RESULT_HOLD * 1000))

    def _work_hold_timeout(self):
        self.set_work_state("idle")

    # ---------- 余额 ----------
    def _balance_cfg(self):
        b = self.cfg.get("balance")
        if not isinstance(b, dict):
            b = {}
        return {
            "auto": bool(b.get("auto", False)),
            "interval_min": max(BALANCE_MIN_INTERVAL_MIN,
                                int(b.get("interval_min", BALANCE_MIN_INTERVAL_MIN) or 0)),
            "last_value": b.get("last_value"),
            "last_time": b.get("last_time"),
        }

    def _save_balance_cfg(self, patch):
        b = self.cfg.get("balance")
        if not isinstance(b, dict):
            b = {}
        b.update(patch)
        self.cfg["balance"] = b
        self.save_cfg()

    def query_balance(self, manual=True):
        """查询余额：后台线程 GET /user/balance，结果用气泡播报。"""
        if self.balance_worker is not None and self.balance_worker.isRunning():
            if manual:
                self.say_main("正在查余额，稍等一下…")
            return
        key = (self.cfg.get("ds_api_key") or "").strip()
        if not key:
            self.say_main("还没设置 API Key：右键桌宠 →「设置 Key」")
            return
        self.balance_worker = BalanceWorker(key)
        self.balance_worker.done.connect(self._on_balance_ok)
        self.balance_worker.failed.connect(self._on_balance_failed)
        self.balance_worker.start()

    def _on_balance_ok(self, amount, meta):
        sign = (meta or {}).get("sign", "¥")
        self._save_balance_cfg({"last_value": float(amount),
                                "last_time": time.time()})
        self.say_main("余额 %s%.2f" % (sign, amount))
        tier = self._balance_tier(amount)
        pet = self.main_pet
        if pet is not None and tier is not None:
            name = tier[2]
            if name in ACTIONS:
                pet._start_chain_action(name)
            elif name in CLICK_RESPONSES:
                pet._play_temp(name, total=CLICK_RESPONSES[name]["duration"], squash=True)
            # 台词延后一点，避免把余额数字立刻盖掉
            line = self._pick_line(tier[3], "balance_" + tier[1])
            QTimer.singleShot(2600, lambda t=line: self.say_main(t))

    def _on_balance_failed(self, msg):
        log("余额查询失败: %s" % msg, logging.WARNING)
        self.say_main("余额查询失败：" + msg)

    def _balance_tier(self, amount):
        """按已用百分比（满额按 ¥20 折算）分 6 档。"""
        try:
            amount = float(amount)
        except Exception:
            return None
        full = self.cfg.get("balance_full", BALANCE_FULL)
        try:
            full = float(full)
        except Exception:
            full = BALANCE_FULL
        if full <= 0:
            full = BALANCE_FULL
        used_pct = max(0.0, min(100.0, (full - amount) / full * 100.0))
        # 照 dsh-pet：正好 100%（一分不剩）单独归到最后一档
        if used_pct >= 99.999:
            return BALANCE_TIERS[-1]
        idx = int(used_pct // 20)
        idx = max(0, min(len(BALANCE_TIERS) - 2, idx))
        return BALANCE_TIERS[idx]

    def set_balance_auto(self, on):
        cfg = self._balance_cfg()
        self._save_balance_cfg({"auto": bool(on),
                                "interval_min": cfg["interval_min"]})
        if on:
            self._balance_timer.start(max(60, cfg["interval_min"] * 60) * 1000)
            self.say_main("已开启自动查询余额（每 %d 分钟）" % cfg["interval_min"])
            self.query_balance(manual=False)
        else:
            self._balance_timer.stop()
            self.say_main("已关闭自动查询余额")

    def set_balance_interval(self, mins):
        mins = max(BALANCE_MIN_INTERVAL_MIN, int(mins))
        self._save_balance_cfg({"interval_min": mins})
        if self._balance_timer.isActive():
            self._balance_timer.start(mins * 60 * 1000)
        self.say_main("余额查询间隔改成 %d 分钟" % mins)

    def _custom_balance_interval(self):
        cfg = self._balance_cfg()
        v, ok = QInputDialog.getInt(self.main_pet, "自动查询余额",
                                    "间隔分钟（最少 %d）:" % BALANCE_MIN_INTERVAL_MIN,
                                    cfg["interval_min"], BALANCE_MIN_INTERVAL_MIN, 1440, 5)
        if ok:
            self.set_balance_interval(v)

    def _balance_tick(self):
        cfg = self._balance_cfg()
        if not cfg["auto"] or not self.is_enabled("balance_auto"):
            return
        last = cfg.get("last_time") or 0
        if time.time() - float(last) < cfg["interval_min"] * 60:
            return
        self.query_balance(manual=False)

    # ---------- 时节特效 ----------
    def _fx_cfg(self):
        f = self.cfg.get("seasonal_fx")
        if not isinstance(f, dict):
            f = {}
        return {"enabled": bool(f.get("enabled", SEASONAL_DEFAULT_ON)),
                "auto": bool(f.get("auto", True))}

    def _save_fx_cfg(self, patch):
        f = self.cfg.get("seasonal_fx")
        if not isinstance(f, dict):
            f = {}
        f.update(patch)
        self.cfg["seasonal_fx"] = f
        self.save_cfg()

    def set_seasonal_fx(self, on):
        self._save_fx_cfg({"enabled": bool(on)})
        if not on:
            self.stop_fx()
        self.say_main("时节特效：" + ("开" if on else "关"))

    def set_fx_auto(self, on):
        self._save_fx_cfg({"auto": bool(on)})
        self.say_main("自动触发时节特效：" + ("开" if on else "关"))

    def stop_fx(self):
        for w in self.pets:
            w.stop_fx()

    def _fx_screens_for_pet(self, pet):
        """特效区域：**宠物窗口周围一圈**（不是整屏）。

        特效是在宠物窗口的 paintEvent 里绘制的，窗口只有 ~220x310，
        若按整屏（1920x1152）撒粒子，落在窗口外的部分全都看不见。
        所以以宠物为中心取一个稍大的局部矩形，保证粒子密集可见。
        """
        try:
            # 矩形原点**相对窗口**：四周各溢出 margin（粒子可飘出窗口边缘，更有氛围）
            # paintEvent 里按 translate(rect[0], rect[1]) 对齐，粒子坐标都是矩形内部坐标
            margin = 90
            w = pet.width() + margin * 2
            h = pet.height() + margin * 2
            return (-margin, -margin, w, h)
        except Exception:
            g = QApplication.primaryScreen().availableGeometry()
            return (g.x(), g.y(), g.width(), g.height())

    def play_fx(self, name, pet=None):
        """播放指定时节特效（几秒后自动结束）。"""
        pet = pet or self.main_pet
        if pet is None or name not in SEASONAL_EFFECTS:
            return
        if not self.is_enabled("seasonal_fx"):
            self.say_main("时节特效已关闭，可在「设置 → 功能开关」里打开")
            return
        rect = self._fx_screens_for_pet(pet)
        scale = 1.0
        try:
            scale = max(0.75, min(2.0, pet.cur_h / 238.0))
        except Exception:
            scale = 1.0
        pet.start_fx(SeasonalEffect(name, rect, scale))
        label = SEASONAL_EFFECTS[name]["label"]
        log("[特效] 播放 %s" % label)
        self._fx_last_auto = name
        # 有对应角色动作就一起播（真实素材，比代码粒子更有节日感）
        act = SEASONAL_ACTIONS.get(name)
        if act and act in ACTIONS:
            pet._start_frame_anim(act)
            # 特效期间循环播放，播完整段（而不是播一次就停）
            pet._frame_loop = True
            pet._frame_hold = False
            pet.chain_action = act
            pet.chain_dur = float(ACTIONS[act].get("duration", 2.1))
            pet.chain_elapsed = 0.0

    def _fx_due_names(self, today=None):
        """今天该触发的特效（按公历区间 / 农历表 / 万圣节等）。"""
        today = today or datetime.date.today()
        md = today.strftime("%m-%d")
        names = []
        for name, spec in SEASONAL_EFFECTS.items():
            trig = spec.get("trigger")
            lunar = spec.get("lunar")
            hit = False
            if trig:
                start, end = trig
                if start <= end:
                    hit = start <= md <= end
                else:                       # 跨年区间，如 12-01 ~ 02-15
                    hit = md >= start or md <= end
            if not hit and lunar:
                table = LUNAR_FESTIVAL_DATES.get(lunar) or {}
                hit = table.get(today.year) == md
            if hit:
                names.append(name)
        return names

    def _fx_auto_tick(self):
        """定时检查：同一天只在冷却后自动播一次。"""
        if not self.is_enabled("seasonal_fx") or not self._fx_cfg()["auto"]:
            return
        if time.time() - self._fx_last_auto_time < SEASONAL_AUTO_COOLDOWN:
            return
        names = self._fx_due_names()
        if not names:
            return
        self.play_fx(names[0])
        self._fx_last_auto_time = time.time()

    def _fx_startup_say(self):
        """启动时按日期触发一次。"""
        if not self.is_enabled("seasonal_fx") or not self._fx_cfg()["auto"]:
            return
        names = self._fx_due_names()
        if not names:
            return
        name = names[0]
        pet = self.main_pet
        if pet is None:
            return
        rect = self._fx_screens_for_pet(pet)
        pet.start_fx(SeasonalEffect(name, rect, 1.0))
        log("[特效] 启动触发 %s" % SEASONAL_EFFECTS[name]["label"])
        self._fx_last_auto_time = time.time()

    # ---------- 节日 / 生日 ----------
    @staticmethod
    def _in_window(d, md, win):
        """日期 d 是否落在节日 md 的 [ahead 预热, span 持续, after 回味] 窗口内。

        返回 (命中?, 阶段) 阶段 = "预热" / "当天" / "假期中" / "回味" / None
        """
        ahead = int(win.get("ahead", FESTIVAL_WINDOW_DEFAULT["ahead"]))
        span = max(1, int(win.get("span", FESTIVAL_WINDOW_DEFAULT["span"])))
        after = int(win.get("after", FESTIVAL_WINDOW_DEFAULT["after"]))
        try:
            base = datetime.date(d.year, int(md[:2]), int(md[3:5]))
        except Exception:
            return False, None
        # 跨年节日（12-31 除夕）要往前一年找
        cands = [base]
        try:
            cands.append(datetime.date(d.year - 1, base.month, base.day))
            cands.append(datetime.date(d.year + 1, base.month, base.day))
        except Exception:
            pass
        for b in cands:
            delta = (d - b).days
            last_day = span - 1          # 假期最后一天相对首日的偏移
            if -ahead <= delta <= last_day:
                if delta < 0:
                    return True, "预热"
                if delta == 0:
                    return True, "当天"
                return True, "假期中"
            if last_day < delta <= last_day + after:
                return True, "回味"
        return False, None

    def _festival_names_today(self, day=None):
        """返回今天命中的节日名列表（支持提前预热 / 持续 / 过后回味三段窗口）。"""
        d = day or datetime.date.today()
        md = d.strftime("%m-%d")
        names = []
        for name, win in FESTIVAL_MD.items():
            if isinstance(win, str):
                win = {"md": win}
            hit, phase = self._in_window(d, win["md"], win)
            if hit:
                names.append(name)
                if phase and phase != "当天":
                    log("[节日] %s（%s）" % (name, phase))
        for name, table in LUNAR_FESTIVAL_DATES.items():
            if table.get(d.year) == md:
                names.append(name)
        bd = (self.cfg.get("birthday") or "").strip()
        if bd:
            try:
                parts = bd.replace("/", "-").replace(".", "-").split("-")
                if len(parts) >= 2 and f"{int(parts[-2]):02d}-{int(parts[-1]):02d}" == md:
                    names.append("生日")
            except Exception:
                pass
        return names

    def _festival_say(self):
        names = self._festival_names_today()
        if not names:
            return False
        name = names[0]
        pool = BIRTHDAY_LINES if name == "生日" else FESTIVAL_LINES.get(name, [])
        if not pool:
            return False
        text = self._pick_line(pool, "festival_" + name)
        log("[节日] %s -> %s" % (name, text))
        if self.main_pet is not None:
            self.main_pet._play_temp("sway", total=1.0)
            self.main_pet.jump_t = 0.6
        self.say_main(text)
        return True

    def _festival_startup_say(self):
        if not self.is_enabled("festival_lines"):
            return
        self._last_festival_hour = datetime.datetime.now().hour
        self._festival_say()

    def _festival_hourly_check(self):
        if not self.is_enabled("festival_lines"):
            return
        now = datetime.datetime.now()
        if now.minute > 2:
            return
        if self._last_festival_hour == now.hour:
            return
        self._last_festival_hour = now.hour
        self._festival_say()

    def _set_birthday_dialog(self):
        cur = self.cfg.get("birthday", "")
        text, ok = QInputDialog.getText(
            self.main_pet, "设置生日",
            "输入生日（如 1995-03-12，或只写 03-12）:",
            QLineEdit.EchoMode.Normal, cur)
        if ok:
            self.cfg["birthday"] = text.strip()
            self.save_cfg()
            self.say_main("生日记住啦！到那天我会准时说生日快乐")

    # ---------- 截屏自动隐藏 ----------
    def apply_capture_exclude(self, pet=None):
        """截屏时隐藏：让系统截图/录屏 API 直接拍不到窗口；关掉开关则恢复可拍"""
        targets = [pet] if pet is not None else list(self.pets)
        enabled = bool(self.is_enabled("capture_hide"))
        aff = 0x11 if enabled else 0x0  # WDA_EXCLUDEFROMCAPTURE / WDA_NONE
        for w in targets:
            try:
                hwnd = int(w.winId())
                ctypes.windll.kernel32.SetLastError(0)
                ok = bool(ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, aff))
                if ok:
                    log("[截屏] 排除截屏: %s" % ("开" if enabled else "关"))
                else:
                    log("[截屏] 排除截屏设置失败, err=%s" % ctypes.get_last_error(), logging.WARNING)
            except Exception as ex:
                log("[截屏] 排除截屏异常: %s" % ex, logging.ERROR)

    def set_hide_in_capture(self, on):
        self.cfg.setdefault("features", {})["capture_hide"] = bool(on)
        self.save_cfg()
        self.apply_capture_exclude()
        if on:
            self.say_main("截屏时我会藏起来，拍不到我啦")
        else:
            self._capture_hidden = False
            for w in self.pets:
                if w.pet.get("display", True) and not w.isVisible():
                    w.show()
                    w.raise_()
            self.say_main("现在可以拍到我啦")

    def set_animation_mode(self, mode):
        """动画效果：auto 跟随系统「显示动画」，on 总开，off 总关（跳过挤压与淡入）"""
        if mode not in ("auto", "on", "off"):
            return
        self.cfg["animation_mode"] = mode
        self.save_cfg()
        for w in self.pets:
            w._sys_anim_cache = None
            if not w._anim_enabled():
                w.cross_t = 0.0
                w.squash_t = 0.0
                w._action_fade = 0.0
            w.update()
        label = dict((k, l) for l, k in ANIMATION_MODE_CHOICES).get(mode, mode)
        self.say_main("动画效果：" + label)

    def animation_mode_label(self):
        mode = self.cfg.get("animation_mode", "auto")
        base = dict((k, l) for l, k in ANIMATION_MODE_CHOICES).get(mode, mode)
        if mode == "auto":
            on = any(w._system_animations_on() for w in self.pets) if self.pets else True
            return base + ("（系统已开启）" if on else "（系统已关闭）")
        return base

    def _physics_cfg(self):
        cfg, _bad = sanitize_physics(self.cfg.get("physics"), DEFAULT_PHYSICS)
        return cfg

    def set_physics(self, key, value):
        """修改 physics 字段并落盘（设置页在第 5 批做，先给菜单入口）"""
        ph = self._physics_cfg()
        ph[key] = value
        self.cfg["physics"] = ph
        self.save_cfg()
        for w in self.pets:
            w._body_state()
            w.update()
        label = {"top_bounce": "顶部反弹", "pet_collision": "宠物互撞"}.get(key, key)
        self.say_main("%s：%s" % (label, "开" if value else "关" if isinstance(value, bool) else value))

    def _edit_physics_value(self, name, key, lo, hi, step):
        ph = self._physics_cfg()
        cur = ph.get(key, DEFAULT_PHYSICS.get(key, 0))
        if key == "elasticity":
            cur_i = int(round(float(cur) * 100))
            v, ok = QInputDialog.getInt(self.main_pet, "物理效果 - " + name,
                                        "百分比（0-100）:", cur_i, lo, hi, step)
            if ok:
                self.set_physics(key, max(0.0, min(1.0, v / 100.0)))
            return
        if key == "power":
            cur_i = int(round(float(cur) * 100))
            v, ok = QInputDialog.getInt(self.main_pet, "物理效果 - " + name,
                                        "百分比（20-300）:", cur_i, lo, hi, step)
            if ok:
                self.set_physics(key, v / 100.0)
            return
        v, ok = QInputDialog.getInt(self.main_pet, "物理效果 - " + name,
                                    "数值（%d-%d）:" % (lo, hi),
                                    int(round(float(cur))), lo, hi, step)
        if ok:
            self.set_physics(key, float(v))

    def _proc_name_of_pid(self, pid):
        try:
            if pid not in self._proc_name_cache:
                try:
                    name = psutil.Process(pid).name().lower()
                except Exception:
                    name = ""
                self._proc_name_cache[pid] = name
            return self._proc_name_cache[pid]
        except Exception:
            return ""

    def _capture_guard_tick(self):
        if not self.is_enabled("capture_hide"):
            # 关掉「截屏时隐藏」：不做任何隐藏/排除，随便拍
            if self._capture_hidden:
                self._capture_hidden = False
                for w in self.pets:
                    if w.pet.get("display", True):
                        w.show()
                        w.raise_()
            return
        try:
            # 聊天窗口打开时不按“全屏覆盖层”隐藏，保证聊天时桌宠一直在
            chat_open = self.chat_window is not None and self.chat_window.isVisible()
            active = self._capture_overlay_active(allow_generic=not chat_open)
        except Exception:
            active = False
        if active:
            if not self._capture_hidden:
                self._capture_hidden = True
                for w in self.pets:
                    w.hide()
        elif self._capture_hidden:
            self._capture_hidden = False
            for w in self.pets:
                if w.pet.get("display", True):
                    w.show()
                    w.raise_()

    def _capture_overlay_active(self, allow_generic=True):
        """检测截图工具/全屏覆盖层：命中时自动隐藏桌宠，免得被拍进图里"""
        user32 = ctypes.windll.user32
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        own = os.getpid()
        found = [False]

        def class_of(h):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, buf, 256)
            return buf.value.lower()

        def title_of(h):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, buf, 256)
            return buf.value.strip().lower()

        def covers(h):
            r = _RECT()
            if not user32.GetWindowRect(h, ctypes.byref(r)):
                return False
            # 必须是真正铺满整屏（最大化 UWP 窗口会因阴影比屏幕还大一点，用 99% 排除）
            return (r.right - r.left) >= sw * 0.99 and (r.bottom - r.top) >= sh * 0.99

        def big(h):
            r = _RECT()
            if not user32.GetWindowRect(h, ctypes.byref(r)):
                return False
            return (r.right - r.left) >= sw * 0.5 and (r.bottom - r.top) >= sh * 0.5

        def cb(h, lp):
            if not user32.IsWindowVisible(h):
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if pid.value == own:
                return True
            cls = class_of(h)
            if cls in SHELL_CLASSES:
                return True
            if cls in SCREENSHOT_CLASSES:
                found[0] = True
                return False
            pname = self._proc_name_of_pid(pid.value)
            if pname in SCREENSHOT_PROCS and big(h):
                found[0] = True
                return False
            title = title_of(h)
            if any(k in title for k in SCREENSHOT_TITLE_HINTS):
                found[0] = True
                return False
            if allow_generic and covers(h):
                if cls in ("applicationframewindow", "windows.ui.core.corewindow"):
                    return True
                ex = user32.GetWindowLongPtrW(h, -20)
                if ex & 0x8:  # WS_EX_TOPMOST：全屏置顶覆盖层（截图/游戏/视频）
                    found[0] = True
                    return False
            return True

        # 先看前台窗口：Windows 自带截图（ms-screenclip:）是 CoreWindow + 标题「屏幕截图」，
        # 盖满整屏但并不带 WS_EX_TOPMOST，所以要单独判一次
        try:
            fg = user32.GetForegroundWindow()
            if fg:
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
                if pid.value != own:
                    cls = class_of(fg)
                    if cls not in SHELL_CLASSES:
                        if any(k in title_of(fg) for k in SCREENSHOT_TITLE_HINTS):
                            return True
                        if self._proc_name_of_pid(pid.value) in SCREENSHOT_PROCS and big(fg):
                            return True
                        if allow_generic and covers(fg):
                            return True
        except Exception:
            pass

        try:
            cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(cb)
            user32.EnumWindows(cb_t, 0)
        except Exception:
            pass
        return found[0]

    # ---------- 聊天 / 记忆 ----------
    def _open_chat(self):
        if self.chat_window is None:
            self.chat_window = ChatWindow(self)
        self.chat_window.show_window()
        # 打开聊天窗口 = 在等用户说话
        if self._work_state in ("idle", "success", "error"):
            self.set_work_state("waiting")

    def _open_memory(self):
        if self.memory_dialog is None:
            self.memory_dialog = MemoryDialog(self)
        self.memory_dialog.reload()
        self.memory_dialog.show()
        self.memory_dialog.raise_()
        self.memory_dialog.activateWindow()

    def _set_key_dialog(self):
        key, ok = QInputDialog.getText(
            self.main_pet,
            "设置 DeepSeek Key",
            "输入你的 API Key（从 platform.deepseek.com 获取）:",
            QLineEdit.EchoMode.Normal,
            self.cfg.get("ds_api_key", "")
        )
        if ok and key.strip():
            self.cfg["ds_api_key"] = key.strip()
            self.say_main("Key 设置成功！")
        elif ok and not key.strip():
            self.say_main("Key 不能为空")

    def _get_weather(self, pet=None):
        speaker = pet or self.main_pet
        try:
            city = self.cfg.get("city", "汕头")
            log("天气请求城市: %s" % city)

            url = f"https://wttr.in/{city}?format=j1"

            r = requests.get(
                url,
                timeout=10,
                headers={
                    "User-Agent": "Mozilla/5.0"
                }
            )

            log("天气接口状态: %s" % r.status_code)
            log("天气接口返回(前500字符): %s" % (r.text[:500]), logging.DEBUG)

            data = r.json()

            weather = data["current_condition"][0]

            temp = weather["temp_C"]

            weather_map = {
                "Sunny": "晴",
                "Clear": "晴",
                "Partly cloudy": "多云",
                "Cloudy": "阴",
                "Light rain": "小雨",
                "Moderate rain": "中雨",
                "Heavy rain": "大雨"
            }

            raw_weather = weather["weatherDesc"][0]["value"]

            desc = weather_map.get(raw_weather, raw_weather)

            if speaker is not None:
                speaker.say(f"{city}今天{temp}°，天气{desc}……带伞了没，笨蛋")

        except Exception as e:
            log("天气错误: %r" % e, logging.WARNING)
            if speaker is not None:
                speaker.say("天气查不到了……不是我不肯说，是它坏了")

    # ---------- 菜单 ----------
    def build_menu(self, pet=None):
        """宠物右键菜单：只保留常用项，其余全部进「设置…」。

        保持：模式 / 聊天 / 动作 / 小游戏 / 查看天气 / 查看余额 /
             时节特效 / 设置… / 退出
        """
        pet = pet or self.main_pet
        m = QMenu(pet)

        mode_menu = m.addMenu("模式")
        for label, key in [("自由散步", "wander"), ("跟随鼠标", "follow"), ("原地待着", "still")]:
            a = mode_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(pet.mode == key)
            a.triggered.connect(lambda _, k=key, p=pet: p.set_mode(k))

        m.addAction("聊天", self._open_chat)
        self._build_action_menu(m, pet)

        gm = m.addMenu("小游戏")
        gm.addAction("猜拳", self._open_rps)
        gm.addAction("接食物（30 秒）", self._open_catch)

        m.addAction("查看天气", lambda: self._get_weather(pet))
        m.addAction("查看余额", lambda: self.query_balance(True))

        fm = m.addMenu("时节特效")
        fc = self._fx_cfg()
        fe = fm.addAction("启用时节特效")
        fe.setCheckable(True)
        fe.setChecked(self.is_enabled("seasonal_fx"))
        fe.triggered.connect(lambda on: self.set_feature("seasonal_fx", on))
        fa = fm.addAction("按日期自动触发")
        fa.setCheckable(True)
        fa.setChecked(fc["auto"])
        fa.triggered.connect(lambda on: self.set_fx_auto(on))
        fm.addSeparator()
        for fname, fspec in SEASONAL_EFFECTS.items():
            fm.addAction(fspec["label"],
                         lambda _=False, n=fname: self.play_fx(n))
        fm.addSeparator()
        fm.addAction("停止当前特效", self.stop_fx)

        m.addSeparator()
        m.addAction("设置…", lambda: self.open_settings())
        m.addAction("退出", self.quit_app)
        return m

    def build_tray_menu(self):
        """托盘菜单：只保留 显示/隐藏、回到屏幕内、设置…、退出。"""
        pet = self.main_pet
        m = QMenu(pet)
        m.addAction("显示/隐藏", lambda: self.toggle_visible(self.main_pet))
        m.addAction("回到屏幕内", lambda: pet.snap_into_screen())
        m.addSeparator()
        m.addAction("设置…", lambda: self.open_settings())
        m.addAction("退出", self.quit_app)
        return m

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Context:
            self.tray.setContextMenu(self.build_tray_menu())
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_visible(self.main_pet)

    # ---------- 全局开关 ----------
    def set_passthrough(self, on):
        self.cfg["passthrough"] = bool(on)
        for w in self.pets:
            w._apply_passthrough(bool(on))
        if on:
            self.say_main("我隐身了！右键托盘图标解除～")

    def set_topmost(self, on):
        self.cfg["topmost"] = bool(on)
        for w in self.pets:
            w.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(on))
            # 只重绘可见的，不改变显示状态
            if w.isVisible():
                w.show()

    def set_autostart(self, on):
        self.cfg["autostart"] = bool(on)
        lnk = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                           "Start Menu", "Programs", "Startup", "大肥鱼桌宠.lnk")
        try:
            if on:
                ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{}');"
                      "$s.TargetPath='{}';$s.Arguments='\"{}\"';$s.WorkingDirectory='{}';$s.Save()"
                      .format(lnk, PYTHONW,
                              "" if getattr(sys, "frozen", False) else os.path.join(APP_DIR, "桌宠.py"),
                              APP_DIR))
                subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
                self.say_main("已开机自启，明天见～")
            else:
                if os.path.exists(lnk):
                    os.remove(lnk)
                self.say_main("已取消开机自启")
        except Exception as ex:
            QMessageBox.warning(self.main_pet, "开机自启", f"设置失败：{ex}")

    # ---------- 宠物显示状态（唯一事实来源） ----------
    def get_pet_state(self, pet_id):
        """返回该宠物当前状态：VISIBLE / HIDDEN / TEMP_HIDDEN。

        判定顺序（以 PetWindow 真实可见性为准）：
          1. 窗口真实可见 → VISIBLE
          2. 不可见且被截屏守卫/小游戏临时隐藏 → TEMP_HIDDEN
          3. 其余不可见 → HIDDEN（配置里 display=false）
        """
        w = self.get_pet(pet_id)
        if w is None:
            return PET_HIDDEN
        # 1) 真实可见就是 VISIBLE（无论配置怎么写）
        try:
            if w.isVisible():
                return PET_VISIBLE
        except Exception:
            pass
        # 2) 不可见：区分临时隐藏与手动隐藏
        if self._capture_hidden or getattr(self, "_game_hidden", False):
            return PET_TEMP_HIDDEN
        return PET_HIDDEN

    @staticmethod
    def state_text(state):
        """状态 → 中文文字（唯一入口，别处不许自己拼）。"""
        return PET_STATE_TEXT.get(state, PET_STATE_TEXT[PET_HIDDEN])

    def pet_mode_text(self, pet_id):
        """模式 → 中文文字。"""
        w = self.get_pet(pet_id)
        mode = (w.mode if w is not None else
                (self._pet_cfg(pet_id) or {}).get("mode", "wander"))
        return PET_MODE_TEXT.get(mode, PET_MODE_TEXT["wander"])

    def pet_name(self, pet_id):
        """宠物名字（缺省时按序号补）。"""
        cfg = self._pet_cfg(pet_id) or {}
        name = (cfg.get("name") or "").strip()
        if name:
            return name
        ids = [x.get("id") for x in (self.cfg.get("pets") or [])]
        try:
            return default_pet_name(ids.index(pet_id))
        except ValueError:
            return pet_id

    def _pet_cfg(self, pet_id):
        for x in (self.cfg.get("pets") or []):
            if x.get("id") == pet_id:
                return x
        return None

    def set_visible(self, pet_id, visible, reason="manual"):
        """所有显示/隐藏操作的唯一入口。

        visible=True  → 显示（并写入 display=true）
        visible=False → 隐藏；reason 决定是否写配置：
                        manual / tray / settings → 写 display=false（HIDDEN）
                        capture / game           → 不写配置（TEMP_HIDDEN）
        """
        w = self.get_pet(pet_id)
        if w is None:
            return
        cfg = self._pet_cfg(pet_id)
        temp = reason in ("capture", "game")
        # 临时隐藏要打上"当前处于临时隐藏"的标记，get_pet_state 靠它区分
        if temp:
            if reason == "capture":
                self._capture_hidden = not visible
            elif reason == "game":
                self._game_hidden = not visible
        if visible:
            try:
                w.show()
                w.raise_()
            except Exception:
                pass
            if cfg is not None:
                cfg["display"] = True
        else:
            try:
                w.hide()
            except Exception:
                pass
            if cfg is not None and not temp:
                cfg["display"] = False
        if not temp:
            self.save_cfg()
        state = self.get_pet_state(pet_id)
        log("[显示] %s -> %s（%s）" % (pet_id, state, reason))
        self.pet_state_changed.emit(pet_id, state, reason)

    def toggle_visible(self, pet=None):
        """按当前状态取反（不改文字、不自己算状态）。"""
        pet = pet or self.main_pet
        if pet is None:
            return
        pid = pet.pet_id
        cur = self.get_pet_state(pid)
        if cur == PET_VISIBLE:
            self.set_visible(pid, False, "tray")
        else:
            # HIDDEN 或 TEMP_HIDDEN 都走"显示"
            self.set_visible(pid, True, "tray")

    def show_all_pets(self, reason="startup"):
        """按配置显示所有该显示的宠物。"""
        for cfg in (self.cfg.get("pets") or []):
            pid = cfg.get("id")
            if cfg.get("display", True):
                self.set_visible(pid, True, reason)

    def quit_app(self):
        """退出：静默清理（不弹任何结算框），停线程后保存配置退出"""
        try:
            if self.rps_panel:
                self.rps_panel.close_panel()
            if self.catch_win:
                score = int(getattr(self.catch_win, "score", 0) or 0)
                try:
                    self.catch_win._timer.stop()
                except Exception:
                    pass
                self.catch_win.hide()
                self.catch_win = None
                g = self.cfg.setdefault("games", _default_games())
                g["catch_last"] = score
                if score > int(g.get("catch_best", 0) or 0):
                    g["catch_best"] = score
                self.game_active = False
                self.chat_paused = False
            if self.settings_window is not None:
                self.settings_window.close()
                self.settings_window = None
            if self.chat_window is not None:
                self.chat_window.shutdown()
        except Exception as ex:
            log("退出清理失败: %s" % ex, logging.WARNING)
        self.save_cfg()
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()

_MUTEX_HANDLE = None
MUTEX_NAME = "DafeiyuPet_SingleInstance_7f3a"


def acquire_single_instance():
    """Windows 命名 Mutex：返回 (handle, is_first)"""
    global _MUTEX_HANDLE
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        err = kernel32.GetLastError()
        if not handle:
            log("创建单实例 Mutex 失败, err=%s" % err, logging.WARNING)
            return None, True
        _MUTEX_HANDLE = handle
        return handle, (err != 183)  # 183 = ERROR_ALREADY_EXISTS
    except Exception as ex:
        log("单实例检查异常: %s" % ex, logging.WARNING)
        return None, True


# 系统通知必需的 AppUserModelID（Win10/11 未设置时托盘通知会被静默丢弃）
APP_USER_MODEL_ID = "DaFeiYu.DesktopPet"


def ensure_app_user_model_id(app_id=None):
    """给当前进程设置 AppUserModelID，否则 Windows 会丢弃托盘通知。

    仅 Windows 有效；失败不影响运行。
    """
    if sys.platform != "win32":
        return False
    app_id = app_id or APP_USER_MODEL_ID
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p(app_id))
        log("[通知] AppUserModelID = %s" % app_id)
        return True
    except Exception as ex:
        log("[通知] 设置 AppUserModelID 失败: %s" % ex, logging.WARNING)
        return False


def main():
    ensure_app_user_model_id()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    log("---- 启动 ----")
    handle, is_first = acquire_single_instance()
    if not is_first:
        log("检测到已有实例，本次启动退出")
        QMessageBox.information(None, "大肥鱼桌宠",
                                "桌宠已经在运行啦～\n（可以在系统托盘里找大肥鱼）")
        return
    controller = PetApp()
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        log("启动失败:\n" + traceback.format_exc(), logging.ERROR)
        try:
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, "大肥鱼桌宠出错", str(ex))
        except Exception:
            pass
        raise
