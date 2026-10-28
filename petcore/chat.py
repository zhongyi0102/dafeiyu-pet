# -*- coding: utf-8 -*-
"""大肥鱼桌宠 —— 聊天窗口、消息气泡、记忆存储与后台请求线程。

从 桌宠.py 拆分（第 23 批重构）。
"""
import base64
import datetime
import html
import json
import logging
import math
import os
import random
import re
import sys
import threading
import time
import traceback

import requests

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

from petcore.consts import *          # noqa: F401,F403
from petcore.consts import (log, LOG, APP_DIR, BUNDLE_DIR, SPRITE_DIR,   # noqa: F401
                            CONFIG_PATH, CHAT_PATH, MEMORY_PATH, LOG_PATH,
                            GPU_AVAILABLE,
                            _FRAME_CACHE, _CHAT_THEME, _CHAT_CODE_RE,
                            _LENGTH_LINE_RE, _RECT, _LOG_READY,
                            _coerce_pet, _default_features, _default_games,
                            _default_pet, _default_remind_extras,
                            _default_reminders, _default_usage,
                            _excepthook, _html_escape_text, _is_number,
                            _merge_remind_extras, _migrate_legacy_features,
                            _setup_logging, _thread_excepthook, _valid_hhmm)

class MemoryStore:
    """长期记忆：memory.json，按 id 顺序稳定拼接进 system 消息（改了才会让前缀缓存失效）

    每条记忆字段：
      id / text / time             —— 原有字段
      source                       —— manual（用户手动）/ auto（模型自动提取）
      strength                     —— 固定 1.0（保留字段，衰减不再依赖它）
      last_access                  —— 衰减基准（写入时间或最后一次被提到的时间）
      mention_count                —— 被再次提到的次数，用来延长半衰期
      origin_session / origin_time —— 自动记忆的来源会话与时间
    """

    MAX_AUTO = 200              # 自动记忆条数上限
    BASE_HALFLIFE_DAYS = 7.0    # 基础半衰期（天）
    MAX_HALFLIFE_DAYS = 28.0    # 半衰期上限（天）
    DECAY_THRESHOLD = 0.15      # 保留率低于此值不再注入（但不删除）
    MAX_INJECT = 15             # 每次最多注入多少条（旧逻辑，保留兼容）
    MAX_RELEVANT = 5            # 每轮按相关性最多追加几条

    def __init__(self, path):
        self.path = path
        self.items = []
        self.next_id = 1
        self._block_cache_day = None
        self._block_cache = ""
        self.load()

    # ---------- 时间工具 ----------

    @staticmethod
    def now_str():
        return time.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def parse_ts(s):
        """把 'YYYY-MM-DD HH:MM:SS' 解析成 time.struct_time；失败返回 None。"""
        try:
            return time.strptime(str(s or "").strip(), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    @classmethod
    def day_index(cls, ts):
        """把 struct_time 折成「自然日序号」，用于按天取整。"""
        return int(time.mktime((ts.tm_year, ts.tm_mon, ts.tm_mday,
                                0, 0, 0, 0, 0, -1)) // 86400)

    # ---------- 读写 ----------

    @classmethod
    def normalize(cls, m):
        """补齐缺失字段（兼容老数据），返回规范化 dict。"""
        text = str(m.get("text") or "").strip()
        src = str(m.get("source") or "manual").strip().lower()
        if src not in ("manual", "auto"):
            src = "manual"
        t0 = str(m.get("time") or "").strip() or cls.now_str()
        last = str(m.get("last_access") or "").strip() or t0
        try:
            mc = int(m.get("mention_count") or 0)
        except Exception:
            mc = 0
        # event_date：有明确日期的事件（YYYY-MM-DD），记忆回访用
        ed = str(m.get("event_date") or "").strip()
        if len(ed) != 10 or ed[4] != "-" or ed[7] != "-":
            ed = ""
        return {
            "id": int(m.get("id") or 0),
            "text": text,
            "time": t0,
            "source": src,
            "strength": 1.0,
            "last_access": last,
            "mention_count": max(0, mc),
            "origin_session": m.get("origin_session") or None,
            "origin_time": m.get("origin_time") or None,
            "event_date": ed,
            "revisit_done": bool(m.get("revisit_done", False)),
            "pinned": bool(m.get("pinned", False)),
        }

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                items = data.get("memories") or []
                self.items = [self.normalize(m) for m in items
                              if isinstance(m, dict)
                              and (m.get("text") or "").strip()]
                for m in self.items:
                    m["id"] = int(m.get("id") or 0)
                max_id = max([m["id"] for m in self.items] or [0])
                self.next_id = max(int(data.get("next_id") or 1), max_id + 1)
        except Exception as ex:
            log("读取 memory.json 失败: %s" % ex, logging.WARNING)
            self.items, self.next_id = [], 1
        self.invalidate_block()

    def save(self):
        try:
            tmp = self.path + ".tmp"
            data = {
                "memories": self.sorted_items(),
                "next_id": self.next_id,
                "version": 2,
                "updated": self.now_str(),
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as ex:
            log("保存 memory.json 失败: %s" % ex, logging.ERROR)
        self.invalidate_block()

    def sorted_items(self):
        return sorted(self.items, key=lambda m: m.get("id", 0))

    def get(self, mem_id):
        for m in self.items:
            if m.get("id") == mem_id:
                return m
        return None

    def auto_items(self):
        return [m for m in self.items if m.get("source") == "auto"]

    # ---------- 增删改 ----------

    def add(self, text, source="manual", origin_session=None,
            origin_time=None, event_date=None, pinned=False):
        """新增一条记忆。source: manual（默认）/ auto（自动提取）。

        pinned=True 表示「核心记忆」：每轮必定注入，不看相关性、不衰减。
        """
        text = (text or "").strip()
        if not text:
            return None
        now = self.now_str()
        src = "auto" if source == "auto" else "manual"
        ed = str(event_date or "").strip()
        if len(ed) != 10 or ed[4] != "-" or ed[7] != "-":
            ed = ""
        item = {
            "id": self.next_id,
            "text": text,
            "time": now,
            "source": src,
            "strength": 1.0,
            "last_access": now,
            "mention_count": 0,
            "origin_session": origin_session,
            "origin_time": (origin_time or now) if src == "auto" else None,
            "event_date": ed,
            "revisit_done": False,
            "pinned": bool(pinned),
        }
        self.next_id += 1
        self.items.append(item)
        if src == "auto":
            self.trim_auto()
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

    def set_pinned(self, mem_id, pinned=True):
        """切换「核心记忆」标记：pinned=True 每轮必带，不看相关性。"""
        try:
            for m in self.items:
                if m.get("id") == mem_id:
                    m["pinned"] = bool(pinned)
                    self.invalidate_block()
                    self.save()
                    return True
        except Exception as ex:
            log("[记忆] 切换核心标记失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
        return False

    def pending_revisits(self, today_str=None):
        """返回今天该回访的记忆（每条最多回访一次）。

        规则：
          - 只取 source=auto 且 event_date 非空、revisit_done=False
          - 事件当天 或 事件第二天（问「昨天怎么样」）
        """
        import datetime as _dt
        try:
            today = today_str or _dt.date.today().isoformat()
            d0 = _dt.date.fromisoformat(today)
        except Exception:
            return []
        out = []
        for m in self.sorted_items():
            if m.get("source") != "auto":
                continue
            if m.get("revisit_done"):
                continue
            ed = (m.get("event_date") or "").strip()
            if not ed:
                continue
            try:
                d = _dt.date.fromisoformat(ed)
            except Exception:
                continue
            delta = (d0 - d).days
            if delta in (0, 1):
                out.append(m)
        return out

    def mark_revisited(self, mem_id):
        """标记已回访（每条最多一次）。"""
        m = self.get(mem_id)
        if m is None:
            return False
        m["revisit_done"] = True
        self.save()
        return True

    def reinforce(self, mem_id, text=None):
        """自动记忆被再次提到：mention_count +1、last_access 刷新。

        只对 source == "auto" 的条目生效；手动记忆不参与强化。
        """
        m = self.get(mem_id)
        if m is None or m.get("source") != "auto":
            return False
        t = (text or "").strip()
        if t:
            m["text"] = t
        m["mention_count"] = int(m.get("mention_count") or 0) + 1
        m["last_access"] = self.now_str()
        self.save()
        return True

    def remove(self, mem_id):
        before = len(self.items)
        self.items = [m for m in self.items if m.get("id") != mem_id]
        if len(self.items) != before:
            self.save()
            return True
        return False

    # ---------- 自动记忆容量 ----------

    def trim_auto(self):
        """自动记忆超过上限时，先清理最淡（保留率最低）的条目。"""
        autos = self.auto_items()
        over = len(autos) - self.MAX_AUTO
        if over <= 0:
            return 0
        autos.sort(key=lambda m: self.retention(m))
        drop = {m.get("id") for m in autos[:over]}
        self.items = [m for m in self.items if m.get("id") not in drop]
        log("[记忆] 自动记忆超过 %d 条，清理最淡的 %d 条（id=%s）"
            % (self.MAX_AUTO, len(drop), sorted(drop)))
        return len(drop)

    # ---------- 遗忘曲线 ----------

    def halflife_days(self, m):
        """半衰期：7 × (1 + 0.5 × mention_count)，上限 28 天。"""
        if m.get("source") != "auto":
            return float("inf")
        try:
            mc = int(m.get("mention_count") or 0)
        except Exception:
            mc = 0
        return min(self.MAX_HALFLIFE_DAYS,
                   self.BASE_HALFLIFE_DAYS * (1.0 + 0.5 * mc))

    def retention(self, m, now=None):
        """保留率 R ∈ (0, 1]。手动记忆恒为 1.0（永不衰减）。

        now 可注入（测试用），默认取当前本地时间。
        按自然日取整，因此同一天内结果恒定。
        """
        if m.get("source") != "auto":
            return 1.0
        ts = self.parse_ts(m.get("last_access")) or self.parse_ts(m.get("time"))
        if ts is None:
            return 1.0
        try:
            cur = now or time.localtime()
            days = max(0, self.day_index(cur) - self.day_index(ts))
            hl = self.halflife_days(m)
            if hl <= 0 or hl == float("inf"):
                return 1.0
            return 0.5 ** (days / hl)
        except Exception:
            return 1.0

    def invalidate_block(self):
        self._block_cache_day = None
        self._block_cache = ""

    def inject_items(self, now=None):
        """挑出本轮要注入的记忆，按保留率降序。

        - 手动记忆永不衰减，恒为 1.0，排在最前（按 id）
        - 自动记忆低于 DECAY_THRESHOLD 不注入（但不删除）
        - 最多 MAX_INJECT 条
        """
        try:
            now = now or time.localtime()
            scored = []
            for m in self.sorted_items():
                r = self.retention(m, now)
                if r < self.DECAY_THRESHOLD:
                    continue
                scored.append((r, m))
            # 保留率降序；同率时手动优先、再按 id 稳定排序
            scored.sort(key=lambda x: (-x[0],
                                      0 if x[1].get("source") == "manual" else 1,
                                      x[1].get("id", 0)))
            return [m for _r, m in scored[:self.MAX_INJECT]]
        except Exception as ex:
            log("[记忆] 计算注入列表失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            return self.sorted_items()

    # ---------- 相关性打分（方案 B） ----------

    # 中文虚词/高频字，不参与相关性（否则「的」「了」会命中一切）
    _STOP_CHARS = set(
        "的了是在我有和就不人都一个上也很到说要去你会着没有看好自己这"
        "里么什么那为与但而或及等被把从对向让给还又再已经过")
    _STOP_WORDS = (
        "什么", "怎么", "为什么", "可以", "这个", "那个", "一下", "现在",
        "还是", "就是", "但是", "因为", "所以", "然后", "如果", "虽然",
        "我们", "你们", "他们", "自己", "时候", "东西", "事情")

    @classmethod
    def _tokens(cls, text):
        """把文本切成相关性候选词（子串匹配用）。

        策略：去停用词 → 只留中文/字母/数字 → 按空白切段。
        每段保留：整段 + 所有 2~4 字滑窗。
        滑窗只用于「查询侧」，记忆侧用整段匹配即可（见 relevance）。
        """
        try:
            s = str(text or "")
            for w in cls._STOP_WORDS:
                s = s.replace(w, " ")
            s = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", " ", s)
            out = set()
            for chunk in s.split():
                low = chunk.lower()
                if len(low) < 2:
                    continue
                out.add(low)
                # 中文段：滑窗切 2~4 字，覆盖「白屏」这类词
                if re.search(r"[\u4e00-\u9fa5]", chunk):
                    n = len(chunk)
                    for size in (2, 3, 4):
                        for i in range(n - size + 1):
                            out.add(chunk[i:i + size].lower())
            return out
        except Exception:
            return set()

    @classmethod
    def relevance(cls, m, query_tokens):
        """记忆与当前问题的相关性 ∈ [0, 1]。

        记忆侧用**整段子串匹配**：
        查询里的每个候选词，只要作为子串出现在记忆原文里就算命中。
        分数 = 命中词数 / 查询词数。
        """
        if not query_tokens:
            return 0.0
        try:
            raw = str(m.get("text") or "").lower()
            if not raw:
                return 0.0
            hit = sum(1 for t in query_tokens if t and t in raw)
            return hit / float(len(query_tokens))
        except Exception:
            return 0.0

    @classmethod
    def query_tokens(cls, text):
        """查询侧词表：整段 + 2 字滑窗，去掉纯虚词组合。

        比 _tokens 窄：只取 2 字和整段，避免「屏怎」「怎么解」这类跨词噪音
        稀释命中率。
        """
        try:
            s = str(text or "")
            for w in cls._STOP_WORDS:
                s = s.replace(w, " ")
            s = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", " ", s)
            out = set()
            for chunk in s.split():
                low = chunk.lower()
                if len(low) < 2:
                    continue
                out.add(low)
                if re.search(r"[\u4e00-\u9fa5]", chunk) and len(chunk) >= 3:
                    for i in range(len(chunk) - 1):
                        bg = chunk[i:i + 2]
                        # 两字都非虚词才算有效词
                        if not (set(bg) & cls._STOP_CHARS):
                            out.add(bg.lower())
            return out
        except Exception:
            return set()

    def core_items(self, now=None):
        """核心记忆：手动记忆 + pinned 标记，按 id 稳定排序（前缀缓存稳定）。"""
        try:
            out = []
            for m in self.sorted_items():
                if m.get("source") != "auto" or m.get("pinned"):
                    out.append(m)
            return out
        except Exception:
            return []

    def relevant_items(self, query, now=None, limit=None):
        """相关记忆：按 (相关性 × 保留率) 排序，排除已在核心里的。"""
        try:
            limit = limit if limit is not None else self.MAX_RELEVANT
            now = now or time.localtime()
            qt = self.query_tokens(query)
            if not qt:
                return []
            core_ids = {m.get("id") for m in self.core_items(now)}
            scored = []
            for m in self.sorted_items():
                if m.get("id") in core_ids:
                    continue
                r = self.retention(m, now)
                if r < self.DECAY_THRESHOLD:
                    continue
                rel = self.relevance(m, qt)
                if rel <= 0:
                    continue
                scored.append((rel * r, rel, m))
            scored.sort(key=lambda x: (-x[0], -x[1], x[2].get("id", 0)))
            return [m for _s, _r, m in scored[:limit]]
        except Exception as ex:
            log("[记忆] 计算相关记忆失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            return []

    def block(self, now=None, query=""):
        """拼到 system 消息尾部（方案 B：固定块 + 相关块）。

        结构：
          固定块【请长期记住的事】 —— 核心记忆，按 id 排序，同日缓存（前缀稳定）
          相关块【可能相关的旧事】 —— 按 query 相关性重排，追加在末尾（前缀变动）

        前面的固定块保持恒定 → DeepSeek 前缀缓存仍能命中，只有末尾新增部分重算。
        """
        if not self.items:
            return ""
        try:
            cur = now or time.localtime()
            parts = []

            # ---- 固定块：核心记忆（同日缓存，前缀稳定）----
            core = self.core_items(cur)
            if core:
                lines = "\n".join("- " + (m.get("text") or "").strip()
                                  for m in core)
                parts.append("\n\n【请长期记住的事】\n" + lines)

            # ---- 相关块：按当前问题重排（不缓存，追加在末尾）----
            if query:
                rel = self.relevant_items(query, cur)
                if rel:
                    lines = "\n".join("- " + (m.get("text") or "").strip()
                                      for m in rel)
                    parts.append("\n\n【可能相关的旧事】\n" + lines)

            return "".join(parts)
        except Exception as ex:
            log("[记忆] 生成 block 失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            lines = "\n".join("- " + (m.get("text") or "").strip()
                              for m in self.sorted_items())
            return "\n\n【请长期记住的事】\n" + lines

    def system_content(self, base, query=""):
        return base + self.block(query=query)


class ChatWorker(QThread):
    """流式请求 /chat/completions，边生成边把增量发回界面线程"""
    chunk = Signal(str)
    done = Signal(str, dict)
    failed = Signal(str)

    def __init__(self, messages, api_key, max_tokens=None, base_url=None,
                 model=None, temperature=None, top_p=None, stream=True,
                 reasoning_effort=None):
        self.base_url = (base_url or DS_BASE_URL).rstrip("/")
        self.model = model or DS_MODEL
        super().__init__()
        self.messages = messages
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.stream = bool(stream)
        self.reasoning_effort = (reasoning_effort or "").strip()
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        full = ""
        usage = {}
        try:
            url = self.base_url + "/chat/completions"
            headers = {
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "messages": self.messages,
                "stream": self.stream,
            }
            if self.stream:
                payload["stream_options"] = {"include_usage": True}
            if self.max_tokens:
                payload["max_tokens"] = int(self.max_tokens)
            if self.temperature is not None:
                payload["temperature"] = float(self.temperature)
            if self.top_p is not None:
                payload["top_p"] = float(self.top_p)
            # 只有支持该参数的模型才传，避免多一次失败重试
            _use_reasoning = (bool(self.reasoning_effort)
                              and model_supports_reasoning(self.model))
            if _use_reasoning:
                payload["reasoning_effort"] = self.reasoning_effort
            elif self.reasoning_effort:
                log("[聊天] %s 不支持推理等级，本次不传"
                    % self.model, logging.DEBUG)
            r = requests.post(url, headers=headers, json=payload,
                              stream=self.stream, timeout=180)
            if r.status_code != 200 and _use_reasoning:
                # 服务端可能不认 reasoning_effort：去掉后重试一次
                log("[聊天] reasoning_effort 被拒（HTTP %s），去掉后重试"
                    % r.status_code, logging.WARNING)
                payload.pop("reasoning_effort", None)
                self.reasoning_effort = ""
                r = requests.post(url, headers=headers, json=payload,
                                  stream=self.stream, timeout=180)
            if r.status_code != 200:
                self.failed.emit(_http_error_message(r.status_code, r.text))
                return
            r.encoding = "utf-8"

            if not self.stream:
                # 非流式：一次性拿完整结果
                try:
                    data = r.json()
                except Exception as ex:
                    self.failed.emit("响应解析失败：" + str(ex))
                    return
                usage = data.get("usage") or {}
                for ch in data.get("choices") or []:
                    full += ((ch.get("message") or {}).get("content")) or ""
                if full:
                    self.chunk.emit(full)
                self.done.emit(full, usage)
                return

            # 流式
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

    def __init__(self, prev_summary, messages, api_key, base_url=None, model=None):
        super().__init__()
        self.base_url = (base_url or DS_BASE_URL).rstrip("/")
        self.model = model or DS_MODEL
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
                "model": self.model,
                "messages": [
                    {"role": "system", "content": DS_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            }
            r = requests.post(self.base_url + "/chat/completions", headers=headers,
                              json=payload, timeout=180)
            if r.status_code != 200:
                self.failed.emit(_http_error_message(r.status_code, r.text))
                return
            data = r.json()
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            self.done.emit((text or "").strip())
        except Exception as ex:
            self.failed.emit("整理前情摘要失败：" + str(ex))


def build_sing_hint(song_name=""):
    """唱歌场景的 system 追加指令。

    song_name: 曲库实际会播放的歌名。给了就明确要求模型唱这首——
    保证屏幕上的歌词和语音条里的音频是同一首。
    """
    base = (
        "\n\n【这一轮是唱歌请求——必须遵守】\n"
        "用户要你唱歌。你已经决定唱一首歌，**必须真的输出歌词**，"
        "不能写「我不好意思唱」这类推脱的话。\n"
        "格式：\n"
        "1. 开头一句很短的铺垫（不超过 15 字，不要括号动作，"
        "语气遵循人设）\n"
        "2. 直接输出歌词，每句一行，4~8 行\n"
        "3. 不写括号舞台动作\n"
        "4. 结尾最多一句短评\n")
    if song_name:
        base += (
            "\n【重要】你已经唱的是《%s》——输出**这首歌的歌词**"
            "（4~8 行），不要唱别的歌。\n" % song_name)
    return base


def _wav_duration(path):
    """从 WAV 头读精确时长（秒）。失败返回 0.0。"""
    try:
        import wave
        with wave.open(path, "rb") as w:
            fr = w.getframerate() or 1
            return w.getnframes() / float(fr)
    except Exception:
        return 0.0


def _fmt_time(ts):
    """时间戳 → 显示文本（三档）。

    今天 → 「14:32」
    昨天 → 「昨天 14:32」
    更早 → 「10月20日 14:32」
    """
    import datetime
    try:
        dt = datetime.datetime.fromtimestamp(float(ts))
    except Exception:
        return ""
    now = datetime.datetime.now()
    hm = dt.strftime("%H:%M")
    d = dt.date()
    today = now.date()
    if d == today:
        return hm
    if d == today - datetime.timedelta(days=1):
        return "昨天 " + hm
    return "%d月%d日 %s" % (dt.month, dt.day, hm)


# 相邻两条消息间隔超过这个秒数才插时间分隔线
TIME_GAP_SEC = 300

# 主动搭话占位符（历史里保留，界面不渲染为用户气泡）
PROACTIVE_PLACEHOLDERS = ("[主动搭话]", "[主动打招呼]",
                          "[宠物主动搭话]")


def _is_proactive_placeholder(text):
    """该用户消息是否是主动搭话占位符。"""
    t = (text or "").strip()
    return t in PROACTIVE_PLACEHOLDERS


def _proactive_label_text(text):
    """占位符 → 气泡上的小灰标签文字。"""
    t = (text or "").strip()
    if t == "[主动打招呼]":
        return "主动打招呼"
    return "主动搭话"


def _fmt_size(n):
    """字节数 → 人类可读。"""
    try:
        n = float(n or 0)
    except Exception:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


def _needs_time_divider(prev_ts, cur_ts):
    """两条消息之间是否要插时间线（间隔 > 5 分钟或跨天）。"""
    try:
        a, b = float(prev_ts), float(cur_ts)
    except Exception:
        return True
    if b - a < 0 or b - a >= TIME_GAP_SEC:
        return True
    import datetime
    try:
        return (datetime.datetime.fromtimestamp(a).date()
                != datetime.datetime.fromtimestamp(b).date())
    except Exception:
        return False


class PersonaDialog(QDialog):
    """人设编辑弹窗。

    为了不重复实现 20 多个方法，直接复用 SettingsWindow 里已有的
    人设控件与逻辑：把 self.persona_edit 等控件临时接到本对话框显示，
    关闭时还原回父窗口。
    """

    def __init__(self, parent, settings_win):
        super().__init__(parent)
        self.sw = settings_win
        self.setWindowTitle("人设")
        self.setModal(True)
        self.setMinimumWidth(520)
        try:
            from PySide6.QtGui import QShortcut, QKeySequence
            _sc = QShortcut(QKeySequence("Esc"), self)
            _sc.activated.connect(self.accept)
        except Exception:
            pass

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)

        # 说明（复用设置页那条，带可点击 i）
        head = QHBoxLayout()
        lb = QLabel("人设内容")
        lb.setStyleSheet("font-size:12.5px;color:#2c3040;")
        head.addWidget(lb)
        head.addStretch(1)
        head.addWidget(settings_win._info_btn(
            "人设",
            "这段是桌宠风格的唯一来源：作为 system 开头发给模型，"
            "也决定固定台词的语气。留空 = 无风格。\n"
            "回复的详细程度也由这里决定——想限制长度就写"
            "「每次回复控制在 80 字以内」，不写则不限制。\n"
            "固定不变才有利于前缀缓存；改完缓存会失效一次，建议新开一个对话。"))
        lay.addLayout(head)

        # 人设页容器（和台词页二选一显示）
        self._page_persona = QWidget()
        _pl = QVBoxLayout(self._page_persona)
        _pl.setContentsMargins(0, 0, 0, 0)
        _pl.setSpacing(8)

        # 把人设输入框临时搬进来
        self._edit = settings_win.persona_edit
        _pl.addWidget(self._edit, 1)

        # 按钮行
        row = QHBoxLayout()
        row.setSpacing(6)
        b_save = QPushButton("保存人设")
        b_save.clicked.connect(settings_win._save_persona)
        row.addWidget(b_save)
        b_import = QPushButton("导入文件")
        b_import.setToolTip(
            "从 .md / .txt 等文本文件读入人设（追加到现有内容后面）")
        b_import.clicked.connect(self._import_persona_file)
        row.addWidget(b_import)
        self._btn_lines = QPushButton("查看/编辑台词")
        self._btn_lines.setCheckable(True)
        self._btn_lines.setToolTip("在同一窗口内切换到台词编辑")
        self._btn_lines.clicked.connect(self._toggle_lines_page)
        row.addWidget(self._btn_lines)
        row.addStretch(1)
        _pl.addLayout(row)

        # 导入结果提示（人设页专用）
        self._import_hint = QLabel("")
        self._import_hint.setWordWrap(True)
        self._import_hint.setStyleSheet("color:#d08a00;font-size:11px;")
        self._import_hint.hide()
        _pl.addWidget(self._import_hint)

        # 状态行
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color:#8a8f9c;font-size:11px;")
        _pl.addWidget(self._status)
        lay.addWidget(self._page_persona, 1)

        # ---- 台词页（默认隐藏，点按钮切换）----
        self._page_lines = self._build_lines_page()
        self._page_lines.hide()
        lay.addWidget(self._page_lines, 1)

        self.resize(620, 440)
        self._refresh_status()

    def _import_persona_file(self):
        """导入人设：支持文本 / JSON 角色卡 / PNG 内嵌角色卡。"""
        try:
            from PySide6.QtWidgets import QFileDialog
            from petcore import chat_attach as CA
            from petcore import card_reader as CR
            exts = " ".join("*" + e for e in CA.TEXT_EXTS)
            path, _ = QFileDialog.getOpenFileName(
                self, "导入人设文件", "",
                "人设卡 (*.json *.png);;"
                "文本文件 (%s);;"
                "所有文件 (*)" % exts)
            if not path:
                return

            ok, text, name, greeting, note = CR.read_card(
                path, text_reader=CA.read_text_file)
            if not ok:
                QMessageBox.warning(self, "导入失败", note or "无法识别该文件")
                return

            text = (text or "").strip()
            if not text:
                QMessageBox.information(self, "导入", "文件里没有人设内容。")
                return

            # 追加到现有内容后面（不覆盖，避免误丢已写好的）
            cur = self._edit.toPlainText().rstrip()
            if cur:
                merged = cur + "\n\n" + text
                msg = "已追加 %d 字（原 %d 字，合计 %d 字）· %s" % (
                    len(text), len(cur), len(merged), note)
            else:
                merged = text
                msg = "已导入 %d 字 · %s" % (len(text), note)
            if name:
                msg += " · 角色名「%s」" % name
            if greeting:
                msg += " · 已保存开场白"
                self.sw.app.cfg["persona_greeting"] = greeting[:500]
                self.sw.app.save_cfg()
            self._import_hint.setText(msg)
            self._import_hint.show()
            self._edit.setPlainText(merged)
            self._refresh_status()
        except Exception as ex:
            log("[人设] 导入失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            QMessageBox.warning(self, "导入失败", str(ex))

    def _build_lines_page(self):
        """台词查看/编辑页（内嵌，不弹新窗口）。"""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        tip = QLabel(
            "这里显示当前人设已生成的台词池。人设为空或某池未生成时，"
            "该池使用中性台词。修改后点「保存修改」写入缓存。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8a8f9c;font-size:11px;")
        v.addWidget(tip)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("台词池"))
        self._pool_box = QComboBox()
        self._pool_box.currentIndexChanged.connect(self._load_pool)
        row.addWidget(self._pool_box, 1)
        v.addLayout(row)

        self._lines_edit = QTextEdit()
        self._lines_edit.setStyleSheet(
            "QTextEdit{background:#ffffff;border:1px solid #dfe3ec;"
            "border-radius:8px;padding:8px;font-size:12px;}")
        v.addWidget(self._lines_edit, 1)

        brow = QHBoxLayout()
        brow.setSpacing(6)
        b_save = QPushButton("保存修改")
        b_save.clicked.connect(self._save_pool)
        brow.addWidget(b_save)
        b_reset = QPushButton("该池恢复中性")
        b_reset.clicked.connect(self._reset_pool)
        brow.addWidget(b_reset)
        brow.addSpacing(12)
        b_regen = QPushButton("重新生成台词")
        b_regen.setToolTip("按当前人设重新生成全部台词池（耗 token）")
        b_regen.clicked.connect(self._regen_all)
        brow.addWidget(b_regen)
        brow.addStretch(1)
        v.addLayout(brow)

        self._lines_hint = QLabel("")
        self._lines_hint.setWordWrap(True)
        self._lines_hint.setStyleSheet("color:#8a8f9c;font-size:11px;")
        v.addWidget(self._lines_hint)

        self._fill_pools()
        return w

    def _regen_all(self):
        """按当前人设重新生成全部台词池，完成后刷新本页。"""
        try:
            self.sw._regen_persona_lines()
            self._lines_hint.setText("已发起生成，完成后会自动写入缓存")
            # 延迟一下再刷新，给后台线程留时间
            from PySide6.QtCore import QTimer
            QTimer.singleShot(1500, self._fill_pools)
            QTimer.singleShot(1500, self._refresh_status)
        except Exception as ex:
            log("[台词] 重新生成失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _toggle_lines_page(self, on):
        """人设页 ↔ 台词页 切换。"""
        self._page_persona.setVisible(not on)
        self._page_lines.setVisible(bool(on))
        self._btn_lines.setText("返回人设" if on else "查看/编辑台词")
        if on:
            self._fill_pools()

    def _fill_pools(self):
        try:
            from petcore import persona_lines as PL
            cur = self._pool_box.currentData()
            self._pool_box.blockSignals(True)
            self._pool_box.clear()
            names = sorted(PL.POOL_SPECS.keys())
            for n in names:
                hint = PL.POOL_HINTS.get(n, n)
                self._pool_box.addItem(hint, n)     # 显示中文，data 存英文名
            if cur:
                for i in range(self._pool_box.count()):
                    if self._pool_box.itemData(i) == cur:
                        self._pool_box.setCurrentIndex(i)
                        break
            self._pool_box.blockSignals(False)
            self._load_pool()
        except Exception as ex:
            log("[台词] 载入池列表失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _cur_persona(self):
        try:
            from petcore.consts import persona_from_cfg
            return persona_from_cfg(self.sw.app.cfg)
        except Exception:
            return ""

    def _load_pool(self, *a):
        name = (self._pool_box.currentData()
                if hasattr(self, "_pool_box") else "") or ""
        if not name:
            return
        try:
            from petcore import persona_lines as PL
            from petcore.consts import neutral_pool
            p = self._cur_persona()
            pools = PL.get_persona_pools(APP_DIR, p) if p else {}
            lines = pools.get(name)
            if not isinstance(lines, list) or not lines:
                lines = neutral_pool(name)
                self._lines_hint.setText("该池尚未按人设生成，当前显示中性台词")
            else:
                self._lines_hint.setText("该池已按人设生成 %d 句" % len(lines))
            self._lines_edit.setPlainText("\n".join(lines))
        except Exception as ex:
            log("[台词] 载入池失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _save_pool(self):
        name = self._pool_box.currentData() or ""
        p = self._cur_persona()
        if not name:
            return
        if not p:
            QMessageBox.information(
                self, "台词", "人设为空，台词固定使用中性池，无需保存。")
            return
        try:
            from petcore import persona_lines as PL
            raw_lines = (self._lines_edit.toPlainText() or "").split("\n")
            okk, why, clean = PL.validate_pool(name, raw_lines, min_count=1)
            if not clean:
                QMessageBox.warning(self, "台词", "没有合格台词：%s" % why)
                return
            pools = PL.get_persona_pools(APP_DIR, p)
            pools[name] = clean
            PL.save_persona_pools(APP_DIR, p, pools)
            self._lines_hint.setText("已保存 %d 句" % len(clean))
            self._refresh_status()
        except Exception as ex:
            QMessageBox.warning(self, "台词", "保存失败：%s" % ex)

    def _reset_pool(self):
        name = self._pool_box.currentData() or ""
        p = self._cur_persona()
        if not name or not p:
            return
        try:
            from petcore import persona_lines as PL
            pools = PL.get_persona_pools(APP_DIR, p)
            pools.pop(name, None)
            PL.save_persona_pools(APP_DIR, p, pools)
            self._load_pool()
            self._lines_hint.setText("已恢复该池为中性台词")
            self._refresh_status()
        except Exception as ex:
            QMessageBox.warning(self, "台词", "恢复失败：%s" % ex)

    def _refresh_status(self):
        try:
            from petcore import persona_lines as PL
            from petcore.consts import persona_from_cfg
            p = persona_from_cfg(self.sw.app.cfg)
            if not p:
                self._status.setText("当前：无风格（台词用中性池）")
                return
            pools = PL.get_persona_pools(APP_DIR, p) if p else {}
            n = len(pools or {})
            if n:
                self._status.setText(
                    "当前人设 %d 字 · 台词池 %d 个" % (len(p), n))
            else:
                self._status.setText(
                    "当前人设 %d 字 · 尚未生成台词池（用中性池）" % len(p))
        except Exception:
            pass

    def _detach_edit(self):
        """把输入框从本对话框摘出来，归还给设置窗口。

        控件原本就没有父布局（只是预建未加入布局），
        所以要 setParent 回设置窗口，否则本对话框销毁时会连带删除它。
        """
        try:
            self._edit.setParent(self.sw)
            self._edit.hide()
        except Exception:
            pass

    def accept(self):
        self._detach_edit()
        super().accept()

    def reject(self):
        self._detach_edit()
        super().reject()


class DialogParamsDialog(QDialog):
    """对话参数弹窗：上下文条数 / 温度 / Top P / 流式输出。

    从聊天窗口的「设置」菜单进入，改完立即写配置生效。
    """

    def __init__(self, parent, pet):
        super().__init__(parent)
        self.pet = pet
        self.setWindowTitle("对话参数")
        self.setModal(True)
        self.setMinimumWidth(420)
        # Esc 关闭
        try:
            from PySide6.QtGui import QShortcut, QKeySequence
            _sc = QShortcut(QKeySequence("Esc"), self)
            _sc.activated.connect(self.accept)
        except Exception:
            pass
        self.setStyleSheet("""
            QDialog { background: #ffffff; }
            QLabel#DlgLabel {
                font-size: 12.5px; color: #2c3040; background: transparent;
            }
            QLabel#DlgHint {
                font-size: 11px; color: #8a8f9c; background: transparent;
            }
            QSpinBox, QDoubleSpinBox {
                background: #ffffff; border: 1px solid #dfe4ee;
                border-radius: 8px; padding: 4px 6px;
                min-height: 22px; font-size: 12.5px; color: #2c3040;
            }
            QSpinBox:focus, QDoubleSpinBox:focus {
                border: 1px solid #5686fe;
            }
            QSpinBox::up-button, QDoubleSpinBox::up-button,
            QSpinBox::down-button, QDoubleSpinBox::down-button {
                width: 16px; border: none; background: transparent;
            }
            QSlider::groove:horizontal {
                height: 4px; background: #e8ecf3; border-radius: 2px;
            }
            QSlider::sub-page:horizontal {
                background: #5686fe; border-radius: 2px;
            }
            QSlider::handle:horizontal {
                width: 14px; height: 14px; margin: -5px 0;
                background: #5686fe; border-radius: 7px;
            }
            QSlider::handle:horizontal:hover { background: #3f6fe8; }
            QPushButton#DialogInfoBtn {
                background: #e3e8f0; color: #5a6376;
                border: 1px solid #d3dae6; border-radius: 9px;
                font-size: 12px; font-weight: 700; padding: 0 0 1px 0;
                font-family: 'Segoe UI', 'Microsoft YaHei UI';
            }
            QPushButton#DialogInfoBtn:hover {
                background: #5686fe; color: #ffffff;
                border-color: #5686fe;
            }
            QPushButton#DialogInfoBtn:pressed {
                background: #3f6fe8; border-color: #3f6fe8;
            }
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(4)

        def _row(label, tip, widget):
            bar = QHBoxLayout()
            bar.setContentsMargins(0, 6, 0, 6)
            bar.setSpacing(6)
            lb = QLabel(label)
            lb.setObjectName("DlgLabel")
            bar.addWidget(lb)
            if tip:
                ic = QPushButton("i")
                ic.setObjectName("DialogInfoBtn")
                ic.setFixedSize(18, 18)
                ic.setCursor(Qt.CursorShape.PointingHandCursor)
                ic.setToolTip(tip)
                ic.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                ic.clicked.connect(
                    lambda _=False, t=tip, n=label:
                    QMessageBox.information(self, n, t))
                bar.addWidget(ic)
            bar.addStretch(1)
            bar.addWidget(widget, 0)
            lay.addLayout(bar)

        # 1) 上下文条数
        self.history_spin = QSpinBox()
        self.history_spin.setRange(HISTORY_MSG_MIN, HISTORY_MSG_MAX)
        self.history_spin.setValue(int(self.pet.cfg.get(
            "history_msg_limit", HISTORY_MSG_LIMIT) or HISTORY_MSG_LIMIT))
        self.history_spin.setFixedWidth(92)
        self.history_spin.setAlignment(Qt.AlignmentFlag.AlignRight
                                       | Qt.AlignmentFlag.AlignVCenter)
        self.history_spin.valueChanged.connect(self._set_history)
        _row("上下文的消息数量上限", "每次请求最多携带多少条历史消息",
             self.history_spin)

        self.history_slider = QSlider(Qt.Orientation.Horizontal)
        self.history_slider.setRange(HISTORY_MSG_MIN, HISTORY_MSG_MAX)
        self.history_slider.setValue(self.history_spin.value())
        self.history_slider.valueChanged.connect(self.history_spin.setValue)
        self.history_spin.valueChanged.connect(self.history_slider.setValue)
        _sl = QHBoxLayout()
        _sl.setContentsMargins(0, 0, 0, 6)
        _sl.addWidget(self.history_slider, 1)
        lay.addLayout(_sl)

        # 2) 温度
        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(-1.0, 2.0)
        self.temp_spin.setDecimals(1)
        self.temp_spin.setSingleStep(0.1)
        self.temp_spin.setSpecialValueText("未设置")
        self.temp_spin.setFixedWidth(92)
        self.temp_spin.setAlignment(Qt.AlignmentFlag.AlignRight
                                    | Qt.AlignmentFlag.AlignVCenter)
        _ct = self.pet.cfg.get("temperature")
        self.temp_spin.setValue(-1.0 if _ct is None else float(_ct))
        self.temp_spin.valueChanged.connect(self._set_temp)
        _row("温度", "越高越随机，越低越稳定。不设置则用 API 默认",
             self.temp_spin)

        # 3) Top P
        self.topp_spin = QDoubleSpinBox()
        self.topp_spin.setRange(-1.0, 1.0)
        self.topp_spin.setDecimals(2)
        self.topp_spin.setSingleStep(0.05)
        self.topp_spin.setSpecialValueText("未设置")
        self.topp_spin.setFixedWidth(92)
        self.topp_spin.setAlignment(Qt.AlignmentFlag.AlignRight
                                    | Qt.AlignmentFlag.AlignVCenter)
        _cp = self.pet.cfg.get("top_p")
        self.topp_spin.setValue(-1.0 if _cp is None else float(_cp))
        self.topp_spin.valueChanged.connect(self._set_topp)
        _row("Top P", "核采样。不设置则用 API 默认", self.topp_spin)

        # 4) 流式输出
        self.stream_sw = QCheckBox()
        self.stream_sw.setChecked(bool(self.pet.cfg.get("stream_output", True)))
        self.stream_sw.toggled.connect(self._set_stream)
        _row("流式输出", "关闭后要等整段回复生成完才显示", self.stream_sw)

        # 关闭用窗口右上角 × 或 Esc，不再放底部按钮

    def _set_history(self, v):
        try:
            v = max(HISTORY_MSG_MIN, min(HISTORY_MSG_MAX, int(v)))
            self.pet.cfg["history_msg_limit"] = v
            self.pet.save_cfg()
        except Exception as ex:
            log("[聊天] 保存上下文条数失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _set_temp(self, v):
        try:
            v = float(v)
            self.pet.cfg["temperature"] = None if v < 0 else round(v, 2)
            self.pet.save_cfg()
        except Exception as ex:
            log("[聊天] 保存温度失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _set_topp(self, v):
        try:
            v = float(v)
            self.pet.cfg["top_p"] = None if v < 0 else round(v, 3)
            self.pet.save_cfg()
        except Exception as ex:
            log("[聊天] 保存 Top P 失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _set_stream(self, on):
        try:
            self.pet.cfg["stream_output"] = bool(on)
            self.pet.save_cfg()
        except Exception as ex:
            log("[聊天] 保存流式开关失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)


class SendButton(QPushButton):
    """发送按钮：圆形底 + 手绘几何箭头。

    用 QPainter 画箭头而不是「↑」字符，原因是字符笔画太细
    且受字体影响；几何图形可以精确控制粗细和大小。
    busy 状态画一个实心方块（停止生成）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SendBtn")
        self.setFixedSize(30, 30)
        self._busy = False

    def set_busy(self, busy):
        self._busy = bool(busy)
        self.update()

    def paintEvent(self, e):
        from PySide6.QtGui import QPainter, QPainterPath, QPen, QBrush
        from PySide6.QtCore import Qt as _Qt, QRectF, QPointF
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(0, 0, self.width(), self.height())
        cx = self.width() / 2.0
        cy = self.height() / 2.0

        # 圆形底
        if not self.isEnabled():
            bg = QColor("#eef0f3")
        elif self.isDown():
            bg = QColor("#8aaaf0")
        elif self.underMouse():
            bg = QColor("#9db8f0")
        else:
            bg = QColor("#b3c8f5")
        p.setPen(_Qt.PenStyle.NoPen)
        p.setBrush(QBrush(bg))
        p.drawEllipse(r)

        # 前景色
        fg = QColor("#c2c7cf") if not self.isEnabled() else QColor("#ffffff")

        if self._busy:
            # 停止：实心圆角方块
            s = 7.0
            box = QRectF(cx - s / 2, cy - s / 2, s, s)
            p.setBrush(QBrush(fg))
            p.drawRoundedRect(box, 1.5, 1.5)
        else:
            # 发送：锥形箭头（实心填充，比线条更粗更有力）
            p.setPen(_Qt.PenStyle.NoPen)
            p.setBrush(QBrush(fg))
            top = cy - 6.5
            bottom = cy + 6.5
            head_h = 6.0          # 箭头三角高度
            head_w = 5.2          # 箭头三角半宽
            shaft_w = 1.5         # 竖杆半宽
            head_bottom = top + head_h

            tri = QPainterPath()
            tri.moveTo(cx, top)
            tri.lineTo(cx + head_w, head_bottom)
            tri.lineTo(cx - head_w, head_bottom)
            tri.closeSubpath()
            p.drawPath(tri)

            shaft = QPainterPath()
            shaft.moveTo(cx - shaft_w, head_bottom - 0.8)
            shaft.lineTo(cx + shaft_w, head_bottom - 0.8)
            shaft.lineTo(cx + shaft_w, bottom)
            shaft.lineTo(cx - shaft_w, bottom)
            shaft.closeSubpath()
            p.drawPath(shaft)
        p.end()


class SidebarDragHandle(QWidget):
    """侧栏右边缘拖拽条：拖动调整侧栏宽度。

    - hover 时变蓝，提示可拖
    - 拖动范围 CHAT_LEFT_MIN ~ CHAT_LEFT_MAX
    - 松手回调 on_release(新宽度)，由调用方持久化
    """
    WIDTH = 4

    def __init__(self, target, on_release, parent=None):
        super().__init__(parent)
        self.setFixedWidth(self.WIDTH)
        self.setCursor(Qt.CursorShape.SplitHCursor)
        self.setObjectName("SidebarDragHandle")
        self.setToolTip("拖动调整侧栏宽度")
        self._target = target
        self._on_release = on_release
        self._dragging = False
        self._start_x = 0
        self._start_w = 0

    def _repolish(self):
        try:
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()
        except Exception:
            pass

    def enterEvent(self, e):
        if not self._dragging:
            self.setObjectName("SidebarDragHandleHover")
            self._repolish()
        super().enterEvent(e)

    def leaveEvent(self, e):
        if not self._dragging:
            self.setObjectName("SidebarDragHandle")
            self._repolish()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._start_x = e.globalPosition().x()
            self._start_w = self._target.width()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if not self._dragging:
            return
        dx = e.globalPosition().x() - self._start_x
        w = int(self._start_w + dx)
        w = max(CHAT_LEFT_MIN, min(CHAT_LEFT_MAX, w))
        self._target.setFixedWidth(w)
        e.accept()

    def mouseReleaseEvent(self, e):
        if self._dragging:
            self._dragging = False
            self.setObjectName("SidebarDragHandle")
            self._repolish()
            try:
                self._on_release(self._target.width())
            except Exception as ex:
                log("[聊天] 保存侧栏宽度失败: %s: %s"
                    % (type(ex).__name__, ex), logging.WARNING)
            e.accept()
            return
        super().mouseReleaseEvent(e)


class ChatTopBar(QWidget):
    """聊天顶部栏：标题 + 在线状态。标题双击可改名。"""

    name_changed = Signal(str)      # 用户改了显示名

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatTopBar")
        self.setFixedHeight(CHAT_TOP_BAR_H)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(8)

        self.title = QLabel(PET_NAME_DEFAULT)
        self.title.setObjectName("TopBarTitle")
        self.title.setToolTip("双击改名")
        self.title.setCursor(Qt.CursorShape.IBeamCursor)
        self.title.installEventFilter(self)
        lay.addWidget(self.title)

        self.sub = QLabel("在线")
        self.sub.setObjectName("TopBarSub")
        lay.addWidget(self.sub)
        lay.addStretch(1)

        # 双击时临时替换成输入框
        self._edit = None

    # ---- 改名 ----
    def set_name(self, name):
        try:
            self.title.setText((name or "").strip() or PET_NAME_DEFAULT)
        except Exception:
            pass

    def eventFilter(self, obj, event):
        _ed = getattr(self, "_edit", None)
        if obj is self.title and event.type() == QEvent.Type.MouseButtonDblClick:
            self._begin_rename()
            return True
        if _ed is not None and obj is _ed and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._commit_rename()
                return True
            if key == Qt.Key.Key_Escape:
                self._cancel_rename()
                return True
        if _ed is not None and obj is _ed and event.type() == QEvent.Type.FocusOut:
            self._commit_rename()
            return False
        return super().eventFilter(obj, event)

    def _begin_rename(self):
        if self._edit is not None:
            return
        try:
            from PySide6.QtWidgets import QLineEdit
            lay = self.layout()
            self._edit = QLineEdit(self.title.text(), self)
            self._edit.setObjectName("TopBarRename")
            self._edit.setFixedWidth(140)
            self._edit.selectAll()
            self._edit.installEventFilter(self)
            idx = lay.indexOf(self.title)
            self.title.hide()
            lay.insertWidget(idx, self._edit)
            self._edit.setFocus()
        except Exception as ex:
            log("[聊天] 开始改名失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            self._edit = None

    def _commit_rename(self):
        if self._edit is None:
            return
        try:
            new = (self._edit.text() or "").strip()[:20]
            self._end_edit()
            if not new:
                return
            if new != self.title.text():
                self.set_name(new)
                self.name_changed.emit(new)
        except Exception as ex:
            log("[聊天] 提交改名失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _cancel_rename(self):
        self._end_edit()

    def _end_edit(self):
        try:
            if self._edit is None:
                return
            lay = self.layout()
            lay.removeWidget(self._edit)
            self._edit.deleteLater()
            self._edit = None
            self.title.show()
        except Exception:
            self._edit = None


class ChatBubble(QFrame):
    """聊天气泡：用户靠右、宠物靠左（头像在 ChatWindow 里加），支持 ``` 代码块"""

    def __init__(self, role, text="", image_path=None, voice_path=None,
                 voice_sec=0.0, ts=None, voice_text="", attachments=None):
        super().__init__()
        self.role = role
        self.attachments = list(attachments or [])
        self.setObjectName("Bubble")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum)
        self.label = QLabel()
        self.label.setObjectName("BubbleText")
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # 右键菜单挂在 label 上（事件发生在 label，QFrame 的
        # contextMenuEvent 收不到）；系统默认菜单只有 Copy/Select All
        self.label.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.label.customContextMenuRequested.connect(self._label_menu)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(*CHAT_BUBBLE_PAD)
        lay.setSpacing(0)
        # 顶部小灰标签（主动搭话等），默认隐藏
        self.head_tag = QLabel("")
        self.head_tag.setStyleSheet(
            "color:#a8aebb;font-size:10px;background:#f2f4f8;"
            "border-radius:4px;padding:1px 6px;")
        self.head_tag.setSizePolicy(QSizePolicy.Policy.Maximum,
                                    QSizePolicy.Policy.Fixed)
        self.head_tag.hide()
        lay.addWidget(self.head_tag)
        # 附件缩略条（图片 2×2 / 文本文件名卡片），在文字上方
        if self.attachments:
            try:
                lay.addWidget(self._build_attachments())
                lay.addSpacing(6)
            except Exception as ex:
                log("[聊天] 附件渲染失败: %s: %s"
                    % (type(ex).__name__, ex), logging.WARNING)
        # 截图缩略图（可选）：放在文字上方，点击放大
        self.image_label = None
        if image_path:
            try:
                pm = QPixmap(image_path)
                if not pm.isNull():
                    thumb = pm.scaled(260, 180,
                                      Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
                    self.image_label = QLabel()
                    self.image_label.setPixmap(thumb)
                    self.image_label.setCursor(Qt.CursorShape.PointingHandCursor)
                    self.image_label.setToolTip("点击查看大图")
                    self.image_label.setStyleSheet(
                        "border-radius:6px;border:1px solid #dfe3ec;")
                    self._full_image_path = image_path
                    self.image_label.mousePressEvent = (
                        lambda e, p=image_path: self._show_full_image(p))
                    lay.addWidget(self.image_label)
                    lay.addSpacing(6)
            except Exception as ex:
                log("[聊天] 气泡图片加载失败: %s: %s"
                    % (type(ex).__name__, ex), logging.WARNING)
        # QQ 风格语音条：单行播放条（▶ + 波形 + 时长）
        self.voice_btn = None   # 兼容别名，指向播放钮
        if voice_path:
            try:
                from PySide6.QtWidgets import QPushButton
                import os as _os
                self._voice_path = voice_path
                self._voice_text = voice_text or ""
                # 诊断：卡片创建时记录路径与文件状态
                try:
                    log("[语音] 卡片创建: path=%r exists=%s sec=%s"
                        % (voice_path, os.path.exists(voice_path or ""),
                           voice_sec))
                except Exception:
                    pass
                sec = float(voice_sec or 0.0)
                # 优先从 WAV 头读精确时长（估算公式对补齐/采样率会偏差）
                exact = _wav_duration(voice_path)
                if exact > 0.05:
                    sec = exact
                elif sec <= 0:
                    try:
                        sec = max(1.0, _os.path.getsize(voice_path) / 80000.0)
                    except Exception:
                        sec = 1.0
                self._voice_sec = sec
                # ===== 音乐卡片：第一行 [▶] [波形] [00:12 / 03:58] [1.0x] [📝]
                #      第二行 [歌名]；整卡固定宽 min(340, 可用宽)
                _t = _CHAT_THEME
                self.voice_ctrl = QWidget()
                self.voice_ctrl.setObjectName("VoiceCard")
                _vh = QHBoxLayout(self.voice_ctrl)
                _vh.setContentsMargins(16, 12, 16, 12)
                _vh.setSpacing(10)
                # 播放/暂停圆钮：32×32 圆形，固定尺寸不被压缩
                self.voice_pause_btn = QPushButton("▶")
                self.voice_pause_btn.setObjectName("VoicePlayBtn")
                # 36x36：比设计稿稍大，点击更容易命中
                self.voice_pause_btn.setFixedSize(36, 36)
                self.voice_pause_btn.setSizePolicy(
                    QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                self.voice_pause_btn.setCursor(
                    Qt.CursorShape.PointingHandCursor)
                self.voice_pause_btn.setToolTip("播放 / 暂停")
                self.voice_pause_btn.setFocusPolicy(
                    Qt.FocusPolicy.NoFocus)
                # 必须连 _play_voice（三态）：连错成只处理暂停/继续的函数时，
                # 从未播放状态下点击无响应
                self.voice_pause_btn.clicked.connect(self._play_voice)
                _vh.addWidget(self.voice_pause_btn, 0)
                # 波形进度条：可伸缩，占满剩余空间（min-width 0）
                self.wave_bar = WaveBar(voice_path, sec)
                self.wave_bar.setMinimumWidth(0)
                self.wave_bar.setFixedHeight(24)
                self.wave_bar.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                self.wave_bar.seekRequested.connect(self._seek_voice)
                _vh.addWidget(self.wave_bar, 1)
                # 时间：当前进度 / 总时长，固定不压缩
                self.voice_time = QLabel(self._fmt_pos(0, sec))
                self.voice_time.setObjectName("VoiceTime")
                self.voice_time.setSizePolicy(
                    QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                _vh.addWidget(self.voice_time, 0)
                # 倍速（循环切换），固定不压缩
                self._voice_rates = (1.0, 1.25, 1.5, 2.0, 0.75)
                self._voice_rate_i = 0
                self.voice_rate_btn = QPushButton("1.0x")
                self.voice_rate_btn.setObjectName("VoiceRateBtn")
                self.voice_rate_btn.setFixedHeight(24)
                self.voice_rate_btn.setMinimumWidth(40)
                self.voice_rate_btn.setSizePolicy(
                    QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                self.voice_rate_btn.setCursor(
                    Qt.CursorShape.PointingHandCursor)
                self.voice_rate_btn.setToolTip("播放倍速")
                self.voice_rate_btn.setFocusPolicy(
                    Qt.FocusPolicy.NoFocus)
                self.voice_rate_btn.clicked.connect(self._cycle_voice_rate)
                _vh.addWidget(self.voice_rate_btn, 0)
                # 转文字：24×24，固定不压缩
                self.voice_text_btn = QPushButton("Aa")
                self.voice_text_btn.setObjectName("VoiceTextBtn")
                self.voice_text_btn.setFixedSize(26, 24)
                self.voice_text_btn.setSizePolicy(
                    QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                self.voice_text_btn.setCursor(
                    Qt.CursorShape.PointingHandCursor)
                self.voice_text_btn.setToolTip("显示文字内容")
                self.voice_text_btn.setFocusPolicy(
                    Qt.FocusPolicy.NoFocus)
                self.voice_text_btn.clicked.connect(self._toggle_voice_text)
                _vh.addWidget(self.voice_text_btn, 0)
                # 展开区：转文字内容（默认隐藏）
                self.voice_text_area = None
                self.voice_btn = self.voice_pause_btn  # 兼容别名
                # 卡片样式（浅/深色由 _CHAT_THEME 决定）
                self.voice_ctrl.setStyleSheet(
                    "QWidget#VoiceCard{"
                    "background:%(vc_card_bg)s;"
                    "border:1px solid %(vc_card_border)s;"
                    "border-radius:4px 16px 16px 16px;}"
                    "QPushButton#VoicePlayBtn{"
                    "background:%(vc_play_bg)s;color:%(vc_play_fg)s;"
                    "border:none;border-radius:16px;font-size:13px;}"
                    "QPushButton#VoicePlayBtn:hover{background:%(vc_play_bg)s;}"
                    "QLabel#VoiceTime{color:%(vc_time_fg)s;font-size:12px;}"
                    "QPushButton#VoiceRateBtn{"
                    "background:%(vc_rate_bg)s;color:%(vc_rate_fg)s;"
                    "border:1px solid %(vc_rate_border)s;"
                    "border-radius:12px;padding:0 8px;font-size:12px;}"
                    "QPushButton#VoiceRateBtn:hover{"
                    "background:%(vc_rate_bg)s;}"
                    "QPushButton#VoiceTextBtn{"
                    "background:%(vc_rate_bg)s;color:%(vc_rate_fg)s;"
                    "border:1px solid %(vc_rate_border)s;"
                    "border-radius:12px;font-size:10px;font-weight:bold;"
                    "padding:0;}"
                    "QPushButton#VoiceTextBtn:hover{"
                    "background:%(vc_rate_bg)s;}" % {
                        "vc_card_bg": _t["vc_card_bg"],
                        "vc_card_border": _t["vc_card_border"],
                        "vc_play_bg": _t["vc_play_bg"],
                        "vc_play_fg": _t["vc_play_fg"],
                        "vc_time_fg": _t["vc_time_fg"],
                        "vc_rate_bg": _t["vc_rate_bg"],
                        "vc_rate_fg": _t["vc_rate_fg"],
                        "vc_rate_border": _t["vc_rate_border"],
                    })
                lay.addWidget(self.voice_ctrl)
                # ===== 第二行：歌名（13px，单行省略号，悬停看全名）=====
                self.voice_song_label = None
                self._song_line_src = self._voice_text
                self._playing_now = False
                self._paused_now = False
                # 卡片固定宽 min(340, 可用宽)：由 _apply_voice_card_width 控制
                self._voice_card_max = 340
                self._apply_voice_card_width()
            except Exception as ex:
                log("[聊天] 语音条创建失败: %s: %s"
                    % (type(ex).__name__, ex), logging.WARNING)
        lay.addWidget(self.label)
        # 消息时间戳（语音气泡：置底最右、更淡；普通气泡：右上）
        self.ts = ts
        self.time_label = None
        if ts:
            try:
                self.time_label = QLabel(_fmt_time(ts))
                self.time_label.setObjectName("BubbleTime")
                if voice_path:
                    # 语音气泡的时间戳：底部右对齐，留足右侧内边距
                    # （原来只有 2px，贴边看起来像被截断）
                    self.time_label.setStyleSheet(
                        "color:#c3c8d2;font-size:10px;"
                        "padding:2px 8px 0 0;")
                    self.time_label.setAlignment(
                        Qt.AlignmentFlag.AlignRight)
                else:
                    self.time_label.setStyleSheet(
                        "color:#a8aebb;font-size:10px;padding:2px 2px 0 0;")
                    if role == "user":
                        self.time_label.setAlignment(
                            Qt.AlignmentFlag.AlignRight)
                lay.addWidget(self.time_label)
            except Exception:
                self.time_label = None
        self._plain = ""
        self._style()
        self.set_text(text)

    def _style(self, theme=None):
        """应用气泡底色。theme 显式传入时用它（主题切换），
        否则用当前全局主题（新建气泡）。"""
        try:
            t = theme if theme is not None else _CHAT_THEME
            self.setStyleSheet(chat_bubble_qss(self.role, t))
        except Exception as ex:
            log("[聊天] 气泡样式失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)

    def _refresh_voice_theme(self, theme=None):
        """主题切换后刷新语音卡片的内联样式与波形配色。"""
        try:
            vc = getattr(self, "voice_ctrl", None)
            if vc is None:
                return
            _t = theme if theme is not None else _CHAT_THEME
            vc.setStyleSheet(
                "QWidget#VoiceCard{"
                "background:%(vc_card_bg)s;"
                "border:1px solid %(vc_card_border)s;"
                "border-radius:4px 16px 16px 16px;}"
                "QPushButton#VoicePlayBtn{"
                "background:%(vc_play_bg)s;color:%(vc_play_fg)s;"
                "border:none;border-radius:16px;font-size:13px;}"
                "QLabel#VoiceTime{color:%(vc_time_fg)s;font-size:12px;}"
                "QPushButton#VoiceRateBtn{"
                "background:%(vc_rate_bg)s;color:%(vc_rate_fg)s;"
                "border:1px solid %(vc_rate_border)s;"
                "border-radius:12px;padding:0 8px;font-size:12px;}"
                "QPushButton#VoiceTextBtn{"
                "background:%(vc_rate_bg)s;color:%(vc_rate_fg)s;"
                "border:1px solid %(vc_rate_border)s;"
                "border-radius:12px;font-size:10px;font-weight:bold;"
                "padding:0;}" % {
                    "vc_card_bg": _t["vc_card_bg"],
                    "vc_card_border": _t["vc_card_border"],
                    "vc_play_bg": _t["vc_play_bg"],
                    "vc_play_fg": _t["vc_play_fg"],
                    "vc_time_fg": _t["vc_time_fg"],
                    "vc_rate_bg": _t["vc_rate_bg"],
                    "vc_rate_fg": _t["vc_rate_fg"],
                    "vc_rate_border": _t["vc_rate_border"],
                })
            lab = getattr(self, "voice_song_label", None)
            if lab is not None:
                # 错误提示态保留红字，不覆盖成歌名色
                if getattr(self, "_voice_error", None):
                    lab.setStyleSheet(
                        "QLabel#VoiceSong{color:%s;font-size:12px;"
                        "background:transparent;padding:2px 0 0 2px;}"
                        % _t.get("vc_error_fg", "#c42b31"))
                else:
                    lab.setStyleSheet(
                        "QLabel#VoiceSong{color:%s;font-size:13px;"
                        "background:transparent;padding:0;}"
                        % _t["vc_song_fg"])
            wb = getattr(self, "wave_bar", None)
            if wb is not None:
                wb.update()
        except Exception as ex:
            log("[聊天] 语音卡片主题刷新失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _build_attachments(self):
        """附件区：图片 2×2 排布（最多 4 张）+ 文本文件名卡片。"""
        from PySide6.QtWidgets import QGridLayout
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        imgs = [a for a in self.attachments if a.get("kind") == "image"]
        files = [a for a in self.attachments if a.get("kind") == "text"]

        if imgs:
            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(4)
            for i, a in enumerate(imgs[:4]):
                p = a.get("thumb") or a.get("path") or ""
                cell = QLabel()
                pm = QPixmap(p) if p else QPixmap()
                if not pm.isNull():
                    cell.setPixmap(pm.scaled(
                        120, 120, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation))
                else:
                    cell.setText("图片")
                    cell.setStyleSheet("color:#a8aebb;font-size:11px;")
                cell.setCursor(Qt.CursorShape.PointingHandCursor)
                cell.setToolTip("点击查看大图")
                cell.setStyleSheet(
                    "border-radius:6px;border:1px solid #dfe3ec;padding:2px;")
                full = a.get("path") or p
                cell.mousePressEvent = (
                    lambda e, fp=full: self._show_full_image(fp))
                grid.addWidget(cell, i // 2, i % 2)
            v.addLayout(grid)

        for a in files:
            name = a.get("name") or "文件"
            size = int(a.get("size") or 0)
            card = QLabel("📄 %s　%s" % (name, _fmt_size(size)))
            card.setStyleSheet(
                "color:#5a6070;font-size:11px;background:#f5f7fb;"
                "border-radius:6px;padding:6px 8px;")
            card.setToolTip(name)
            v.addWidget(card)
        return box

    def _show_full_image(self, path):
        """弹出大图预览（由 ChatWindow 实现；这里兜底）。"""
        try:
            w = self.window()
            if hasattr(w, "_show_full_image"):
                w._show_full_image(path)
        except Exception:
            pass

    def set_head_tag(self, text):
        """在气泡顶部显示一个小灰标签（如「主动搭话」）。"""
        try:
            t = (text or "").strip()
            if t:
                self.head_tag.setText(t)
                self.head_tag.show()
            else:
                self.head_tag.setText("")
                self.head_tag.hide()
        except Exception:
            pass

    def set_text(self, text):
        self._plain = text or ""
        try:
            if getattr(self, "voice_ctrl", None) is not None:
                # 语音卡片：宽度由卡片自己决定，绝不能用文字宽度卡死
                # （否则右侧倍速/图标会被挤掉）
                self.label.setMaximumWidth(self._voice_card_max)
                self.setMinimumWidth(0)
                self.setMaximumWidth(16777215)   # QWIDGETSIZE_MAX
                # 正文就是「🎵 歌名」时不再重复显示（歌名行已有）
                if self._plain.strip().startswith("🎵"):
                    self.label.setText("")
                    self.label.hide()
                else:
                    self.label.setText(chat_render_html(self._plain))
                    self.label.show()
            else:
                self.label.setText(chat_render_html(self._plain))
                w = chat_text_width(self._plain, self.label.font())
                self.label.setFixedWidth(w)
                self.setMaximumWidth(w + 26)
        except Exception:
            pass
        # 语音气泡：从正文提取「🎵 歌名」修正歌名行（避免显示路径）
        self._refresh_song_label()

    def mark_error(self):
        if self.role != "error":
            self.role = "error"
            self._style()

    def _find_pet(self):
        owner = self
        while owner is not None and not hasattr(owner, "pet"):
            owner = owner.parent()
        return owner if owner is not None else self

    def _label_menu(self, pos):
        """气泡文字右键菜单：复制选中/复制全文/播放语音。"""
        from PySide6.QtWidgets import QMenu
        from PySide6.QtGui import QGuiApplication, QCursor
        menu = QMenu(self)
        sel = ""
        try:
            sel = self.label.selectedText()
        except Exception:
            pass
        if sel and sel != self._plain:
            menu.addAction("复制选中",
                           lambda: QGuiApplication.clipboard().setText(sel))
        if self._plain:
            menu.addAction("复制这条消息",
                           lambda: QGuiApplication.clipboard().setText(
                               self._plain))
        if getattr(self, "_voice_path", None):
            menu.addSeparator()
            menu.addAction("播放 / 暂停", self._play_voice)
            menu.addAction("停止", self._stop_voice)
            menu.addAction("显示文字内容", self._toggle_voice_text)
        if menu.actions():
            menu.exec(QCursor.pos())

    def plain_text(self):
        return self._plain

    def _reset_other_voice_bubbles(self):
        """复位除自己外、处于播放中/暂停中的其他语音气泡。

        播放器是全局单例，同一时刻只有一条语音在播；
        新播放开始前必须让旧卡片停下并复位 UI，否则多张卡片
        同时显示暂停图标、共享同一进度，界面错乱。
        """
        try:
            owner = self
            while owner is not None and not hasattr(owner, "pet"):
                owner = owner.parent()
            if owner is None:
                return
            from petcore.chat import ChatBubble as _B
            reset = 0
            for b in owner.findChildren(_B):
                if b is self:
                    continue
                if not (getattr(b, "_playing_now", False)
                        or getattr(b, "_paused_now", False)):
                    continue
                b._playing_now = False
                b._paused_now = False
                try:
                    w = getattr(b, "_voice_watch", None)
                    if w is not None:
                        w.stop()
                except Exception:
                    pass
                try:
                    b.voice_time.setText(
                        b._fmt_pos(0, b._voice_sec))
                    if getattr(b, "wave_bar", None) is not None:
                        b.wave_bar.set_progress(0.0)
                    # 直接设 ▶：该卡片已停止播放。
                    # 不能调 _sync_pause_btn()——那看的是全局
                    # 播放器状态（此刻正在播新的音源），
                    # 会错误地把这张已停的卡片也显示成暂停图标。
                    b.voice_pause_btn.setText("▶")
                    b.voice_pause_btn.setToolTip("播放")
                except Exception:
                    pass
                reset += 1
            if reset:
                log("[语音] 已复位 %d 张其他卡片的播放状态" % reset)
        except Exception as ex:
            log("[语音] 复位其他卡片失败: %s: %s"
                % (type(ex).__name__, ex), logging.DEBUG)

    def _voice_player(self):
        """找到全局 VoicePlayer（气泡本身没有 pet，往上找 ChatWindow）。"""
        try:
            owner = self
            while owner is not None and not hasattr(owner, "pet"):
                owner = owner.parent()
            pet = getattr(owner, "pet", None) if owner is not None else None
            return getattr(pet, "_voice_player", None) if pet else None
        except Exception:
            return None

    def _apply_voice_card_width(self):
        """卡片宽度 = min(340, 可用宽度)，窗口变窄时只压缩波形。"""
        try:
            cap = getattr(self, "_voice_card_max", 340)
            avail = 16777215
            # 可用宽度：父滚动区视口宽 - 两侧留白
            p = self.parent()
            while p is not None:
                vp = getattr(p, "viewport", None)
                if callable(vp):
                    avail = max(180, vp().width() - 24)
                    break
                p = p.parent()
            target = min(cap, avail)
            if getattr(self, "voice_ctrl", None) is not None:
                vc = self.voice_ctrl
                vc.setSizePolicy(QSizePolicy.Policy.Fixed,
                                 QSizePolicy.Policy.Fixed)
                vc.setMinimumWidth(target)
                vc.setMaximumWidth(target)
                vc.resize(target, vc.height())
                # 让波形吃掉剩余宽度（按钮都是 Fixed，只有它 Expanding）
                try:
                    vc.layout().activate()
                    vc.updateGeometry()
                except Exception:
                    pass
            # 歌名行同步宽度（省略号按新宽度重算）
            if getattr(self, "voice_song_label", None) is not None:
                self.voice_song_label.setMaximumWidth(target)
                self._refresh_song_label()
        except Exception:
            pass

    def _show_voice_error(self, msg, disable_play=True):
        """在卡片歌名行位置显示一行红色提示；播放键置灰。

        纯文字，不用 emoji/符号。转文字按钮保持可点。
        """
        try:
            if disable_play and getattr(self, "voice_pause_btn", None):
                self.voice_pause_btn.setEnabled(False)
                self.voice_pause_btn.setToolTip(msg)
                self.voice_pause_btn.setStyleSheet(
                    "QPushButton#VoicePlayBtn{"
                    "background:#c9ccd6;color:#ffffff;border:none;"
                    "border-radius:16px;font-size:13px;}")
            lab = getattr(self, "voice_song_label", None)
            if lab is None:
                from PySide6.QtWidgets import QLabel as _QL
                self.voice_song_label = _QL()
                self.voice_song_label.setObjectName("VoiceSong")
                self.voice_song_label.setWordWrap(False)
                self.voice_song_label.setMinimumWidth(0)
                self.voice_song_label.setSizePolicy(
                    QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                try:
                    lay = self.layout()
                    lay.insertWidget(lay.indexOf(self.voice_ctrl) + 1,
                                     self.voice_song_label)
                    lay.setSpacing(0)
                except Exception:
                    self.layout().addWidget(self.voice_song_label)
                lab = self.voice_song_label
            _t = _CHAT_THEME
            lab.setText(msg)
            lab.setToolTip(msg)
            lab.setStyleSheet(
                "QLabel#VoiceSong{color:%s;font-size:12px;"
                "background:transparent;padding:2px 0 0 2px;}"
                % _t.get("vc_error_fg", "#c42b31"))
            lab.show()
            self._voice_error = msg
        except Exception as ex:
            log("[聊天] 语音错误提示失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)

    def _fmt_pos(self, pos_s, dur_s):
        """播放进度显示：00:12 / 03:58"""
        try:
            pos_s = max(0.0, float(pos_s or 0.0))
            dur_s = max(0.0, float(dur_s or 0.0))
            m1, s1 = divmod(int(pos_s), 60)
            m2, s2 = divmod(int(round(dur_s)), 60)
            return "%02d:%02d / %02d:%02d" % (m1, s1, m2, s2)
        except Exception:
            return "00:00 / 00:00"

    def resizeEvent(self, e):
        """窗口尺寸变化时重算卡片宽度（只压波形，不挤按钮）。"""
        try:
            super().resizeEvent(e)
        except Exception:
            pass
        if getattr(self, "voice_ctrl", None) is not None:
            self._apply_voice_card_width()

    def _refresh_song_label(self):
        """用正文里的「🎵 歌名」更新歌名小字行；没有就隐藏。"""
        lab = getattr(self, "voice_song_label", None)
        if lab is None:
            # 首次：根据来源创建（正文优先，其次歌词首行）
            if not getattr(self, "_song_line_src", ""):
                return
            from PySide6.QtWidgets import QLabel as _QL
            _t = _CHAT_THEME
            self.voice_song_label = _QL()
            self.voice_song_label.setObjectName("VoiceSong")
            self.voice_song_label.setStyleSheet(
                "QLabel#VoiceSong{color:%(vc_song_fg)s;font-size:13px;"
                "background:transparent;padding:0;}"
                % {"vc_song_fg": _t["vc_song_fg"]})
            # 单行 + 超长省略号（任何长度都不换行、不裁切）
            self.voice_song_label.setWordWrap(False)
            self.voice_song_label.setMinimumWidth(0)
            self.voice_song_label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            self.voice_song_label.setCursor(
                Qt.CursorShape.PointingHandCursor)
            self.voice_song_label.mousePressEvent = (
                lambda e: self._toggle_voice_text())
            try:
                lay = self.layout()
                lay.insertWidget(lay.indexOf(self.voice_ctrl) + 1,
                                 self.voice_song_label)
                lay.setSpacing(0)
            except Exception:
                self.layout().addWidget(self.voice_song_label)
            lab = self.voice_song_label
        # 提取歌名：正文「🎵 XXX - YYY」优先
        import re as _re
        name = ""
        try:
            m = _re.search(r"🎵\s*([^\n]+?)\s*$",
                           (self._plain or "").strip(),
                           _re.MULTILINE)
            if m:
                name = m.group(1).strip()
        except Exception:
            pass
        if not name:
            # 退化：歌词首行（不是路径）
            src = (getattr(self, "_song_line_src", "") or "").strip()
            first = src.splitlines()[0].strip() if src else ""
            if first and ":" not in first and ":" not in first \
                    and "/" not in first and "\\" not in first:
                name = first
        if name:
            full = "🎵 %s" % name
            # 单行省略：按卡片宽度用 QFontMetrics 截断
            try:
                from PySide6.QtGui import QFontMetrics as _FM
                cap = max(80, getattr(self, "_voice_card_max", 340) - 32)
                fm = _FM(lab.font())
                shown = fm.elidedText(full, Qt.TextElideMode.ElideRight, cap)
            except Exception:
                shown = full
            lab.setText(shown)
            lab.setToolTip(full)          # 悬停显示完整歌名
            lab.show()
        else:
            lab.hide()

    def _play_voice(self):
        """播放这条语音。三态：未播 → 播放；播放中 → 暂停；暂停中 → 继续。"""
        try:
            # 诊断日志：确认点击真的进来了（排查「点了没反应」用）
            try:
                log("[语音] 点击播放: path=%r exists=%s player=%s"
                    % (getattr(self, "_voice_path", ""),
                       os.path.exists(getattr(self, "_voice_path", "") or ""),
                       type(self._voice_player()).__name__
                       if self._voice_player() is not None else None))
            except Exception:
                pass
            player = self._voice_player()
            if player is None:
                self._show_voice_error("播放组件缺失，暂时无法播放")
                return
            # 播放器是全局单例：同一时间只有一条语音真正在播。
            # 若别的气泡处于「播放中/暂停中」，先复位它们，
            # 否则两张卡片会同时显示暂停图标、抢同一个进度，
            # 出现「进度超过自身总时长」这类错乱。
            self._reset_other_voice_bubbles()
            # 播放器存在但后端不可用（_player 为 False）
            if getattr(player, "_player", None) is False:
                self._show_voice_error("播放组件缺失，暂时无法播放")
                return

            # 正在播这条 → 暂停
            if getattr(self, "_playing_now", False) and player.is_playing():
                if player.pause():
                    self._playing_now = False
                    self._paused_now = True
                    self._sync_pause_btn()
                return

            # 暂停中 → 继续
            if getattr(self, "_paused_now", False) and player.is_paused():
                if player.resume():
                    self._playing_now = True
                    self._paused_now = False
                    self._sync_pause_btn()
                    self._start_voice_watch()
                return

            # 全新播放
            p = getattr(self, "_voice_path", "")
            if not p or not os.path.exists(p):
                self._show_voice_error("音频文件已丢失")
                return
            with open(p, "rb") as f:
                data = f.read()
            # 临时置零冷却，允许连点重播
            old_cd = getattr(player, "_cooldown", 0.0)
            old_ts = getattr(player, "_last_ok_ts", 0.0)
            player._cooldown = 0.0
            player._last_ok_ts = 0.0
            try:
                player._play_bytes(data, player._cfg_getter().get("volume", 80))
            finally:
                player._cooldown = old_cd
                player._last_ok_ts = old_ts
            self._playing_now = True
            self._paused_now = False
            if getattr(self, "wave_bar", None) is not None:
                self.wave_bar.set_progress(0.0)
            self._sync_pause_btn()
            self._start_voice_watch()
        except Exception as ex:
            log("[聊天] 语音播放失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)
            self._show_voice_error("播放失败")

    def _seek_voice(self, ms):
        """波形条拖动定位。"""
        log("[语音] 点击波形条 seek -> %d ms" % ms)
        player = self._voice_player()
        if player is None:
            return
        player.seek_ms(ms)

    def _cycle_voice_rate(self):
        """循环切换倍速。"""
        try:
            log("[语音] 点击倍速按钮（不是播放键）")
            self._voice_rate_i = (self._voice_rate_i + 1) % len(
                self._voice_rates)
            rate = self._voice_rates[self._voice_rate_i]
            # 0.75x / 1.25x 两位小数；1.0x / 1.5x / 2.0x 一位
            self.voice_rate_btn.setText(
                ("%.2fx" % rate) if abs(rate * 10 - int(rate * 10)) > 0.01
                else ("%.1fx" % rate))
            player = self._voice_player()
            if player is not None:
                try:
                    from PySide6.QtCore import QTimer as _QT
                    _QT.singleShot(0, lambda: self._apply_rate(rate))
                except Exception:
                    pass
        except Exception as ex:
            log("[聊天] 倍速切换失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)

    def _apply_rate(self, rate):
        """把倍速应用到播放器。"""
        player = self._voice_player()
        if player is None:
            return
        try:
            player.set_rate(rate)
        except Exception as ex:
            log("[聊天] 应用倍速失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)

    def _toggle_voice_text(self):
        """展开 / 收起文字内容。"""
        try:
            if getattr(self, "voice_text_area", None) is not None:
                vis = self.voice_text_area.isVisible()
                self.voice_text_area.setVisible(not vis)
                return
            # 首次展开：创建
            text = ""
            # 优先用气泡自带的原文（TTS 念的内容 / 歌词）
            text = getattr(self, "_voice_text", "") or ""
            if not text:
                # 兜底：从消息列表里找
                try:
                    owner = self
                    while owner is not None and not hasattr(owner, "pet"):
                        owner = owner.parent()
                    cw = owner
                except Exception:
                    cw = None
                if cw is not None and hasattr(cw, "messages"):
                    vp = getattr(self, "_voice_path", "")
                    msgs = cw.messages
                    for idx in range(len(msgs) - 1, -1, -1):
                        if msgs[idx].get("voice") == vp:
                            for j in range(idx + 1, min(idx + 3, len(msgs))):
                                if (msgs[j].get("role") == "assistant"
                                        and not msgs[j].get("voice")):
                                    text = msgs[j].get("content") or ""
                                    break
                            if text:
                                break
            if not text:
                text = "（这条语音没有保存文字内容）"
            from PySide6.QtWidgets import QLabel
            self.voice_text_area = QLabel(text)
            self.voice_text_area.setWordWrap(True)
            self.voice_text_area.setTextFormat(Qt.TextFormat.PlainText)
            self.voice_text_area.setStyleSheet(
                "background:#f7f8fb;border:1px solid #e3e7f0;"
                "border-radius:8px;padding:8px;color:#2f3542;font-size:12px;")
            self.voice_text_area.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            # 插到控制条后面
            lay = self.layout()
            lay.insertWidget(lay.indexOf(self.voice_ctrl) + 1,
                             self.voice_text_area)
            self.voice_text_area.show()
        except Exception as ex:
            log("[聊天] 转文字失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)

    def _sync_pause_btn(self):
        """播放钮图标跟随状态：待播/播放中=⏸（可暂停），暂停中=▶。"""
        b = getattr(self, "voice_pause_btn", None)
        if b is None:
            return
        player = self._voice_player()
        playing = bool(player is not None and player.is_playing())
        b.setText("⏸" if playing else "▶")
        b.setToolTip("暂停" if playing else "播放")

    def _stop_voice(self):
        """停止播放并复位 UI。"""
        player = self._voice_player()
        if player is not None:
            player.stop()
        self._playing_now = False
        self._paused_now = False
        try:
            self.voice_time.setText(self._fmt_pos(0, self._voice_sec))
            if getattr(self, "wave_bar", None) is not None:
                self.wave_bar.set_progress(0.0)
            self._sync_pause_btn()
        except Exception:
            pass

    def _start_voice_watch(self):
        """播放期间每 500ms 同步一次 UI，播完自动复位。"""
        from PySide6.QtCore import QTimer as _QT
        t = getattr(self, "_voice_watch", None)
        if t is None:
            t = _QT(self)
            t.timeout.connect(self._on_voice_watch)
            self._voice_watch = t
        t.start(500)

    def _on_voice_watch(self):
        player = self._voice_player()
        if player is None:
            return
        st = player.state()
        # 实时刷新进度和时间
        try:
            pos = max(0, player.position_ms())
            dur = max(0.2, float(getattr(self, "_voice_sec", 1.0)))
            if getattr(self, "wave_bar", None) is not None:
                self.wave_bar.set_progress(min(1.0, pos / (dur * 1000.0)))
            if getattr(self, "voice_time", None) is not None:
                self.voice_time.setText(
                    self._fmt_pos(pos / 1000.0, dur))
        except Exception:
            pass
        if st == "stopped":
            # 播完了：复位
            try:
                self._voice_watch.stop()
            except Exception:
                pass
            self._playing_now = False
            self._paused_now = False
            try:
                self.voice_time.setText(
                    self._fmt_pos(0, self._voice_sec))
                if getattr(self, "wave_bar", None) is not None:
                    self.wave_bar.set_progress(0.0)
                self._sync_pause_btn()
            except Exception:
                pass
        else:
            self._sync_pause_btn()


class WaveBar(QWidget):
    """QQ 风格波形图：播放进度用亮色竖条表示，可点击/拖动定位。"""

    seekRequested = Signal(int)   # 请求跳到毫秒

    def __init__(self, wav_path=None, duration=1.0, parent=None):
        super().__init__(parent)
        self._peaks = self._sample_peaks(wav_path)
        self._duration = max(0.2, float(duration or 1.0))
        self._progress = 0.0          # 0~1
        self._dragging = False
        self.setMinimumHeight(24)
        self.setMaximumHeight(24)
        # 最小宽度 0：窗口变窄时只让波形收缩，不挤掉两侧按钮
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # ---- 采样：读 WAV 转 N 个峰值 ----
    def _sample_peaks(self, path, bars=36):
        try:
            import wave as _w
            import struct as _s
            with _w.open(path, "rb") as w:
                sr = w.getframerate() or 22050
                n = w.getnframes()
                raw = w.readframes(n)
            width = w.getsampwidth()
            ch = w.getnchannels() or 1
            if width != 2 or n <= 0:
                return [0.2] * bars
            step = max(1, n // (bars * 8))
            samples = _s.unpack("<%dh" % (len(raw) // 2),
                                raw[:len(raw) // 2 * 2])
            if ch > 1:
                samples = samples[::ch]
            peaks = []
            per = max(1, len(samples) // bars)
            for i in range(bars):
                seg = samples[i * per:(i + 1) * per:step]
                if not seg:
                    peaks.append(0.1)
                    continue
                peak = max(abs(x) for x in seg) / 32768.0
                peaks.append(max(0.08, min(1.0, peak)))
            mx = max(peaks) or 1.0
            return [p / mx for p in peaks]
        except Exception:
            return [0.3] * bars

    def set_progress(self, ratio):
        self._progress = max(0.0, min(1.0, ratio))
        self.update()

    def set_duration(self, sec):
        self._duration = max(0.2, float(sec or 1.0))

    # ---- 交互 ----
    def mousePressEvent(self, e):
        self._dragging = True
        self._seek_to(e.position().x())

    def mouseMoveEvent(self, e):
        if self._dragging:
            self._seek_to(e.position().x())

    def mouseReleaseEvent(self, e):
        self._dragging = False

    def _seek_to(self, x):
        w = max(1, self.width())
        ratio = max(0.0, min(1.0, x / w))
        self._progress = ratio
        self.update()
        self.seekRequested.emit(int(ratio * self._duration * 1000))

    # ---- 绘制 ----
    def paintEvent(self, _):
        from PySide6.QtGui import QPainter, QColor
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        if w <= 2:
            p.end()
            return
        mid = h / 2
        n = len(self._peaks)
        # 条宽 + 固定 2px 间距；宽度不足时自动减少条数（绝不溢出裁切）
        gap = 2
        bw = max(1, (w - 2 - gap * (n - 1)) // max(1, n))
        if bw < 2:
            n = max(1, (w - 2 + gap) // (2 + gap))
            bw = 2
            peaks = self._peaks[:n]
        else:
            peaks = self._peaks
        total = bw * len(peaks) + gap * max(0, len(peaks) - 1)
        x = max(0, (w - total) // 2)
        played = self._progress
        on = QColor(_CHAT_THEME["vc_wave_on"])
        off = QColor(_CHAT_THEME["vc_wave_off"])
        p.setPen(Qt.PenStyle.NoPen)
        for i, pk in enumerate(peaks):
            bh = max(3, int(pk * (h - 4)))
            p.setBrush(on if (i + 1) / len(peaks) <= played + 1e-9 else off)
            p.drawRoundedRect(QRectF(x, mid - bh / 2, bw, bh), 1.0, 1.0)
            x += bw + gap
        p.end()


class ColorSwatchRow(QWidget):
    """主色卡片的一行：色块 + 色号 + 占比横条 + 百分比。点击复制色号。"""

    def __init__(self, hex_color, ratio, parent=None):
        super().__init__(parent)
        self._hex = hex_color or "#000000"
        self._copied = False
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(8)

        sw = QLabel()
        sw.setFixedSize(22, 22)
        sw.setStyleSheet(
            "background:%s;border-radius:4px;border:1px solid #dfe3ec;"
            % self._hex)
        h.addWidget(sw)

        self.code = QLabel(self._hex.upper())
        self.code.setFixedWidth(80)
        self.code.setStyleSheet(
            "color:#2f3542;font-size:11px;font-family:Consolas,monospace;")
        h.addWidget(self.code)

        bar = QWidget()
        bar.setFixedHeight(8)
        bar.setMinimumWidth(120)
        pct = max(0.0, min(1.0, float(ratio or 0)))
        bar.setStyleSheet(
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 %s,stop:%.4f %s,stop:%.4f #eef0f5,stop:1 #eef0f5);"
            "border-radius:4px;" % (self._hex, pct, self._hex, pct))
        h.addWidget(bar, 1)

        self.pct = QLabel("%.0f%%" % (pct * 100))
        self.pct.setFixedWidth(44)
        self.pct.setAlignment(Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
        self.pct.setStyleSheet("color:#8a8f9c;font-size:11px;")
        h.addWidget(self.pct)

        for w in (sw, self.code):
            w.setCursor(Qt.CursorShape.PointingHandCursor)
            w.setToolTip("点击复制色号")
            w.mousePressEvent = lambda e: self._copy()

    def _copy(self):
        try:
            QApplication.clipboard().setText(self._hex.upper())
            self._show_copied()
        except Exception:
            pass

    def _show_copied(self):
        """复制后该行显示「已复制」1 秒。"""
        try:
            if self._copied:
                return
            self._copied = True
            old = self.code.text()
            self.code.setText("已复制")
            self.code.setStyleSheet(
                "color:#1a7f37;font-size:11px;")

            def _restore():
                try:
                    self.code.setText(old)
                    self.code.setStyleSheet(
                        "color:#2f3542;font-size:11px;"
                        "font-family:Consolas,monospace;")
                    self._copied = False
                except Exception:
                    pass

            QTimer.singleShot(1000, _restore)
        except Exception:
            pass


class ColorCard(QWidget):
    """主色提取结果卡片：标题 + 每行一个色块。"""

    def __init__(self, colors, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        title = QLabel("页面主色")
        title.setStyleSheet(
            "color:#2f3542;font-size:12px;font-weight:bold;")
        v.addWidget(title)
        for c in (colors or [])[:8]:
            try:
                v.addWidget(ColorSwatchRow(c.get("hex"), c.get("ratio")))
            except Exception:
                continue


class TimeDivider(QWidget):
    """时间分隔线：—— 今天 14:32 ——（居中灰字）。"""

    def __init__(self, ts):
        super().__init__()
        from PySide6.QtWidgets import QHBoxLayout
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 10, 0, 10)
        lay.setSpacing(8)
        left = QFrame()
        left.setFrameShape(QFrame.Shape.HLine)
        left.setStyleSheet("color:#e3e7f0;")
        right = QFrame()
        right.setFrameShape(QFrame.Shape.HLine)
        right.setStyleSheet("color:#e3e7f0;")
        label = QLabel(_fmt_time(ts))
        label.setStyleSheet("color:#a8aebb;font-size:11px;")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(left, 1)
        lay.addWidget(label, 0)
        lay.addWidget(right, 1)


class ChatWindow(QWidget):
    _voice_ready = Signal(str, float, str, object)   # 语音文件就绪: path, 秒, 原文, 消息
    _cover_ready = Signal(str, str, str, str)        # 翻唱完成: 路径, 歌名, 歌手, 错误
    _vision_tool_done = Signal(bool, str, str)         # 视觉工具完成: 成功, 文字, 附图路径
    _cover_progress = Signal(str)                    # 翻唱进度文本
    _vision_attach_done = Signal()                   # 附件图片视觉描述完成
    """聊天窗口：多会话（sessions）+ 气泡列表 + 流式回复；请求/摘要/缓存逻辑沿用原实现。

    self.pet 指向 PetApp（全局控制器），不是单只宠物。"""


    def __init__(self, app):
        super().__init__(None)
        global _CHAT_THEME
        self.pet = app
        try:
            self._voice_ready.connect(self._on_voice_ready)
            self._cover_ready.connect(self._on_cover_ready)
            self._vision_tool_done.connect(self._on_vision_tool_done)
            self._vision_attach_done.connect(self._on_vision_attach_done)
            self._cover_progress.connect(self._on_cover_progress)
        except Exception:
            pass
        self.setObjectName("ChatRoot")
        # 窗口标题跟随宠物显示名（顶栏双击改名后同步）
        self.setWindowTitle("%s · 聊天" % pet_name_from_cfg(app.cfg))
        self.setMinimumSize(CHAT_MIN_WIDTH, 460)
        self.worker = None
        self.summary_worker = None

        # 自动记忆：已提取位置 / 待处理计数 / 提取线程
        self._mem_extracted_upto = 0
        self._mem_pending = 0
        self._mem_worker = None
        self._mem_worker_sid = None
        self._mem_worker_upto = 0
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
        _side_w = int(self.pet.cfg.get("chat_sidebar_width",
                                       CHAT_LEFT_DEFAULT)
                      or CHAT_LEFT_DEFAULT)
        _side_w = max(CHAT_LEFT_MIN, min(CHAT_LEFT_MAX, _side_w))
        self.sidebar.setFixedWidth(_side_w)
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

        # 底部：一个「设置」文字按钮，菜单里含记忆入口
        foot = QHBoxLayout()
        foot.setSpacing(6)
        foot.setContentsMargins(4, 6, 4, 4)
        self.settings_btn = QPushButton("设置")
        self.settings_btn.setObjectName("SideTextBtn")
        self.settings_btn.setFixedHeight(30)
        self.settings_btn.setToolTip("设置与记忆")
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.settings_btn.setPopupMode = None
        self.settings_menu = QMenu(self.settings_btn)
        self.settings_menu.setFixedWidth(168)
        # 菜单项顺序：记忆 → ─── → Key → 重命名 → 清空 → ─── → 删除 → 完整设置
        # 文字在 aboutToShow 时刷新（每次打开菜单都重新读 Key 状态）
        # 菜单：长期记忆 + 对话参数 + 完整设置
        self.memory_action = self.settings_menu.addAction(
            "长期记忆…", lambda: self.pet._open_memory())
        self.settings_menu.addAction("人设…", self._open_persona_dlg)
        self.settings_menu.addSeparator()
        self.settings_menu.addAction("对话参数…", self._open_dialog_params)
        self.settings_menu.addSeparator()
        self.settings_menu.addAction("打开完整设置…",
                                     lambda: self.pet.open_settings())
        # 菜单阴影（QSS 不支持 box-shadow，用 QGraphicsDropShadowEffect）
        try:
            from PySide6.QtWidgets import QGraphicsDropShadowEffect
            from PySide6.QtGui import QColor as _QC
            _sh = QGraphicsDropShadowEffect(self.settings_menu)
            _sh.setBlurRadius(24)
            _sh.setXOffset(0)
            _sh.setYOffset(8)
            _sh.setColor(_QC(40, 50, 120, 38))
            self.settings_menu.setGraphicsEffect(_sh)
        except Exception:
            pass
        self.settings_menu.aboutToShow.connect(self._refresh_settings_menu)
        # 展开时按钮保持高亮：换 objectName，收起后还原
        self.settings_menu.aboutToShow.connect(
            lambda: self.settings_btn.setObjectName("SideTextBtnActive"))
        self.settings_menu.aboutToHide.connect(self._restore_settings_btn_style)
        # 不用 setMenu 自动弹（会自动溢出窗口），改手动控制位置
        self.settings_btn.clicked.connect(self._show_settings_menu)
        foot.addWidget(self.settings_btn, 0)
        foot.addStretch(1)
        side.addLayout(foot)
        root.addWidget(self.sidebar)

        # 侧栏右边缘拖拽条：拖动调整宽度
        self.drag_handle = SidebarDragHandle(
            self.sidebar, self._save_sidebar_width)
        root.addWidget(self.drag_handle)

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
        rlay.setContentsMargins(0, 0, 0, 8)
        rlay.setSpacing(0)

        # 顶部栏：标题 + 在线状态（标题双击可改名）
        self.top_bar = ChatTopBar(right)
        self.top_bar.set_name(pet_name_from_cfg(app.cfg))
        self.top_bar.name_changed.connect(self._on_pet_name_changed)
        rlay.addWidget(self.top_bar)

        # 消息区容器（保留原有内边距）
        _body = QWidget(right)
        _bd = QVBoxLayout(_body)
        _bd.setContentsMargins(10, 8, 10, 0)
        _bd.setSpacing(7)

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
        _bd.addWidget(self.scroll, 1)
        rlay.addWidget(_body, 1)

        # 右下角浮动「回到底部」按钮（父级是消息区容器，绝对定位）
        self._follow_bottom = True
        self._msg_body = _body          # resizeEvent 里定位按钮用
        self._build_scroll_bottom_btn(_body)
        _sb = self.scroll.verticalScrollBar()
        _sb.valueChanged.connect(lambda _v: self._update_scroll_btn())
        _sb.rangeChanged.connect(lambda _a, _b: self._update_scroll_btn())

        self._build_empty_state()

        self.load_more_btn = QPushButton("加载更早消息")
        self.load_more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.load_more_btn.clicked.connect(self._load_earlier)

        # 输入框：2 行起，随内容增高，最多 6 行
        self.input = QTextEdit(right)
        self.input.setObjectName("ChatInput")
        self.input.setPlaceholderText("输入消息…")
        self.input.setAcceptRichText(False)
        # 复制粘贴等标准操作
        self.input.setUndoRedoEnabled(True)
        self.input.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.input.installEventFilter(self)
        self.input.textChanged.connect(self._autosize_input)
        self.input.setFixedHeight(CHAT_INPUT_MIN_H)
        # 附件缩略条（有附件时才显示）
        self._pending_atts = []
        self.attach_bar = QWidget()
        self.attach_bar.setMaximumHeight(64)   # 56px 缩略图 + 上下留白
        _ab = QHBoxLayout(self.attach_bar)
        _ab.setContentsMargins(0, 0, 0, 2)
        _ab.setSpacing(6)
        _ab.addStretch(1)
        self.attach_bar.hide()
        rlay.addWidget(self.attach_bar)

        # 输入区卡片：附件条 + [加号 | 输入框 | 发送] 一行
        # 方案 A：加号嵌进输入框左侧，不再单独占一行
        self.attach_btn = QPushButton("+")
        self.attach_btn.setObjectName("PlusBtn")
        self.attach_btn.setFixedSize(30, 30)   # radius 15 = 真圆
        self.attach_btn.setToolTip("添加图片或文本文件")
        self.attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.attach_btn.clicked.connect(self._show_attach_menu)

        self.send_btn = SendButton()
        self.send_btn.setDefault(True)
        self.send_btn.setToolTip("发送")
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.clicked.connect(self._on_send_clicked)

        # ---- 第一行：输入框（全宽，DSH 结构）----
        _inrow = QHBoxLayout()
        _inrow.setSpacing(0)
        _inrow.setContentsMargins(2, 0, 2, 0)
        _inrow.addWidget(self.input, 1)

        # ---- 第二行：加号 + 模型名 + 发送（DSH 工具行）----
        _botrow = QHBoxLayout()
        _botrow.setSpacing(8)
        _botrow.setContentsMargins(0, 0, 0, 0)
        _botrow.addWidget(self.attach_btn, 0)
        _botrow.addStretch(1)
        # 模型信息按钮：点击弹菜单切换模型 / 推理等级
        self.model_btn = QPushButton("")
        self.model_btn.setObjectName("ModelBtn")
        self.model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.model_btn.setFlat(True)
        self.model_btn.clicked.connect(self._show_model_menu)
        _botrow.addWidget(self.model_btn, 0)
        _botrow.addWidget(self.send_btn, 0)

        self.input_card = QFrame()
        self.input_card.setObjectName("InputCard")
        self.input_card.setProperty("focused", "false")   # 默认灰边，聚焦才蓝
        _card = QVBoxLayout(self.input_card)
        _card.setContentsMargins(12, 8, 10, 6)
        _card.setSpacing(4)
        _card.addWidget(self.attach_bar)
        _card.addLayout(_inrow)
        _card.addLayout(_botrow)
        rlay.addWidget(self.input_card)
        self._refresh_model_label()

        # 允许拖拽文件进窗口
        self.setAcceptDrops(True)

        # 底部行：用量文字（独立一行，参考图样式）
        self.token_label = QLabel("")
        self.token_label.setObjectName("UsageLabel")
        self.token_label.setToolTip(self._usage_tooltip())
        self.token_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Preferred)
        self.token_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
        self.token_label.setVisible(False)   # 没有用量数据时整行隐藏
        rlay.addWidget(self.token_label)

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
        return {"id": self._new_id(), "title": title, "messages": [],
                "summary": "", "summarized_upto": 0,
                "mem_extracted_upto": 0, "created": now,
                "updated": now}

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

    # 消息里要保留的附加字段（voice/图片/原文/时间戳等）
    _MSG_KEEP_FIELDS = ("voice", "voice_sec", "voice_text", "image", "ts")

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
                    item = {"role": m["role"],
                            "content": str(m.get("content") or "")}
                    # 保留语音条 / 图片 / 时间戳等附加字段，否则重开聊天全部丢失
                    for k in self._MSG_KEEP_FIELDS:
                        if k in m and m[k] is not None:
                            item[k] = m[k]
                    clean.append(item)
        title = str(raw.get("title") or "未命名").strip()[:30] or "未命名"
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        created = str(raw.get("created") or now)
        updated = str(raw.get("updated") or created)
        try:
            su = int(raw.get("summarized_upto") or 0)
        except Exception:
            su = 0
        su = max(0, min(su, len(clean)))
        try:
            me = int(raw.get("mem_extracted_upto") or 0)
        except Exception:
            me = 0
        me = max(0, min(me, len(clean)))
        return {"id": sid, "title": title, "messages": clean,
                "summary": str(raw.get("summary") or ""),
                "summarized_upto": su, "mem_extracted_upto": me,
                "created": created, "updated": updated}

    def _sync_session(self, touch=True):
        s = self._current_session()
        if s is None:
            return
        s["messages"] = self.messages
        s["summary"] = self.summary
        s["summarized_upto"] = int(self.summarized_upto or 0)
        s["mem_extracted_upto"] = int(
            getattr(self, "_mem_extracted_upto", 0) or 0)
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
        try:
            self._mem_extracted_upto = int(
                s.get("mem_extracted_upto") or 0)
        except Exception:
            self._mem_extracted_upto = 0
        self._mem_extracted_upto = max(
            0, min(self._mem_extracted_upto, len(self.messages)))
        self._mem_pending = 0
        self._render_limit = 50
        # 切换/新建会话时清空待发送附件，避免串到别的对话里
        try:
            self._pending_atts = []
            self._refresh_attach_bar()
        except Exception:
            pass
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
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "load_history", type(ex).__name__, ex),
                logging.WARNING)

    def save_history(self):
        self._sync_session(touch=True)
        self._write_sessions()

    # ---------- 会话栏 ----------
    # ---------- 左栏：会话列表 ----------
    def _refresh_model_label_safe(self):
        try:
            self._refresh_model_label()
        except Exception:
            pass

    def _refresh_combo(self):
        """把会话按最近更新排序刷进左侧列表（保留方法名，内部已改为 QListWidget）。"""
        try:
            self._rebuilding = True
            self.session_list.blockSignals(True)
            self.session_list.clear()
            ordered = sorted(self.sessions, key=lambda s: s.get("updated") or "", reverse=True)
            cur_row = 0
            # 侧栏可用宽度 → 单行省略号（保留完整标题在 tooltip）
            _avail = max(80, CHAT_LEFT_WIDTH - 34)
            _fm = self.session_list.fontMetrics()
            for i, s in enumerate(ordered):
                _full = s.get("title") or "未命名"
                _shown = _fm.elidedText(_full, Qt.TextElideMode.ElideRight,
                                        _avail)
                item = QListWidgetItem(_shown)
                item.setData(Qt.ItemDataRole.UserRole, s.get("id"))
                item.setData(Qt.ItemDataRole.UserRole + 1, _full)
                item.setToolTip(_full)
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

    def _on_pet_name_changed(self, name):
        """顶栏双击改名：写配置 + 同步窗口标题。"""
        try:
            n = (name or "").strip()[:20]
            if not n:
                return
            self.pet.cfg["pet_name"] = n
            self.pet.save_cfg()
            self.setWindowTitle("%s · 聊天" % n)
            log("[聊天] 显示名改为：%s" % n)
        except Exception as ex:
            log("[聊天] 保存显示名失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _current_user_query(self):
        """本轮用于记忆相关性匹配的用户问题（最近一条 user 消息）。"""
        try:
            for m in reversed(self.messages or []):
                if m.get("role") == "user":
                    t = (m.get("content") or "").strip()
                    if t and not _is_proactive_placeholder(t):
                        return t[:500]
            return ""
        except Exception:
            return ""

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

    def _switch_session(self, sid):
        if self.busy or not sid or sid == self.current_id:
            return
        self._sync_session(touch=False)
        if not self._activate_session(sid):
            return
        self._render_current_session()
        self._set_usage_label("")
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

    def _inject_greeting(self, sess):
        """新会话开头插入一条宠物开场白（来自导入的角色卡）。

        只在新会话为空时插入，避免重复或覆盖已有内容。
        """
        try:
            greet = (self.pet.cfg.get("persona_greeting") or "").strip()
            if not greet:
                return
            if not self._session_is_empty(sess):
                return
            msg = {
                "role": "assistant",
                "content": greet,
                "ts": time.time(),
            }
            msgs = sess.setdefault("messages", [])
            msgs.append(msg)
            self.messages = msgs
            log("[聊天] 已插入开场白（%d 字）" % len(greet))
        except Exception as ex:
            log("[聊天] 插入开场白失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _new_session(self):
        """新建会话。当前会话还是空的就直接复用，只把焦点放回输入框。"""
        if self.busy:
            return
        self._sync_session(touch=False)
        cur = self._current_session()
        # 当前会话没有任何消息 → 不新建，直接聚焦输入框
        if self._session_is_empty(cur):
            # 待发送附件也不保留（用户点「新对话」就是要干净的开端）
            try:
                self._pending_atts = []
                self._refresh_attach_bar()
            except Exception:
                pass
            self._render_current_session()
            self._refresh_combo()
            self._update_busy_ui()
            self.input.setFocus()
            return
        s = self._make_session(self._next_default_title())
        self.sessions.append(s)
        self._activate_session(s["id"])
        self._inject_greeting(s)
        self._render_current_session()
        self._set_usage_label("")
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
        # 同时清理该会话的附件缩略图
        try:
            from petcore import chat_attach as CA
            n = CA.delete_session_images(APP_DIR, sid)
            if n:
                log("[聊天] 已清理会话 %s 的 %d 个附件图片" % (sid, n))
        except Exception as ex:
            log("[聊天] 清理附件图片失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
        if not self.sessions:
            self.sessions = [self._make_session(self._next_default_title())]
            self._activate_session(self.sessions[0]["id"])
        elif sid == self.current_id:
            recent = max(self.sessions, key=lambda x: x.get("updated") or "")
            self._activate_session(recent["id"])
        self._render_current_session()
        self._set_usage_label("")
        self._write_sessions()
        self._refresh_combo()
        self._update_busy_ui()

    def _delete_current_session(self):
        self._delete_session(self.current_id)

    # ---------- 左栏收起/展开 ----------
    def _save_sidebar_width(self, w):
        """拖拽结束后保存侧栏宽度。"""
        try:
            w = int(max(CHAT_LEFT_MIN, min(CHAT_LEFT_MAX, int(w))))
            self.pet.cfg["chat_sidebar_width"] = w
            self.pet.save_cfg()
            log("[聊天] 侧栏宽度 -> %d" % w)
        except Exception as ex:
            log("[聊天] 保存侧栏宽度失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _set_sidebar_collapsed(self, collapsed):
        self._sidebar_collapsed = bool(collapsed)
        try:
            self.sidebar.setVisible(not collapsed)
            self.sidebar_mini.setVisible(bool(collapsed))
            if hasattr(self, "drag_handle"):
                self.drag_handle.setVisible(not collapsed)
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
            if hasattr(self, "drag_handle"):
                self.drag_handle.setVisible(not collapsed)
        except Exception:
            pass

    # ---------- 输入框自动增高 ----------
    def _exec_menu_in_window(self, menu, anchor_widget):
        """在窗口内弹出菜单，自动避开窗口边界。

        优先显示在 anchor 下方；下方放不下就翻到上方；
        左右也做夹紧，保证菜单不会溢出聊天窗口。
        """
        from PySide6.QtCore import QPoint
        win = self.geometry()          # 聊天窗口的屏幕矩形
        size = menu.sizeHint()
        mw, mh = size.width(), size.height()

        # anchor 左下角的全局坐标
        r = anchor_widget.rect()
        below = anchor_widget.mapToGlobal(QPoint(r.left(), r.bottom() + 2))
        above = anchor_widget.mapToGlobal(QPoint(r.left(), r.top() - mh - 2))

        # 垂直：优先下方，放不下就上方
        y = below.y()
        if y + mh > win.bottom():
            y = above.y()
            if y < win.top():
                y = max(win.top(), win.bottom() - mh)

        # 水平：夹在窗口内
        x = below.x()
        if x + mw > win.right():
            x = win.right() - mw
        x = max(win.left(), x)

        menu.exec(QPoint(int(x), int(y)))

    def _open_persona_dlg(self):
        """人设编辑弹窗（复用设置窗口的人设控件与逻辑）。"""
        try:
            sw = self.pet.settings_window
            if sw is None:
                # 没开过设置窗口：临时建一个（不显示），用完销毁
                from petcore.settings import SettingsWindow
                sw = SettingsWindow(self.pet)
                self.pet.settings_window = sw
            dlg = PersonaDialog(self, sw)
            dlg.exec()
            self._refresh_model_label()
        except Exception as ex:
            log("[聊天] 打开人设失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _open_dialog_params(self):
        """对话参数弹窗：上下文条数 / 温度 / Top P / 流式输出。"""
        try:
            dlg = DialogParamsDialog(self, self.pet)
            dlg.exec()
            self._refresh_model_label()
        except Exception as ex:
            log("[聊天] 打开对话参数失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _known_models(self):
        """收集可选模型：用过的历史 + 设置页识别过的 + 当前生效的 + 内置。"""
        out = []
        seen = set()

        def _add(n):
            n = (n or "").strip()
            if n and n not in seen:
                seen.add(n)
                out.append(n)

        # 1) 用过的模型（最近在前）
        try:
            for n in (self.pet.cfg.get("model_history") or []):
                _add(n)
        except Exception:
            pass
        # 2) 当前生效的
        _add(self.pet.cfg.get("chat_model") or "")
        # 3) 设置页识别过的
        try:
            sw = getattr(self.pet, "settings_window", None)
            cb = getattr(sw, "chat_model", None) if sw is not None else None
            if cb is not None:
                for i in range(cb.count()):
                    _add(cb.itemText(i))
        except Exception:
            pass
        # 4) 内置兜底
        for n in ("deepseek-chat", "deepseek-reasoner"):
            _add(n)
        return out

    def _show_model_menu(self):
        """模型按钮菜单：模型 › / 推理等级 ›"""
        m = QMenu(self)
        m.setObjectName("ModelMenu")

        # ---- 模型二级菜单 ----
        mm = m.addMenu("模型")
        cur_model = (self.pet.cfg.get("chat_model") or "").strip()
        for name in self._known_models():
            act = mm.addAction(name)
            act.setCheckable(True)
            act.setChecked(name == cur_model)
            act.triggered.connect(
                lambda _=False, n=name: self._set_model(n))

        # ---- 推理等级二级菜单 ----
        rm = m.addMenu("推理等级")
        cur_lv = (self.pet.cfg.get("reasoning_effort") or "").strip()
        for key, label in REASONING_LEVELS:
            act = rm.addAction(label)
            act.setCheckable(True)
            act.setChecked(key == cur_lv)
            act.triggered.connect(
                lambda _=False, k=key: self._set_reasoning(k))

        self._exec_menu_in_window(m, self.model_btn)

    def _set_model(self, name):
        try:
            name = (name or "").strip()
            if not name:
                return
            old = (self.pet.cfg.get("chat_model") or "").strip()
            self.pet.cfg["chat_model"] = name
            # 记进历史：新模型排最前，旧模型也要留下（首次切换时它还没进过历史）
            hist = []
            for n in [name, old] + list(self.pet.cfg.get("model_history") or []):
                if isinstance(n, str) and n and n not in hist:
                    hist.append(n)
            self.pet.cfg["model_history"] = hist[:10]
            self.pet.save_cfg()
            self._refresh_model_label()
            log("[聊天] 模型 -> %s（历史 %d 个）"
                % (name, len(hist[:10])))
        except Exception as ex:
            log("[聊天] 切换模型失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _set_reasoning(self, key):
        try:
            key = (key or "").strip()
            self.pet.cfg["reasoning_effort"] = key
            self.pet.save_cfg()
            self._refresh_model_label()
            log("[聊天] 推理等级 -> %r" % key)
        except Exception as ex:
            log("[聊天] 设置推理等级失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _refresh_model_label(self):
        """刷新底部工具行的模型按钮文字（模型名 · 推理等级）。"""
        try:
            name = (self.pet.cfg.get("chat_model") or "").strip()
            name = name or "未配置模型"
            if len(name) > 22:
                name = name[:20] + "…"
            lv = (self.pet.cfg.get("reasoning_effort") or "").strip()
            if lv:
                label = dict((k, v) for k, v in REASONING_LEVELS).get(lv, lv)
                name = "%s · %s" % (name, label)
            _btn = getattr(self, "model_btn", None)
            if _btn is not None:
                _btn.setText(name + "  ⌄")
                _btn.setToolTip("点击切换模型 / 推理等级")
        except Exception:
            pass

    def _autosize_input(self):
        """默认 1 行，随内容增高，最多 6 行。"""
        try:
            fm = QFontMetrics(self.input.font())
            line_h = max(15, fm.lineSpacing())
            doc = self.input.document()
            doc.setTextWidth(max(80, self.input.viewport().width()))
            lines = max(2, min(CHAT_INPUT_MAX_LINES,
                               doc.size().height() / max(1.0, line_h)))
            h = int(lines * line_h + 12)
            h = max(CHAT_INPUT_MIN_H, h)
            if abs(self.input.height() - h) > 1:
                self.input.setFixedHeight(h)
        except Exception:
            pass

    def _maybe_auto_title(self, text):
        """新对话发第一条消息后自动命名（默认名才生效）。

        - 取第一条真实用户消息的前 12 个字（跳过主动搭话等占位符）
        - 第一条是纯图片 → 「图片对话」
        - 全是宠物主动消息 → 「主动搭话」
        - 已有标题保持不变
        """
        s = self._current_session()
        if s is None:
            return
        if not re.match(r"^新对话\s*\d*$", (s.get("title") or "").strip()):
            return
        msgs = s.get("messages") or []
        # 找第一条真实用户消息（跳过占位符）
        first_user = None
        for m in msgs:
            if m.get("role") != "user":
                continue
            if _is_proactive_placeholder(m.get("content")):
                continue
            first_user = m
            break
        if first_user is None:
            # 没有真实用户消息：有宠物消息就是主动搭话
            if any(m.get("role") == "assistant" for m in msgs):
                s["title"] = "主动搭话"
                s["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._refresh_combo()
            return
        raw = " ".join((first_user.get("content") or "").split())
        # 去掉附件标记，只看用户真正打的字
        raw = re.sub(r"【附件[:：][^】]*】", "", raw).strip()
        raw = re.sub(r"【用户发送了图片[:：][^】]*】", "", raw).strip()
        has_img = bool(first_user.get("attachments")) or bool(
            first_user.get("image"))
        if not raw:
            if has_img:
                s["title"] = "图片对话"
                s["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._refresh_combo()
            return
        title = raw[:12].strip()
        if not title:
            return
        s["title"] = title
        s["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._refresh_combo()

    def _show_settings_menu(self):
        """弹出设置菜单，位置夹在聊天窗口内。"""
        try:
            self._exec_menu_in_window(self.settings_menu, self.settings_btn)
        except Exception as ex:
            log("[聊天] 打开设置菜单失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _restore_settings_btn_style(self):
        """菜单收起后把「设置」按钮还原成普通样式（重新应用 QSS）。"""
        try:
            self.settings_btn.setObjectName("SideTextBtn")
            # objectName 改变后必须重新 polish 才能生效
            self.settings_btn.style().unpolish(self.settings_btn)
            self.settings_btn.style().polish(self.settings_btn)
            self.settings_btn.update()
        except Exception:
            pass

    def _refresh_settings_menu(self):
        """菜单每次打开时重新读取：Key 是否已设置、生成中禁用哪些项。"""
        try:
            key = (self.pet.cfg.get("ds_api_key") or "").strip()
            _ka = getattr(self, "key_action", None)
            if _ka is not None:
                _ka.setText("设置 Key（已设置）" if key
                            else "设置 Key（未设置）")
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_refresh_settings_menu", type(ex).__name__, ex),
                logging.WARNING)
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
            for _n in ("rename_action", "delete_action", "clear_action"):
                _a = getattr(self, _n, None)
                if _a is not None:
                    _a.setEnabled(not busy)
            self.memory_action.setEnabled(True)
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
            b._style(t)              # 用当前主题刷新底色
            b._refresh_voice_theme(t)   # 卡片内联样式/歌名色/波形

    def _load_avatar(self):
        for name in ("正面.png", "icon.png"):
            try:
                pix = QPixmap(os.path.join(SPRITE_DIR, name))
                if not pix.isNull():
                    return pix
            except Exception as ex:
                log("[%s] %s 失败: %s: %s"
                    % (__name__, "_load_avatar", type(ex).__name__, ex),
                    logging.WARNING)
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
        self.empty_text = QLabel(random.choice(
            self._empty_greetings()))
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

    def _show_full_image(self, path):
        """点缩略图 → 弹窗看大图。"""
        try:
            dlg = QDialog(self)
            dlg.setWindowTitle("截图")
            lay = QVBoxLayout(dlg)
            lay.setContentsMargins(8, 8, 8, 8)
            lab = QLabel()
            pm = QPixmap(path)
            if pm.isNull():
                lab.setText("图片读不出来")
            else:
                screen = QApplication.primaryScreen().availableGeometry()
                max_w = int(screen.width() * 0.8)
                max_h = int(screen.height() * 0.8)
                if pm.width() > max_w or pm.height() > max_h:
                    pm = pm.scaled(max_w, max_h,
                                   Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
                lab.setPixmap(pm)
            lay.addWidget(lab)
            dlg.exec()
        except Exception as ex:
            log("[聊天] 查看大图失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)

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
                    self.empty_text.setText(random.choice(
            self._empty_greetings()))
                except Exception as ex:
                    log("[%s] %s 失败: %s: %s"
                        % (__name__, "_update_empty_state", type(ex).__name__, ex),
                        logging.WARNING)
            self._fit_empty_state()
            self.empty_state.setVisible(show)
            if show:
                self.empty_state.raise_()
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_update_empty_state", type(ex).__name__, ex),
                logging.WARNING)

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
        """恢复窗口大小/位置与左栏收起状态（多屏安全）。"""
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
                    # 与任一屏幕有交集就恢复（副屏也支持）
                    try:
                        from PySide6.QtCore import QRect
                        rect = QRect(x, y, w, h)
                        ok = any(s.availableGeometry().intersects(rect)
                                 for s in QApplication.screens())
                    except Exception:
                        ok = False
                    if ok:
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
            self._place_scroll_bottom_btn()
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
    def _is_at_bottom(self, threshold=40):
        """当前是否贴着底部（阈值内算在底部）。"""
        try:
            sb = self.scroll.verticalScrollBar()
            return (sb.maximum() - sb.value()) <= threshold
        except Exception:
            return True

    def _scroll_bottom(self):
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _scroll_bottom_if_follow(self):
        """只有原本就在底部时才跟随到底；用户翻看历史时不打扰。"""
        if getattr(self, "_follow_bottom", True):
            self._scroll_bottom()

    def _update_scroll_btn(self):
        """按当前滚动位置显示/隐藏「回到底部」按钮。"""
        try:
            btn = getattr(self, "scroll_bottom_btn", None)
            if btn is None:
                return
            at_bottom = self._is_at_bottom()
            # 在底部时记住「跟随」状态，离开底部则取消跟随
            self._follow_bottom = at_bottom
            if at_bottom:
                btn.hide()
            else:
                # 先定位再显示（布局可能刚变过）
                self._place_scroll_bottom_btn()
                btn.show()
                btn.raise_()
        except Exception:
            pass

    def _build_scroll_bottom_btn(self, parent):
        """右下角浮动「回到底部」圆形按钮。"""
        btn = QPushButton("∨", parent)
        btn.setObjectName("ScrollBottomBtn")
        btn.setFixedSize(32, 32)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip("回到底部")
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setStyleSheet(
            "QPushButton#ScrollBottomBtn {"
            "  background:#ffffff; color:#5a6070;"
            "  border:1px solid #dfe3ec; border-radius:16px;"
            "  font-size:14px; font-weight:700; padding:0 0 2px 0;"
            "}"
            "QPushButton#ScrollBottomBtn:hover {"
            "  background:#5686fe; color:#ffffff; border-color:#5686fe;"
            "}"
            "QPushButton#ScrollBottomBtn:pressed {"
            "  background:#3f6fe8; border-color:#3f6fe8;"
            "}")
        btn.clicked.connect(self._on_scroll_bottom_clicked)
        btn.hide()
        self.scroll_bottom_btn = btn
        return btn

    def _on_scroll_bottom_clicked(self):
        self._follow_bottom = True
        self._scroll_bottom()
        self._update_scroll_btn()

    def _place_scroll_bottom_btn(self):
        """把浮动按钮放到消息滚动区右下角。

        用 scroll 的实际几何（映射到父级坐标）算位置，
        不依赖父容器高度（布局未完成时父高度可能还是 0）。
        """
        try:
            btn = getattr(self, "scroll_bottom_btn", None)
            if btn is None:
                return
            host = btn.parentWidget()
            if host is None:
                return
            # scroll 在 host 坐标系里的矩形
            r = self.scroll.geometry()
            # 距滚动区右边缘 20px、下边缘 16px
            x = r.x() + r.width() - btn.width() - 20
            y = r.y() + r.height() - btn.height() - 16
            # 兜底：不越出 host
            x = max(0, min(x, host.width() - btn.width()))
            y = max(0, min(y, host.height() - btn.height()))
            btn.move(int(x), int(y))
        except Exception:
            pass

    def _add_bubble(self, role, text, image_path=None, voice_path=None,
                    voice_sec=0.0, ts=None, voice_text="", attachments=None):
        b = ChatBubble(role, text, image_path=image_path,
                       voice_path=voice_path, voice_sec=voice_sec, ts=ts,
                       voice_text=voice_text, attachments=attachments)
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
        # 实时消息：与上一条间隔超过 5 分钟就插时间分隔线
        prev = getattr(self, "_last_msg_ts", None)
        if ts and (prev is None or _needs_time_divider(prev, ts)):
            self.msg_layout.insertWidget(
                self.msg_layout.count() - 1, TimeDivider(ts))
        if ts:
            self._last_msg_ts = ts
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, wrap)
        self._update_empty_state()
        self._scroll_bottom_if_follow()
        return b

    def _remove_bubble(self, b):
        if b is None:
            return
        try:
            # 会话切换重渲染可能已把 C++ 对象删掉，此时无需再删
            import shiboken6 as _sb
            if not _sb.isValid(b):
                return
            wrap = getattr(b, "_wrap", None)
            if wrap is not None:
                if _sb.isValid(wrap):
                    self.msg_layout.removeWidget(wrap)
                    wrap.deleteLater()
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_remove_bubble", type(ex).__name__, ex),
                logging.WARNING)

    def render_message(self, role, text, image_path=None, voice_path=None,
                       voice_sec=0.0, ts=None, voice_text="", attachments=None):
        return self._add_bubble(role, text, image_path=image_path,
                                voice_path=voice_path, voice_sec=voice_sec,
                                ts=ts, voice_text=voice_text,
                                attachments=attachments)

    def append_external_to_session(self, sid, role, text, notice=None,
                                   image_path=None, voice_path=None,
                                   voice_sec=0.0, voice_text="", ts=None):
        """把消息写进指定会话（不依赖当前显示），用于记忆回访。

        若该会话正在显示，同时渲染气泡；否则只落盘。
        """
        try:
            s = self._find_session(sid)
            if s is None:
                log("[记忆] 目标会话不存在: %s" % sid)
                return None
            msg = {"role": role, "content": text,
                   "ts": float(ts or time.time())}
            if image_path:
                msg["image"] = image_path
            if voice_path:
                msg["voice"] = voice_path
                msg["voice_sec"] = float(voice_sec or 0.0)
                msg["voice_text"] = voice_text or ""
            s.setdefault("messages", []).append(msg)
            s["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            # 正在显示该会话 → 同步渲染
            if sid == self.current_id:
                self.messages = s["messages"]
                try:
                    self.render_message(role, text,
                                        image_path=image_path,
                                        voice_path=voice_path,
                                        voice_sec=voice_sec,
                                        voice_text=voice_text,
                                        ts=msg["ts"])
                    self._scroll_bottom()
                except Exception as ex:
                    log("[记忆] 渲染回访消息失败: %s: %s"
                        % (type(ex).__name__, ex), logging.DEBUG)
            self.save_history()
            return msg
        except Exception as ex:
            log("[记忆] 写入指定会话失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            return None

    def append_external(self, role, text, notice=None, image_path=None,
                        voice_path=None, voice_sec=0.0, ts=None,
                        voice_text=""):
        """外部功能（如「看看屏幕」）把消息追加到当前会话。

        作用：
          - 写入 self.messages 并落盘 → 下次打开还在，可继续追问
          - 立刻在界面上显示气泡
          - 可选在消息前插一条 notice（提示气泡，不入会话历史）
          - image_path: 本地图片路径，给了就在气泡里显示缩略图（点击放大）
          - voice_text: 语音对应的原文（TTS 念的内容 / 歌词），供转文字用

        返回写入的消息对象；无当前会话时返回 None。
        """
        sess = self._current_session()
        if sess is None:
            return None
        text = (text or "").strip()
        if not text:
            return None
        if ts is None:
            ts = time.time()
        msg = {"role": role, "content": text, "ts": float(ts)}
        if image_path:
            msg["image"] = str(image_path)
        if voice_path:
            msg["voice"] = str(voice_path)
            msg["voice_sec"] = float(voice_sec or 0.0)
            if voice_text:
                msg["voice_text"] = str(voice_text)
        self.messages.append(msg)
        if notice:
            self._add_bubble("note", notice)
        self._add_bubble(role, text, image_path=image_path,
                         voice_path=voice_path, voice_sec=voice_sec, ts=ts,
                         voice_text=voice_text)
        self.save_history()
        self._update_empty_state()
        return msg

    def render_history(self):
        self._render_current_session()

    def _clear_bubbles(self):
        # 重建气泡前，先放弃对「正在翻唱」状态气泡的引用：
        # 它马上会被 deleteLater 销毁，留着就是悬空引用，
        # 后续 _close_cover_status/_update_cover_status 操作它会静默失效，
        # 导致切回会话时残留一个永不消失的「正在翻唱」气泡。
        self._cover_status_bubble = None
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

    def _empty_greetings(self):
        """空聊天问候语：按人设缓存 → 中性池 → 内置。"""
        try:
            from petcore import persona_lines as PL
            from petcore.consts import persona_from_cfg
            lines = PL.get_pool(
                "CHAT_EMPTY_GREETINGS",
                persona_from_cfg(self.pet.cfg), APP_DIR)
            if lines:
                return lines
        except Exception:
            pass
        return list(CHAT_EMPTY_GREETINGS)

    def _render_current_session(self):
        """只渲染当前会话；消息多时先渲染最近 50 条，上方给「加载更早消息」"""
        self._hide_typing()
        self.current_bubble = None
        self.current_text = ""
        self._clear_bubbles()
        self._last_msg_ts = None
        total = len(self.messages)
        self._render_from = max(0, total - max(1, self._render_limit))
        if self._render_from > 0:
            self.msg_layout.insertWidget(0, self.load_more_btn)
        self._repair_voice_msgs()
        # 找回 cover_cache 里未被引用的翻唱成品（旧版本/异常导致的丢失）
        try:
            self._relink_cover_songs()
        except Exception:
            pass
        # 渲染时：跳过「[主动搭话]」占位用户消息（历史里保留，用于 API 角色交替），
        # 紧随其后的宠物气泡顶部加一个小灰标签
        _pro_label = ""
        for m in self.messages[self._render_from:]:
            _role = m.get("role", "assistant")
            _content = m.get("content", "")
            if _role == "user" and _is_proactive_placeholder(_content):
                _pro_label = _proactive_label_text(_content)
                continue        # 不渲染占位气泡
            _b = self.render_message(_role, _content,
                                     image_path=m.get("image"),
                                     voice_path=m.get("voice"),
                                     voice_sec=m.get("voice_sec") or 0.0,
                                     voice_text=m.get("voice_text") or "",
                                     ts=m.get("ts"),
                                     attachments=m.get("attachments"))
            if _pro_label and _role == "assistant" and _b is not None:
                try:
                    _b.set_head_tag(_pro_label)
                except Exception:
                    pass
                _pro_label = ""
        # 若翻唱任务仍在跑、且当前渲染的正是发起会话，补回状态气泡
        self._restore_cover_status()
        self._update_empty_state()
        self._scroll_bottom()
        # 渲染新会话时同步刷新删除按钮可用性（空对话不可删）
        try:
            self._update_busy_ui()
        except Exception:
            pass

    def _relink_cover_songs(self):
        """把 cover_cache 里未被引用的翻唱成品挂回聊天历史。

        场景：翻唱在旧版本生成时 voice 引用没写进历史，或者中途异常。
        按「🎵 歌名」标题就近匹配；完全找不到宿主的跳过（避免乱挂）。
        每次打开聊天窗口时执行一次。
        """
        try:
            import os as _os
            base = _os.path.join(_os.path.dirname(
                _os.path.abspath(__file__)), _os.pardir, "cover_cache")
            base = _os.path.normpath(base)
            if not _os.path.isdir(base):
                return 0
            # 收集所有已引用的 voice 路径
            used = set()
            for s in (self.pet.cfg.get("_chat_sessions_all") or []):
                pass
            used = set()
            for sess in self._all_sessions():
                for msg in (sess.get("messages") or []):
                    v = msg.get("voice")
                    if v:
                        used.add(_os.path.normcase(_os.path.normpath(v)))
            # 扫描成品
            products = []
            for root, _dirs, files in _os.walk(base):
                for f in files:
                    if f.endswith("_翻唱.wav"):
                        fp = _os.path.join(root, f)
                        if _os.path.normcase(fp) not in used:
                            products.append(fp)
            if not products:
                return 0
            # 按修改时间排序（旧→新）
            products.sort(key=lambda x: _os.path.getmtime(x))
            relinked = 0
            for fp in products:
                fname = _os.path.basename(fp)
                # 从文件名提取歌名：XXX_YYYYMMDD_HHMMSS_翻唱.wav 或 XXX_翻唱.wav
                stem = fname[:-len("_翻唱.wav")]
                song = stem.split("_")[0] if "_" in stem else stem
                # 剥掉查询残留符号（如「·指纹」的中点）
                song = song.lstrip("·•・. ")
                if not song:
                    continue
                # 找含「🎵 歌名」的 assistant 消息
                host = None
                for sess in self._all_sessions():
                    for msg in (sess.get("messages") or []):
                        if (msg.get("role") == "assistant"
                                and not msg.get("voice")
                                and "🎵" in (msg.get("content") or "")
                                and song in (msg.get("content") or "")):
                            host = (sess, msg)
                            break
                    if host:
                        break
                if host is None:
                    continue
                sess, msg = host
                try:
                    import wave as _w
                    with _w.open(fp, "rb") as w:
                        sec = w.getnframes() / float(w.getframerate() or 1)
                except Exception:
                    sec = 1.0
                msg["voice"] = fp
                msg["voice_sec"] = max(1.0, sec)
                relinked += 1
                log("[翻唱] 找回未引用的成品: %s -> %r" % (fname, song))
            if relinked:
                self.save_history()
                # 重新渲染当前会话，让语音条出现
                self._render_current_session()
                log("[翻唱] 共找回 %d 条语音" % relinked)
            return relinked
        except Exception as ex:
            log("[翻唱] 找回失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)
            return 0

    def _all_sessions(self):
        """所有会话（当前 + 存档）。"""
        try:
            return self.sessions or []
        except Exception:
            return []

    def _repair_voice_msgs(self):
        """修复历史里缺 voice 字段的唱歌消息（老版本写入的）。

        早期版本只写了「🎵 唱给你听」文字，没存语音文件路径，
        导致重开聊天看不到语音条。这里按时间就近匹配 voice_cache
        里的文件补上，让老消息也能点播放。
        """
        try:
            import glob
            cache_dir = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "voice_cache")
            if not os.path.isdir(cache_dir):
                return
            files = sorted(glob.glob(os.path.join(cache_dir, "*.wav")))
            if not files:
                return
            changed = False
            used = set()
            for m in self.messages:
                if m.get("voice"):
                    continue
                if "唱给你听" not in (m.get("content") or ""):
                    continue
                ts = m.get("ts")
                best, best_d = None, None
                for f in files:
                    if f in used:
                        continue
                    if ts:
                        # 有 ts：按时间就近匹配（10 分钟内）
                        base = os.path.basename(f).replace(".wav", "")
                        try:
                            import datetime
                            ft = datetime.datetime.strptime(
                                base, "%Y%m%d_%H%M%S").timestamp()
                        except Exception:
                            continue
                        d = abs(ft - float(ts))
                        if d > 600:
                            continue
                        if best_d is None or d < best_d:
                            best, best_d = f, d
                    else:
                        # 没有 ts（老数据）：按顺序领一个没用过的
                        best = f
                        break
                if best:
                    m["voice"] = best
                    m["voice_sec"] = max(1.0, _wav_duration(best))
                    used.add(best)
                    changed = True
                    log("[聊天] 已为历史语音消息补上文件：%s" % best)
            if changed:
                self.save_history()
        except Exception as ex:
            log("[聊天] 修复历史语音消息失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

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
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_load_earlier", type(ex).__name__, ex),
                logging.WARNING)

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
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "shutdown", type(ex).__name__, ex),
                logging.WARNING)
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
        # 输入框聚焦/失焦 → 输入卡片边框高亮
        if obj is self.input and et in (QEvent.Type.FocusIn,
                                        QEvent.Type.FocusOut):
            try:
                focused = (et == QEvent.Type.FocusIn)
                self.input_card.setProperty("focused",
                                            "true" if focused else "false")
                self.input_card.style().unpolish(self.input_card)
                self.input_card.style().polish(self.input_card)
            except Exception:
                pass
        if obj is self.input and et == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if mods & Qt.KeyboardModifier.ShiftModifier:
                    return False  # Shift+Enter 换行，交给 QTextEdit
                self._on_send_clicked()
                return True
            # Ctrl+V：剪贴板里有图片就先加附件，不把图片当文本粘贴
            if key == Qt.Key.Key_V and (mods & Qt.KeyboardModifier.ControlModifier):
                if self._paste_image_from_clipboard():
                    return True
        return super().eventFilter(obj, event)

    # ---------- 发送 / 停止 ----------
    def _run_vision_tool_async(self, intent, target=""):
        """后台执行视觉工具，结果写回当前会话。"""
        import threading
        self.busy = True
        self._update_busy_ui()
        self._add_notice("🔍 处理中…")

        def work():
            try:
                self._vision_colors = None
                if intent == "colors":
                    # 主色：走卡片渲染，这里只取结构化数据
                    try:
                        from petcore import vision_tools as VT
                        pil = self.pet._vision_tool_shot()
                        cols = VT.dominant_colors(pil, top=8) if pil else []
                        if cols:
                            self._vision_colors = cols
                            okk, msg, path = True, "", ""
                        else:
                            okk, msg, path = False, "没看出什么颜色。", ""
                    except Exception as ex:
                        okk, msg, path = False, "出错了：%s" % str(ex)[:60], ""
                else:
                    okk, msg, path = self.pet.run_vision_tool(intent, target)
            except Exception as ex:
                okk, msg, path = False, "出错了：%s" % str(ex)[:60], ""
            try:
                self._vision_tool_done.emit(bool(okk), str(msg or ""),
                                            str(path or ""))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _on_vision_tool_done(self, okk, msg, path):
        """主线程：把视觉工具结果写进聊天。"""
        try:
            self._finish_busy()
            if okk and msg:
                # 主色提取：开场白 + 色块卡片
                colors = getattr(self, "_vision_colors", None)
                if colors:
                    self._vision_colors = None
                    self._append_color_card(colors)
                    return
                self.append_external("assistant", msg,
                                     image_path=(path or None))
                # 桌宠也说一句
                try:
                    self.pet.say_main(self._first_sentence(msg))
                except Exception:
                    pass
            else:
                self.append_external("assistant", msg or "没做成……")
        except Exception as ex:
            log("[视觉工具] 结果写回失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            self._finish_busy()

    def _tool_opener_line(self):
        """工具结果卡片上方的开场白：人设缓存 → 中性池 → 内置。"""
        try:
            from petcore import persona_lines as PL
            from petcore.consts import persona_from_cfg
            lines = PL.get_pool("TOOL_RESULT_OPENERS",
                                persona_from_cfg(self.pet.cfg), APP_DIR)
            if lines:
                return self.pet._pick_line(lines, "tool_opener",
                                           pool_name="TOOL_RESULT_OPENERS")
        except Exception:
            pass
        return "我看了一下，主要是这几种颜色。"

    def _append_color_card(self, colors):
        """写入「开场白 + 主色卡片」到当前会话。"""
        sess = self._current_session()
        if sess is None:
            return
        opener = self._tool_opener_line()
        # 开场白作为 assistant 消息落盘（卡片本身不落盘，重开时用文字版）
        text_lines = [opener, "", "页面主色："]
        for c in (colors or [])[:8]:
            text_lines.append("  %s  占 %.0f%%"
                              % (c.get("hex", "").upper(),
                                 float(c.get("ratio") or 0) * 100))
        plain = chr(10).join(text_lines)

        self.messages.append({"role": "assistant", "content": plain,
                              "ts": time.time()})
        sess["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save_history()

        # 界面：开场白气泡 + 卡片
        self.render_message("assistant", opener, ts=time.time())
        wrap = QWidget()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        h.addSpacing(CHAT_AVATAR_SIZE + 8)
        card_box = QFrame()
        card_box.setObjectName("Bubble")
        cv = QVBoxLayout(card_box)
        cv.setContentsMargins(*CHAT_BUBBLE_PAD)
        cv.setSpacing(0)
        cv.addWidget(ColorCard(colors))
        h.addWidget(card_box)
        h.addStretch(1)
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, wrap)
        self._scroll_bottom()
        try:
            self.pet.say_main(opener)
        except Exception:
            pass

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

    def _send_with_attachments(self, text, atts):
        """带附件的发送流程（B2）。

        1) 有图片且命中本地视觉工具 → 本地处理，不调模型
        2) 有图片未命中 → 视觉模型描述 → 再交聊天模型
        3) 文本文件 → 拼进用户消息，只调聊天模型
        """
        from petcore import chat_attach as CA
        imgs = [a for a in atts if a.get("kind") == "image"]
        texts = [a for a in atts if a.get("kind") == "text"]

        # ---- 1) 图片 + 本地工具命中 ----
        if imgs:
            try:
                from petcore import vision_intent as _VI
                _vi, _vt = _VI.detect_intent(text)
            except Exception:
                _vi, _vt = "", ""
            if _vi in ("colors", "diff", "long_ocr", "foreground"):
                self._run_vision_tool_on_image(_vi, _vt, imgs[0]["src"], text)
                return

        # ---- 组装用户消息文本 ----
        msg_text = text
        for t in texts:
            msg_text = (msg_text + "\n\n" if msg_text else "") + \
                CA.format_text_attachment(t)

        # ---- 3) 只有文本文件：直接走聊天模型 ----
        if not imgs:
            self.messages.append({"role": "user", "content": msg_text,
                                  "ts": time.time()})
            self._maybe_auto_title(msg_text)
            self._turn_user_bubble = self.render_message(
                "user", msg_text, attachments=texts)
            self._turn_user_text = msg_text
            self.save_history()
            self._start_plain_chat(msg_text)
            return

        # ---- 2) 有图片：先视觉描述 ----
        ok, base, vkey, vmodel = self._vision_ready()
        if not ok:
            self._add_notice("先在「聊天」页配置视觉模型")
            self._finish_busy()
            return

        self.busy = True
        self._update_busy_ui()
        self.pet.chat_paused = True
        self._set_usage_label("正在看图…")
        self._show_typing()

        import threading
        sess = self._current_session()
        sid = sess.get("id") if sess else "default"
        ts = time.time()

        def work():
            descs = []
            err = ""
            try:
                from petcore import chat_attach as CA2
                from petcore.consts import LOOK_SCREEN_QUALITY_DEFAULT
                _ls = self.pet.cfg.get("look_screen") or {}
                side = int(_ls.get("max_side") or 1024)
                qual = int(_ls.get("quality") or LOOK_SCREEN_QUALITY_DEFAULT)
                stored = []
                for i, a in enumerate(imgs[:CA2.MAX_IMAGES]):
                    att = CA2.store_image(APP_DIR, sid, a["src"], ts, i,
                                          max_side=side, quality=qual)
                    stored.append(att)
                    d, e = self._vision_describe(att["path"])
                    if e:
                        err = e
                        break
                    descs.append(d)
                if not err:
                    self._vision_attach_result = {
                        "stored": stored, "descs": descs,
                        "text": text, "texts": texts, "ts": ts}
                else:
                    self._vision_attach_result = {"error": err}
            except Exception as ex:
                self._vision_attach_result = {
                    "error": "出错了：%s" % str(ex)[:60]}
            try:
                self._vision_attach_done.emit()
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _vision_ready(self):
        """视觉模型是否可用。返回 (ok, base, key, model)。"""
        try:
            ls = self.pet.cfg.get("look_screen") or {}
            base = (ls.get("vision_base") or "").strip()
            key = (ls.get("vision_key") or "").strip()
            model = (ls.get("vision_model") or "").strip()
            return bool(base and key and model), base, key, model
        except Exception:
            return False, "", "", ""

    def _vision_describe(self, img_path):
        """调视觉模型描述一张图。返回 (描述, 错误码)。"""
        ok, base, key, model = self._vision_ready()
        if not ok:
            return "", "nokey"
        try:
            import base64
            import requests
            from petcore.consts import (VISION_STYLE_DEFAULT,
                                        vision_style_suffix,
                                        LOOK_SCREEN_TIMEOUT_DEFAULT)
            _ls = self.pet.cfg.get("look_screen") or {}
            style = _ls.get("style") or VISION_STYLE_DEFAULT
            suffix = vision_style_suffix(style)
            sys_txt = (_ls.get("vision_system") or "").strip()
            if not sys_txt:
                from petcore.consts import VISION_SYSTEM_DEFAULT
                sys_txt = VISION_SYSTEM_DEFAULT
            if suffix:
                sys_txt = sys_txt + "\n\n" + suffix
            with open(img_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_txt},
                    {"role": "user", "content": [
                        {"type": "text", "text": "描述这张图片。"},
                        {"type": "image_url",
                         "image_url": {"url": "data:image/jpeg;base64," + b64}},
                    ]},
                ],
                "max_tokens": int(_ls.get("max_tokens") or 800),
                "temperature": 0.2,
            }
            timeout = int(_ls.get("timeout") or LOOK_SCREEN_TIMEOUT_DEFAULT)
            r = requests.post(
                (base or "").rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + key,
                         "Content-Type": "application/json"},
                json=payload, timeout=timeout)
            if r.status_code in (401, 402, 403):
                return "", "auth_rejected"
            if r.status_code != 200:
                return "", "failed"
            txt = ((r.json().get("choices") or [{}])[0]
                   .get("message", {}).get("content") or "").strip()
            if not txt:
                return "", "failed"
            return txt, ""
        except Exception as ex:
            nm = type(ex).__name__.lower()
            if "timeout" in nm:
                return "", "timeout"
            log("[附件] 视觉描述失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)
            return "", "failed"

    def _on_vision_attach_done(self):
        """视觉描述完成（主线程）：拼进用户消息后走聊天模型。"""
        from petcore import chat_attach as CA
        res = getattr(self, "_vision_attach_result", None) or {}
        self._vision_attach_result = None
        self._hide_typing()

        err = res.get("error")
        if err:
            self._finish_busy()
            self._set_usage_label("")
            if err == "nokey":
                self._add_notice("先在「聊天」页配置视觉模型")
            elif err == "timeout":
                self._add_notice("看图超时了，稍后再试")
            elif err == "auth_rejected":
                self._add_notice("视觉模型服务拒绝请求，请检查 Key 或余额")
            else:
                self._add_notice("看图失败了，稍后再试")
            return

        stored = res.get("stored") or []
        descs = res.get("descs") or []
        text = res.get("text") or ""
        texts = res.get("texts") or []
        ts = res.get("ts") or time.time()

        # 用户消息：原文 + 图片描述占位（历史里不重发图）
        hist_parts = [text] if text else []
        for d in descs:
            hist_parts.append(CA.image_history_text(d))
        for t in texts:
            hist_parts.append(CA.format_text_attachment(t))
        hist_text = "\n\n".join(hist_parts)

        self.messages.append({
            "role": "user", "content": hist_text, "ts": ts,
            "attachments": [
                {"kind": "image", "thumb": a.get("thumb"),
                 "path": a.get("path"), "name": a.get("name"),
                 "size": a.get("size"), "desc": d}
                for a, d in zip(stored, descs)
            ] + list(texts),
        })
        self._maybe_auto_title(text or hist_text)
        # 气泡：显示缩略图 + 用户原文（描述不进气泡）
        self._turn_user_bubble = self.render_message(
            "user", text, attachments=[
                {"kind": "image", "thumb": a.get("thumb"),
                 "path": a.get("path"), "name": a.get("name")}
                for a in stored] + list(texts))
        self._turn_user_text = text
        self.save_history()

        # 交给聊天模型（历史里是描述文字，不重发图片）
        self._start_plain_chat(hist_text, display_text=text)

    def _start_plain_chat(self, send_text, display_text=None):
        """走普通聊天流程（内部复用原有流式逻辑）。"""
        self._mem_after_user_message(display_text or send_text)
        try:
            if getattr(self.pet, "_care", None) is not None:
                self.pet._care.note_interaction()
        except Exception:
            pass
        self.busy = True
        self._stop_seen = False
        self._update_busy_ui()
        self.pet.chat_paused = True
        self.current_text = ""
        self.current_bubble = None
        self._set_usage_label("本次　生成中…")
        self._show_typing()
        key = (self.pet.cfg.get("ds_api_key") or "").strip()
        if not key:
            self._on_failed(
                "还没配置聊天 Key。去 设置 →「聊天」→「聊天模型」填一下就能对话了。")
            return
        if self._need_summary():
            self._summarize_then_chat()
        else:
            self._start_chat_stream()

    def _run_vision_tool_on_image(self, intent, target, img_path, text):
        """对附件图片跑本地视觉工具（不调模型）。"""
        import threading
        self.busy = True
        self._update_busy_ui()
        self._add_notice("🔍 处理中…")
        # 用户消息先落盘（带图）
        self.messages.append({"role": "user", "content": text,
                              "ts": time.time(),
                              "attachments": [{"kind": "image",
                                               "thumb": img_path,
                                               "path": img_path,
                                               "name": os.path.basename(img_path)}]})
        self._maybe_auto_title(text)
        self.render_message("user", text,
                            attachments=[{"kind": "image",
                                          "thumb": img_path,
                                          "path": img_path}])
        self.save_history()

        def work():
            try:
                from petcore import vision_tools as VT
                from PIL import Image
                pil = Image.open(img_path).convert("RGB")
                self._vision_colors = None
                if intent == "colors":
                    cols = VT.dominant_colors(pil, top=8)
                    if cols:
                        self._vision_colors = cols
                        okk, msg, path = True, "", ""
                    else:
                        okk, msg, path = False, "没看出什么颜色。", ""
                else:
                    okk, msg, path = False, "这个工具暂不支持附件图片。", ""
            except Exception as ex:
                okk, msg, path = False, "出错了：%s" % str(ex)[:60], ""
            try:
                self._vision_tool_done.emit(bool(okk), str(msg or ""),
                                            str(path or ""))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _on_send_clicked(self):
        if self.busy:
            self.stop_stream()
        else:
            self.send_message()

    def _update_send_button(self):
        """圆形按钮：空闲时上箭头，生成中变成停止方块。"""
        try:
            self.send_btn.set_busy(bool(self.busy))
            self.send_btn.setToolTip("停止生成" if self.busy else "发送")
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_update_send_button", type(ex).__name__, ex),
                logging.WARNING)

    def _usage_placeholder(self):
        return "输入 - · 输出 - · 缓存命中 -"

    # ---------- 附件：添加 / 移除 / 渲染缩略条 ----------
    def _show_attach_menu(self):
        """加号按钮：选择图片或文本文件。"""
        from PySide6.QtWidgets import QMenu
        try:
            m = QMenu(self)
            m.addAction("图片…",
                        lambda: self._pick_attachments(images_only=True))
            m.addAction("文本文件…",
                        lambda: self._pick_attachments(text_only=True))
            self._exec_menu_in_window(m, self.attach_btn)
        except Exception as ex:
            log("[附件] 打开菜单失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _pick_attachments(self, images_only=False, text_only=False):
        """选图片或文本文件。images_only / text_only 限定类型。"""
        from PySide6.QtWidgets import QFileDialog
        if images_only:
            flt = "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"
        elif text_only:
            flt = "文本文件 (*.txt *.md *.py *.json *.csv *.log)"
        else:
            flt = ("图片或文本 (*.png *.jpg *.jpeg *.webp *.bmp *.gif "
                   "*.txt *.md *.py *.json *.csv *.log);;所有文件 (*)")
        try:
            files, _ = QFileDialog.getOpenFileNames(
                self, "添加附件", "", flt)
        except Exception as ex:
            log("[附件] 打开文件对话框失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            return
        for p in (files or []):
            self._add_attachment_path(p)

    def _add_attachment_path(self, path):
        """加一个附件（图片或文本）。返回是否加入。"""
        from petcore import chat_attach as CA
        try:
            if not os.path.exists(path):
                return False
            if CA.is_image(path):
                imgs = [a for a in self._pending_atts if a.get("kind") == "image"]
                if len(imgs) >= CA.MAX_IMAGES:
                    self._add_notice("一次最多 4 张图片")
                    return False
                self._pending_atts.append({
                    "kind": "image", "src": path,
                    "name": os.path.basename(path),
                    "size": os.path.getsize(path),
                    "thumb": path,       # 预览先用原图，发送时再生成缩略图
                })
                self._refresh_attach_bar()
                return True
            if CA.is_text_file(path):
                att = CA.store_text(path)
                if att.get("error"):
                    self._add_notice("读不了这个文件：%s" % att["error"])
                    return False
                self._pending_atts.append(att)
                self._refresh_attach_bar()
                return True
            self._add_notice("暂不支持该文件类型")
            return False
        except Exception as ex:
            log("[附件] 添加失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)
            return False

    def _remove_attachment(self, idx):
        try:
            if 0 <= idx < len(self._pending_atts):
                self._pending_atts.pop(idx)
            self._refresh_attach_bar()
        except Exception:
            pass

    def _refresh_attach_bar(self):
        """重绘附件缩略条。"""
        try:
            from petcore import chat_attach as CA
            lay = self.attach_bar.layout()
            while lay.count():
                it = lay.takeAt(0)
                w = it.widget()
                if w is not None:
                    w.deleteLater()
            if not self._pending_atts:
                self.attach_bar.hide()
                return
            for i, a in enumerate(self._pending_atts):
                lay.addWidget(self._attach_chip(a, i), 0,
                              Qt.AlignmentFlag.AlignVCenter)
            lay.addStretch(1)
            self.attach_bar.show()
        except Exception as ex:
            log("[附件] 刷新缩略条失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _attach_chip(self, att, idx):
        """单个附件缩略图：56x56 方形卡片，右上角悬浮 × 删除。

        图片：显示真实缩略图，铺满卡片（KeepAspectRatioByExpanding + 裁剪）
        文件：居中显示 📄 图标
        ×  ：绝对定位在右上角，hover 变红，点击移除该附件
        """
        SIZE = 56
        box = QFrame()
        box.setFixedSize(SIZE, SIZE)
        box.setObjectName("AttachChip")
        box.setStyleSheet(
            "QFrame#AttachChip{background:#f2f5fb;"
            "border:1px solid #e2e7f2;border-radius:8px;}")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        if att.get("kind") == "image":
            lab = QLabel()
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setStyleSheet("background:transparent;border:none;")
            pm = QPixmap(att.get("thumb") or "")
            if not pm.isNull():
                # 铺满 + 居中裁剪
                pm2 = pm.scaled(
                    SIZE - 4, SIZE - 4,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation)
                x0 = max(0, (pm2.width() - (SIZE - 4)) // 2)
                y0 = max(0, (pm2.height() - (SIZE - 4)) // 2)
                pm2 = pm2.copy(x0, y0, SIZE - 4, SIZE - 4)
                lab.setPixmap(pm2)
            else:
                lab.setText("🖼")
                lab.setStyleSheet(
                    "background:transparent;border:none;font-size:20px;")
            lay.addWidget(lab)
        else:
            lab = QLabel("📄")
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setStyleSheet(
                "background:transparent;border:none;font-size:20px;")
            lay.addWidget(lab)

        # 文件名 tooltip
        tip = att.get("name") or ("图片" if att.get("kind") == "image" else "文件")
        box.setToolTip(tip)

        # 右上角删除按钮：绝对定位，压在缩略图上
        x = QPushButton("×", box)
        x.setFixedSize(16, 16)
        x.move(SIZE - 17, 1)
        x.setCursor(Qt.CursorShape.PointingHandCursor)
        x.setToolTip("移除")
        x.setStyleSheet(
            "QPushButton{background:rgba(40,48,72,0.62);color:#ffffff;"
            "border:none;border-radius:8px;font-size:12px;"
            "font-weight:bold;padding:0;}"
            "QPushButton:hover{background:#e05353;}")
        x.clicked.connect(
            lambda _=False, i=idx: self._remove_attachment(i))
        x.raise_()
        return box

    def dragEnterEvent(self, e):
        try:
            if e.mimeData().hasUrls():
                e.acceptProposedAction()
                return
        except Exception:
            pass
        super().dragEnterEvent(e)

    def dropEvent(self, e):
        """拖拽文件到聊天窗口。"""
        try:
            urls = e.mimeData().urls()
            for u in (urls or []):
                p = u.toLocalFile()
                if p:
                    self._add_attachment_path(p)
            e.acceptProposedAction()
        except Exception as ex:
            log("[附件] 拖拽处理失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _paste_image_from_clipboard(self):
        """Ctrl+V 时优先取剪贴板图片。返回是否取到。"""
        try:
            cb = QApplication.clipboard()
            md = cb.mimeData()
            if md is not None and md.hasImage():
                img = cb.image()
                if not img.isNull():
                    import tempfile
                    fd, tmp = tempfile.mkstemp(suffix=".png")
                    os.close(fd)
                    img.save(tmp, "PNG")
                    self._add_attachment_path(tmp)
                    return True
        except Exception as ex:
            log("[附件] 粘贴图片失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
        return False

    def _ensure_image_privacy_ok(self):
        """首次发图确认（只弹一次，勾选后记住）。"""
        try:
            if self.pet.cfg.get("chat_image_privacy_ok") is True:
                return True
        except Exception:
            pass
        try:
            from PySide6.QtWidgets import QMessageBox, QCheckBox
            box = QMessageBox(self)
            box.setWindowTitle("发送图片")
            box.setText("图片会发送给视觉模型服务商")
            box.setIcon(QMessageBox.Icon.Information)
            cb = QCheckBox("不再提示")
            box.setCheckBox(cb)
            box.setStandardButtons(QMessageBox.StandardButton.Ok
                                   | QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(QMessageBox.StandardButton.Ok)
            if box.exec() != QMessageBox.StandardButton.Ok:
                return False
            if cb.isChecked():
                self.pet.cfg["chat_image_privacy_ok"] = True
                self.pet.save_cfg()
            return True
        except Exception as ex:
            log("[附件] 隐私确认失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            return True

    def _set_usage_label(self, line):
        """用量行：没有数据时整行隐藏，有过带用量的回复后再显示。"""
        try:
            if not line:
                self.token_label.setText("")
                self.token_label.setVisible(False)
                return
            self.token_label.setText(line)
            self.token_label.setToolTip(self._usage_tooltip())
            self.token_label.setVisible(True)
        except Exception:
            pass

    def send_message(self):
        text = self.input.toPlainText().strip()
        atts = list(getattr(self, "_pending_atts", []) or [])
        # 有附件时允许只发附件、不写字
        if (not text and not atts) or self.busy or self._current_session() is None:
            return
        # 有图片：首次确认一次
        if any(a.get("kind") == "image" for a in atts):
            if not self._ensure_image_privacy_ok():
                return
        self.input.clear()
        self._pending_atts = []
        self._refresh_attach_bar()

        if atts:
            self._send_with_attachments(text, atts)
            return

        self.messages.append({"role": "user", "content": text, "ts": time.time()})
        self._maybe_auto_title(text)
        self._turn_user_bubble = self.render_message("user", text)
        self._turn_user_text = text
        self.save_history()
        # 自动记忆：重置空闲计时 + 累计条数检查（点歌类内部会跳过）
        self._mem_after_user_message(text)
        # 好感度：记一次互动（+1，每天最多 +10）
        try:
            if getattr(self.pet, "_care", None) is not None:
                self.pet._care.note_interaction()
        except Exception:
            pass

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

        # 视觉工具意图（找元素/列元素/主色/对比/长图OCR/抠图）
        # 本地关键词识别，命中才触发，不命中继续走普通聊天
        try:
            from petcore import vision_intent as _VI
            _vi, _vt = _VI.detect_intent(text)
        except Exception:
            _vi, _vt = "", ""
        if _vi:
            self._run_vision_tool_async(_vi, _vt)
            return

        self.busy = True
        self._stop_seen = False
        self._update_busy_ui()
        self.pet.chat_paused = True
        self.current_text = ""
        self.current_bubble = None
        self._set_usage_label("本次　生成中…")
        self._show_typing()

        key = (self.pet.cfg.get("ds_api_key") or "").strip()
        if not key:
            self._on_failed(
                "还没配置聊天 Key。去 设置 →「聊天」→「聊天模型」填一下就能对话了。")
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
            self.messages.append({"role": "assistant", "content": text, "ts": time.time()})
            self._turn_user_bubble = None
            self._turn_user_text = ""
            self.save_history()
        else:
            self._remove_bubble(self.current_bubble)
            self.current_bubble = None
        self.current_text = ""
        self._set_usage_label("")
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
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_hide_typing", type(ex).__name__, ex),
                logging.WARNING)
        if self._typing_bubble is not None:
            self._remove_bubble(self._typing_bubble)
            self._typing_bubble = None

    # ---------- 请求（保持原逻辑，只使用当前会话数据） ----------
    def _estimate_tokens(self):
        _persona = persona_from_cfg(self.pet.cfg)
        total = len(build_system_prompt(_persona)) + len(self.summary)
        for m in self.messages[self.summarized_upto:]:
            total += len(m.get("content") or "")
        return int(total / 1.6)

    def _need_summary(self):
        return (self._estimate_tokens() > SUMMARIZE_AT_TOKENS or
                self._estimate_tokens() > CONTEXT_LIMIT_TOKENS * 0.7)

    def _build_request_messages(self):
        # 开头永远是同一段 system（人设 + 功能规则 + 长期记忆）。
        # 回复长短由人设决定：人设不变则 system 恒定 → 前缀缓存稳定。
        persona = persona_from_cfg(self.pet.cfg)
        sys_text = build_system_prompt(persona)
        # 唱歌场景：**联网搜真实歌词**，搜到就直接用（跳过模型）
        # 搜不到才回退到"让模型写歌词"
        self._sing_lyrics = None
        self._sing_title = ""
        self._sing_query = ""
        try:
            _last_user = ""
            for m in reversed(self.messages):
                if m.get("role") == "user":
                    _last_user = m.get("content") or ""
                    break
            import pet_voice as _V
            # 只有「唱歌」意图才搜歌词走翻唱；
            # 「用语言说X」「念一下X」是说话，走普通 TTS（模型正常回复）
            if _V.is_sing_request(_last_user) and _V.is_cover_request(_last_user):
                import lyrics as _LY
                # 从用户话里提取歌名（去掉"唱""给我听"等）
                _q = _V.strip_actions(_last_user)
                # 先剥长词组，再剥单字动词，避免「唱指纹」把"唱"留在 query 里
                for w in ("唱一首", "唱个", "唱一段", "唱首", "唱一曲",
                          "来一首", "来首", "来一曲",
                          "哼一首", "哼一段", "哼个", "哼一句", "哼一下",
                          "唱唱歌", "唱歌", "唱唱", "清唱", "翻唱",
                          "给我听", "给我", "放一首", "放首",
                          "念一下", "念一段", "放一下", "看一下", "一下",
                          "吧", "好吗", "好不好", "可以吗", "行吗",
                          "一首", "一曲", "一句",
                          "唱", "哼", "听", "来"):
                    _q = _q.replace(w, " ")
                _q = " ".join(_q.split()).strip()
                if _q:
                    self._sing_query = _q
                    _text, _name, _artist = _LY.build_sing_text(_q)
                    if _text:
                        self._sing_lyrics = _text
                        self._sing_title = ("%s - %s" % (_name, _artist)
                                            if _artist else _name)
                        log("[歌词] 搜到《%s》%d 行，跳过模型"
                            % (self._sing_title, len(_text.splitlines())))
                    else:
                        log("[歌词] 没搜到 %r 的歌词，回退让模型写" % _q)
                        sys_text = sys_text + build_sing_hint("")
                else:
                    sys_text = sys_text + build_sing_hint("")
        except Exception as ex:
            self._sing_lyrics = None
            log("[歌词] 搜索异常: %s" % ex, logging.WARNING)
        msgs = [{"role": "system",
                 "content": self.pet.memory.system_content(
                     sys_text, query=self._current_user_query())}]
        if self.summary:
            msgs.append({"role": "system",
                         "content": "以下是你和用户更早的聊天摘要（原始记录保留在本地，不再重复发送）：\n" + self.summary})
        # 当前会话的历史按原顺序追加，但受「上下文消息条数上限」约束
        try:
            limit = int(self.pet.cfg.get("history_msg_limit",
                                         HISTORY_MSG_LIMIT) or HISTORY_MSG_LIMIT)
        except Exception:
            limit = HISTORY_MSG_LIMIT
        limit = max(HISTORY_MSG_MIN, min(HISTORY_MSG_MAX, limit))
        hist = self.messages[self.summarized_upto:]
        if len(hist) > limit:
            # 只保留最近 limit 条；条数对齐到偶数，避免把一问一答拆开
            keep = limit if limit % 2 == 0 else limit - 1
            keep = max(HISTORY_MSG_MIN, keep)
            hist = hist[-keep:]
        for m in hist:
            msgs.append({"role": m["role"], "content": m["content"]})
        return msgs

    # ---------- 自动记忆（遗忘曲线 + 提取） ----------

    def notify_auto_memory_enabled(self):
        """开关打开时提示一次（只显示，不入会话历史）。"""
        try:
            self._add_bubble(
                "note",
                "自动记忆已开启，我会记住你说过的偏好，"
                "可在记忆里查看和删除。")
            log("[记忆] 自动记忆已开启（仅处理之后的新消息）")
        except Exception as ex:
            log("[记忆] 开启提示失败: %s: %s"
                % (type(ex).__name__, ex), logging.DEBUG)

    MEM_TRIGGER_COUNT = 8        # 累计多少条用户消息触发
    MEM_IDLE_MS = 10 * 60 * 1000  # 空闲多久触发（10 分钟）

    def _mem_enabled(self):
        """自动记忆总开关（features.auto_memory，默认关）。"""
        try:
            feats = self.pet.cfg.get("features") or {}
            return bool(feats.get("auto_memory"))
        except Exception:
            return False

    def _mem_is_reasoning_model(self):
        """推理型模型（带思考）跳过提取：输出里有推理内容，JSON 不可靠。"""
        try:
            _b, _k, model = self.pet._chat_cfg()
            m = (model or "").lower()
            for kw in ("reasoner", "-r1", "o1", "o3", "thinking",
                       "reasoning", "deepseek-r"):
                if kw in m:
                    return True
        except Exception:
            pass
        return False

    def _mem_reset_idle_timer(self):
        """每来一条用户消息就重置空闲计时器（单次触发）。"""
        try:
            from PySide6.QtCore import QTimer as _QT
            t = getattr(self, "_mem_idle_timer", None)
            if t is None:
                t = _QT(self)
                t.setSingleShot(True)
                t.timeout.connect(self._mem_on_idle)
                self._mem_idle_timer = t
            t.stop()
            t.start(self.MEM_IDLE_MS)
        except Exception as ex:
            log("[记忆] 启动空闲计时器失败: %s: %s"
                % (type(ex).__name__, ex), logging.DEBUG)

    def _mem_stop_idle_timer(self):
        try:
            t = getattr(self, "_mem_idle_timer", None)
            if t is not None:
                t.stop()
        except Exception:
            pass

    def _mem_on_idle(self):
        """空闲到点：有未处理的用户消息就提取一次。"""
        if not self._mem_enabled():
            return
        self._mem_maybe_extract(force=True)

    @staticmethod
    def _mem_is_sing_text(text):
        """点歌 / 翻唱类消息不计数、不送提取。"""
        try:
            import pet_voice as _V
            return bool(_V.is_sing_request(text or ""))
        except Exception:
            return False

    def _mem_pending_user_messages(self):
        """返回 (待处理片段, 新 upto)。

        只取 role=user、排除点歌/翻唱，且索引夹在合法范围内。
        """
        try:
            total = len(self.messages)
            upto = int(getattr(self, "_mem_extracted_upto", 0) or 0)
            upto = max(0, min(upto, total))
            out = []
            for m in self.messages[upto:]:
                if m.get("role") != "user":
                    continue
                c = (m.get("content") or "").strip()
                if not c:
                    continue
                if self._mem_is_sing_text(c):
                    continue
                out.append("user: " + c[:200])
            return out, total
        except Exception as ex:
            log("[记忆] 收集待处理消息失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            return [], len(self.messages)

    def _mem_maybe_extract(self, force=False):
        """检查是否需要提取。force=True 时忽略条数阈值（空闲触发）。"""
        if not self._mem_enabled():
            return
        if getattr(self, "_mem_worker", None) is not None:
            return                      # 上一轮还在跑
        if self._mem_is_reasoning_model():
            log("[记忆] 当前模型疑似推理型，跳过自动记忆提取")
            return
        lines, new_upto = self._mem_pending_user_messages()
        if not lines:
            return
        # 条数阈值（空闲触发时 force=True 直接通过）
        if not force and len(lines) < self.MEM_TRIGGER_COUNT:
            return
        try:
            _b, _k, _m = self.pet._chat_cfg()
            if not (_b and _k and _m):
                return
            existing = [dict(x) for x in self.pet.memory.sorted_items()]
            from petcore.memory_extract import MemoryExtractWorker
            sid = self.current_id
            w = MemoryExtractWorker(sid, new_upto, lines, existing,
                                    (_b, _k, _m))
            w.done.connect(self._mem_on_done)
            w.failed.connect(self._mem_on_failed)
            self._mem_worker = w
            self._mem_worker_sid = sid
            self._mem_worker_upto = new_upto
            log("[记忆] 开始提取：会话 %s，消息区间 [%s, %s)，待处理 %d 条"
                % (sid, getattr(self, "_mem_extracted_upto", 0), new_upto,
                   len(lines)))
            w.start()
        except Exception as ex:
            log("[记忆] 启动提取线程失败（会话 %s）: %s: %s"
                % (self.current_id, type(ex).__name__, ex), logging.ERROR)
            self._mem_worker = None

    def _mem_apply_ops(self, ops):
        """把提取结果写进全局记忆。返回 (新增数, 强化数)。"""
        added = 0
        updated = 0
        mem = self.pet.memory
        for op in ops or []:
            kind = op.get("op")
            text = (op.get("text") or "").strip()
            if kind == "ADD":
                if mem.add(text, source="auto",
                           origin_session=getattr(self, "_mem_worker_sid", None)):
                    added += 1
            elif kind == "UPDATE":
                try:
                    mid = int(op.get("id") or 0)
                except Exception:
                    continue
                if mem.reinforce(mid, text):
                    updated += 1
        return added, updated

    def _mem_on_done(self, session_id, upto, ops):
        """提取完成（主线程）：写记忆 + 推进索引。"""
        try:
            added, updated = self._mem_apply_ops(ops)
            # 只有真正处理成功才推进索引（写盘失败时保持原值，下轮重试）
            total = len(self.messages) if session_id == self.current_id else None
            if session_id == self.current_id:
                self._mem_extracted_upto = max(
                    0, min(int(upto), len(self.messages)))
                self.save_history()
            else:
                s = self._find_session(session_id)
                if s is not None:
                    s["mem_extracted_upto"] = max(
                        0, min(int(upto), len(s.get("messages") or [])))
                    self._write_sessions()
            log("[记忆] 提取完成：会话 %s，区间至 %s，新增 %d 条、强化 %d 条"
                % (session_id, upto, added, updated))
            try:
                self.pet.refresh_memory_dialog()
            except Exception:
                pass
        except Exception as ex:
            log("[记忆] 应用提取结果失败（会话 %s）: %s: %s"
                % (session_id, type(ex).__name__, ex), logging.ERROR)
        finally:
            self._mem_worker = None
            self._mem_worker_sid = None
            self._mem_worker_upto = 0

    def _mem_on_failed(self, session_id, upto, msg):
        """提取失败：只记日志，不推进索引，不影响聊天。"""
        log("[记忆] 提取失败：会话 %s，区间至 %s，原因 %s"
            % (session_id, upto, msg), logging.WARNING)
        self._mem_worker = None
        self._mem_worker_sid = None
        self._mem_worker_upto = 0

    def _mem_after_user_message(self, text):
        """每条用户消息后调用：重置空闲计时 + 累计条数检查。"""
        try:
            if not self._mem_enabled():
                return
            if self._mem_is_sing_text(text or ""):
                return                  # 点歌/翻唱不计数
            self._mem_reset_idle_timer()
            self._mem_pending = int(getattr(self, "_mem_pending", 0) or 0) + 1
            if self._mem_pending >= self.MEM_TRIGGER_COUNT:
                self._mem_pending = 0
                self._mem_maybe_extract(force=True)
        except Exception as ex:
            log("[记忆] 触发检查失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _summarize_then_chat(self):
        key = (self.pet.cfg.get("ds_api_key") or "").strip()
        cut = max(0, len(self.messages) - KEEP_RECENT_MESSAGES)
        old = self.messages[self.summarized_upto:cut]
        if cut <= self.summarized_upto:
            self._start_chat_stream()
            return
        self._set_usage_label("正在整理前情摘要…")
        _b2, _k3, _m2 = self.pet._chat_cfg()
        self.summary_worker = SummaryWorker(
            self.summary, old, key, base_url=_b2, model=_m2)
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
        # 搜到真实歌词：跳过模型，直接用歌词当回复
        if getattr(self, "_sing_lyrics", None):
            _lyr = self._sing_lyrics
            self._sing_lyrics = None
            log("[歌词] 用搜到的歌词直接回复（不调模型）")
            try:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, lambda: self._on_done(_lyr, None))
            except Exception:
                self._on_done(_lyr, None)
            return
        self._set_usage_label("本次　生成中…")
        _base, _key2, _model = self.pet._chat_cfg()
        self.worker = ChatWorker(
            messages, key,
            max_tokens=self.pet.cfg.get("max_tokens", MAX_TOKENS_DEFAULT),
            base_url=_base, model=_model,
            temperature=self.pet.cfg.get("temperature"),
            top_p=self.pet.cfg.get("top_p"),
            stream=bool(self.pet.cfg.get("stream_output", True)),
            reasoning_effort=self.pet.cfg.get("reasoning_effort") or None)
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
        self._scroll_bottom_if_follow()

    def _on_done(self, full, usage):
        _turn_user = self._turn_user_text or ""
        self.pet.set_work_state("success")
        if not self.busy:
            return
        self._hide_typing()
        full = (full or "").strip()
        if not full:
            full = "（这轮没有返回内容）"

        # 唱歌/念台词：语音没就绪前**不显示文字回复**，等语音条好了
        # 再一起显示（"先用语音回复，然后才是文字"）
        _is_sing = False
        try:
            import pet_voice as _V
            _is_sing = _V.is_sing_request(_turn_user)
        except Exception:
            pass
        if _is_sing:
            # 暂存文字，等语音条就绪后再补上
            self._pending_voice_text = full
            self._pending_voice_user = _turn_user
            self._pending_voice_usage = usage
            self._turn_user_bubble = None
            self._turn_user_text = ""
            # 流式期间 _on_chunk 已创建了文字气泡。记下它，
            # 成功路径里随语音条一起处理（语音条在前、文字在后），
            # 失败路径里复用它显示文字，避免出现两条相同回复。
            self._stream_bubble_for_sing = self.current_bubble
            self.current_bubble = None
            self._finish_busy()
            # 唱歌：走完整翻唱流水线（有旋律）
            _q = getattr(self, "_sing_query", "") or ""
            if _q:
                self._gen_voice_bubble(full, sing_query=_q)
            else:
                self._gen_voice_bubble(full)
            return

        if self.current_bubble is None:
            self.current_bubble = self._add_bubble("assistant", full)
        else:
            self.current_bubble.set_text(full)
        self.messages.append({"role": "assistant", "content": full, "ts": time.time()})
        self._turn_user_bubble = None
        self._turn_user_text = ""
        self.save_history()
        self._finish_busy()
        self._update_usage(usage, full)
        first = self._first_sentence(full)
        if first:
            self.pet.say(first)
        # 情绪反馈：按这轮对话内容播一个对应动作
        # （_mood_action 内部会停掉 work_hold_timer 并重置 work_state，
        #   所以能覆盖掉 work_ok）
        try:
            self.pet._mood_action(_turn_user, full)
        except Exception:
            pass

    def _gen_voice_bubble(self, text, sing_query=""):
        """后台生成语音。

        sing_query 非空 = 唱歌场景：走完整翻唱流水线
        （搜歌 -> 下载 -> UVR5 分离 -> RVC 换音色 -> 混音），
        产出真旋律的翻唱。否则用普通 TTS。
        """
        import threading
        self._hide_typing()
        # 无论翻唱还是普通念台词，都记录发起时的会话
        # （完成回调写回这个会话，期间用户可能切走）
        self._voice_session_id = self.current_id

        if sing_query:
            # 并发保护：上一首还没唱完就不再启动第二个任务
            # （否则两个翻唱并行跑，回调会互相覆盖状态、结果写错会话）
            if getattr(self, "_cover_session_id", None):
                log("[翻唱] 已有翻唱任务进行中（会话 %s），忽略本次请求《%s》"
                    % (self._cover_session_id, sing_query), logging.INFO)
                try:
                    self._say_cover_busy()
                except Exception as ex:
                    log("[翻唱] 忙碌提示写入失败: %s: %s"
                        % (type(ex).__name__, ex), logging.WARNING)
                return
            # 先在聊天流里放一条「正在翻唱」状态气泡（实时刷新已用秒数）
            try:
                self._cover_status_bubble = self._add_bubble(
                    "note", "🎵 正在翻唱《%s》…\n启动语音服务与下载伴奏中（约 5 分钟）"
                    % sing_query)
            except Exception:
                self._cover_status_bubble = None
            # 记住发起翻唱的会话：完成时写回这个会话（期间用户可能切走）
            self._cover_session_id = self.current_id
            self._cover_started = time.time()
            # 计时器：每 2 秒刷新已用时间，让用户确认没有卡死
            self._cover_timer = getattr(self, "_cover_timer", None)
            if self._cover_timer is None:
                from PySide6.QtCore import QTimer as _QT
                self._cover_timer = _QT(self)
                self._cover_timer.timeout.connect(self._on_cover_tick)
            self._cover_timer.start(2000)

            def work_cover():
                import cover as _CV
                def prog(msg):
                    try:
                        self._cover_progress.emit(msg)
                    except Exception:
                        pass
                try:
                    path, name, artist, err = _CV.make_cover(
                        sing_query, progress=prog)
                except Exception as ex:
                    path, name, artist, err = "", "", "", "%s: %s" % (
                        type(ex).__name__, ex)
                self._cover_ready.emit(path or "", name or "",
                                       artist or "", err or "")
            threading.Thread(target=work_cover, daemon=True).start()
            return

        def work():
            path, sec, err = "", 0.0, ""
            try:
                path, sec, err = self.pet.make_voice_file(text)
            except Exception as ex:
                err = "%s: %s" % (type(ex).__name__, ex)
            self._voice_ready.emit(path or "", float(sec or 0.0),
                                   err or "", text)

        threading.Thread(target=work, daemon=True).start()

    def _on_cover_progress(self, msg):
        """翻唱进度（主线程）：更新状态气泡 + 输入框上方提示。"""
        try:
            self._set_usage_label(msg)
        except Exception:
            pass
        self._update_cover_status(msg)

    def _restore_cover_status(self):
        """翻唱任务进行中、且当前会话是发起会话时，重建状态气泡。

        切换会话会销毁所有气泡（含状态气泡），切回时必须补回来，
        否则用户看不到「还在跑」的反馈。
        任务已完成（_cover_session_id 已清）时不重建，避免残留。
        """
        try:
            sid = getattr(self, "_cover_session_id", None)
            if not sid or sid != self.current_id:
                return
            # 已有有效气泡就不重复建
            b = getattr(self, "_cover_status_bubble", None)
            if b is not None:
                try:
                    import shiboken6 as _sb
                    if _sb.isValid(b):
                        return
                except Exception:
                    pass
            self._cover_status_bubble = self._add_bubble(
                "note", "🎵 正在翻唱…\n恢复显示中（已用 %d 秒）"
                % int(time.time() - getattr(self, "_cover_started",
                                            time.time())))
            log("[翻唱] 切回发起会话 %s，恢复状态气泡" % sid)
        except Exception as ex:
            log("[翻唱] 恢复状态气泡失败（会话 %s）: %s: %s"
                % (getattr(self, "_cover_session_id", None),
                   type(ex).__name__, ex), logging.WARNING)

    def _update_cover_status(self, msg=None):
        """刷新「正在翻唱」状态气泡的文字（含已用秒数）。"""
        b = getattr(self, "_cover_status_bubble", None)
        if b is None:
            return
        try:
            import time as _t
            sec = int(_t.time() - getattr(self, "_cover_started", _t.time()))
            stage = msg or "正在处理"
            mm, ss = divmod(sec, 60)
            b.set_text("🎵 正在翻唱…\n%s（已用 %d分%02d 秒，全程约 5 分钟）"
                       % (stage, mm, ss))
            self._scroll_bottom()
        except Exception:
            pass

    def _on_cover_tick(self):
        """每 2 秒刷新一次已用时间（没有新 stage 文案也刷新秒数）。"""
        if getattr(self, "_cover_status_bubble", None) is not None:
            self._update_cover_status()

    def _close_cover_status(self):
        """翻唱结束：移除状态气泡、停计时器、清任务标记。"""
        # 先清 _cover_session_id，标记任务已结束：
        # 这样之后任何 _render_current_session 都不会再重建状态气泡。
        self._cover_session_id = None
        try:
            t = getattr(self, "_cover_timer", None)
            if t is not None:
                t.stop()
        except Exception as ex:
            log("[翻唱] 停止状态计时器失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
        b = getattr(self, "_cover_status_bubble", None)
        self._cover_status_bubble = None
        if b is not None:
            try:
                import shiboken6 as _sb
                if _sb.isValid(b):
                    self._remove_bubble(b)
            except Exception as ex:
                # 对象已被销毁属正常（会话切换重渲染），不刷屏
                log("[翻唱] 移除状态气泡跳过: %s: %s"
                    % (type(ex).__name__, ex), logging.DEBUG)

    def _say_cover_busy(self):
        """翻唱进行中又来请求：回一句傲娇短话，不启动新任务。"""
        try:
            line = random.choice(COVER_BUSY_LINES)
            self.append_external("assistant", line)
            try:
                self.pet.say(self._first_sentence(line))
            except Exception as ex:
                log("[翻唱] 忙碌提示桌宠气泡失败: %s: %s"
                    % (type(ex).__name__, ex), logging.DEBUG)
        except Exception as ex:
            log("[翻唱] 忙碌提示失败（会话 %s）: %s: %s"
                % (self.current_id, type(ex).__name__, ex), logging.WARNING)

    def _say_cover_line(self, song="", fail=False):
        """翻唱结束后发一句本地人设短评（不调模型、不入语音链路）。"""
        try:
            pool = COVER_FAIL_LINES if fail else COVER_DONE_LINES
            line = random.choice(pool)
            if "{song}" in line:
                s = (song or "").strip()
                if s:
                    line = line.replace("{song}", s)
                else:
                    # 没有歌名时退化成不带书名号的句子
                    line = line.replace("《{song}》", "这首歌")
                    line = line.replace("{song}", "这首歌")
            self.append_external("assistant", line)
            # 桌宠气泡也顺带说一句
            try:
                self.pet.say(self._first_sentence(line))
            except Exception:
                pass
        except Exception as ex:
            log("[翻唱] 短评发送失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)

    def _on_cover_ready(self, path, name, artist, err):
        """翻唱完成（主线程）：写回发起翻唱的那个会话。

        翻唱约 5 分钟，期间用户可能切换 / 新建会话。
        完成时：先记下用户当前在哪，切回发起会话写入，
        再把界面切回用户所在的会话（如果用户已切走）。
        """
        # 先取发起会话 id，再关闭状态气泡：
        # _close_cover_status 会把 _cover_session_id 清成 None（标记任务结束），
        # 顺序反了就取不到发起会话，结果会写错地方。
        origin_sid = getattr(self, "_cover_session_id", None)
        user_sid = self.current_id          # 用户当前所在会话
        need_switch_back = bool(origin_sid) and origin_sid != user_sid

        self._close_cover_status()
        pending = getattr(self, "_pending_voice_text", "")
        self._pending_voice_text = ""
        self._pending_voice_usage = None
        try:
            self._set_usage_label("")
        except Exception:
            pass

        def _write_to_origin():
            """在发起会话里写入语音卡片 + 一句人设短评（不再发歌词）。"""
            if err or not path:
                log("[翻唱] 失败（会话 %s）：%s"
                    % (self.current_id, err or "空路径"), logging.WARNING)
                # 失败也只发一句短评，不再把错误详情/歌词甩到聊天里
                try:
                    self._say_cover_line(fail=True)
                except Exception as ex:
                    log("[翻唱] 失败短评写入失败（会话 %s）: %s: %s"
                        % (self.current_id, type(ex).__name__, ex),
                        logging.ERROR)
                return
            try:
                import wave as _wave
                sec = 0.0
                try:
                    with _wave.open(path, "rb") as w:
                        sec = w.getnframes() / float(w.getframerate() or 1)
                except Exception:
                    pass
                title = ("🎵 %s - %s" % (name, artist)) if artist else (
                    "🎵 %s" % (name or "翻唱"))
                # voice_text 仍传歌词：卡片「转文字」靠它工作
                self.append_external("assistant", title,
                                     voice_path=path, voice_sec=sec,
                                     voice_text=pending)
                log("[翻唱] 完成：%s（%.1f 秒）" % (title, sec))
                # 卡片之后补一句本地短评（不调模型）
                self._say_cover_line(song=(name or ""))
            except Exception as ex:
                log("[翻唱] 插入失败（会话 %s）: %s: %s"
                    % (self.current_id, type(ex).__name__, ex),
                    logging.ERROR)

        if need_switch_back:
            # 同步用户当前会话 → 切回发起会话写入 → 再切回用户会话
            try:
                self._sync_session(touch=False)
                if not self._activate_session(origin_sid):
                    # 发起会话已被删除：退化为写当前会话，别丢结果
                    log("[翻唱] 发起会话 %s 已不存在，写入当前会话 %s"
                        % (origin_sid, self.current_id), logging.WARNING)
                    _write_to_origin()
                else:
                    _write_to_origin()
                    self._sync_session(touch=False)
                    self._activate_session(user_sid)
                    self._render_current_session()
                    log("[翻唱] 已写回发起会话 %s（用户当前在 %s）"
                        % (origin_sid, user_sid))
            except Exception as ex:
                log("[翻唱] 写回发起会话 %s 失败: %s: %s"
                    % (origin_sid, type(ex).__name__, ex), logging.ERROR)
                # 尽力回到用户原会话，避免界面卡在错误状态
                try:
                    self._activate_session(user_sid)
                    self._render_current_session()
                except Exception:
                    pass
        else:
            _write_to_origin()

    def _on_voice_ready(self, path, sec, err, text):
        """语音文件就绪（主线程）：先插可点语音条，再补文字回复。

        唱歌场景的顺序要求：语音条先出现（点了才播，不自动播放），
        文字回复随后补上——语音没就绪前不显示文字。
        """
        pending = getattr(self, "_pending_voice_text", "")
        self._pending_voice_text = ""
        _usage = getattr(self, "_pending_voice_usage", None)
        self._pending_voice_usage = None
        # 写回发起语音的会话（期间用户可能切走）
        _origin = getattr(self, "_voice_session_id", None)
        _user_now = self.current_id
        _switched = False
        if _origin and _origin != _user_now and self._find_session(_origin):
            self._sync_session(touch=False)
            self._activate_session(_origin)
            _switched = True

        if err or not path:
            log("[语音] 语音条生成失败：%s" % (err or "空路径"))
            # 失败也要把文字回复补上，不能什么都不显示。
            # 复用流式气泡（已在屏上显示同一段文字），没有才新建，
            # 否则同一句回复会出现两条气泡。
            if pending:
                try:
                    reuse = getattr(self, "_stream_bubble_for_sing", None)
                    if reuse is not None:
                        try:
                            import shiboken6 as _sb
                            if not _sb.isValid(reuse):
                                reuse = None
                        except Exception:
                            reuse = None
                    if reuse is not None:
                        reuse.set_text(pending)
                    else:
                        self._add_bubble("assistant", pending)
                    self.messages.append({"role": "assistant",
                                          "content": pending,
                                          "ts": time.time()})
                    self.save_history()
                    first = self._first_sentence(pending)
                    if first:
                        self.pet.say(first)
                except Exception as ex:
                    log("[语音] 补文字失败: %s" % ex, logging.WARNING)
            self._stream_bubble_for_sing = None
            return
        try:
            # 1) 先插语音条（不自动播放，用户点了才播）
            self.append_external("assistant", "\U0001F3B5 唱给你听",
                                 voice_path=path, voice_sec=sec)
            log("[语音] 语音条已插入：%s（%.1f 秒）" % (path, sec))
            # 2) 再补文字回复（复用流式气泡，避免两条相同文字）
            if pending:
                reuse = getattr(self, "_stream_bubble_for_sing", None)
                if reuse is not None:
                    try:
                        import shiboken6 as _sb
                        if not _sb.isValid(reuse):
                            reuse = None
                    except Exception:
                        reuse = None
                if reuse is not None:
                    reuse.set_text(pending)
                else:
                    self._add_bubble("assistant", pending)
                self.messages.append({"role": "assistant",
                                      "content": pending,
                                      "ts": time.time()})
                self.save_history()
                first = self._first_sentence(pending)
                if first:
                    self.pet.say(first)
                if _usage:
                    self._update_usage(_usage, pending)
            self._stream_bubble_for_sing = None
        except Exception as ex:
            log("[语音] 插入语音条失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
        # 写完了：切回用户所在的会话
        if _switched:
            try:
                self._sync_session(touch=False)
                self._activate_session(_user_now)
                self._render_current_session()
            except Exception:
                pass

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
        self._set_usage_label("")
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

    @staticmethod
    def _fmt_tokens(n):
        """token 数 → 4.2K / 5.4M 这种缩写。"""
        try:
            n = int(n or 0)
        except Exception:
            return "0"
        if n >= 1_000_000:
            return "%.1fM" % (n / 1_000_000.0)
        if n >= 1_000:
            return "%.1fK" % (n / 1_000.0)
        return str(n)

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
            rate = ("%.1f%%" % (hit / denom * 100.0)) if denom else "-"
            if prompt or comp:
                # 参考图样式：本会话 ¥金额 · 命中 x% · 输入 · 缓存 · 输出
                # 余额（若已查询过）也并进来，避免侧栏与用量行两套账
                _bal = self._balance_short()
                _parts = ["本会话 ¥%.4f" % self._session_cost(),
                          "命中 %s" % rate,
                          "输入 %s" % self._fmt_tokens(prompt),
                          "缓存 %s" % self._fmt_tokens(hit),
                          "输出 %s" % self._fmt_tokens(comp)]
                if _bal:
                    _parts.append("余额 %s" % _bal)
                line = " · ".join(_parts)
            else:
                line = ""
        else:
            line = ""
        self._set_usage_label(line)

    def _session_cost(self):
        """本会话估算花费（元）。按 DeepSeek 定价粗略折算。"""
        try:
            u = self.pet.cfg.get("usage") or {}
            hit = int(u.get("prompt_cache_hit_tokens", 0) or 0)
            miss = int(u.get("prompt_cache_miss_tokens", 0) or 0)
            comp = int(u.get("completion_tokens", 0) or 0)
            # deepseek-chat: 命中 0.5 元/M，未命中 2 元/M，输出 8 元/M
            return (hit / 1e6 * 0.5 + miss / 1e6 * 2.0 + comp / 1e6 * 8.0)
        except Exception:
            return 0.0

    def _balance_short(self):
        """账户余额（上次查询结果），没有则返回空串。"""
        try:
            b = self.pet.cfg.get("balance") or {}
            v = b.get("last_value")
            if v is None:
                return ""
            return "≈¥%.2f" % float(v)
        except Exception:
            return ""

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
        # 清空后消息重新从 0 开始，自动记忆的已提取位置也要归零，
        # 否则索引会指向不存在的消息、或漏掉重新发的消息。
        self._mem_extracted_upto = 0
        s["mem_extracted_upto"] = 0
        self._mem_pending = 0
        self.current_bubble = None
        self._turn_user_bubble = None
        self._turn_user_text = ""
        self._hide_typing()
        self.save_history()
        self._render_current_session()
        self._set_usage_label("")
        self.input.clear()

class MemoryDialog(QDialog):
    """记忆管理：查看 / 添加 / 编辑 / 删除长期记忆（全局唯一，归属 PetApp）"""

    def __init__(self, app):
        super().__init__(app.main_pet)
        self.app = app
        self.setWindowTitle("记忆管理")
        # 恢复上次位置/尺寸
        restore_window_geometry(app.cfg, "win_memory", self, 480, 440,
                                min_w=380, min_h=320)
        lay = QVBoxLayout(self)
        tip = QLabel(
            "这些记忆会拼在系统提示词末尾；只有新增或修改记忆时，"
            "前缀缓存才会失效一次。\n"
            "标记 [自动] 的记忆由模型从对话中提取，会随时间淡化；"
            "[手动] 记忆永不淡化。\n"
            "标为 ★核心 的记忆每轮都会带上，不看相关性、不淡化。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#777;font-size:11px;")
        lay.addWidget(tip)
        self.list = QListWidget(self)
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        for label, fn in (("添加记忆", self.add_item), ("编辑", self.edit_item),
                          ("★ 核心", self.toggle_pin), ("删除", self.delete_item)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.hide)
        row.addWidget(close)
        lay.addLayout(row)
        self.reload()

    def closeEvent(self, e):
        """关闭时记住窗口位置/尺寸。"""
        self._remember_geometry()
        super().closeEvent(e)

    def hideEvent(self, e):
        """隐藏也记住（close() 在已隐藏时不触发 closeEvent）。"""
        self._remember_geometry()
        super().hideEvent(e)

    def _remember_geometry(self):
        try:
            save_window_geometry(self.app.cfg, "win_memory", self)
            self.app.save_cfg()
        except Exception:
            pass

    def reload(self):
        """刷新列表：显示 [手动]/[自动]/[★核心] 标记与「淡化中」状态。"""
        self.list.clear()
        mem = self.app.memory
        now = time.localtime()
        for m in mem.sorted_items():
            is_pinned = bool(m.get("pinned"))
            if is_pinned:
                src = "[★核心]"
            elif m.get("source") != "auto":
                src = "[手动]"
            else:
                src = "[自动]"
            text = m.get("text") or ""
            tag = ""
            if m.get("source") == "auto" and not is_pinned:
                try:
                    r = mem.retention(m, now)
                    if r < mem.DECAY_THRESHOLD:
                        tag = "    (淡化中 %d%%)" % int(round(r * 100))
                except Exception:
                    pass
            item = QListWidgetItem("#%s  %s  %s%s"
                                   % (m["id"], src, text, tag))
            if is_pinned:
                item.setForeground(QColor("#b8860b"))     # 核心：金色
            elif m.get("source") == "auto" and tag:
                item.setForeground(QColor("#999999"))
            elif m.get("source") == "auto":
                item.setForeground(QColor("#2f6fb0"))
            self.list.addItem(item)

    def _current(self):
        row = self.list.currentRow()
        items = self.app.memory.sorted_items()
        if row < 0 or row >= len(items):
            return None
        return items[row]

    def toggle_pin(self):
        """切换「核心记忆」标记。"""
        m = self._current()
        if not m:
            QMessageBox.information(self, "核心记忆", "先在列表里选中一条记忆")
            return
        new = not bool(m.get("pinned"))
        self.app.memory.set_pinned(m["id"], new)
        self.reload()
        # 保持选中同一行
        try:
            items = self.app.memory.sorted_items()
            for i, x in enumerate(items):
                if x.get("id") == m["id"]:
                    self.list.setCurrentRow(i)
                    break
        except Exception:
            pass

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
