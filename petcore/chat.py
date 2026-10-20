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
