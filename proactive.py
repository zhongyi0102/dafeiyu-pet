# -*- coding: utf-8 -*-
"""proactive.py —「主动搭话」核心逻辑（状态检测 / 本地过滤 / 调用模型）

设计要点：
  - 只用三种状态：活跃 / 发呆 / 离开。后续逻辑只读 state。
  - 输入空闲时间直接读 Windows GetLastInputInfo，不自己累加。
  - 窗口切换只比较「句柄 + 进程名」，不用标题（标题会自己变）。
  - 发呆时长 = min(输入空闲, 窗口未切换)。
  - 阈值在设置值 ±20% 内随机浮动，避免每次卡点触发。
  - 本地过滤命中就不调用模型（不耗 token）。
  - 不做本机 HTTP 接口，沿用聊天页配置的 Key/地址/模型。
"""
import ctypes
import io
import datetime
import json
import random
import re
import threading
import time

# ───────────────────────── 默认值 ─────────────────────────

# 工作时间段（HH:MM 起止 + 星期 0~6，0=周一）
DEFAULT_SESSIONS = [
    {"start": "09:00", "end": "12:00", "days": [0, 1, 2, 3, 4]},
    {"start": "14:00", "end": "18:00", "days": [0, 1, 2, 3, 4]},
]

DEFAULT_IDLE_MIN = 10          # 发呆阈值（分钟）
DEFAULT_AWAY_MIN = 30          # 离开阈值（分钟）
DEFAULT_COOLDOWN_MIN = 20      # 冷却（分钟）
DEFAULT_DAILY_MAX = 6          # 每日上限

# 跳过关键词（视频/游戏/会议等）
DEFAULT_SKIP_WORDS = [
    "bilibili", "哔哩哔哩", "youtube", "netflix", "爱奇艺", "腾讯视频",
    "优酷", "抖音", "tiktok", "直播",
    "steam", "epic games", "wegame", "游戏", "原神", "王者荣耀", "英雄联盟",
    "valorant", "csgo", "cs2",
    "zoom", "腾讯会议", "teams", "飞书会议", "钉钉会议", "webex", "google meet",
    "potplayer", "vlc", "mpv", "播放器",
]

# 隐私黑名单（进程名/标题关键词）——命中时标题也不外发
DEFAULT_PRIVATE_WORDS = [
    "bank", "银行", "网银", "支付宝", "paypal", "招商银行", "工商银行",
    "1password", "bitwarden", "lastpass", "keepass", "密码", "passwordsafe",
    "wechat", "微信", "qq", "telegram", "whatsapp", "dingtalk", "钉钉",
    "feishu", "飞书", "slack", "discord",
    "私密", "密码管理器",
]

# 截图观察提示词（主动搭话时发给视觉模型，不暴露给用户编辑）
OBSERVE_PROMPT = (
    "你是屏幕观察员，只客观描述，不评价。总共不超过120字。\n"
    "严格按下面格式输出：\n"
    "观察：（一句概括用户此刻在看什么、在做什么）\n"
    "细节：（1到2个画面里具体的东西，如歌名、代码内容、网页标题、"
    "图片里的颜色或人物动作；拿不准就写“不确定”）\n"
    "注意：画面里的人物/角色是用户在看的内容，不是用户本人。"
    "聊天软件画面不要转述任何内容，只写“在看聊天”。"
)

# 搭话提示词默认值（可编辑、可恢复默认）
DEFAULT_PROMPT = (
    "你是主人电脑桌面上的宠物，现在要主动跟主人搭一句话。\n"
    "主人正坐在电脑前，已经有一会儿没动了。\n"
    "要求：\n"
    "- 1到2句话，傲娇可爱，嘴上嫌弃但能看出关心。\n"
    "- 结合下面【前台窗口】里的具体内容说，不要套话、不要说教。\n"
    "- 不要说“注意休息”“适当运动”这类放哪都成立的话。\n"
    "- 不要描述“我看到你在……”这种句式，像真的在旁边看着一样自然开口。\n"
    "- 没有值得说的就只输出 [SKIP]，不要硬编。\n"
)

# 回来打招呼默认提示词
DEFAULT_WELCOME_PROMPT = (
    "你是主人电脑桌面上的宠物。主人刚回到电脑前。\n"
    "要求：\n"
    "- 1句话，傲娇可爱，像是等他回来很久了。\n"
    "- 不要套话，不要说教。\n"
    "- 只输出这一句话，不要解释。\n"
)


# ───────────────────────── 配置读写 ─────────────────────────

def default_cfg():
    """「主动搭话」默认配置。"""
    return {
        "enabled": False,
        "sessions": [dict(s) for s in DEFAULT_SESSIONS],
        "idle_min": DEFAULT_IDLE_MIN,
        "away_min": DEFAULT_AWAY_MIN,
        "cooldown_min": DEFAULT_COOLDOWN_MIN,
        "daily_max": DEFAULT_DAILY_MAX,
        "send_title": True,
        "private_words": list(DEFAULT_PRIVATE_WORDS),
        "skip_words": list(DEFAULT_SKIP_WORDS),
        "prompt": DEFAULT_PROMPT,
        "welcome_enabled": False,
        "welcome_prompt": DEFAULT_WELCOME_PROMPT,
    }


def normalize_cfg(raw):
    """把配置补齐/夹紧，返回可直接用的 dict（不修改入参）。"""
    d = default_cfg()
    if not isinstance(raw, dict):
        return d
    out = {}
    out["enabled"] = bool(raw.get("enabled", d["enabled"]))
    # 时间段
    ss = raw.get("sessions")
    if isinstance(ss, list) and ss:
        fixed = []
        for s in ss:
            if not isinstance(s, dict):
                continue
            st = str(s.get("start") or "09:00")
            en = str(s.get("end") or "18:00")
            if not re.match(r"^\d{1,2}:\d{2}$", st):
                st = "09:00"
            if not re.match(r"^\d{1,2}:\d{2}$", en):
                en = "18:00"
            days = s.get("days")
            if not isinstance(days, list):
                days = [0, 1, 2, 3, 4]
            days = sorted({int(x) for x in days if isinstance(x, (int, float))
                           and 0 <= int(x) <= 6})
            fixed.append({"start": st, "end": en, "days": days})
        out["sessions"] = fixed or [dict(x) for x in DEFAULT_SESSIONS]
    else:
        out["sessions"] = [dict(x) for x in DEFAULT_SESSIONS]

    def _int(key, lo, hi):
        try:
            v = int(raw.get(key, d[key]))
        except Exception:
            v = d[key]
        return max(lo, min(hi, v))

    out["idle_min"] = _int("idle_min", 1, 120)
    out["away_min"] = _int("away_min", 2, 240)
    out["cooldown_min"] = _int("cooldown_min", 1, 240)
    out["daily_max"] = _int("daily_max", 1, 50)
    out["send_title"] = bool(raw.get("send_title", True))

    for key in ("private_words", "skip_words"):
        v = raw.get(key)
        if isinstance(v, list):
            out[key] = [str(x).strip() for x in v if str(x).strip()]
        else:
            out[key] = list(d[key])

    for key in ("prompt", "welcome_prompt"):
        v = raw.get(key)
        out[key] = str(v) if isinstance(v, str) and v.strip() else d[key]

    out["welcome_enabled"] = bool(raw.get("welcome_enabled", False))
    return out


# ───────────────────────── 时间窗口 ─────────────────────────

def _hm_to_min(s):
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return -1


def in_work_session(cfg, now=None):
    """当前是否落在任一工作时间段内。返回 (是否在内, 命中的段索引或 -1)。"""
    now = now or datetime.datetime.now()
    wd = now.weekday()                    # 0=周一
    cur = now.hour * 60 + now.minute
    for i, s in enumerate(cfg.get("sessions") or []):
        days = s.get("days") or []
        if days and wd not in days:
            continue
        a = _hm_to_min(s.get("start"))
        b = _hm_to_min(s.get("end"))
        if a < 0 or b < 0 or a == b:
            continue
        if a < b:
            if a <= cur < b:
                return True, i
        else:                             # 跨午夜
            if cur >= a or cur < b:
                return True, i
    return False, -1


# ───────────────────────── Windows 状态读取 ─────────────────────────

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def input_idle_seconds():
    """读 GetLastInputInfo，返回输入空闲秒数。失败返回 0。"""
    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        now = ctypes.windll.kernel32.GetTickCount()
        # 32 位计数器回绕
        diff = (now - lii.dwTime) & 0xFFFFFFFF
        if diff > 0x7FFFFFFF:
            diff = 0
        return max(0.0, diff / 1000.0)
    except Exception:
        return 0.0


def shrink_for_vision(pil_img, max_side=1024, quality=80):
    """把 PIL 图片压成长边 ≤ max_side 的 JPEG data URL（只用于发给视觉模型）。"""
    try:
        from PIL import Image
        img = pil_img.convert("RGB")
        w, h = img.size
        m = max(w, h)
        if m > max_side:
            s = max_side / float(m)
            img = img.resize((max(1, int(w * s)), max(1, int(h * s))),
                             Image.LANCZOS if hasattr(Image, "LANCZOS") else 0)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        import base64 as _b64
        return "data:image/jpeg;base64," + _b64.b64encode(buf.getvalue()).decode()
    except Exception as ex:
        import logging
        logging.getLogger("dafeiyu").warning(
            "[主动] shrink_for_vision 失败: %s: %s" % (type(ex).__name__, ex))
        return ""


def foreground_window():
    """返回 (hwnd, 进程名, 标题)。失败时各项为空。"""
    try:
        user32 = ctypes.windll.user32
        h = user32.GetForegroundWindow()
        if not h:
            return 0, "", ""
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(h, buf, 512)
        title = (buf.value or "").strip()
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        name = ""
        try:
            import psutil
            name = psutil.Process(pid.value).name() or ""
        except Exception:
            name = ""
        return int(h), name, title
    except Exception:
        return 0, "", ""


def screen_locked():
    """锁屏 / 屏保 / 休眠时返回 True（此时应暂停计时）。"""
    try:
        user32 = ctypes.windll.user32
        # 锁屏：OpenInputDesktop 失败
        hdesk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_READOBJECTS
        if not hdesk:
            return True
        user32.CloseDesktop(hdesk)
    except Exception:
        pass
    try:
        # 屏保运行中
        if ctypes.windll.user32.SystemParametersInfoW(0x0072, 0, None, 0):
            return True
    except Exception:
        pass
    return False


def is_fullscreen_foreground():
    """前台是否全屏应用（覆盖整块屏幕且无边框）。"""
    try:
        user32 = ctypes.windll.user32
        h = user32.GetForegroundWindow()
        if not h:
            return False
        rect = ctypes.wintypes.RECT() if hasattr(ctypes, "wintypes") else None
        # 避免依赖 wintypes，直接定义
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = RECT()
        if not user32.GetWindowRect(h, ctypes.byref(r)):
            return False
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        return (r.left <= 0 and r.top <= 0
                and r.right >= sw and r.bottom >= sh)
    except Exception:
        return False


# ───────────────────────── 状态检测 ─────────────────────────

ACTIVE = "active"
IDLE = "idle"
AWAY = "away"


class StateTracker:
    """只负责算「活跃 / 发呆 / 离开」，其余逻辑读 state。

    - 输入空闲直接读 GetLastInputInfo
    - 窗口切换只比 (hwnd, 进程名)
    - 发呆时长 = min(输入空闲, 窗口未切换)
    - 阈值每次进入新周期时在 ±20% 内随机一次
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.state = ACTIVE
        self.last_hwnd = 0
        self.last_proc = ""
        self.switch_ts = time.time()      # 最后切换时间
        self.idle_eff = 0.0               # 有效发呆秒数
        self._thresholds = None           # (idle_sec, away_sec) 本周期随机后的值
        self._fired_this_idle = False     # 一次发呆只搭话一次
        self._last_state_before = ACTIVE

    def _pick_thresholds(self, idle_min, away_min):
        """±20% 随机浮动（每个发呆周期只随机一次）。"""
        if self._thresholds is None:
            f1 = random.uniform(0.8, 1.2)
            f2 = random.uniform(0.8, 1.2)
            i = max(10.0, idle_min * 60.0 * f1)
            a = max(i + 30.0, away_min * 60.0 * f2)
            self._thresholds = (i, a)
        return self._thresholds

    def tick(self, idle_min, away_min, own_hwnds=()):
        """轮询一次，返回 (state, 有效发呆秒数, 前台信息 dict)。

        调用方保证只在工作时间段内调用。
        """
        if screen_locked():
            # 锁屏/屏保：暂停计时（不推进，也不改状态）
            return self.state, self.idle_eff, {"proc": "", "title": "", "hwnd": 0}

        hwnd, proc, title = foreground_window()

        # 桌宠自己的窗口不算切换
        own = False
        try:
            own = int(hwnd) in set(int(x) for x in own_hwnds if x)
        except Exception:
            own = False

        now = time.time()
        if not own and (hwnd != self.last_hwnd or proc != self.last_proc):
            self.last_hwnd = hwnd
            self.last_proc = proc
            self.switch_ts = now

        idle_in = input_idle_seconds()
        same_win = max(0.0, now - self.switch_ts)
        self.idle_eff = min(idle_in, same_win)

        i_th, a_th = self._pick_thresholds(idle_min, away_min)
        prev = self.state
        if self.idle_eff >= a_th:
            self.state = AWAY
        elif self.idle_eff >= i_th:
            self.state = IDLE
        else:
            self.state = ACTIVE
            # 回到活跃：本发呆周期结束，阈值下次重随机
            self._thresholds = None
            self._fired_this_idle = False

        self._last_state_before = prev
        return self.state, self.idle_eff, {
            "proc": proc, "title": title, "hwnd": hwnd, "own": own,
        }

    def can_fire(self):
        """这次发呆是否还没搭过话。"""
        return not self._fired_this_idle

    def mark_fired(self):
        self._fired_this_idle = True

    def was_away(self):
        """上一次 tick 的状态是否离开（用于「回来打招呼」）。"""
        return self._last_state_before == AWAY


# ───────────────────────── 本地过滤 ─────────────────────────

def _hit(text, words):
    """text 是否命中任一关键词（不区分大小写）。返回命中的词或空串。"""
    low = (text or "").lower()
    for w in words or []:
        w = (w or "").strip().lower()
        if w and w in low:
            return w
    return ""


def local_filter(info, cfg, cooldown_left_sec, used_today, chat_key_ok):
    """本地过滤：命中就返回原因字符串，否则返回 ""。

    顺序：没 Key → 每日上限 → 冷却 → 隐私 → 全屏 → 跳过词
    """
    if not chat_key_ok:
        return "没配聊天 Key"
    if used_today >= int(cfg.get("daily_max") or 0):
        return "已达每日上限"
    if cooldown_left_sec > 0:
        return "冷却中"
    proc = info.get("proc") or ""
    title = info.get("title") or ""
    both = "%s %s" % (proc, title)
    w = _hit(both, cfg.get("private_words"))
    if w:
        return "命中隐私黑名单（%s）" % w
    if is_fullscreen_foreground():
        return "前台是全屏应用"
    w = _hit(both, cfg.get("skip_words"))
    if w:
        return "命中跳过词（%s）" % w
    return ""


# ───────────────────────── 搭话提示词组装 ─────────────────────────

def build_messages(cfg, info, idle_sec, recent_lines, now=None):
    """组装发给聊天模型的 messages。返回 (system, user)。"""
    now = now or datetime.datetime.now()
    parts = []
    parts.append("当前时间：%s" % now.strftime("%Y-%m-%d %H:%M %A"))
    parts.append("主人已经 %d 分钟没动了。" % max(1, int(idle_sec // 60)))
    if cfg.get("send_title"):
        proc = info.get("proc") or "未知"
        title = (info.get("title") or "").strip()
        if len(title) > 60:
            title = title[:58] + "…"
        parts.append("前台程序：%s" % proc)
        if title:
            parts.append("窗口标题：%s" % title)
    else:
        parts.append("前台程序：%s" % (info.get("proc") or "未知"))
    if recent_lines:
        parts.append("你最近说过（别重复这些）：")
        for line in recent_lines[-5:]:
            parts.append("- " + str(line)[:60])
    user = "\n".join(parts)
    system = cfg.get("prompt") or DEFAULT_PROMPT
    return system, user


def build_observe_messages(info, shot_data_url, now=None):
    """截图观察：发给视觉模型的 messages。返回 None 表示不该发截图。"""
    if not shot_data_url:
        return None
    proc = info.get("proc") or "未知"
    title = (info.get("title") or "").strip()
    if len(title) > 60:
        title = title[:58] + "…"
    user = ("【进程】%s\n【窗口标题】%s\n请观察这张全屏截图。"
            % (proc, title))
    return [{"role": "system", "content": OBSERVE_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": shot_data_url}},
            ]}]


def build_welcome_messages(cfg, info, recent_lines, now=None):
    """回来打招呼的 messages。"""
    now = now or datetime.datetime.now()
    parts = ["当前时间：%s" % now.strftime("%Y-%m-%d %H:%M %A"),
             "主人刚回到电脑前。"]
    if cfg.get("send_title"):
        proc = info.get("proc") or "未知"
        title = (info.get("title") or "").strip()
        if len(title) > 60:
            title = title[:58] + "…"
        parts.append("前台程序：%s" % proc)
        if title:
            parts.append("窗口标题：%s" % title)
    if recent_lines:
        parts.append("你最近说过（别重复这些）：")
        for line in recent_lines[-5:]:
            parts.append("- " + str(line)[:60])
    system = cfg.get("welcome_prompt") or DEFAULT_WELCOME_PROMPT
    return system, "\n".join(parts)


# ───────────────────────── 回复清洗 ─────────────────────────

def clean_reply(text):
    """清洗模型回复：去 [SKIP]、去引号包裹、限长。

    返回空串表示「不搭话」。
    """
    t = (text or "").strip()
    if not t:
        return ""
    # 明确 SKIP
    if re.search(r"\[\s*SKIP\s*\]", t, re.I) or t.upper() == "SKIP":
        return ""
    # 去掉可能的 [SKIP] 残留与其他方括号标记
    t = re.sub(r"\[\s*SKIP\s*\]", "", t, flags=re.I).strip()
    # 去掉整句被引号包住的情况
    if len(t) >= 2 and t[0] in "\"'“「『" and t[-1] in "\"'”」』":
        t = t[1:-1].strip()
    # 去掉「宠物：」这类前缀
    t = re.sub(r"^[^：:\n]{1,6}[：:]\s*", "", t) if "\n" not in t[:12] else t
    # 太长就截到句号
    if len(t) > 80:
        cut = max(t.rfind(c, 0, 80) for c in "。！？!?~…")
        t = t[:cut + 1] if cut > 20 else t[:80]
    return t.strip()


# ───────────────────────── 调用模型 ─────────────────────────

def call_chat(base_url, api_key, model, system, user, timeout=60,
              max_tokens=300, temperature=0.9, top_p=0.95, messages=None):
    """调用模型（OpenAI 兼容），返回 (text, err)。同步阻塞，调用方放线程里。

    messages 传入时直接作为请求体（用于带 image_url 的截图观察），
    否则用 system/user 组装两条消息。
    """
    try:
        import requests
    except Exception as ex:
        return "", "requests 不可用: %s" % ex
    url = (base_url or "").rstrip("/") + "/chat/completions"
    if messages:
        msgs = messages
    else:
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    payload = {
        "model": model,
        "messages": msgs,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }
    try:
        r = requests.post(url,
                          headers={"Authorization": "Bearer " + (api_key or ""),
                                   "Content-Type": "application/json"},
                          json=payload, timeout=timeout)
        if r.status_code != 200:
            return "", "HTTP %s" % r.status_code
        d = r.json()
        ch = (d.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        return (msg.get("content") or "").strip(), ""
    except Exception as ex:
        return "", "%s: %s" % (type(ex).__name__, str(ex)[:80])


# ───────────────────────── 状态持久化（每日计数等） ─────────────────────────

class RuntimeState:
    """运行期状态：每日计数、冷却、连续无视次数、最近说过的话。

    持久化到 proactive_state.json（用户数据目录），重启保留计数与最近台词。
    """

    def __init__(self, path):
        self.path = path
        self.day = ""
        self.used = 0
        self.last_fire_ts = 0.0
        self.ignored_streak = 0
        self.interval_mult = 1.0
        self.blocked_today = False
        self.recent = []
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                return
            self.day = str(d.get("day") or "")
            self.used = int(d.get("used") or 0)
            self.last_fire_ts = float(d.get("last_fire_ts") or 0.0)
            self.ignored_streak = int(d.get("ignored_streak") or 0)
            self.interval_mult = float(d.get("interval_mult") or 1.0)
            self.blocked_today = bool(d.get("blocked_today", False))
            self.recent = list(d.get("recent") or [])[-10:]
        except Exception:
            pass

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({
                    "day": self.day, "used": self.used,
                    "last_fire_ts": self.last_fire_ts,
                    "ignored_streak": self.ignored_streak,
                    "interval_mult": self.interval_mult,
                    "blocked_today": self.blocked_today,
                    "recent": self.recent[-10:],
                }, f, ensure_ascii=False)
        except Exception:
            pass

    def roll_day(self):
        """跨天重置。"""
        today = datetime.date.today().isoformat()
        if self.day != today:
            self.day = today
            self.used = 0
            self.blocked_today = False
            self.ignored_streak = 0
            self.interval_mult = 1.0
            self.save()

    def mark_fire(self, line):
        self.last_fire_ts = time.time()
        self.used += 1
        if line:
            self.recent.append(line)
            self.recent = self.recent[-10:]
        self.save()

    def mark_clicked(self):
        """被点开 → 清零无视计数、缩短间隔。"""
        self.ignored_streak = 0
        self.interval_mult = 1.0
        self.save()

    def mark_ignored(self):
        """被无视 → 记一次；连续 3 次翻倍并当天停止。"""
        self.ignored_streak += 1
        if self.ignored_streak >= 3:
            self.interval_mult = min(8.0, self.interval_mult * 2.0)
            self.ignored_streak = 0
            self.blocked_today = True
        self.save()
