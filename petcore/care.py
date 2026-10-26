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
        if self.global_left(cur) > 0:
            return None, "全局互斥中"
        if self.startup_grace_left(cur) > 0:
            return None, "启动保护中"

        ready = []
        for kind, cd_sec, daily_max in candidates:
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

    def reset_timer(self, kind, now=None):
        """用户改了间隔/重新打开开关：把该类的计时清零重来。"""
        cur = float(now if now is not None else time.time())
        self.last_fire.pop(kind, None)
        self.deferred_until.pop(kind, None)
        self.save()
        _log("[关心] %s 计时已重置" % kind)
