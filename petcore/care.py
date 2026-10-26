# -*- coding: utf-8 -*-
r"""care.py — 主动关心触发引擎（阶段①：只做框架）

职责：
  - 管理每个关心类型的冷却、每日上限、优先级、互斥
  - 维护 care_state.json（原子写、损坏自愈）
  - 提供「本周期该触发哪一条」的决策（同一时刻最多一条）

不负责：
  - 台词、按钮、开关（后续阶段）
  - 静默判断的具体实现（由调用方传入 busy 原因）
  - 实际弹气泡（由桌宠调用 _fire_reminder）

计数规则（重要）：
  只有气泡真的显示出来（mark_fired）才记当日次数、冷却、last_fire。
  被静默规则或 _fire_reminder 的二次判断拦住时调用 mark_deferred，
  不消耗次数、不进入正式冷却，只按短间隔顺延重试。
"""
import json
import logging
import os
import shutil
import time

_logger = logging.getLogger("dafeiyu")


def _log(msg, level=logging.INFO):
    try:
        _logger.log(level, msg)
    except Exception:
        pass


# 被拦截时顺延重试的间隔（秒）
DEFER_RETRY_SEC = 60

# 启动后多久内不触发（秒）
STARTUP_GRACE_SEC = 300

# 一条关心弹出后，全局互斥窗口（秒）
ACTIVE_HOLD_SEC = 8

# 同类连续忽略多少次后当天降频停提
IGNORE_STREAK_LIMIT = 3

# 「别吵我」默认静默小时数
MUTE_HOURS_DEFAULT = 4

# 连续多少天点「别提这个」后提示是否关闭该类
MUTE_DAYS_HINT = 3


class CareState:
    """关心引擎的状态存储与决策。

    状态文件结构见 care_state.json；写入用「临时文件 + os.replace」保证原子。
    读到损坏文件时先备份成 .corrupt.<时间戳>，再重建空状态，绝不抛异常。
    """

    def __init__(self, path, kinds=None):
        self.path = path
        self.kinds = dict(kinds or {})
        self.day = ""
        self.used = {}          # kind -> 当日已触发次数
        self.last_fire = {}     # kind -> 上次成功触发的时间戳
        self.deferred_until = {}  # kind -> 顺延重试的最早时间戳
        self.started_at = 0.0   # 程序启动时间（用于前 5 分钟保护）
        self.active_until = 0.0  # 全局互斥窗口结束时间
        self.ignored_streak = {}  # kind -> 连续忽略次数（气泡超时无操作）
        self.muted_kinds = {}     # kind -> 当天不再出现的日期（YYYY-MM-DD）
        self.muted_until = 0.0    # 「别吵我」全局静默截止时间戳
        self.muted_days = {}      # kind -> 连续多少天被「别提这个」
        self.stats = {}           # kind -> {"fired":n,"acked":n,"ignored":n}
        # 好感度（阶段⑥）：只影响台词，不影响功能
        self.affection = 50
        self.aff_day = ""         # 当天日期（跨天重置每日上限）
        self.aff_gain_today = 0   # 当天靠聊天加了多少
        self.aff_lose_today = 0   # 当天掉了多少
        self.aff_last_active = "" # 最后一次互动的日期（判断久未互动）
        self.load()

    # ---------- 持久化 ----------

    @staticmethod
    def _today(now=None):
        try:
            return time.strftime("%Y-%m-%d", time.localtime(now))
        except Exception:
            return ""

    def load(self):
        try:
            if not os.path.exists(self.path):
                self._reset_fields()
                return
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("顶层不是对象")
            self.day = str(data.get("day") or "")
            self.used = self._clean_map(data.get("used"))
            self.last_fire = self._clean_map(data.get("last_fire"), float)
            self.deferred_until = self._clean_map(
                data.get("deferred_until"), float)
            try:
                self.started_at = float(data.get("started_at") or 0)
            except Exception:
                self.started_at = 0.0
            try:
                self.active_until = float(data.get("active_until") or 0)
            except Exception:
                self.active_until = 0.0
            self.ignored_streak = self._clean_map(
                data.get("ignored_streak"), int)
            self.muted_kinds = self._clean_map(
                data.get("muted_kinds"), str)
            try:
                self.muted_until = float(data.get("muted_until") or 0)
            except Exception:
                self.muted_until = 0.0
            self.muted_days = self._clean_map(data.get("muted_days"), int)
            try:
                self.affection = int(data.get("affection") or 50)
            except Exception:
                self.affection = 50
            self.aff_day = str(data.get("aff_day") or "")
            try:
                self.aff_gain_today = int(data.get("aff_gain_today") or 0)
            except Exception:
                self.aff_gain_today = 0
            try:
                self.aff_lose_today = int(data.get("aff_lose_today") or 0)
            except Exception:
                self.aff_lose_today = 0
            self.aff_last_active = str(data.get("aff_last_active") or "")
            st = data.get("stats")
            self.stats = {}
            if isinstance(st, dict):
                for k, v in st.items():
                    if isinstance(v, dict):
                        self.stats[str(k)] = {
                            "fired": int(v.get("fired") or 0),
                            "acked": int(v.get("acked") or 0),
                            "ignored": int(v.get("ignored") or 0),
                        }
        except Exception as ex:
            # 损坏：备份后重建，绝不让启动失败
            self._quarantine(ex)
            self._reset_fields()

    def _clean_map(self, raw, caster=int):
        out = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    out[str(k)] = caster(v)
                except Exception:
                    continue
        return out

    def _quarantine(self, ex):
        """把损坏的状态文件备份一份，便于事后排查。"""
        try:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            bak = "%s.corrupt.%s" % (self.path, stamp)
            shutil.copy2(self.path, bak)
            _log("[关心] care_state.json 损坏已备份为 %s（原因 %s）"
                 % (os.path.basename(bak), type(ex).__name__), logging.WARNING)
        except Exception as ex2:
            _log("[关心] 备份损坏状态失败: %s: %s"
                 % (type(ex2).__name__, ex2), logging.WARNING)

    def _reset_fields(self):
        self.day = self._today()
        self.used = {}
        self.last_fire = {}
        self.deferred_until = {}
        self.started_at = 0.0
        self.active_until = 0.0
        self.ignored_streak = {}
        self.muted_kinds = {}
        self.muted_until = 0.0
        self.muted_days = {}
        self.stats = {}
        self.affection = 50
        self.aff_day = ""
        self.aff_gain_today = 0
        self.aff_lose_today = 0
        self.aff_last_active = ""

    def save(self):
        """原子写：写 .tmp 再 os.replace。"""
        try:
            d = os.path.dirname(os.path.abspath(self.path))
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            data = {
                "version": 1,
                "day": self.day,
                "used": dict(self.used),
                "last_fire": dict(self.last_fire),
                "deferred_until": dict(self.deferred_until),
                "started_at": self.started_at,
                "active_until": self.active_until,
                "ignored_streak": dict(self.ignored_streak),
                "muted_kinds": dict(self.muted_kinds),
                "muted_until": self.muted_until,
                "muted_days": dict(self.muted_days),
                "stats": {k: dict(v) for k, v in self.stats.items()},
                "affection": int(self.affection),
                "aff_day": self.aff_day,
                "aff_gain_today": int(self.aff_gain_today),
                "aff_lose_today": int(self.aff_lose_today),
                "aff_last_active": self.aff_last_active,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as ex:
            _log("[关心] 保存 care_state.json 失败: %s: %s"
                 % (type(ex).__name__, ex), logging.ERROR)

    # ---------- 跨天 ----------

    def roll_day(self, now=None):
        """跨天自动清零当日计数。返回是否发生了重置。"""
        today = self._today(now)
        if self.day != today:
            self.day = today
            self.used = {}
            self.deferred_until = {}
            self.ignored_streak = {}
            # 跨天：清掉昨天设的「别提这个」（muted_kinds 存的就是日期）
            self.muted_kinds = {}
            _log("[关心] 跨天重置当日计数")
            return True
        return False

    # ---------- 启动保护 ----------

    def mark_started(self, now=None):
        """记录启动时间（前 STARTUP_GRACE_SEC 内不触发）。"""
        self.started_at = float(now if now is not None else time.time())
        self.save()

    def startup_grace_left(self, now=None):
        if self.started_at <= 0:
            return 0
        cur = float(now if now is not None else time.time())
        left = STARTUP_GRACE_SEC - (cur - self.started_at)
        return max(0.0, left)

    # ---------- 查询 ----------

    def used_today(self, kind):
        return int(self.used.get(kind) or 0)

    def cooldown_left(self, kind, now, cooldown_sec):
        """距离下次可触发还剩多少秒。"""
        last = float(self.last_fire.get(kind) or 0)
        if last <= 0:
            return 0.0
        return max(0.0, (last + cooldown_sec) - float(now))

    def defer_left(self, kind, now):
        until = float(self.deferred_until.get(kind) or 0)
        return max(0.0, until - float(now))

    def global_left(self, now):
        return max(0.0, self.active_until - float(now))

    # ---------- 决策 ----------

    def pick_next(self, now, candidates):
        """挑出本周期该触发的一条。

        candidates: [(kind, cooldown_sec, daily_max), ...]
        返回 (kind 或 None, 原因字符串)。
        规则：全局互斥 > 启动保护 > 每日上限 > 冷却/顺延 > 优先级最高。
        """
        cur = float(now)
        if self.global_muted_left(cur) > 0:
            return None, "全局静默中"
        if self.global_left(cur) > 0:
            return None, "全局互斥中"
        if self.startup_grace_left(cur) > 0:
            return None, "启动保护中"

        ready = []
        for kind, cd_sec, daily_max in candidates:
            if self.is_kind_muted_today(kind, cur):
                continue
            if self.is_streak_blocked(kind):
                continue
            if daily_max > 0 and self.used_today(kind) >= daily_max:
                continue
            if self.cooldown_left(kind, cur, cd_sec) > 0:
                continue
            if self.defer_left(kind, cur) > 0:
                continue
            ready.append(kind)

        if not ready:
            return None, "无到期项"

        # 优先级降序；同优先级按名字稳定排序，避免抖动
        ready.sort(key=lambda k: (-int(self.kinds.get(k, {}).get("priority", 0)), k))
        return ready[0], ""

    # ---------- 记账 ----------

    def mark_fired(self, kind, now=None):
        """气泡确实显示出来了：记次数、刷新 last_fire、开全局互斥。"""
        cur = float(now if now is not None else time.time())
        self.roll_day(cur)
        self.used[kind] = self.used_today(kind) + 1
        self.last_fire[kind] = cur
        self.deferred_until.pop(kind, None)
        self.active_until = cur + ACTIVE_HOLD_SEC
        self.save()
        _log("[关心] %s 触发（今日第 %d 次）" % (kind, self.used[kind]))

    def mark_deferred(self, kind, now=None, retry_sec=DEFER_RETRY_SEC):
        """被拦住：不记次数、不进入正式冷却，只按短间隔顺延。"""
        cur = float(now if now is not None else time.time())
        self.deferred_until[kind] = cur + max(1, int(retry_sec))
        self.save()
        _log("[关心] %s 顺延重试（%d 秒后）" % (kind, int(retry_sec)))

    # ---------- 降频与静默（阶段③）----------

    def _today_str(self, now=None):
        return self._today(now)

    def is_kind_muted_today(self, kind, now=None):
        """该类今天是否被「别提这个」关掉。"""
        return self.muted_kinds.get(kind) == self._today_str(now)

    def global_muted_left(self, now=None):
        """「别吵我」剩余秒数。"""
        cur = float(now if now is not None else time.time())
        return max(0.0, self.muted_until - cur)

    def is_streak_blocked(self, kind):
        """同类连续忽略是否已达降频阈值。"""
        return int(self.ignored_streak.get(kind) or 0) >= IGNORE_STREAK_LIMIT

    def _stat(self, kind):
        st = self.stats.get(kind)
        if not isinstance(st, dict):
            st = {"fired": 0, "acked": 0, "ignored": 0}
            self.stats[kind] = st
        return st

    def note_acknowledged(self, kind, now=None):
        """用户点了「知道了」：连续忽略清零。"""
        self.ignored_streak.pop(kind, None)
        st = self._stat(kind)
        st["acked"] = int(st.get("acked") or 0) + 1
        self.save()
        _log("[关心] %s 已确认（累计 %d 次）" % (kind, st["acked"]))

    def note_ignored(self, kind, now=None):
        """气泡超时无操作 = 忽略：streak +1，达阈值当天停提。"""
        cur = int(self.ignored_streak.get(kind) or 0) + 1
        self.ignored_streak[kind] = cur
        st = self._stat(kind)
        st["ignored"] = int(st.get("ignored") or 0) + 1
        self.save()
        if cur >= IGNORE_STREAK_LIMIT:
            _log("[关心] %s 连续忽略 %d 次，当天降频停提" % (kind, cur))
        else:
            _log("[关心] %s 被忽略（第 %d 次）" % (kind, cur))

    def mute_kind_today(self, kind, now=None):
        """「别提这个」：该类当天不再出现；返回连续天数（用于提示）。"""
        self.muted_kinds[kind] = self._today_str(now)
        days = int(self.muted_days.get(kind) or 0) + 1
        self.muted_days[kind] = days
        self.save()
        _log("[关心] %s 当天不再出现（连续 %d 天）" % (kind, days))
        return days

    def mute_all(self, hours=None, now=None):
        """「别吵我」：全局静默 N 小时。"""
        cur = float(now if now is not None else time.time())
        h = MUTE_HOURS_DEFAULT if hours is None else max(1, int(hours))
        self.muted_until = cur + h * 3600
        self.save()
        _log("[关心] 全局静默 %d 小时" % h)

    def clear_mute_all(self):
        self.muted_until = 0.0
        self.save()

    def snooze_kind(self, kind, factor=2, now=None):
        """「等会儿」：冷却翻倍（用 deferred 表示，避免污染正式冷却）。"""
        cur = float(now if now is not None else time.time())
        cd = 0
        for k, spec in self.kinds.items():
            if k == kind:
                cd = int(spec.get("_cooldown_sec") or 0)
                break
        base = cd if cd > 0 else 3600
        self.deferred_until[kind] = cur + base * max(1, int(factor))
        self.save()
        _log("[关心] %s 延后（%d 倍冷却）" % (kind, factor))

    # ---------- 好感度（阶段⑥）----------

    AFF_INIT = 50
    AFF_MIN = 30
    AFF_MAX = 100
    AFF_GAIN_CHAT = 1
    AFF_GAIN_CHAT_DAILY_MAX = 10
    AFF_GAIN_ACK = 2
    AFF_LOSE_IGNORE = 1
    AFF_LOSE_IDLE_DAY = 3
    AFF_IDLE_DAYS = 3
    AFF_DAILY_DOWN_MAX = 3

    def _aff_roll_day(self, now=None):
        """好感度的跨天重置（独立于关心计数的 day）。"""
        today = self._today(now)
        if self.aff_day != today:
            self.aff_day = today
            self.aff_gain_today = 0
            self.aff_lose_today = 0
            return True
        return False

    def _aff_clamp(self, v):
        try:
            v = int(v)
        except Exception:
            v = self.AFF_INIT
        return max(self.AFF_MIN, min(self.AFF_MAX, v))

    def _aff_up(self, delta, now=None):
        """加好感（受每日聊天加成上限约束）。"""
        self._aff_roll_day(now)
        d = max(0, int(delta))
        if d <= 0:
            return self.affection
        self.affection = self._aff_clamp(self.affection + d)
        self.save()
        return self.affection

    def _aff_down(self, delta, now=None):
        """掉好感（受每日掉点上限约束）。"""
        self._aff_roll_day(now)
        d = max(0, int(delta))
        if d <= 0:
            return self.affection
        left = max(0, self.AFF_DAILY_DOWN_MAX - int(self.aff_lose_today))
        d = min(d, left)
        if d <= 0:
            return self.affection
        self.aff_lose_today = int(self.aff_lose_today) + d
        self.affection = self._aff_clamp(self.affection - d)
        self.save()
        return self.affection

    def note_interaction(self, now=None):
        """用户发消息：+1（每天最多 +10），并刷新最后活跃日期。"""
        try:
            self._aff_roll_day(now)
            self.aff_last_active = self._today(now)
            if self.aff_gain_today < self.AFF_GAIN_CHAT_DAILY_MAX:
                self.aff_gain_today = int(self.aff_gain_today) + self.AFF_GAIN_CHAT
                self.affection = self._aff_clamp(
                    self.affection + self.AFF_GAIN_CHAT)
                self.save()
            return self.affection
        except Exception as ex:
            _log("[好感] 记录互动失败: %s: %s"
                 % (type(ex).__name__, ex), logging.DEBUG)
            return self.affection

    def note_acknowledged_aff(self, now=None):
        """回应关心（点知道了）：+2。"""
        return self._aff_up(self.AFF_GAIN_ACK, now)

    def note_ignored_aff(self, now=None):
        """忽略关心：-1（受每日上限）。"""
        return self._aff_down(self.AFF_LOSE_IGNORE, now)

    def check_idle_decay(self, now=None):
        """连续多天未互动 → 每天掉 3 点（最低到 30，不做愧疚绑架）。

        返回本次掉了几点。
        """
        try:
            import datetime as _dt
            today = self._today(now)
            if not self.aff_last_active:
                # 从未互动过：以今天为基准，不掉
                self.aff_last_active = today
                self.save()
                return 0
            try:
                d0 = _dt.date.fromisoformat(today)
                d1 = _dt.date.fromisoformat(self.aff_last_active)
            except Exception:
                return 0
            idle_days = (d0 - d1).days
            if idle_days < self.AFF_IDLE_DAYS:
                return 0
            before = self.affection
            self._aff_down(self.AFF_LOSE_IDLE_DAY, now)
            got = before - self.affection
            if got > 0:
                _log("[好感] 已 %d 天未互动，好感 -%d（当前 %d）"
                     % (idle_days, got, self.affection))
            return got
        except Exception as ex:
            _log("[好感] 久未互动检查失败: %s: %s"
                 % (type(ex).__name__, ex), logging.DEBUG)
            return 0

    def affection_tier(self):
        """当前好感度区间名（cold / normal / clingy）。"""
        try:
            v = int(self.affection)
        except Exception:
            v = self.AFF_INIT
        if v <= 30:
            return "cold"
        if v <= 60:
            return "normal"
        return "clingy"

    def reset_timer(self, kind, now=None):
        """用户改了间隔/重新打开开关：把该类的计时清零重来。"""
        cur = float(now if now is not None else time.time())
        self.last_fire.pop(kind, None)
        self.deferred_until.pop(kind, None)
        self.save()
        _log("[关心] %s 计时已重置" % kind)
