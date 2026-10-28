# -*- coding: utf-8 -*-
"""大肥鱼桌宠 —— 统一设置窗口（10 个分页）。

从 桌宠.py 拆分（第 23 批重构）。依赖 petcore.consts，
通过 `self.app`（PetApp）访问应用级功能。
"""
import datetime
import json
import logging
import math
import os
import random
import re
import sys
import io
import time

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
                               QAbstractItemView, QFormLayout, QTabWidget)

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

class _VisionBus(QObject):
    """视觉识别 → UI 的跨线程信号总线。

    worker 线程只 emit；Qt 自动把这些调用排队到主线程执行，
    因此不依赖「该线程有自己的事件循环」（QTimer.singleShot 就会失效）。
    """
    base_found = Signal(str, str, object)      # base, provider, stale
    model_found = Signal(str, object)          # model, stale
    progress = Signal(int, object)             # 候选数, stale
    test_done = Signal(object, bool, str, str, list, object)   # stale,ok,err,base,found,total
    real_done = Signal(object, bool, str, str, int, int, object, str, object, object)
    # real_done: stale, ok, err, text, used, mt, finish, reason

    def __init__(self, win):
        super().__init__(win)
        self.win = win



class _ChatBus(QObject):
    """聊天 Key 识别 → UI 的跨线程信号总线（与 _VisionBus 同构）。"""
    base_found = Signal(str, str, object)      # base, provider, stale
    model_found = Signal(str, object)          # model, stale
    progress = Signal(int, object)             # 候选数, stale
    test_done = Signal(object, bool, str, str, list, object)  # stale,ok,err,base,found,total

    def __init__(self, win):
        super().__init__(win)
        self.win = win



class CollapsibleSection(QWidget):
    """可收缩区块：标题行（箭头 + 标题 + 状态小字）+ 内容区。

    行为：
      - 点击标题行切换展开/收起，箭头 ▾ / ▸
      - 收起只隐藏内容，不影响内部控件的读写
      - 状态小字由外部通过 set_state() 更新
      - 展开状态可持久化（由外部读写配置）
    """

    toggled = Signal(bool)   # True=展开

    def __init__(self, title, parent=None, expanded=True, state_key=None,
                 app=None):
        super().__init__(parent)
        # 状态持久化：给了 state_key 就自己读写 cfg["ui_sections"]
        self._state_key = state_key
        self._app = app
        if state_key and app is not None:
            try:
                saved = (app.cfg.get("ui_sections") or {}).get(state_key)
                if isinstance(saved, bool):
                    expanded = saved
            except Exception:
                pass
        self._expanded = bool(expanded)
        self._title_text = title
        self._state_text = ""
        self._state_color = "#8a8f9c"
        self._summary_text = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- 标题行（整行可点）----
        self._header = QPushButton()
        self._header.setCheckable(False)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setStyleSheet(
            "QPushButton{border:none;background:transparent;text-align:left;"
            "padding:6px 2px;}")
        self._header.clicked.connect(self.toggle)
        self._header.setToolTip("点击展开 / 收起")

        hrow = QHBoxLayout(self._header)
        hrow.setContentsMargins(2, 0, 8, 0)
        hrow.setSpacing(6)
        self._arrow = QLabel("▾")
        self._arrow.setStyleSheet("color:#5a6070;font-size:12px;")
        self._arrow.setFixedWidth(12)
        hrow.addWidget(self._arrow, 0, Qt.AlignmentFlag.AlignVCenter)
        self._title_lab = QLabel(title)
        f = QFont()
        f.setPointSize(11)
        f.setBold(True)
        self._title_lab.setFont(f)
        self._title_lab.setStyleSheet("color:#2f3542;")
        hrow.addWidget(self._title_lab, 0, Qt.AlignmentFlag.AlignVCenter)
        hrow.addStretch(1)
        self._state_lab = QLabel("")
        self._state_lab.setStyleSheet("color:#8a8f9c;font-size:11px;")
        hrow.addWidget(self._state_lab, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(self._header)

        # ---- 内容区（外部往里 addWidget）----
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(6)
        outer.addWidget(self.body)

        self._apply_visibility()

    # ---------- 对外 ----------
    def body_add(self, w):
        self.body_layout.addWidget(w)
        return w

    def body_add_layout(self, l):
        self.body_layout.addLayout(l)
        return l

    def is_expanded(self):
        return self._expanded

    def set_expanded(self, on, emit=True, remember=True):
        on = bool(on)
        if on == self._expanded:
            self._apply_visibility()
            return
        self._expanded = on
        self._apply_visibility()
        # 状态持久化：记住这次展开/收起
        if remember and self._state_key and self._app is not None:
            try:
                sec = self._app.cfg.get("ui_sections")
                if not isinstance(sec, dict):
                    sec = {}
                sec[self._state_key] = bool(on)
                self._app.cfg["ui_sections"] = sec
                self._app.save_cfg()
            except Exception:
                pass
        if emit:
            self.toggled.emit(on)

    def toggle(self):
        self.set_expanded(not self._expanded)

    def set_summary(self, text):
        """收起时标题行右侧显示的当前值摘要（展开时隐藏）。"""
        self._summary_text = text or ""
        self._refresh_header_tail()

    def _refresh_header_tail(self):
        """标题行右侧：展开显示 state，收起显示 summary（无 summary 则显示 state）。"""
        if not self._expanded and self._summary_text:
            self._state_lab.setText(self._summary_text)
        else:
            self._state_lab.setText(self._state_text)
        self._state_lab.setStyleSheet(
            "color:%s;font-size:11px;" % self._state_color)


    def set_state(self, text, color="#8a8f9c"):
        """标题行右侧状态小字（展开时显示）。"""
        self._state_text = text or ""
        self._state_color = color
        self._refresh_header_tail()


    def _apply_visibility(self):
        self._arrow.setText("▾" if self._expanded else "▸")
        self._refresh_header_tail()
        self.body.setVisible(self._expanded)


class ChatGroupBox(QWidget):
    """聊天页里的一个可折叠分组（外壳）。

    只做三件事：套一个 CollapsibleSection、暴露 body_layout、转发状态小字。
    具体内容由 SettingsWindow 既有的 build 代码填充 —— 不重写、不复制，
    这样原来的视觉页 / 记忆页控件可以原样搬进来。
    """

    def __init__(self, title, expanded=True, parent=None, state_key=None,
                 app=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.section = CollapsibleSection(title, expanded=expanded,
                                          state_key=state_key, app=app)
        outer.addWidget(self.section)

    @property
    def body_layout(self):
        """内容区布局（build 方法往里 addWidget / addLayout）。"""
        return self.section.body_layout

    def set_state(self, text, color="#8a8f9c"):
        self.section.set_state(text, color)

    def set_summary(self, text):
        self.section.set_summary(text)

    def is_expanded(self):
        return self.section.is_expanded()

    def set_expanded(self, on):
        self.section.set_expanded(on, emit=False)


class _ChatModelGroup(ChatGroupBox):
    """① 聊天模型：Key / 地址 / 模型 + 单次输出上限。默认展开。"""

    def __init__(self, parent=None, app=None):
        super().__init__("聊天模型", expanded=True, parent=parent, state_key="chat_model", app=app)


class _PersonaGroup(ChatGroupBox):
    """② 人设：风格的唯一来源。默认折叠，标题右侧显示「无风格」或人设摘要。"""

    def __init__(self, parent=None, app=None):
        super().__init__("人设", expanded=False, parent=parent, state_key="persona", app=app)


class _MemoryGroup(ChatGroupBox):
    """③ 长期记忆：说明 + 打开记忆管理。默认折叠，标题右侧显示「N 条」。"""

    def __init__(self, parent=None, app=None):
        super().__init__("长期记忆", expanded=False, parent=parent, state_key="sec_memory", app=app)


class _VisionGroup(ChatGroupBox):
    """④ 看屏幕 / 视觉：原「视觉」页全部控件原样搬入。默认折叠。

    标题右侧显示「模型名 已配置」或「未配置」。
    """

    def __init__(self, parent=None, app=None):
        super().__init__("看屏幕 / 视觉", expanded=False, parent=parent, state_key="sec_vision", app=app)


class _FieldDialog(QDialog):
    """带中文按钮的输入对话框基类。

    - 父窗口是设置窗口 → 居中于设置窗口，不再跑到屏幕角落
    - 模态，层级高于宠物窗口
    - 打开期间临时取消宠物置顶，关闭后恢复（否则宠物压住对话框按钮）
    - 按钮文字中文「确定 / 取消」
    - 校验失败用对话框内红色小字，不弹第二个窗口
    """

    def __init__(self, parent, title, label_text, value="", password=False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(360)
        self._restore_topmost = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(8)

        lab = QLabel(label_text)
        lab.setWordWrap(True)
        outer.addWidget(lab)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.edit = QLineEdit(value or "")
        self.edit.setFixedHeight(30)
        if password:
            self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.textChanged.connect(self._clear_error)
        row.addWidget(self.edit, 1)
        if password:
            self._eye = QPushButton("显示")
            self._eye.setFixedSize(52, 30)
            self._eye.setToolTip("显示 / 隐藏")
            self._eye.clicked.connect(self._toggle_echo)
            row.addWidget(self._eye)
        outer.addLayout(row)

        self._err = QLabel("")
        self._err.setWordWrap(True)
        self._err.setStyleSheet("color:#d93025;font-size:11px;")
        self._err.hide()
        outer.addWidget(self._err)

        brow = QHBoxLayout()
        brow.addStretch(1)
        self._ok = QPushButton("确定")
        self._ok.setFixedWidth(84)
        self._ok.setDefault(True)
        self._ok.clicked.connect(self._on_ok)
        brow.addWidget(self._ok)
        self._cancel = QPushButton("取消")
        self._cancel.setFixedWidth(84)
        self._cancel.clicked.connect(self.reject)
        brow.addWidget(self._cancel)
        outer.addLayout(brow)

    # ---- 宠物置顶临时取消 ----
    def showEvent(self, e):
        super().showEvent(e)
        try:
            for w in getattr(self.parent().app, "pets", []) if self.parent() else []:
                if w.isVisible() and (w.windowFlags() & Qt.WindowType.WindowStaysOnTopHint):
                    self._restore_topmost.append(w)
                    w.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
                    w.show()
        except Exception:
            pass
        self._center_on_parent()

    def closeEvent(self, e):
        self._restore_pets_topmost()
        super().closeEvent(e)

    def done(self, r):
        self._restore_pets_topmost()
        super().done(r)

    def _restore_pets_topmost(self):
        for w in self._restore_topmost:
            try:
                w.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
                w.show()
            except Exception:
                pass
        self._restore_topmost = []

    def _center_on_parent(self):
        try:
            p = self.parent()
            if p is None:
                return
            pg = p.frameGeometry()
            self.adjustSize()
            x = pg.center().x() - self.width() // 2
            y = pg.center().y() - self.height() // 2
            self.move(x, y)
        except Exception:
            pass

    def _toggle_echo(self):
        if self.edit.echoMode() == QLineEdit.EchoMode.Password:
            self.edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self._eye.setText("隐藏")
        else:
            self.edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._eye.setText("显示")

    def _clear_error(self):
        if self._err.isVisible():
            self._err.hide()

    def set_error(self, msg):
        self._err.setText(msg)
        self._err.show()

    def _on_ok(self):
        self.accept()


class _CityDialog(_FieldDialog):
    """设置城市。"""

    def __init__(self, parent, value=""):
        super().__init__(parent, "设置城市",
                         "天气查询用的城市名（如 重庆 / 汕头）：", value)

    def _on_ok(self):
        if not (self.edit.text() or "").strip():
            self.set_error("城市名不能为空")
            return
        self.accept()


class SettingsWindow(QDialog):
    """统一设置窗口：左侧分页，右侧内容。所有入口都从这里进（右键「设置…」/ 托盘）。"""

    # 主动搭话测试结果回主线程（worker 线程只 emit）
    _pro_test_done = Signal(object, str, str, float, str, bool, bool)
    _voice_test_done = Signal(object, str, float)   # 测试语音：wav, err, 耗时
    _update_done = Signal(str, str, str)            # 检查更新：tag, page, err

    PAGES = [
        ("features", "功能开关"),
        ("pet", "宠物"),
        ("motion", "物理·动画"),
        ("reminder", "提醒"),
        ("chat", "聊天"),
        ("proactive", "主动搭话"),
        ("voice", "语音"),
        ("general", "通用"),
    ]

    # 人设台词生成完成: 成功, 消息, 池数量
    _persona_gen_result = Signal(object)   # 台词池生成结果（dict）

    def __init__(self, app, page=None):
        super().__init__(app.main_pet)
        self.app = app
        self.pet = app.main_pet
        self.setWindowTitle("大肥鱼桌宠 · 设置")
        self.setMinimumSize(760, 560)
        # 恢复上次的位置/尺寸（多屏安全，不合法则居中 820x600）
        restore_window_geometry(app.cfg, "win_settings", self, 820, 600,
                                min_w=760, min_h=560)
        # 说明图标样式：挂窗口级，所有页面的 i 按钮都生效
        self.setStyleSheet(
            "QPushButton#DialogInfoBtn {"
            "  background: #e3e8f0; color: #5a6376;"
            "  border: 1px solid #d3dae6; border-radius: 9px;"
            "  font-size: 12px; font-weight: 700; padding: 0 0 1px 0;"
            "  font-family: 'Segoe UI', 'Microsoft YaHei UI';"
            "}"
            "QPushButton#DialogInfoBtn:hover {"
            "  background: #5686fe; color: #ffffff;"
            "  border-color: #5686fe;"
            "}"
            "QPushButton#DialogInfoBtn:pressed {"
            "  background: #3f6fe8; border-color: #3f6fe8;"
            "}")
        self._init_persona_widgets()
        self._build_ui()
        # 视觉识别的跨线程信号（替代在线程里用 QTimer.singleShot）
        self._vbus = _VisionBus(self)
        self._vbus.base_found.connect(self._on_vision_base_found)
        self._vbus.model_found.connect(self._on_vision_model_found)
        self._vbus.progress.connect(self._on_vision_progress)
        self._vbus.test_done.connect(self._on_vision_test_done)
        self._vbus.real_done.connect(self._vision_real_test_done)

        # 聊天 Key 识别的跨线程信号
        self._cbus = _ChatBus(self)
        self._cbus.base_found.connect(self._on_chat_base_found)
        self._cbus.model_found.connect(self._on_chat_model_found)
        self._cbus.progress.connect(self._on_chat_progress)
        self._cbus.test_done.connect(self._on_chat_test_done)
        if page:
            self._select_page(page)
        self.reload()
        # 宠物显示状态变化 → 只刷新宠物页
        try:
            self.app.pet_state_changed.connect(self._on_pet_state_changed)
        except Exception:
            pass

    def _remember_geometry(self):
        """记住窗口位置/尺寸（closeEvent / hideEvent 共用）。"""
        try:
            save_window_geometry(self.app.cfg, "win_settings", self)
            self.app.save_cfg()
        except Exception as ex:
            log("[窗口] 保存设置窗口位置失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _init_persona_widgets(self):
        """预先创建人设编辑框和状态标签。

        人设 UI 已从设置页移到聊天窗口的弹窗，但这些控件和逻辑
        仍留在本类里被复用，所以先建好但不加进任何布局。
        """
        self.persona_edit = QTextEdit()
        self.persona_edit.setFixedHeight(150)
        self.persona_edit.setPlaceholderText(
            "留空 = 无风格（台词用中性池）。风格和回复长短都由这里决定。\n"
            "例：你是猫娘，说话简短，每次回复控制在 50 字以内。")
        self.persona_edit.setStyleSheet(
            "QTextEdit{background:#ffffff;border:1px solid #dfe3ec;"
            "border-radius:8px;padding:8px;"
            "font-family:'Microsoft YaHei UI';font-size:12px;}")
        self.persona_edit.setPlainText(self.app.cfg.get("persona") or "")
        self.persona_edit.textChanged.connect(self._on_persona_edited)

        self._persona_hint = QLabel("")
        self._persona_hint.setWordWrap(True)
        self._persona_hint.setStyleSheet(
            "color:#d08a00;font-size:11px;margin-left:2px;")

        self._persona_lines_status = QLabel("")
        self._persona_lines_status.setWordWrap(True)
        self._persona_lines_status.setStyleSheet(
            "color:#8a8f9c;font-size:11px;")

        self._persona_reasons = QLabel("")
        self._persona_reasons.setWordWrap(True)
        self._persona_reasons.setStyleSheet(
            "color:#8a8f9c;font-size:11px;background:#f7f8fa;"
            "border-radius:6px;padding:8px;")
        self._persona_reasons.hide()

        # 动作按钮（弹窗里显示，写进设置页也能用）
        self._persona_retry_btn = QPushButton("重试")
        self._persona_retry_btn.setFixedWidth(64)
        self._persona_retry_btn.clicked.connect(self._regen_persona_lines)
        self._persona_retry_btn.hide()
        self._persona_retry_failed_btn = QPushButton("重试失败项")
        self._persona_retry_failed_btn.setFixedWidth(96)
        self._persona_retry_failed_btn.clicked.connect(
            self._retry_failed_persona_pools)
        self._persona_retry_failed_btn.hide()
        self._persona_reason_btn = QPushButton("查看原因")
        self._persona_reason_btn.setFixedWidth(84)
        self._persona_reason_btn.setCheckable(True)
        self._persona_reason_btn.clicked.connect(self._toggle_persona_reasons)
        self._persona_reason_btn.hide()
        self._persona_reset_lines_btn = QPushButton("恢复默认台词")
        self._persona_reset_lines_btn.clicked.connect(
            self._reset_persona_lines)
        self._persona_regen_btn = QPushButton("重新生成")
        self._persona_regen_btn.clicked.connect(self._regen_persona_lines)
        # 状态位（原折叠组，已移除；保留占位避免方法报错）
        self._persona_section = None

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
        # 主动搭话测试结果（一次性连接，避免重复触发）
        self._pro_test_done.connect(self._on_pro_test_done)
        # 检查更新结果（后台线程 → 主线程）
        self._update_done.connect(self._on_update_done)
        # 台词池生成结果（后台线程 → 主线程）
        self._persona_gen_result.connect(self._on_persona_gen_result)
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
        self._build_motion_page(self._pages["motion"])
        self._build_reminder_page(self._pages["reminder"])
        self._build_chat_page(self._pages["chat"])
        self._build_proactive_page(self._pages["proactive"])
        self._build_voice_page(self._pages["voice"])
        self._build_general_page(self._pages["general"])

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
        """页面标题。desc 不为空时，改为标题行右侧的可点击 i 图标。"""
        if desc:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            lab = QLabel(text)
            f = QFont()
            f.setPointSize(11)
            f.setBold(True)
            lab.setFont(f)
            row.addWidget(lab)
            row.addStretch(1)
            row.addWidget(self._info_btn(text, desc))
            lay.addLayout(row)
        else:
            lab = QLabel(text)
            f = QFont()
            f.setPointSize(11)
            f.setBold(True)
            lab.setFont(f)
            lay.addWidget(lab)

    def _check(self, lay, label, checked, on_toggle, desc=None, note=None,
               indent=0):
        """复选框。desc 改为复选框右侧的可点击 i 图标。"""
        cb = QCheckBox(label)
        cb.setChecked(bool(checked))
        cb.toggled.connect(on_toggle)
        if desc:
            wrap = QHBoxLayout()
            wrap.setContentsMargins(int(indent), 0, 0, 0)
            wrap.setSpacing(6)
            wrap.addWidget(cb)
            wrap.addWidget(self._info_btn(label, desc), 0)
            wrap.addStretch(1)
            lay.addLayout(wrap)
        elif indent:
            wrap = QHBoxLayout()
            wrap.setContentsMargins(int(indent), 0, 0, 0)
            wrap.addWidget(cb)
            wrap.addStretch(1)
            lay.addLayout(wrap)
        else:
            lay.addWidget(cb)
        if note:
            n = QLabel(note)
            n.setStyleSheet("color:#d08a00;font-size:11px;margin-left:%dpx;"
                            % (22 + int(indent)))
            lay.addWidget(n)
        return cb

    def _slider_row(self, lay, label, value, lo, hi, step, fmt, on_change, suffix=""):
        """一行：标签 + 滑块 + 数值框（双向同步）

        step 为小数时（如弹性 0.05），QSlider 内部用整数刻度
        [0, (hi-lo)/step]，显示时再乘回 step —— 否则 0.05 会被
        QSlider 的 int 截成 0，滑条只能整档跳（0 或 1）。
        """
        row = QHBoxLayout()
        name = QLabel(label)
        name.setFixedWidth(96)
        row.addWidget(name)

        is_float_step = isinstance(step, float) and step < 1
        if is_float_step:
            scale = step
            s = QSlider(Qt.Orientation.Horizontal)
            s.setRange(0, int(round((hi - lo) / scale)))
            s.setSingleStep(1)
            s.setPageStep(5)
            s.setValue(int(round((float(value) - lo) / scale)))
        else:
            scale = 1.0
            s = QSlider(Qt.Orientation.Horizontal)
            s.setRange(int(lo), int(hi))
            s.setSingleStep(int(step))
            s.setPageStep(int(step) * 5)
            s.setValue(int(value))
        s.setMinimumWidth(220)
        row.addWidget(s, 1)
        box = QDoubleSpinBox()
        box.setRange(lo, hi)
        box.setSingleStep(step)
        box.setDecimals(2 if is_float_step else 0)
        box.setValue(float(value))
        box.setFixedWidth(96)
        row.addWidget(box)
        unit = QLabel(suffix)
        unit.setStyleSheet("color:#8a8f9c;")
        row.addWidget(unit)
        lay.addLayout(row)

        def slider_to_val(iv):
            return lo + iv * scale if is_float_step else float(iv)

        def val_to_slider(v):
            return int(round((float(v) - lo) / scale)) if is_float_step else int(round(v))

        def from_slider(iv):
            v = slider_to_val(iv)
            if abs(box.value() - v) > 1e-9:
                box.blockSignals(True)
                box.setValue(v)
                box.blockSignals(False)
            on_change(v)

        def from_box(v):
            iv = val_to_slider(v)
            if s.value() != iv:
                s.blockSignals(True)
                s.setValue(iv)
                s.blockSignals(False)
            on_change(float(v))

        s.valueChanged.connect(from_slider)
        box.valueChanged.connect(from_box)
        return s, box

    def _hint_label(self, text):
        """统一风格的灰色小字说明。"""
        w = QLabel(text)
        w.setWordWrap(True)
        w.setStyleSheet("color:#8a8f9c;font-size:11px;margin-left:22px;")
        return w


    def _sub_hint(self, text):
        """紧贴控件的单行小字：灰色、比控件标签小一号、无左缩进。

        放在 QFormLayout 的字段列，与上方控件留小间距（由行距控制）。
        """
        w = QLabel(text)
        w.setWordWrap(True)
        w.setStyleSheet("color:#8a8f9c;font-size:10px;")
        w.setContentsMargins(0, 0, 0, 0)
        return w


    def _tokens_field(self, value, lo, hi, step, on_change, suffix="tokens"):
        """滑块 + 数字框 + 单位，装进一个 QWidget（供 QFormLayout 用）。

        返回 (容器 widget, 滑块, 数字框)。
        """
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setPageStep(step * 5)
        s.setValue(int(value))
        s.setMinimumWidth(200)
        h.addWidget(s, 1)

        box = QDoubleSpinBox()
        box.setRange(lo, hi)
        box.setSingleStep(step)
        box.setDecimals(0)
        box.setValue(float(value))
        box.setFixedWidth(96)
        h.addWidget(box)

        unit = QLabel(suffix)
        unit.setStyleSheet("color:#8a8f9c;")
        h.addWidget(unit)

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
        return w, s, box


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
    def _build_live_gen_row(self, lay):
        """「提醒也实时生成」开关 + 今日已用次数。"""
        try:
            from petcore.consts import LIVE_GEN_DAILY_MAX
            used = 0
            try:
                if getattr(self.app, "_care", None) is not None:
                    used = self.app._care.live_gen_used_today()
            except Exception:
                pass
            _lg_tip = (
                "关闭时固定台词只从缓存池或中性池抽取（0 token）。\n"
                "开启后弹提醒时现场生成一句，每天最多 %d 次，"
                "超时 3 秒或失败自动回落。\n今日已用 %d 次。"
                % (LIVE_GEN_DAILY_MAX, used))
            # 复选框 + 右侧说明图标，同一行
            _lg_row = QHBoxLayout()
            _lg_row.setContentsMargins(0, 0, 0, 0)
            cb = QCheckBox("提醒也实时生成（耗 token）")
            cb.setChecked(bool(self.app.is_enabled("care_live_generate")))
            cb.toggled.connect(
                lambda on: self.app.set_feature("care_live_generate", on))
            _lg_row.addWidget(cb)
            _lg_row.addStretch(1)
            _lg_row.addWidget(self._info_btn("提醒也实时生成", _lg_tip))
            lay.addLayout(_lg_row)
        except Exception as ex:
            log("[设置] 实时生成开关失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _info_btn(self, title, text, size=18):
        """创建一个可点击的说明图标：点开弹出 text。"""
        b = QPushButton("i")
        b.setObjectName("DialogInfoBtn")
        b.setFixedSize(size, size)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(text)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(
            lambda _=False, t=text, n=title:
            QMessageBox.information(self, n, t))
        return b

    def _info_row(self, layout, title, text, add_stretch=True):
        """在给定布局末尾追加一个说明图标。"""
        if add_stretch:
            layout.addStretch(1)
        layout.addWidget(self._info_btn(title, text), 0)
        return layout

    def _build_features_page(self, lay):
        self._title(lay, "功能开关",
                    "关掉的功能不会再触发。带「重启生效」的要重启程序。")
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
                if key == "care_live_generate":
                    continue        # 已由 _build_live_gen_row 显示（带今日计数），不重复
                note = "重启生效" if spec.get("needs_restart") else None
                cb = self._check(
                    lay, spec["name"], self.app.is_enabled(key),
                    lambda on, k=key: self.app.set_feature(k, on),
                    spec.get("desc"), note)
                self._feature_boxes[key] = cb
        self._build_live_gen_row(lay)
        lay.addStretch(1)

    # ---------- 宠物页 ----------
    def _build_pet_page(self, lay):
        """宠物页：四组 —— 宠物管理 / 选中的宠物 / 物理 / 窗口。"""
        # ================= ① 宠物管理 =================
        self._title(lay, "宠物管理", "可以养多只。至少要留一只。")
        self.pet_list = QListWidget()
        self.pet_list.setFixedHeight(120)
        self.pet_list.currentRowChanged.connect(lambda _r: self.refresh_pet_page())
        lay.addWidget(self.pet_list)

        row = QHBoxLayout()
        add = QPushButton("添加宠物")
        add.clicked.connect(self._on_add_pet)
        rm = QPushButton("删除选中")
        rm.clicked.connect(self._delete_selected_pet)
        row.addWidget(add)
        row.addWidget(rm)
        row.addStretch(1)
        lay.addLayout(row)

        # ================= ② 选中的宠物 =================
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line)

        _sel_hdr = QLabel("选中的宠物")
        _sel_hdr.setStyleSheet(
            "color:#5686fe;font-weight:bold;font-size:11px;margin-top:4px;")
        lay.addWidget(_sel_hdr)

        self._pet_size_row = QHBoxLayout()
        self.pet_size_box = self._combo_row(
            lay, "大小", [(k, v) for k, v in SIZE_LEVELS.items()], 0.7,
            lambda v: self._apply_to_selected("size", v))
        self.pet_mode_box = self._combo_row(
            lay, "模式",
            [("自由散步", "wander"), ("跟随鼠标", "follow"), ("原地待着", "still")], "wander",
            lambda v: self._apply_to_selected("mode", v))

        # 位置：X / Y / 应用位置 / 居中 全在一行
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

        # ================= ③ 物理 =================
        line_ph = QFrame()
        line_ph.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line_ph)

        _ph_hdr = QLabel("物理")
        _ph_hdr.setStyleSheet(
            "color:#5686fe;font-weight:bold;font-size:11px;margin-top:4px;")
        lay.addWidget(_ph_hdr)

        _ph = self.app._physics_cfg()
        self._feature_boxes["physics"] = self._check(
            lay, "甩抛物理", self.app.is_enabled("physics"),
            lambda on: self._on_physics_toggle(on),
            "拖拽用弹簧跟随、松手按速度甩抛、落地反弹与挤压")

        # 缩进的子项：甩抛物理关闭时置灰
        self._top_bounce_box = self._check(
            lay, "顶部反弹", bool(_ph.get("top_bounce", True)),
            lambda on: self.app.set_physics("top_bounce", bool(on)),
            "撞到屏幕顶部时反弹；关闭则贴顶停住",
            indent=22)
        self._smash_spin_box = self._check(
            lay, "撞飞彩蛋", bool(self.app.cfg.get("smash_spin", True)),
            lambda on: self._set_anim_cfg("smash_spin", bool(on)),
            "高速甩飞撞到屏幕边缘时，头部跟随速度方向旋转，停稳后回正",
            indent=22)

        # 宠物互撞：只有一只时置灰
        _col_row = QHBoxLayout()
        _col_row.setContentsMargins(22, 0, 0, 0)
        _col_row.setSpacing(6)
        self._pet_collision_box = QCheckBox("宠物互撞")
        self._pet_collision_box.setChecked(bool(_ph.get("pet_collision", False)))
        self._pet_collision_box.toggled.connect(
            lambda on: self.app.set_physics("pet_collision", bool(on)))
        _col_row.addWidget(self._pet_collision_box)
        _col_row.addWidget(self._info_btn(
            "宠物互撞",
            "多只宠物碰到会互相弹开。\n只有一只宠物时此项不可用。"), 0)
        self._collision_hint = QLabel("多只时生效")
        self._collision_hint.setStyleSheet("color:#8a8f9c;font-size:11px;")
        _col_row.addWidget(self._collision_hint)
        _col_row.addStretch(1)
        lay.addLayout(_col_row)

        # ================= ④ 窗口 =================
        line_w = QFrame()
        line_w.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line_w)

        _w_hdr = QLabel("窗口")
        _w_hdr.setStyleSheet(
            "color:#5686fe;font-weight:bold;font-size:11px;margin-top:4px;")
        lay.addWidget(_w_hdr)

        cfg = self.app.cfg
        self._check(lay, "窗口置顶", bool(cfg.get("topmost", True)),
                    lambda on: self.app.set_topmost(on))
        self._check(lay, "鼠标穿透（点不到它）", bool(cfg.get("passthrough", False)),
                    lambda on: self.app.set_passthrough(on),
                    "开启后只能从托盘菜单取消")
        self._feature_boxes["capture_hide"] = self._check(
            lay, "截屏时隐藏",
            self.app.is_enabled("capture_hide"),
            lambda on: self.app.set_feature("capture_hide", bool(on)),
            "从系统截图 / 录屏里排除，并在截图工具出现时自动隐藏（关闭后就能被拍到）")

        lay.addStretch(1)
        self._refresh_pet_physics_state()

    def _on_add_pet(self):
        """添加宠物后刷新物理组联动状态（互撞从置灰恢复可用）。"""
        self.app.add_pet()
        self._refresh_pet_physics_state()

    def _on_physics_toggle(self, on):
        """甩抛物理开关：写配置 + 子项置灰联动。"""
        self.app.set_feature("physics", bool(on))
        self._refresh_pet_physics_state()

    def _refresh_pet_physics_state(self):
        """物理组联动：主开关关 → 子项置灰；宠物只有一只 → 互撞置灰。"""
        try:
            physics_on = self.app.is_enabled("physics")
            for w in (getattr(self, "_top_bounce_box", None),
                      getattr(self, "_smash_spin_box", None)):
                if w is not None:
                    w.setEnabled(physics_on)
            box = getattr(self, "_pet_collision_box", None)
            hint = getattr(self, "_collision_hint", None)
            if box is not None:
                n = len(self.app.cfg.get("pets") or [])
                multi = n > 1
                # 互撞要同时满足：有甩抛物理 + 多只宠物
                box.setEnabled(physics_on and multi)
                if hint is not None:
                    if not multi:
                        hint.setText("多只时生效")
                        hint.setStyleSheet("color:#8a8f9c;font-size:11px;")
                    elif not physics_on:
                        hint.setText("需先开启甩抛物理")
                        hint.setStyleSheet("color:#8a8f9c;font-size:11px;")
                    else:
                        hint.setText("多只时生效")
                        hint.setStyleSheet("color:#8a8f9c;font-size:11px;")
        except Exception as ex:
            log("[设置] 刷新物理组状态失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

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

    # ---------- 物理·动画页 ----------
    def _build_motion_page(self, lay):
        self._title(lay, "物理 · 动画", "物理：拖拽手感和反弹。动画：动作频率和帧素材。改完立即生效。\n"
                    "「顶部反弹 / 宠物互撞 / 隐藏时暂停动画 / 撞飞彩蛋」在「宠物」页。")
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

        # 「顶部反弹 / 宠物互撞」已移到「宠物」页（用户指定）

        btn = QPushButton("恢复默认（物理）")
        btn.clicked.connect(self._reset_physics)
        lay.addWidget(btn)

        # ==== 动画部分 ====
        line_pa = QFrame()
        line_pa.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line_pa)
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

        # ---- 播放速率 ----
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line2)
        self._combo_row(
            lay, "播放速率",
            [("%.2gx" % r, r) for r in ANIM_RATE_CHOICES],
            self.app.cfg.get("anim_rate", ANIM_RATE_DEFAULT),
            lambda v: self._set_anim_rate(float(v)))

        # ---- 气泡文字大小 ----
        self._bubble_scale_row = self._slider_row(
            lay, "气泡文字", int(round(float(self.app.cfg.get("bubble_scale", 1.0)) * 100)),
            50, 300, 10, None,
            lambda v: self._set_bubble_scale(int(v)), "%")

        # 「隐藏时暂停动画 / 撞飞彩蛋」已移到「宠物」页（用户指定）

        # 主动搭话已独立成页（见「主动搭话」）

        # ---- 帧序列动画（可收缩）----
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line2)
        fa = self.app.cfg.get("frame_anim")
        if not isinstance(fa, dict):
            fa = {}
        self._frame_section = CollapsibleSection("帧序列动画", expanded=True, state_key="sec_frame", app=self.app)
        _fbody = self._frame_section.body_layout

        # 说明改为「启用帧序列动画」右侧的 i 图标
        self._frame_anim_box = self._check(
            _fbody, "启用帧序列动画", bool(fa.get("enabled", True)),
            lambda on: self._set_frame_anim(on),
            "桌宠有两套动画引擎：\n"
            "· 代码变形——实时拉伸/旋转一张图，省内存但略僵。\n"
            "· 帧序列动画——每个动作一整套手绘 PNG 逐帧播放（12 fps），"
            "像看动画一样流畅。\n"
            "动作目录 sprites/anim/<动作名>/ 里有 png 就走帧序列，"
            "没有的仍用代码变形。\n\n"
            "关掉后所有动作退回代码变形，画面变糙但能省内存"
            "（缓存 4 个动作约 92 MB）。")

        row = QHBoxLayout()
        row.addSpacing(22)
        reload_btn = QPushButton("重新扫描帧目录")
        reload_btn.setToolTip("往 sprites/anim/ 加了新动作文件夹后点这里，不用重启")
        reload_btn.clicked.connect(lambda _=False: self._reload_frames())
        row.addWidget(reload_btn)
        clear_btn = QPushButton("释放帧缓存")
        clear_btn.setToolTip("立刻清空内存里的帧图，下次播放时再从磁盘加载")
        clear_btn.clicked.connect(lambda _=False: self._clear_frames())
        row.addWidget(clear_btn)
        row.addStretch(1)
        _fbody.addLayout(row)

        lay.addWidget(self._frame_section)

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

        # ---- 当前状态（折叠组，默认收起，展开时自动刷新）----
        line5 = QFrame()
        line5.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line5)
        self._debug_section = CollapsibleSection(
            "当前状态", expanded=False, state_key="sec_debug", app=self.app)
        _dbgbody = self._debug_section.body_layout
        self._idle_state = QLabel("")
        self._idle_state.setWordWrap(True)
        self._idle_state.setStyleSheet(
            "color:#8a8f9c;font-size:11px;margin-left:8px;")
        _dbgbody.addWidget(self._idle_state)
        self._action_log = QLabel("")
        self._action_log.setWordWrap(True)
        self._action_log.setStyleSheet(
            "color:#8a8f9c;font-size:11px;margin-left:8px;"
            "font-family:Consolas,monospace;")
        _dbgbody.addWidget(self._action_log)
        lay.addWidget(self._debug_section)

        # 自动刷新定时器：只在展开时运行
        self._debug_timer = QTimer(self)
        self._debug_timer.setInterval(2000)
        self._debug_timer.timeout.connect(self._refresh_debug)
        self._debug_section.toggled.connect(self._on_debug_toggled)

        btn = QPushButton("恢复默认（动画）")
        btn.clicked.connect(self._reset_anim)
        lay.addWidget(btn)
        self._refresh_frame_stat()
        self._refresh_debug()

    def _refresh_motion_controls(self):
        """把物理/权重/tier 滑条回填成当前配置值。

        reload() 不刷这些滑条（历史上只刷功能开关/宠物/聊天/视觉），
        导致「恢复默认」后 UI 停在旧值、配置却是新值 —— 看起来像没生效。

        回填只动 box 并 blockSignals：spin 的 from_box 会同步滑条位置
        （小数 step 时滑条内部是整数刻度，由 _slider_row 的映射换算）。
        """
        try:
            ph = self.app._physics_cfg()
            for key, pair in getattr(self, "_physics_sliders", {}).items():
                s, box = pair
                v = float(ph.get(key, DEFAULT_PHYSICS.get(key, box.minimum())))
                box.blockSignals(True)
                box.setValue(v)
                box.blockSignals(False)
                # 手动同步滑条位置（与 _slider_row 的映射一致）
                lo = box.minimum()
                if isinstance(box.singleStep(), float) and 0 < box.singleStep() < 1:
                    scale = box.singleStep()
                    s.blockSignals(True)
                    s.setValue(int(round((v - lo) / scale)))
                    s.blockSignals(False)
                else:
                    s.blockSignals(True)
                    s.setValue(int(round(v)))
                    s.blockSignals(False)
        except Exception:
            pass
        try:
            weights = self.app.cfg.get("animation_weights") or {}
            for key, pair in getattr(self, "_anim_sliders", {}).items():
                s, box = pair
                v = float(weights.get(key, ACTION_CATEGORY_WEIGHTS.get(key, 5)))
                s.blockSignals(True); box.blockSignals(True)
                s.setValue(int(v)); box.setValue(v)
                s.blockSignals(False); box.blockSignals(False)
            tiers = self.app.cfg.get("animation_tiers") or {}
            for key, pair in getattr(self, "_tier_sliders", {}).items():
                s, box = pair
                v = float(tiers.get(key, ANIMATION_TIERS_DEFAULT.get(key, 0))) * 100.0
                s.blockSignals(True); box.blockSignals(True)
                s.setValue(int(v)); box.setValue(v)
                s.blockSignals(False); box.blockSignals(False)
        except Exception:
            pass

    def _reset_physics(self):
        self.app.cfg["physics"] = dict(DEFAULT_PHYSICS)
        self.app.save_cfg()
        self._refresh_motion_controls()
        self.app.say_main("物理参数已恢复默认")

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

    def _on_debug_toggled(self, expanded):
        """展开时启动自动刷新，收起时停止并只更新标题小字。"""
        try:
            if expanded:
                self._refresh_debug()
                self._debug_timer.start()
            else:
                self._debug_timer.stop()
                self._refresh_debug()
        except Exception:
            pass

    def _refresh_debug(self):
        """显示当前待机状态 + 最近 20 个动作 + 帧动作数与内存。

        收起时只更新标题右侧的状态小字（省渲染开销）。
        """
        try:
            pet = self.app.main_pet
            if pet is None:
                return
            sec = pet._idle_seconds()
            stage = pet._idle_stage
            # 标题右侧小字：阶段 · 当前动作
            try:
                act = pet.chain_action or pet._combo or ""
                summary = stage + (" · " + str(act) if act else "")
                self._debug_section.set_summary(summary)
            except Exception:
                pass
            if not self._debug_section.is_expanded():
                return          # 收起时不填详细内容
            self._idle_state.setText(
                "待机状态：%s · 已静默 %d 秒（%d 秒后犯困 / %d 秒后坐下 / %d 秒后睡觉）\n"
                "当前动作：%s · 连招：%s · 转向中：%s"
                % (stage, int(sec),
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
        """帧状态行已移除，保留空方法供既有调用点使用。"""

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

    def _set_anim_rate(self, rate):
        self.app.cfg["anim_rate"] = float(rate)
        self.app.save_cfg()
        self.app.say_main("播放速率 %.2gx" % float(rate))

    def _set_bubble_scale(self, percent):
        v = max(50, min(150, int(percent))) / 100.0
        self.app.cfg["bubble_scale"] = v
        self.app.save_cfg()
        # 立即生效：重设窗口尺寸（气泡区跟着变）
        for w in self.app.pets:
            try:
                w.apply_bubble_scale()
            except Exception:
                pass

    def _set_anim_cfg(self, key, value):
        self.app.cfg[key] = value
        self.app.save_cfg()

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
        self._refresh_motion_controls()
        self._refresh_frame_stat()
        self._refresh_debug()
        self.app.say_main("动画参数已恢复默认")

    # ---------- 提醒页 ----------
    def _build_reminder_page(self, lay):
        ex = self.app._rem_extras()

        self._title(lay, "提醒", "到点宠物会走过来提醒你。免打扰时段内不弹。\n"
                    "右侧「已用/上限」会随间隔和免打扰自动调整。")

        # ================= ① 健康提醒 =================
        self._health_section = CollapsibleSection("健康提醒", expanded=True, state_key="sec_health", app=self.app)
        _hbody = self._health_section.body_layout

        _hrow = QHBoxLayout()
        _hrow.addSpacing(2)
        _uni = QPushButton("全部统一设置…")
        _uni.setToolTip("把喝水 / 起身 / 护眼三个间隔一次改成同一个值")
        _uni.clicked.connect(self._unify_reminder_interval)
        _hrow.addWidget(_uni)
        _hrow.addStretch(1)
        _hbody.addLayout(_hrow)

        # 三行：开关 + 名称 + 每 [间隔] 分钟 + 今日确认次数 + 小测试按钮
        self._rem_boxes = {}
        self._rem_spins = {}
        self._rem_done_labs = {}
        self._rem_test_btns = {}
        for kind, label in REMINDERS.items():
            row = QHBoxLayout()
            row.setSpacing(6)

            cb = QCheckBox()
            cb.setChecked(self.app._rem_on(kind))
            cb.setToolTip("开启 / 关闭%s" % label)
            cb.toggled.connect(lambda on, k=kind: self.app.set_reminder_on(k, on))
            row.addWidget(cb)

            name = QLabel(label)
            name.setFixedWidth(88)
            row.addWidget(name)

            row.addWidget(QLabel("每"))
            sp = QSpinBox()
            sp.setRange(1, 600)
            sp.setValue(self.app._rem_minutes(kind))
            sp.setSuffix(" 分钟")
            sp.setFixedWidth(110)       # 保证「120 分钟」不被截断
            sp.setAlignment(Qt.AlignmentFlag.AlignRight
                            | Qt.AlignmentFlag.AlignVCenter)
            sp.valueChanged.connect(
                lambda v, k=kind: self._on_rem_min_changed(k, int(v)))
            row.addWidget(sp)

            done = QLabel("")
            done.setFixedWidth(64)      # 容得下「12/12」
            done.setStyleSheet("color:#5686fe;font-size:11px;")
            done.setAlignment(Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(done)

            # 「测试」是调试控件：打包版隐藏（见 is_dev_mode），
            # 隐藏后不留空位，今日次数列自然靠右
            test = QPushButton("测试")
            test.setFixedWidth(52)
            test.setToolTip("立刻弹一次%s（不影响间隔计时）" % label)
            test.clicked.connect(lambda _=False, kk=kind: self.app._fire_reminder(kk))
            test.setVisible(is_dev_mode())
            row.addWidget(test)

            row.addStretch(1)
            _hbody.addLayout(row)

            self._rem_boxes[kind] = cb
            self._rem_spins[kind] = sp
            self._rem_done_labs[kind] = done
            self._rem_test_btns[kind] = test

        lay.addWidget(self._health_section)

        # ================= ② 待办与定时（标签页）=================
        self._todo_section = CollapsibleSection("待办与定时", expanded=True, state_key="sec_todo", app=self.app)
        _tbody = self._todo_section.body_layout

        self._rem_tabs = QTabWidget()
        self._rem_tabs.setDocumentMode(True)

        # ---- 标签「待办」----
        _todo_page = QWidget()
        _tl = QVBoxLayout(_todo_page)
        _tl.setContentsMargins(8, 8, 8, 8)
        _tl.setSpacing(6)
        self._feature_boxes["remind_todo"] = QCheckBox("启用待办提醒")
        self._feature_boxes["remind_todo"].setChecked(
            self.app.is_enabled("remind_todo"))
        self._feature_boxes["remind_todo"].toggled.connect(
            lambda on: self.app.set_feature("remind_todo", bool(on)))
        _tl.addWidget(self._feature_boxes["remind_todo"])
        _tl.addWidget(self._sub_hint(
            "最多 %d 条；到点用同样的方式提醒。"
            "聊天里说「提醒我 10 分钟后 xxx」也能加。" % TODO_MAX))
        self.todo_list = QListWidget()
        self.todo_list.setFixedHeight(110)
        _tl.addWidget(self.todo_list)
        _trow = QHBoxLayout()
        for text, fn in (("添加", self._add_todo_dialog),
                         ("编辑", self._edit_todo_dialog),
                         ("删除", self._remove_todo_dialog),
                         ("启用/停用", self._toggle_todo_dialog)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            _trow.addWidget(b)
        _trow.addStretch(1)
        _tl.addLayout(_trow)
        _tl.addStretch(1)
        self._rem_tabs.addTab(_todo_page, "待办")

        # ---- 标签「定时」----
        _sch_page = QWidget()
        _sl = QVBoxLayout(_sch_page)
        _sl.setContentsMargins(8, 8, 8, 8)
        _sl.setSpacing(6)
        self._feature_boxes["remind_schedule"] = QCheckBox("启用定时提醒")
        self._feature_boxes["remind_schedule"].setChecked(
            self.app.is_enabled("remind_schedule"))
        self._feature_boxes["remind_schedule"].toggled.connect(
            lambda on: self.app.set_feature("remind_schedule", bool(on)))
        _sl.addWidget(self._feature_boxes["remind_schedule"])
        _sl.addWidget(self._sub_hint(
            "固定时刻提醒（如 12:00 吃饭）；可设每天 / 工作日 / 仅一次，"
            "台词留空用默认。"))
        self.sch_list = QListWidget()
        self.sch_list.setFixedHeight(100)
        _sl.addWidget(self.sch_list)
        _srow = QHBoxLayout()
        for text, fn in (("添加", self._add_schedule_dialog),
                         ("删除", self._remove_schedule_dialog),
                         ("启用/停用", self._toggle_schedule_dialog)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            _srow.addWidget(b)
        _srow.addStretch(1)
        _sl.addLayout(_srow)
        _sl.addStretch(1)
        self._rem_tabs.addTab(_sch_page, "定时")

        _tbody.addWidget(self._rem_tabs)
        lay.addWidget(self._todo_section)

        # ================= ③ 提醒规则 =================
        self._rule_rem_section = CollapsibleSection("提醒规则", expanded=True, state_key="sec_rule_rem", app=self.app)
        _rbody = self._rule_rem_section.body_layout

        self._feature_boxes["remind_walk"] = self._check(
            _rbody, "提醒时走到屏幕中间", self.app.is_enabled("remind_walk"),
            lambda on: self.app.set_feature("remind_walk", bool(on)),
            "关闭后原地提醒，不移动宠物")

        _rline = QFrame()
        _rline.setFrameShape(QFrame.Shape.HLine)
        _rbody.addWidget(_rline)

        dnd = ex.get("dnd") or {}
        _dnd_row = QHBoxLayout()
        _dnd_row.setSpacing(6)
        self._feature_boxes["remind_dnd"] = QCheckBox("免打扰时段")
        self._feature_boxes["remind_dnd"].setChecked(bool(dnd.get("enabled", True)))
        self._feature_boxes["remind_dnd"].toggled.connect(
            lambda on: self._set_dnd("enabled", bool(on)))
        _dnd_row.addWidget(self._feature_boxes["remind_dnd"])
        _dnd_row.addSpacing(8)
        _dnd_row.addWidget(QLabel("从"))
        self.dnd_start = QLineEdit(dnd.get("start", "22:00"))
        self.dnd_start.setFixedWidth(72)
        self.dnd_start.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dnd_start.editingFinished.connect(
            lambda: self._set_dnd("start", self.dnd_start.text()))
        _dnd_row.addWidget(self.dnd_start)
        _dnd_row.addWidget(QLabel("到"))
        self.dnd_end = QLineEdit(dnd.get("end", "08:00"))
        self.dnd_end.setFixedWidth(72)
        self.dnd_end.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dnd_end.editingFinished.connect(
            lambda: self._set_dnd("end", self.dnd_end.text()))
        _dnd_row.addWidget(self.dnd_end)
        _dnd_row.addStretch(1)
        _rbody.addLayout(_dnd_row)
        _rbody.addWidget(self._sub_hint(
            "期间提醒静音且不弹气泡，到点后不补发；"
            "检测到全屏 / 演示模式也会自动暂停。"))

        lay.addWidget(self._rule_rem_section)

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
        # 免打扰变化会影响可用时长 → 上限跟着变，刷新显示
        self._refresh_remind_lists()

    def _unify_reminder_interval(self):
        cur = self.app._rem_minutes("water")
        v, ok = QInputDialog.getInt(self, "全部统一设置", "所有提醒统一为多少分钟？",
                                    cur, 1, 600, 5)
        if ok:
            self.app.set_all_reminder_min(v)
            # 只回填三个间隔框，不用 reload()（避免整页控件重建丢状态）
            for kind, sp in getattr(self, "_rem_spins", {}).items():
                sp.blockSignals(True)
                sp.setValue(self.app._rem_minutes(kind))
                sp.blockSignals(False)
            self.app.say_main("三个提醒间隔都改成 %d 分钟" % v)

    def _on_rem_min_changed(self, kind, mins):
        """改间隔：写配置 + 刷新上限显示（上限随间隔变化）。"""
        try:
            self.app.set_reminder_min(kind, int(mins))
        except Exception:
            pass
        self._refresh_remind_lists()

    def _refresh_remind_lists(self):
        """刷新待办/定时列表 + 标签页标题条数 + 三个健康提醒的今日次数。"""
        try:
            ex = self.app._rem_extras()
            todos = ex.get("todos") or []
            schs = ex.get("schedules") or []

            self.todo_list.clear()
            for t in todos:
                mark = "✓ " if t.get("done") else ("" if t.get("enabled", True) else "⏸ ")
                rep = dict((k, l) for l, k in TODO_REPEATS).get(t.get("repeat"), "")
                self.todo_list.addItem("%s%s  [%s %s]" % (mark, t.get("text"),
                                                         rep, t.get("at", "")))
            self.sch_list.clear()
            for it in schs:
                mark = "" if it.get("enabled", True) else "⏸ "
                rep = dict((k, l) for l, k in TODO_REPEATS).get(it.get("repeat"), "")
                self.sch_list.addItem("%s%s  [%s %s]" % (mark, it.get("at"),
                                                        rep, it.get("name", "")))

            # 标签标题带条数
            try:
                self._rem_tabs.setTabText(0, "待办 %d" % len(todos))
                self._rem_tabs.setTabText(1, "定时 %d" % len(schs))
            except Exception:
                pass

            # 每行右侧的今日确认次数 + 动态上限（方案 C：显示 X/N）
            dt = self.app._done_today()
            try:
                ex = self.app._rem_extras()
                dnd = ex.get("dnd") or {}
                dnd_on = bool(dnd.get("enabled", True))
            except Exception:
                dnd, dnd_on = {}, True
            for kind, lab in getattr(self, "_rem_done_labs", {}).items():
                used = int(dt.get(kind, 0) or 0)
                try:
                    mins = int(self.app._rem_minutes(kind))
                    cap = calc_daily_max(kind, mins, dnd, dnd_on)
                except Exception:
                    cap = 0
                if cap > 0:
                    lab.setText("%d/%d" % (used, cap))
                    lab.setToolTip(
                        "今日已确认 %d 次，上限 %d 次\n"
                        "上限 = 可用时长 ÷ 间隔 × 85%%（留出忙碌余量）；"
                        "改间隔或免打扰时段会自动跟着变" % (used, cap))
                    # 用满时变灰提示
                    try:
                        lab.setStyleSheet(
                            "color:%s;font-size:11px;"
                            % ("#c0c4cc" if used >= cap else "#5686fe"))
                    except Exception:
                        pass
                else:
                    lab.setText("%d 次" % used)
                    lab.setToolTip("今日已确认 %d 次" % used)
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
    def _set_balance_provider(self, value):
        """切换余额来源：auto / deepseek / zhipu。"""
        v = str(value or "auto").lower()
        if v not in ("auto", "deepseek", "zhipu"):
            v = "auto"
        b = self.app.cfg.get("balance")
        if not isinstance(b, dict):
            b = {}
        b["provider"] = v
        self.app.cfg["balance"] = b
        self.app.save_cfg()

    def _on_balance_click(self):
        """点「立即查询」：禁用按钮、显示查询中…，结果回同一行。"""
        if getattr(self, "_balance_busy", False):
            return
        self._balance_busy = True
        try:
            self._balance_btn.setEnabled(False)
            self._balance_btn.setText("查询中…")
            self._balance_result.setText("")
        except Exception:
            pass

        def done(parts, ok_count, total):
            self._balance_busy = False
            try:
                self._balance_btn.setEnabled(True)
                self._balance_btn.setText("立即查询")
            except Exception:
                pass
            try:
                if not parts:
                    self._balance_result.setText("未配置")
                    self._balance_result.setStyleSheet(
                        "color:#8a8f9c;font-size:11px;")
                elif ok_count == 0:
                    self._balance_result.setText("查询失败")
                    self._balance_result.setStyleSheet(
                        "color:#d93025;font-size:11px;")
                else:
                    self._balance_result.setText(" · ".join(parts))
                    self._balance_result.setStyleSheet(
                        "color:#2f3542;font-size:11px;")
            except Exception:
                pass

        try:
            self.app.query_balance(True, on_done=done)
        except Exception as ex:
            log("[设置] 查询余额失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            done([], 0, 0.0)

    # ---------- 聊天页 ----------
    def _build_chat_page(self, lay):
        self._title(lay, "聊天", "选模型、写人设、配视觉。人设决定说话风格。四组可点标题展开。")

        # ================= ① 聊天模型（默认展开）=================
        self._chat_section = _ChatModelGroup(app=self.app)
        _body = self._chat_section.body_layout

        _rowh = 30
        _cf = QFormLayout()
        _cf.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        _cf.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        _cf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        _cf.setHorizontalSpacing(10)
        _cf.setVerticalSpacing(6)
        _cf.setContentsMargins(22, 0, 0, 0)

        # 1) 聊天 Key（密码 + 显示/隐藏）
        self.chat_key = QLineEdit()
        self.chat_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.chat_key.setFixedHeight(_rowh)
        self.chat_key.textChanged.connect(self._on_chat_key_changed)
        _ckw = QWidget()
        _ckh = QHBoxLayout(_ckw)
        _ckh.setContentsMargins(0, 0, 0, 0)
        _ckh.setSpacing(6)
        _ckh.addWidget(self.chat_key, 1)
        self._chat_eye_btn = QPushButton("显示")
        self._chat_eye_btn.setFixedSize(52, _rowh)
        self._chat_eye_btn.setToolTip("显示 / 隐藏 Key")
        self._chat_eye_btn.clicked.connect(self._toggle_chat_key)
        _ckh.addWidget(self._chat_eye_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        _cf.addRow("聊天 Key", _ckw)

        # 2) 接口地址（只读）
        self.chat_base = QLineEdit()
        self.chat_base.setReadOnly(True)
        self.chat_base.setFixedHeight(_rowh)
        self.chat_base.setStyleSheet(
            "QLineEdit{background:#f2f4f8;color:#5a6070;}")
        _cf.addRow("接口地址", self.chat_base)

        # 3) 聊天模型（可编辑）
        self.chat_model = QComboBox()
        self.chat_model.setEditable(True)
        self.chat_model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.chat_model.setFixedHeight(_rowh)
        self.chat_model.currentIndexChanged.connect(self._on_chat_model_changed)
        self.chat_model.editTextChanged.connect(self._on_chat_model_changed)
        _cf.addRow("聊天模型", self.chat_model)

        _body.addLayout(_cf)

        # 按钮：保存 / 测试 / 取消 / 清空
        _cbtn = QHBoxLayout()
        _cbtn.addSpacing(22)
        self._chat_save_btn = QPushButton("保存")
        self._chat_save_btn.clicked.connect(self._save_chat_cfg)
        _cbtn.addWidget(self._chat_save_btn)
        self._chat_test_btn = QPushButton("测试")
        self._chat_test_btn.setToolTip("识别服务商并验证可用的聊天模型")
        self._chat_test_btn.clicked.connect(self._test_chat)
        _cbtn.addWidget(self._chat_test_btn)
        self._chat_cancel_btn = QPushButton("取消")
        self._chat_cancel_btn.clicked.connect(self._cancel_chat_test)
        self._chat_cancel_btn.hide()
        _cbtn.addWidget(self._chat_cancel_btn)
        self._chat_clear_btn = QPushButton("清空")
        self._chat_clear_btn.setToolTip("清掉 Key、地址、模型")
        self._chat_clear_btn.clicked.connect(self._clear_chat_cfg)
        _cbtn.addWidget(self._chat_clear_btn)
        _cbtn.addStretch(1)
        _body.addLayout(_cbtn)

        # 状态行
        self._chat_status = QLabel("")
        self._chat_status.setWordWrap(True)
        self._chat_status.setStyleSheet(
            "color:#8a8f9c;font-size:11px;margin-left:22px;")
        _body.addWidget(self._chat_status)

        # 缓存提示
        _body.addWidget(self._hint_label("更换模型后缓存失效一次属正常"))
        self._chat_fill_from_cfg()

        # 回复长度由人设控制，此处不再提供风格下拉框
        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.HLine)
        _body.addWidget(_sep)

        # ---- 单次输出上限（滑块 + 数字框）----
        self._max_tokens_field, self.max_tokens_slider, self.max_tokens_spin = \
            self._tokens_field(
                int(self.app.cfg.get("max_tokens", MAX_TOKENS_DEFAULT)),
                MAX_TOKENS_MIN, MAX_TOKENS_MAX, 50,
                lambda v: self._set_max_tokens(int(v)), "tokens")
        _mt_row = QHBoxLayout()
        _mt_row.addWidget(QLabel("单次输出上限"))
        _mt_row.addWidget(self._max_tokens_field, 1)
        _mt_row.addWidget(self._info_btn(
            "单次输出上限",
            "模型单次最多输出多少 token（中文约 1.5 字/token）。\n"
            "人设决定说话风格和长短，这个值只是硬性截断，"
            "防止一次回复过长消耗过多额度。"), 0)
        _mt_row.addStretch(0)
        _body.addLayout(_mt_row)

        # ---- 余额（原「余额」页迁入，一行）----
        _bsep = QFrame()
        _bsep.setFrameShape(QFrame.Shape.HLine)
        _body.addWidget(_bsep)
        _brow = QHBoxLayout()
        _brow.setSpacing(6)
        _brow.addWidget(QLabel("余额"))
        bc = self.app._balance_cfg()
        self._balance_provider_box = QComboBox()
        self._balance_provider_box.setFixedHeight(26)
        for _txt, _val in (("两家同时查", "auto"),
                           ("只查 DeepSeek", "deepseek"),
                           ("只查智谱", "zhipu")):
            self._balance_provider_box.addItem(_txt, _val)
        for _i in range(self._balance_provider_box.count()):
            if self._balance_provider_box.itemData(_i) == bc.get("provider", "auto"):
                self._balance_provider_box.setCurrentIndex(_i)
                break
        self._balance_provider_box.currentIndexChanged.connect(
            lambda _i: self._set_balance_provider(
                self._balance_provider_box.currentData()))
        _brow.addWidget(self._balance_provider_box)
        self._balance_btn = QPushButton("立即查询")
        self._balance_btn.setFixedWidth(84)
        self._balance_btn.clicked.connect(self._on_balance_click)
        _brow.addWidget(self._balance_btn)
        self._balance_result = QLabel("")
        self._balance_result.setStyleSheet("color:#8a8f9c;font-size:11px;")
        _brow.addWidget(self._balance_result, 1)
        _body.addLayout(_brow)

        lay.addWidget(self._chat_section)

        # 长期记忆入口已移到聊天窗口的「设置」菜单
        # ================= ④ 看屏幕 / 视觉（默认折叠）=================
        self._vision_section = _VisionGroup(app=self.app)
        self._build_vision_into(self._vision_section.body_layout)
        lay.addWidget(self._vision_section)

        lay.addStretch(1)
        self._refresh_chat_hint()
        self._refresh_memory_summary()
        self._refresh_vision_summary()

    # ---------- 看看屏幕：视觉配置 ----------
    def _build_vision_into(self, lay):
        """把原「视觉」页的全部控件原样填进给定布局（聊天页第 ④ 组）。"""

        _rowh = 30

        # 全部基础字段放进同一个 QFormLayout
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        form.setContentsMargins(22, 0, 0, 0)

        # 1) 视觉 Key（占满整行，右侧只留「显示/隐藏」）
        self.vision_key = QLineEdit()
        self.vision_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.vision_key.setFixedHeight(_rowh)
        self.vision_key.textChanged.connect(self._on_vision_key_changed)

        _keywrap = QWidget()
        _kh = QHBoxLayout(_keywrap)
        _kh.setContentsMargins(0, 0, 0, 0)
        _kh.setSpacing(6)
        _kh.addWidget(self.vision_key, 1)
        self._eye_btn = QPushButton("显示")
        self._eye_btn.setFixedSize(52, _rowh)
        self._eye_btn.setToolTip("显示 / 隐藏 Key")
        self._eye_btn.clicked.connect(self._toggle_vision_key)
        _kh.addWidget(self._eye_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        form.addRow("视觉 Key", _keywrap)

        # 2) 接口地址（只读）
        self.vision_base = QLineEdit()
        self.vision_base.setReadOnly(True)
        self.vision_base.setFixedHeight(_rowh)
        self.vision_base.setStyleSheet(
            "QLineEdit{background:#f2f4f8;color:#5a6070;}")
        form.addRow("接口地址", self.vision_base)

        # 3) 视觉模型
        self.vision_model = QComboBox()
        self.vision_model.setEditable(True)   # 允许手填模型名
        self.vision_model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.vision_model.setFixedHeight(_rowh)
        self.vision_model.currentIndexChanged.connect(self._on_vision_model_changed)
        # 可编辑：手输模型名时也要立即保存
        self.vision_model.editTextChanged.connect(self._on_vision_model_changed)
        form.addRow("视觉模型", self.vision_model)

        # 4) 描述风格
        self.vision_style = QComboBox()
        self.vision_style.setFixedHeight(_rowh)
        for _txt, _val in VISION_STYLE_CHOICES:
            self.vision_style.addItem(_txt, _val)
        self.vision_style.currentIndexChanged.connect(self._on_vision_style_changed)
        form.addRow("描述风格", self.vision_style)

        lay.addLayout(form)

        # 描述风格说明：改为可点击图标（内容动态生成）
        self._vision_style_hint = self._info_btn("描述风格", "…")

        # ---- 识别规则（视觉请求 system，可收缩，默认收起）----
        _r_exp = bool((self.app.cfg.get("ui_sections") or {}).get("vision_rule", False))
        self._rule_section = CollapsibleSection("识别规则", expanded=_r_exp, state_key="vision_rule", app=self.app)
        self._rule_section.toggled.connect(
            lambda on: self._save_section_state("vision_rule", on))

        _rrow = QHBoxLayout()
        _rrow.addWidget(QLabel("识别规则内容"))
        _rrow.addStretch(1)
        _rrow.addWidget(self._info_btn(
            "识别规则",
            "这段会作为视觉请求的 system，长度句自动追加在末尾"))
        self._rule_section.body_add_layout(_rrow)

        self.vision_rule_edit = QTextEdit()
        self.vision_rule_edit.setFixedHeight(120)   # 约 6 行
        self.vision_rule_edit.setPlaceholderText("留空则使用内置默认规则")
        self.vision_rule_edit.setStyleSheet(
            "QTextEdit{background:#ffffff;border:1px solid #dfe3ec;border-radius:8px;"
            "padding:8px;font-family:'Microsoft YaHei UI';font-size:12px;}")
        self.vision_rule_edit.textChanged.connect(self._on_vision_rule_edited)
        self._rule_section.body_add(self.vision_rule_edit)

        _rrow = QHBoxLayout()
        self._vision_rule_save = QPushButton("保存规则")
        self._vision_rule_save.clicked.connect(self._save_vision_rule)
        _rrow.addWidget(self._vision_rule_save)
        _rrow.addStretch(1)
        self._rule_section.body_add_layout(_rrow)

        lay.addWidget(self._rule_section)
        self._refresh_rule_state()

        # ---- 高级设置（可收缩，默认收起）----
        _a_exp = bool((self.app.cfg.get("ui_sections") or {}).get("vision_advanced", False))
        self._advanced_section = CollapsibleSection("高级设置", expanded=_a_exp, state_key="vision_advanced", app=self.app)
        self._advanced_section.toggled.connect(
            lambda on: self._save_section_state("vision_advanced", on))

        _af = QFormLayout()
        _af.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        _af.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        _af.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        _af.setHorizontalSpacing(10)
        _af.setVerticalSpacing(4)
        _af.setContentsMargins(22, 0, 0, 0)

        # 四项放进同一个 QFormLayout：标签列同宽 → 滑块起点、数字框左边缘自动对齐
        _af = QFormLayout()
        _af.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        _af.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        _af.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        _af.setHorizontalSpacing(10)
        _af.setVerticalSpacing(8)
        _af.setContentsMargins(22, 0, 0, 0)

        # 5) 单次上限
        self._vision_tokens_field, self.vision_tokens_slider, self.vision_tokens_spin = \
            self._tokens_field(
                int(self.app.cfg.get("look_screen", {}).get("max_tokens")
                    or VISION_TOKENS_DEFAULT),
                VISION_TOKENS_MIN, VISION_TOKENS_MAX, 50,
                lambda v: self._on_vision_tokens_changed(int(v)), "")
        _af.addRow("单次上限", self._vision_tokens_field)
        _af.addRow("", self._sub_hint(
            "识别结果最多写多少字。太小会截断。"))

        # 6) 图片最长边
        self._vision_side_field, self.vision_side_slider, self.vision_side_spin = \
            self._tokens_field(
                int((self.app.cfg.get("look_screen") or {}).get("max_side")
                    or LOOK_SCREEN_SIDE_DEFAULT),
                LOOK_SCREEN_SIDE_MIN, LOOK_SCREEN_SIDE_MAX, LOOK_SCREEN_SIDE_STEP,
                lambda v: self._on_vision_limit_changed("max_side", int(v)), "px")
        _af.addRow("图片最长边", self._vision_side_field)
        _af.addRow("", self._sub_hint(
            "截图缩放后的最长边。越大越清楚，也越慢越贵。1024 一般够用。"))

        # 7) 图片质量
        self._vision_quality_field, self.vision_quality_slider, self.vision_quality_spin = \
            self._tokens_field(
                int((self.app.cfg.get("look_screen") or {}).get("quality")
                    or LOOK_SCREEN_QUALITY_DEFAULT),
                LOOK_SCREEN_QUALITY_MIN, LOOK_SCREEN_QUALITY_MAX, 5,
                lambda v: self._on_vision_limit_changed("quality", int(v)), "")
        _af.addRow("图片质量", self._vision_quality_field)
        _af.addRow("", self._sub_hint(
            "越低上传越快，字会变糊。一般不用动。"))

        # 8) 请求超时（宽度与数字框一致）
        self.vision_timeout_spin = QSpinBox()
        self.vision_timeout_spin.setRange(LOOK_SCREEN_TIMEOUT_MIN, LOOK_SCREEN_TIMEOUT_MAX)
        self.vision_timeout_spin.setSingleStep(5)
        self.vision_timeout_spin.setSuffix(" 秒")
        self.vision_timeout_spin.setFixedWidth(96)
        self.vision_timeout_spin.setValue(
            int((self.app.cfg.get("look_screen") or {}).get("timeout")
                or LOOK_SCREEN_TIMEOUT_DEFAULT))
        self.vision_timeout_spin.valueChanged.connect(
            lambda v: self._on_vision_limit_changed("timeout", int(v)))
        _af.addRow("请求超时", self.vision_timeout_spin)
        _af.addRow("", self._sub_hint(
            "等多久没反应就放弃。慢模型可以调大。"))

        self._advanced_section.body_add_layout(_af)

        # 保存屏幕描述到聊天记录（默认关闭）
        self._vision_save_desc = self._check(
            self._advanced_section.body_layout, "保存屏幕描述到聊天记录",
            bool((self.app.cfg.get("look_screen") or {}).get("save_desc", False)),
            lambda on: self._set_vision_save_desc(bool(on)),
            "关闭时只用于这一次回复，不写入聊天记录。")

        # 把截图原图也存进聊天记录（默认关闭）
        self._vision_save_shot = self._check(
            self._advanced_section.body_layout, "把截图原图也存进聊天记录",
            bool((self.app.cfg.get("look_screen") or {}).get("save_shot", False)),
            lambda on: self._set_vision_save_shot(bool(on)),
            "开启后聊天记录里会显示这次框选的截图（缩略图，点击可看大图）。")


        # 高级设置加入页面（放在识别规则之后、按钮之前）
        lay.addWidget(self._advanced_section)


        # 按钮：保存 / 测试 / 取消 / 清空
        vbtn_row = QHBoxLayout()
        vbtn_row.addSpacing(22)
        self._vision_save_btn = QPushButton("保存")
        self._vision_save_btn.clicked.connect(self._save_vision_cfg)
        vbtn_row.addWidget(self._vision_save_btn)
        self._vision_test_btn = QPushButton("测试")
        self._vision_test_btn.setToolTip("识别服务商并验证可用的视觉模型")
        self._vision_test_btn.clicked.connect(self._test_vision)
        vbtn_row.addWidget(self._vision_test_btn)
        self._vision_cancel_btn = QPushButton("取消")
        self._vision_cancel_btn.setToolTip("停止识别（已发出的请求无法中断，结果会被丢弃）")
        self._vision_cancel_btn.clicked.connect(self._cancel_vision_test)
        self._vision_cancel_btn.hide()
        vbtn_row.addWidget(self._vision_cancel_btn)
        self._vision_clear_btn = QPushButton("清除 Key")
        self._vision_clear_btn.setToolTip("清掉 Key、地址、模型（不影响高级设置）")
        self._vision_clear_btn.clicked.connect(self._clear_vision_cfg)
        vbtn_row.addWidget(self._vision_clear_btn)
        self._vision_reset_btn = QPushButton("恢复默认")
        self._vision_reset_btn.setToolTip(
            "把高级设置四项和识别规则恢复为默认值（不影响 Key / 地址 / 模型）")
        self._vision_reset_btn.clicked.connect(self._reset_vision_defaults)
        vbtn_row.addWidget(self._vision_reset_btn)
        vbtn_row.addStretch(1)
        lay.addLayout(vbtn_row)

        # 状态栏
        self._vision_hint = QLabel("")
        self._vision_hint.setWordWrap(True)
        self._vision_hint.setStyleSheet(
            "color:#8a8f9c;font-size:11px;margin-left:22px;")
        lay.addWidget(self._vision_hint)
        self._vision_fill_from_cfg()
        self._refresh_advanced_summary()

        # 明文 Key 提示（灰色小字，不用警告色）
        _keywarn = QLabel("配置文件（config.json）含明文 Key，请勿分享该文件")
        _keywarn.setWordWrap(True)
        _keywarn.setStyleSheet(
            "color:#8a8f9c;font-size:11px;margin-left:22px;")
        lay.addWidget(_keywarn)


    # ---------- 识别规则（视觉 system） ----------
    def _rule_from_widget(self):
        """从文本框读规则（空则回落默认）。"""
        try:
            t = (self.vision_rule_edit.toPlainText() or "").strip()
        except Exception:
            t = ""
        return t or VISION_SYSTEM_DEFAULT

    def _saved_rule(self):
        """配置里保存的规则（空则默认）。"""
        ls = self.app.cfg.get("look_screen") or {}
        return (ls.get("vision_system") or "").strip() or VISION_SYSTEM_DEFAULT

    def _on_vision_rule_edited(self):
        """实时刷新规则区块的状态小字。"""
        self._refresh_rule_state()

    def _refresh_rule_state(self):
        """规则状态：默认（跟随代码） / 已自定义。"""
        from petcore.consts import is_default_vision_system
        try:
            cur = (self.vision_rule_edit.toPlainText() or "").strip()
        except Exception as ex:
            log("[设置] 读规则文本框失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            return
        saved = (self.app.cfg.get("look_screen") or {}).get("vision_system") or ""
        # 配置里有副本且不是默认 → 已自定义
        if saved.strip() and not is_default_vision_system(saved):
            if cur == saved.strip():
                self._rule_section.set_state("已自定义", "#1a7f37")
            else:
                self._rule_section.set_state("未保存", "#d08a00")
        else:
            # 配置里没副本 → 默认
            if not cur or is_default_vision_system(cur):
                self._rule_section.set_state("默认", "#8a8f9c")
            else:
                self._rule_section.set_state("未保存", "#d08a00")


    def _refresh_advanced_summary(self):
        """高级设置收起时显示的摘要：上限 · 最长边 · 质量 · 超时。"""
        try:
            mt = int(round(self.vision_tokens_spin.value()))
            side = int(round(self.vision_side_spin.value()))
            qual = int(round(self.vision_quality_spin.value()))
            tout = int(self.vision_timeout_spin.value())
            self._advanced_section.set_summary(
                "上限 %d · 最长边 %d · 质量 %d · 超时 %d 秒" % (mt, side, qual, tout))
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_refresh_advanced_summary", type(ex).__name__, ex),
                logging.WARNING)


    def _on_persona_edited(self):
        """人设改动时刷新状态。"""
        self._refresh_persona_state()

    def _refresh_persona_state(self):
        """人设状态：默认 / 已保存 / 未保存。"""
        try:
            cur = (self.persona_edit.toPlainText() or "").strip()
        except Exception:
            return
        saved = (self.app.cfg.get("persona") or "").strip()
        sec = self._persona_section
        if sec is not None:
            if cur == saved:
                if not saved:
                    sec.set_state("无风格", "#8a8f9c")
                else:
                    sec.set_state("已保存", "#1a7f37")
            else:
                sec.set_state("未保存", "#d08a00")
        # 收起时的标题摘要跟着刷新
        self._refresh_persona_summary()

    def _save_section_state(self, name, expanded):
        """把区块展开状态写进配置（只影响显示）。"""
        try:
            sec = self.app.cfg.get("ui_sections")
            if not isinstance(sec, dict):
                sec = {}
            sec[str(name)] = bool(expanded)
            self.app.cfg["ui_sections"] = sec
            self.app.save_cfg()
        except Exception as ex:
            log("保存区块状态失败: %s" % ex, logging.WARNING)

    # ---------- 聊天页分组摘要 ----------
    def _refresh_memory_summary(self):
        """长期记忆组：标题右侧显示「N 条」。"""
        try:
            n = len(self.app.memory.items)
        except Exception:
            n = 0
        try:
            self._memory_section.set_summary("%d 条" % n)
            self._memory_count.setText("当前共 %d 条记忆" % n)
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_refresh_memory_summary", type(ex).__name__, ex),
                logging.WARNING)

    def _refresh_vision_summary(self):
        """看屏幕 / 视觉组：标题右侧显示「模型名 已配置」或「未配置」。"""
        try:
            ls = self.app.cfg.get("look_screen") or {}
            model = (ls.get("vision_model") or "").strip()
            base = (ls.get("vision_base") or "").strip()
            key = (ls.get("vision_key") or "").strip()
            if model and base and key:
                self._vision_section.set_summary("%s 已配置" % model)
            elif model:
                self._vision_section.set_summary("%s 未配置" % model)
            else:
                self._vision_section.set_summary("未配置")
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_refresh_vision_summary", type(ex).__name__, ex),
                logging.WARNING)

    def _refresh_persona_summary(self):
        """人设组：标题右侧显示「无风格」或人设摘要（收起时才显示）。"""
        try:
            txt = (self.app.cfg.get("persona") or "").strip()
        except Exception:
            txt = ""
        try:
            if not txt:
                self._persona_section.set_summary("无风格")
                return
            one = re.sub(r"\s+", " ", txt)
            if len(one) > 18:
                one = one[:18] + "…"
            self._persona_section.set_summary(one)
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_refresh_persona_summary", type(ex).__name__, ex),
                logging.WARNING)


    def _save_vision_rule(self):
        """保存规则：等于任一默认值就存空串（不存副本），否则存自定义文本。"""
        from petcore.consts import is_default_vision_system
        text = (self.vision_rule_edit.toPlainText() or "").strip()
        if not text or is_default_vision_system(text):
            saved = ""          # 不存副本，跟随默认
        else:
            saved = text[:8000]
        ls = self._vision_cfg_dict()
        ls["vision_system"] = saved
        self.app.cfg["look_screen"] = ls
        self.app.save_cfg()
        self._on_vision_rule_edited()
        self._set_vision_status(
            "✓ 已保存（跟随默认）" if not saved else "✓ 已保存自定义规则", "ok")


    def _reset_vision_rule(self, ask=True):
        """恢复默认 = 清除配置里的副本（之后自动跟随代码里的最新默认值）。"""
        if ask:
            ans = QMessageBox.question(
                self, "恢复默认规则",
                "确定把识别规则恢复成内置默认吗？\n"
                "（会清除你自定义的副本，之后默认规则更新时自动跟随）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
        # 清空副本
        ls = self._vision_cfg_dict()
        ls["vision_system"] = ""
        self.app.cfg["look_screen"] = ls
        self.app.save_cfg()
        # 界面回填最新默认值
        self.vision_rule_edit.blockSignals(True)
        self.vision_rule_edit.setPlainText(VISION_SYSTEM_DEFAULT)
        self.vision_rule_edit.blockSignals(False)
        self._on_vision_rule_edited()
        if ask:
            self._set_vision_status(
                "✓ 已恢复默认（副本已清除，将跟随新版默认）", "ok")


    def _chat_cfg_dict(self):
        """从配置读聊天三项（空则回落内置默认，供迁移带入）。"""
        base, key, model = self.app._chat_cfg_raw()
        return {"chat_key": key, "chat_base": base, "chat_model": model}

    def _chat_fill_from_cfg(self):
        """把聊天 Key/地址/模型填进控件（不触发识别、不触发保存）。"""
        d = self._chat_cfg_dict()
        for w in (self.chat_key, self.chat_base):
            w.blockSignals(True)
        self.chat_key.setText(d["chat_key"])
        self.chat_base.setText(d["chat_base"])
        for w in (self.chat_key, self.chat_base):
            w.blockSignals(False)
        self._chat_filling = True
        self.chat_model.blockSignals(True)
        self.chat_model.clear()
        if d["chat_model"]:
            self.chat_model.addItem(d["chat_model"])
            self.chat_model.setCurrentText(d["chat_model"])
        self.chat_model.blockSignals(False)
        self._chat_filling = False
        self._refresh_chat_status()

    def _refresh_chat_status(self):
        """状态行：未配置 / 已配置。"""
        try:
            key = (self.chat_key.text() or "").strip()
            model = (self.chat_model.currentText() or "").strip()
            base = (self.chat_base.text() or "").strip()
            if not key:
                self._set_chat_status("尚未配置聊天 Key，点「测试」前请先填写", "info")
            elif not model:
                self._set_chat_status("已填 Key，点「测试」识别可用模型", "info")
            else:
                self._set_chat_status("当前：%s @ %s" % (model, base), "ok")
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_refresh_chat_status", type(ex).__name__, ex),
                logging.WARNING)

    def _set_chat_status(self, text, kind="info"):
        color = {"ok": "#1a7f37", "err": "#c0392b", "info": "#8a8f9c"}.get(kind, "#8a8f9c")
        self._chat_status.setText(text or "")
        self._chat_status.setStyleSheet(
            "color:%s;font-size:11px;margin-left:22px;" % color)

    def _toggle_chat_key(self):
        if self.chat_key.echoMode() == QLineEdit.EchoMode.Password:
            self.chat_key.setEchoMode(QLineEdit.EchoMode.Normal)
            self._chat_eye_btn.setText("隐藏")
        else:
            self.chat_key.setEchoMode(QLineEdit.EchoMode.Password)
            self._chat_eye_btn.setText("显示")

    def _on_chat_key_changed(self):
        if getattr(self, "_chat_filling", False):
            return
        self._refresh_chat_status()

    def _on_chat_model_changed(self, *_a):
        """切换/手填模型后立即保存。"""
        if getattr(self, "_chat_filling", False):
            return
        if getattr(self, "_chat_testing", False):
            return
        model = (self.chat_model.currentText() or "").strip()
        key = (self.chat_key.text() or "").strip()
        base = (self.chat_base.text() or "").strip()
        if model:
            self.app.cfg["chat_model"] = model[:300]
            if key:
                self.app.cfg["ds_api_key"] = key[:300]
            if base:
                self.app.cfg["chat_base"] = base[:300]
            self.app.save_cfg()
            self._set_chat_status("✓ 模型已保存：%s" % model, "ok")

    def _save_chat_cfg(self):
        """三项一起写入配置。"""
        key = (self.chat_key.text() or "").strip()
        base = (self.chat_base.text() or "").strip()
        model = (self.chat_model.currentText() or "").strip()
        self.app.cfg["ds_api_key"] = key[:300]
        self.app.cfg["chat_base"] = base[:300]
        self.app.cfg["chat_model"] = model[:300]
        self.app.save_cfg()
        self._set_chat_status("✓ 已保存", "ok")
        log("[聊天] 已保存 Key/地址/模型")

    def _clear_chat_cfg(self):
        """清空三项。"""
        self._chat_filling = True
        self.chat_key.clear()
        self.chat_base.clear()
        self.chat_model.blockSignals(True)
        self.chat_model.clear()
        self.chat_model.blockSignals(False)
        self._chat_filling = False
        self.app.cfg["ds_api_key"] = ""
        self.app.cfg["chat_base"] = ""
        self.app.cfg["chat_model"] = ""
        self.app.save_cfg()
        self._set_chat_status(
            "已清空。尚未配置聊天 Key，点「测试」前请先填写", "info")

    def _reset_chat_test_ui(self):
        """复位聊天识别界面状态（完成/失败/取消/隐藏/关闭/超时都走这里）。"""
        self._chat_testing = False
        try:
            self._chat_deadline_timer.stop()
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_reset_chat_test_ui", type(ex).__name__, ex),
                logging.WARNING)
        self._chat_token = getattr(self, "_chat_token", 0) + 1
        try:
            self._chat_test_btn.setEnabled(True)
            self._chat_cancel_btn.hide()
        except Exception as ex:
            log("[设置] 复位聊天识别 UI 失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)


    def _cancel_chat_test(self):
        """取消聊天识别：立刻复位，旧线程结果会被丢弃。"""
        self._reset_chat_test_ui()
        self._set_chat_status("已取消识别", "info")


    def _test_chat(self):
        """点「测试」才识别：服务商/地址 + 可用模型（子线程，硬性总超时 60 秒）。"""
        if getattr(self, "_chat_testing", False):
            return
        key = (self.chat_key.text() or "").strip()
        if not key:
            self._set_chat_status("✗ 请先填写聊天 Key", "err")
            return
        self._chat_testing = True
        self._chat_token = getattr(self, "_chat_token", 0) + 1
        token = self._chat_token
        self._chat_test_btn.setEnabled(False)
        self._chat_cancel_btn.show()
        self._set_chat_status("识别中…（正在探测服务商）", "info")

        def stale():
            return getattr(self, "_chat_token", 0) != token

        # 硬性总超时：到点直接复位 UI 并提示，不等线程 join
        self._chat_deadline_timer = QTimer(self)
        self._chat_deadline_timer.setSingleShot(True)
        self._chat_deadline_timer.timeout.connect(
            lambda t=token: self._on_chat_timeout(t))
        self._chat_deadline_timer.start(60000)

        def log2(m):
            log("[聊天识别] " + str(m))

        def work():
            import time as _t
            t0 = _t.time()
            from petcore import vision as V
            try:
                base, provider, err = V.detect_provider(key, log=log2)
                if stale():
                    return
                if not base:
                    self._cbus.test_done.emit(stale, False, err or "无法识别服务商",
                                              "", [], None)
                    return
                self._cbus.base_found.emit(base, provider, stale)
                if _t.time() - t0 > 60 or stale():
                    return
                ok, models, err2 = V.list_models(base, key, timeout=4)
                if stale():
                    return
                if not ok:
                    models = []
                EXCLUDE = ("embed", "rerank", "whisper", "tts", "asr", "audio",
                           "moderation", "dall", "flux", "video", "image",
                           "speech", "transcri")
                cand = [m for m in (models or [])
                        if not any(w in m.lower() for w in EXCLUDE)]
                try:
                    cand = V.filter_and_rank(cand)
                except Exception as ex:
                    log("[聊天识别] 排序失败: %s: %s"
                        % (type(ex).__name__, ex), logging.WARNING)
                cand = cand[:12]
                if not cand:
                    self._cbus.test_done.emit(stale, False,
                                              "没有可验证的对话模型", base, [], 0)
                    return
                self._cbus.progress.emit(len(cand), stale)

                found = []
                lock = __import__("threading").Lock()
                q = list(cand)
                qi = [0]

                def worker():
                    import requests as _rq
                    from petcore.consts import vision_join
                    while True:
                        with lock:
                            if qi[0] >= len(q):
                                return
                            m = q[qi[0]]
                            qi[0] += 1
                        if stale() or _t.time() - t0 > 60:
                            return
                        try:
                            r = _rq.post(
                                vision_join(base, "chat/completions"),
                                headers={"Authorization": "Bearer " + key,
                                         "Content-Type": "application/json"},
                                json={"model": m,
                                      "messages": [{"role": "user",
                                                    "content": "回复 ok"}],
                                      "max_tokens": 16},
                                timeout=40)
                            if r.status_code == 200:
                                with lock:
                                    if m not in found:
                                        found.append(m)
                                if not stale():
                                    self._cbus.model_found.emit(m, stale)
                        except Exception:
                            continue

                import threading as _th
                ths = [_th.Thread(target=worker, daemon=True) for _ in range(4)]
                for th in ths:
                    th.start()
                for th in ths:
                    th.join()
                if stale():
                    return
                self._cbus.test_done.emit(stale, True, "", base, list(found),
                                          len(cand))
            except Exception as ex:
                self._cbus.test_done.emit(stale, False, str(ex)[:80], "", [], None)

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _on_chat_timeout(self, token):
        """硬性总超时：保留已找到的模型，提示验证被中断。"""
        if getattr(self, "_chat_token", 0) != token:
            return
        found = [self.chat_model.itemText(i) for i in range(self.chat_model.count())]
        found = [m for m in found if m]
        self._reset_chat_test_ui()
        if found:
            self._set_chat_status(
                "⚠ 已找到 %d 个，验证被超时中断" % len(found), "err")
            # 已找到的保留在下拉框，并保存
            try:
                self._save_chat_cfg()
            except Exception as ex:
                log("[聊天识别] 超时后保存失败: %s: %s"
                    % (type(ex).__name__, ex), logging.WARNING)
        else:
            self._set_chat_status("✗ 识别超时（60 秒），未找到可用模型", "err")
        log("[聊天识别] 硬性总超时，保留 %d 个已找到的模型" % len(found))


    def _on_chat_base_found(self, base, provider, stale):
        if stale():
            return
        self.chat_base.setText(base)
        self._set_chat_status("识别中…（服务商：%s，正在取模型列表）" % provider, "info")
        # 探测完成 → 验证阶段重新计 60 秒
        try:
            self._chat_deadline_timer.start(60000)
        except Exception as ex:
            log("[聊天识别] 重启超时定时器失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)


    def _on_chat_model_found(self, model, stale):
        if stale():
            return
        if self.chat_model.findText(model) < 0:
            self.chat_model.blockSignals(True)
            self.chat_model.addItem(model)
            self.chat_model.blockSignals(False)
        self._set_chat_status("识别中…（已验证 %d 个）" % self.chat_model.count(), "info")

    def _on_chat_progress(self, n, stale):
        if stale():
            return
        self._set_chat_status("识别中…（正在验证 %d 个候选模型）" % n, "info")

    def _on_chat_test_done(self, stale, ok, err, base, found, total=None):
        if stale():
            return
        self._chat_testing = False
        self._chat_test_btn.setEnabled(True)
        self._chat_cancel_btn.hide()
        if not ok:
            self._set_chat_status("✗ " + (err or "识别失败"), "err")
            return
        n = len(found or [])
        if n == 0:
            self._set_chat_status("✗ 没有可用的对话模型", "err")
            return
        # 排序后填入
        try:
            from petcore.vision import filter_and_rank
            ranked = filter_and_rank(list(found))
        except Exception:
            ranked = list(found)
        if len(ranked) != n:
            ranked = list(found)
        # 保留用户已选（若仍可用）
        cur = (self.chat_model.currentText() or "").strip()
        chosen = cur if cur in ranked else ranked[0]
        self._chat_filling = True
        self.chat_model.blockSignals(True)
        self.chat_model.clear()
        for m in ranked:
            self.chat_model.addItem(m)
        self.chat_model.setCurrentText(chosen)
        self.chat_model.blockSignals(False)
        self._chat_filling = False
        if base:
            self.chat_base.setText(base)
        self._set_chat_status("✓ 找到 %d 个可用模型，已选 %s" % (n, chosen), "ok")
        self._save_chat_cfg()
        log("[聊天识别] 完成：%d 个可用，已选 %s" % (n, chosen))


    def _vision_provider_name(self):
        """从当前地址判断服务商名（用于选默认模型）。"""
        base = (self.vision_base.text() or "").strip().lower()
        if "bigmodel" in base or "z.ai" in base:
            return "智谱"
        if "deepseek" in base:
            return "DeepSeek"
        return ""


    def _current_vision_model(self):
        """当前下拉/手填的模型名（空就是空，不回落）。"""
        try:
            return (self.vision_model.currentText() or "").strip()
        except Exception as ex:
            log("[设置] 读视觉模型失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            return ""


    def _vision_cfg_dict(self):
        """取 look_screen 配置（缺失时补空）。"""
        ls = self.app.cfg.get("look_screen")
        if not isinstance(ls, dict):
            ls = {}
        for k in ("vision_key", "vision_base", "vision_model"):
            if not isinstance(ls.get(k), str):
                ls[k] = ""
        return ls


    def _style_from_widget(self):
        """从下拉框读当前风格 key（保存时用这个，不能用配置里的旧值）。"""
        try:
            v = self.vision_style.currentData()
            if v in VISION_STYLE_SUFFIXES:
                return v
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_style_from_widget", type(ex).__name__, ex),
                logging.WARNING)
        return self._vision_style_value()

    def _set_vision_save_desc(self, on):
        """保存「是否把屏幕描述写进聊天记录」。"""
        ls = self._vision_cfg_dict()
        ls["save_desc"] = bool(on)
        self.app.cfg["look_screen"] = ls
        self.app.save_cfg()


    def _set_vision_save_shot(self, on):
        """保存截图原图开关：写入 look_screen.save_shot。"""
        ls = self._vision_cfg_dict()
        ls["save_shot"] = bool(on)
        self.app.cfg["look_screen"] = ls
        self.app.save_cfg()
        self._set_vision_status(
            "✓ 已%s把截图原图存进聊天记录" % ("开启" if on else "关闭"), "ok")

    def _on_vision_limit_changed(self, key, value):
        """三项限制（最长边/质量/超时）改动后立即保存。"""
        if getattr(self, "_vision_filling", False):
            return
        ls = self._vision_cfg_dict()
        ls[key] = int(value)
        self.app.cfg["look_screen"] = ls
        self.app.save_cfg()
        self._refresh_advanced_summary()

    def _limits_from_widget(self):
        """从控件读三项限制（保存时用）。"""
        try:
            _s1, _b1 = self._vision_side_field, self.vision_side_spin
            _s2, _b2 = self._vision_quality_field, self.vision_quality_spin
            return (int(round(self.vision_side_spin.value())),
                    int(round(self.vision_quality_spin.value())),
                    int(self.vision_timeout_spin.value()))
        except Exception:
            ls = self._vision_cfg_dict()
            return (int(ls.get("max_side") or LOOK_SCREEN_SIDE_DEFAULT),
                    int(ls.get("quality") or LOOK_SCREEN_QUALITY_DEFAULT),
                    int(ls.get("timeout") or LOOK_SCREEN_TIMEOUT_DEFAULT))


    def _tokens_from_widget(self):
        """从滑块/数字框读当前上限（保存时用这个）。"""
        try:
            _s, box = self.vision_tokens_slider, self.vision_tokens_spin
            v = int(round(box.value()))
            return max(VISION_TOKENS_MIN, min(VISION_TOKENS_MAX, v))
        except Exception:
            return self._vision_tokens_value()


    def _vision_style_value(self):
        """当前配置里的描述风格 key。"""
        ls = self._vision_cfg_dict()
        s = ls.get("style") or VISION_STYLE_DEFAULT
        return s if s in VISION_STYLE_SUFFIXES else VISION_STYLE_DEFAULT

    def _vision_tokens_value(self):
        """当前配置里的视觉单次上限。"""
        ls = self._vision_cfg_dict()
        try:
            v = int(ls.get("max_tokens") or VISION_TOKENS_DEFAULT)
        except Exception:
            v = VISION_TOKENS_DEFAULT
        return max(VISION_TOKENS_MIN, min(VISION_TOKENS_MAX, v))

    def _refresh_vision_model_hint(self):
        """模型控件的悬停提示：未配置 / 已选。"""
        try:
            cur = (self.vision_model.currentText() or "").strip()
            if cur:
                tip = "当前模型：%s（可下拉选择或直接手填）" % cur
            else:
                tip = "尚未选择模型，点「测试」自动识别"
            self.vision_model.setToolTip(tip)
        except Exception as ex:
            log("[设置] 刷新视觉模型提示失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)


    def _refresh_vision_style_hint(self):
        """刷新「描述风格」说明按钮的 tooltip。"""
        try:
            suffix = vision_style_suffix(self._style_from_widget())
            self._vision_style_hint.setToolTip(
                "当前风格下，视觉请求 system 末尾会追加：「%s」" % suffix)
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_refresh_vision_style_hint", type(ex).__name__, ex),
                logging.WARNING)

    def _save_vision_extra(self):
        """把描述风格与单次上限写进配置（独立于聊天页）。"""
        ls = self._vision_cfg_dict()
        ls["style"] = self._style_from_widget()
        ls["max_tokens"] = self._tokens_from_widget()
        _side, _qual, _tout = self._limits_from_widget()
        ls["max_side"] = _side
        ls["quality"] = _qual
        ls["timeout"] = _tout
        ls["vision_key"] = (self.vision_key.text() or "").strip()[:300]
        ls["vision_base"] = (self.vision_base.text() or "").strip()[:300]
        ls["vision_model"] = (self.vision_model.currentText() or "").strip()[:200]
        self.app.cfg["look_screen"] = ls
        self.app.save_cfg()


    def _vision_fill_from_cfg(self):
        """从配置把 Key / 地址 / 模型填进控件（不触发识别、不触发保存）。"""
        ls = self._vision_cfg_dict()
        for w in (self.vision_key, self.vision_base):
            w.blockSignals(True)
        self.vision_key.setText(ls["vision_key"])
        self.vision_base.setText(ls["vision_base"])
        for w in (self.vision_key, self.vision_base):
            w.blockSignals(False)
        # 模型为空就留空（不预填默认值）
        _cur_model = (ls["vision_model"] or "").strip()
        models = [_cur_model] if _cur_model else []
        self._fill_vision_models(models, keep=_cur_model or None)
        # 描述风格与单次上限：独立于聊天页，各自填
        _st = self._vision_style_value()
        self.vision_style.blockSignals(True)
        for _k in range(self.vision_style.count()):
            if self.vision_style.itemData(_k) == _st:
                self.vision_style.setCurrentIndex(_k)
                break
        self.vision_style.blockSignals(False)
        _vs, _vb = (self.vision_tokens_slider, self.vision_tokens_spin)
        _vs.blockSignals(True)
        _vb.blockSignals(True)
        _vs.setValue(int(self._vision_tokens_value()))
        _vb.setValue(float(self._vision_tokens_value()))
        _vs.blockSignals(False)
        _vb.blockSignals(False)
        # 三项限制：最长边 / 质量 / 超时
        _ls2 = self._vision_cfg_dict()
        _side = int(_ls2.get("max_side") or LOOK_SCREEN_SIDE_DEFAULT)
        _qual = int(_ls2.get("quality") or LOOK_SCREEN_QUALITY_DEFAULT)
        _tout = int(_ls2.get("timeout") or LOOK_SCREEN_TIMEOUT_DEFAULT)
        for _sl, _bx, _val in (
                (self.vision_side_slider, self.vision_side_spin, _side),
                (self.vision_quality_slider, self.vision_quality_spin, _qual)):
            _sl.blockSignals(True)
            _bx.blockSignals(True)
            _sl.setValue(_val)
            _bx.setValue(float(_val))
            _sl.blockSignals(False)
            _bx.blockSignals(False)
        self.vision_timeout_spin.blockSignals(True)
        self.vision_timeout_spin.setValue(_tout)
        self.vision_timeout_spin.blockSignals(False)
        self._refresh_vision_style_hint()
        # 识别规则（与请求共用配置里的 vision_system）
        _rule = (ls.get("vision_system") or "").strip() or VISION_SYSTEM_DEFAULT
        self.vision_rule_edit.blockSignals(True)
        self.vision_rule_edit.setPlainText(_rule)
        self.vision_rule_edit.blockSignals(False)
        self._on_vision_rule_edited()
        self._restore_vision_status()

        self._refresh_vision_model_hint()
        self._refresh_vision_summary()

    def _toggle_vision_key(self):
        """显示 / 隐藏 Key。"""
        if self.vision_key.echoMode() == QLineEdit.EchoMode.Password:
            self.vision_key.setEchoMode(QLineEdit.EchoMode.Normal)
            self._eye_btn.setText("隐藏")
        else:
            self.vision_key.setEchoMode(QLineEdit.EchoMode.Password)
            self._eye_btn.setText("显示")

    def _on_vision_key_changed(self, _txt=None):
        """Key 变化：只清空地址与模型，不自动识别。"""
        if getattr(self, "_vision_filling", False):
            return
        self.vision_base.clear()
        self._fill_vision_models([])
        self._set_vision_status("Key 已改，点「测试」重新识别", "info")

    def _fill_vision_models(self, models, keep=None):
        """把模型填进下拉框；keep 在列表里就选中它。不触发保存。"""
        self.vision_model.blockSignals(True)
        self.vision_model.clear()
        for m in models or []:
            if m:
                self.vision_model.addItem(m)
        if keep and keep in (models or []):
            self.vision_model.setCurrentText(keep)
        elif models:
            self.vision_model.setCurrentIndex(0)
        self.vision_model.blockSignals(False)

    def _on_vision_style_changed(self, _idx=None):
        """切换描述风格：立即保存并刷新灰字。"""
        if getattr(self, "_vision_filling", False):
            return
        self._save_vision_extra()
        self._refresh_vision_style_hint()

    def _on_vision_tokens_changed(self, value):
        """改单次上限：立即保存。"""
        if getattr(self, "_vision_filling", False):
            return
        self._save_vision_extra()
        self._refresh_advanced_summary()


    def _on_vision_model_changed(self, *_a):
        """切换/手填模型后立即保存，并刷新默认/自定义提示。"""
        if getattr(self, "_vision_filling", False):
            return
        if getattr(self, "_vision_testing", False):
            return
        model = (self.vision_model.currentText() or "").strip()
        key = (self.vision_key.text() or "").strip()
        base = (self.vision_base.text() or "").strip()
        # 有模型名就存（不强制要求 base/key 已就绪，方便先选模型）
        if model:
            ls = self._vision_cfg_dict()
            ls["vision_model"] = model[:200]
            if key:
                ls["vision_key"] = key[:300]
            if base:
                ls["vision_base"] = base[:300]
            self.app.cfg["look_screen"] = ls
            self.app.save_cfg()
            self._set_vision_status("✓ 模型已保存：%s" % model, "ok")
        self._refresh_vision_model_hint()


    def _restore_vision_status(self):
        """按已保存配置显示状态（不动网络）。"""
        ls = self._vision_cfg_dict()
        key, base, model = ls["vision_key"], ls["vision_base"], ls["vision_model"]
        if not key:
            self._set_vision_status("填入视觉 Key 后点「测试」自动识别", "info")
        elif not (base and model):
            self._set_vision_status("已填 Key，点「测试」识别服务商与模型", "info")
        else:
            self._set_vision_status("已配置：%s @ %s" % (model, base), "ok")

    def _set_vision_status(self, text, kind="info"):
        """状态栏文字 + 颜色。kind: info(灰) / ok(绿) / err(红)"""
        colors = {
            "info": "color:#8a8f9c;font-size:11px;margin-left:22px;",
            "ok": "color:#1a7f37;font-size:11px;margin-left:22px;",
            "err": "color:#c0392b;font-size:11px;margin-left:22px;",
        }
        self._vision_hint.setText(text)
        self._vision_hint.setStyleSheet(colors.get(kind, colors["info"]))

    def _save_vision_cfg(self):
        """保存 Key + 地址 + 模型；地址或模型为空时提示先点测试。"""
        if getattr(self, "_vision_testing", False):
            self._set_vision_status("识别中…，请等结束或点「取消」", "info")
            return False
        key = (self.vision_key.text() or "").strip()
        base = (self.vision_base.text() or "").strip()
        model = (self.vision_model.currentText() or "").strip()
        if not key:
            self._set_vision_status("✗ 请先填写视觉 Key", "err")
            return False
        if not base or not model:
            self._set_vision_status("✗ 请先点「测试」完成识别", "err")
            return False
        ls = self._vision_cfg_dict()
        ls["vision_key"] = key[:300]
        ls["vision_base"] = base[:300]
        ls["vision_model"] = model[:200]
        ls["style"] = self._style_from_widget()
        ls["max_tokens"] = self._tokens_from_widget()
        _side, _qual, _tout = self._limits_from_widget()
        ls["max_side"] = _side
        ls["quality"] = _qual
        ls["timeout"] = _tout
        self.app.cfg["look_screen"] = ls
        self.app.save_cfg()
        self._set_vision_status("✓ 已保存：%s @ %s" % (model, base), "ok")
        self.app.say_main("视觉配置已保存")
        self._refresh_vision_summary()
        return True

    def _reset_vision_test_ui(self):
        """复位识别中的界面状态（完成/失败/取消/隐藏/关闭都走这里）。"""
        self._vision_testing = False
        try:
            self._vision_deadline_timer.stop()
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_reset_vision_test_ui", type(ex).__name__, ex),
                logging.WARNING)
        self._vision_token = getattr(self, "_vision_token", 0) + 1
        try:
            self._vision_test_btn.setEnabled(True)
            self._vision_cancel_btn.hide()
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_reset_vision_test_ui", type(ex).__name__, ex),
                logging.WARNING)

    def _cancel_vision_test(self):
        """取消识别：立刻复位，旧线程结果会被丢弃。"""
        self._reset_vision_test_ui()
        self._set_vision_status("已取消识别", "info")

    def _clear_vision_cfg(self):
        """清空 Key、地址、模型；**不重置**规则/风格/上限/三项限制。"""
        if getattr(self, "_vision_testing", False):
            self._reset_vision_test_ui()
        self._vision_filling = True
        self.vision_key.clear()
        self.vision_base.clear()
        self._fill_vision_models([])
        self._vision_filling = False
        _side, _qual, _tout = self._limits_from_widget()
        self.app.cfg["look_screen"] = {
            "vision_key": "", "vision_base": "", "vision_model": "",
            # 这些不重置（识别规则也不清）
            "vision_system": self._rule_from_widget(),
            "style": self._style_from_widget(),
            "max_tokens": self._tokens_from_widget(),
            "max_side": _side,
            "quality": _qual,
            "timeout": _tout,
        }
        self.app.save_cfg()
        self._set_vision_status("已清空 Key/地址/模型（规则与其余设置保留）", "info")
        self._refresh_vision_summary()


    def _reset_vision_defaults(self):
        """恢复默认：高级设置四项 + 识别规则，一起恢复（不动 Key / 地址 / 模型）。

        原来「识别规则」子折叠里有个独立的「恢复默认」，视觉组底部还有一个，
        两个同名按钮容易点错，现合并成一个。
        """
        ans = QMessageBox.question(
            self, "恢复默认",
            "确定把「高级设置」四项和「识别规则」都恢复成内置默认吗？\n"
            "（不影响视觉 Key / 接口地址 / 模型）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._reset_vision_advanced(ask=False)
        self._reset_vision_rule(ask=False)
        self._set_vision_status(
            "✓ 高级设置与识别规则均已恢复默认（Key / 地址 / 模型保留）", "ok")

    def _reset_vision_advanced(self, ask=True):
        """恢复默认：只把高级设置四项恢复为默认值，**不动 Key / 地址 / 模型**。"""
        ls = self._vision_cfg_dict()
        defaults = {
            "max_tokens": VISION_TOKENS_DEFAULT,        # 800
            "max_side": LOOK_SCREEN_SIDE_DEFAULT,       # 1024
            "quality": LOOK_SCREEN_QUALITY_DEFAULT,     # 85
            "timeout": LOOK_SCREEN_TIMEOUT_DEFAULT,     # 30
        }
        # 1) 写回控件（阻止信号，避免逐项触发保存）
        self.vision_tokens_slider.blockSignals(True)
        self.vision_tokens_spin.blockSignals(True)
        self.vision_tokens_slider.setValue(defaults["max_tokens"])
        self.vision_tokens_spin.setValue(float(defaults["max_tokens"]))
        self.vision_tokens_slider.blockSignals(False)
        self.vision_tokens_spin.blockSignals(False)

        for _sl, _bx, _val in (
                (self.vision_side_slider, self.vision_side_spin, defaults["max_side"]),
                (self.vision_quality_slider, self.vision_quality_spin,
                 defaults["quality"])):
            _sl.blockSignals(True)
            _bx.blockSignals(True)
            _sl.setValue(_val)
            _bx.setValue(float(_val))
            _sl.blockSignals(False)
            _bx.blockSignals(False)

        self.vision_timeout_spin.blockSignals(True)
        self.vision_timeout_spin.setValue(defaults["timeout"])
        self.vision_timeout_spin.blockSignals(False)

        # 2) 写进配置（只改这四项，其余字段原样保留）
        ls.update(defaults)
        self.app.cfg["look_screen"] = ls
        self.app.save_cfg()
        self._refresh_advanced_summary()
        if ask:
            self._set_vision_status(
                "✓ 高级设置已恢复默认（上限 %d · 最长边 %d · 质量 %d · 超时 %d 秒）"
                % (defaults["max_tokens"], defaults["max_side"],
                   defaults["quality"], defaults["timeout"]), "ok")


    def _test_vision(self):
        """识别：服务商 → 模型列表 → 真实图片验证（全程后台线程）。"""
        if getattr(self, "_vision_testing", False):
            return
        key = (self.vision_key.text() or "").strip()
        if not key:
            self._set_vision_status("✗ 请先填写视觉 Key", "err")
            return

        # 记住用户当前选择，识别后若仍在可用列表里就保持
        keep = (self.vision_model.currentText() or "").strip()
        self._vision_testing = True
        self._vision_token = getattr(self, "_vision_token", 0) + 1
        token = self._vision_token

        # 硬性总超时：到点直接复位 UI 并提示，不等线程 join
        self._vision_deadline_timer = QTimer(self)
        self._vision_deadline_timer.setSingleShot(True)
        self._vision_deadline_timer.timeout.connect(
            lambda t=token: self._on_vision_timeout(t))
        self._vision_deadline_timer.start(60000)
        self._vision_test_btn.setEnabled(False)
        self._vision_cancel_btn.show()
        self.vision_base.clear()
        self._fill_vision_models([])
        self._set_vision_status("识别中…（正在试探服务商）", "info")
        self._vision_models_found = []

        import threading

        def stale():
            """本批是否已作废（取消 / 关闭 / 重新测试）"""
            return getattr(self, "_vision_token", 0) != token

        def on_found(model):
            if stale():
                return
            self._vbus.model_found.emit(model, stale)

        def work():
            from petcore import vision as V
            log("===== 视觉识别开始（token=%s）=====" % token)
            base, provider, err = V.detect_provider(key, log=log)
            if stale():
                log("识别已作废，丢弃结果（服务商阶段）")
                return
            if not base:
                self._vbus.test_done.emit(stale, False, err, "", [], None)
                return
            self._vbus.base_found.emit(base, provider, stale)
            ok, models, err2 = V.list_models(base, key)
            if stale():
                log("识别已作废，丢弃结果（模型列表阶段）")
                return
            if not ok:
                # 有些服务商（如智谱）/models 不可用或不列出视觉模型：
                # 不直接失败，改用内置已知列表继续验证
                log("/models 不可用（%s），尝试内置已知列表" % err2)
                models = []
            ranked, from_known = V.suggest_models(provider, models)
            ranked = ranked[:VISION_MAX_VERIFY]
            if from_known:
                log("（/models 无可用视觉模型，改用内置已知列表：%s）" % provider)
            log("候选 %d 个（列出 %d）：%s" % (len(ranked), len(models), ", ".join(ranked[:8])))
            if not ranked:
                self._vbus.test_done.emit(
                    stale, False,
                    "没有可验证的视觉模型（%s）" % (err2 or "列表为空"),
                    base, [], None)
                return
            self._vbus.progress.emit(len(ranked), stale)
            found, _stopped = V.verify_all(base, key, ranked,
                                           on_found=on_found, log=log,
                                           should_stop=stale)
            if stale():
                log("识别已作废，丢弃结果（验证阶段）")
                return
            self._vbus.test_done.emit(stale, True, "", base, found, len(ranked))

        threading.Thread(target=work, daemon=True).start()

    def _on_vision_timeout(self, token):
        """硬性总超时：保留已找到的模型，提示验证被中断。"""
        if getattr(self, "_vision_token", 0) != token:
            return
        found = [self.vision_model.itemText(i) for i in range(self.vision_model.count())]
        found = [m for m in found if m]
        self._reset_vision_test_ui()
        if found:
            self._set_vision_status(
                "⚠ 已找到 %d 个，验证被超时中断" % len(found), "err")
        else:
            self._set_vision_status("✗ 识别超时（60 秒），未找到可用模型", "err")
        log("[视觉识别] 硬性总超时，保留 %d 个已找到的模型" % len(found))


    def _on_vision_progress(self, n, stale):
        if stale():
            return
        self._set_vision_status("识别中…（正在验证 %d 个候选模型）" % n, "info")

    def _on_vision_base_found(self, base, provider, stale):
        if stale():
            return
        self.vision_base.setText(base)
        self._set_vision_status("识别中…（服务商：%s，正在取模型列表）" % provider, "info")
        # 探测完成 → 验证阶段重新计 60 秒
        try:
            self._vision_deadline_timer.start(60000)
        except Exception as ex:
            log("[视觉识别] 重启超时定时器失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)


    def _on_vision_model_found(self, model, stale):
        if stale():
            return
        cur = [self.vision_model.itemText(i) for i in range(self.vision_model.count())]
        if model not in cur:
            self._vision_filling = True
            self.vision_model.blockSignals(True)
            self.vision_model.addItem(model)
            self.vision_model.blockSignals(False)
            self._vision_filling = False
            self._vision_models_found.append(model)
        n = len(getattr(self, "_vision_models_found", []))
        self._set_vision_status("识别中…（已验证可用 %d 个）" % n, "info")

    def _on_vision_test_done(self, stale, ok, err, base, found, total=None):
        """识别结束：按规则重排、复位 UI、自动保存、真实试拍。"""
        if stale():
            log("识别已作废，忽略结果")
            return
        self._reset_vision_test_ui()
        if not ok:
            self._set_vision_status("✗ " + (err or "识别失败"), "err")
            log("===== 视觉识别失败：%s =====" % err)
            return
        raw_found = list(found or [])
        n = len(raw_found)
        if n == 0:
            self._set_vision_status(
                "✗ 这个 Key 下没有可用的视觉模型（已试 %s 个）" % (total or 0), "err")
            return

        # ★ 并行验证的到达顺序是随机的，这里按规则重排一次。
        # 优先用 screen_vision.pick_vision_model（识别 glm-4.6v / glm-5v 等），
        # 失败则退回 petcore.vision.filter_and_rank。
        ranked_found = raw_found
        try:
            import screen_vision as _SV
            ranked_found = _SV.pick_vision_model(list(raw_found))
            # pick_vision_model 会把非视觉模型也保留（-1 分排最后），
            # 这里只保留它认为能用的（分数 >= 0 的排前面部分）
            if not ranked_found:
                ranked_found = list(raw_found)
        except Exception as ex:
            log("[设置] pick_vision_model 失败，退回 filter_and_rank: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            try:
                from petcore.vision import filter_and_rank
                ranked_found = filter_and_rank(raw_found)
            except Exception:
                ranked_found = list(raw_found)
        if len(ranked_found) != n:
            ranked_found = raw_found
        log("验证通过 %d 个，排序后：%s" % (n, ", ".join(ranked_found)))

        # 恢复选择：原选择仍在列表里就用它，否则选第一个（排序后的）
        cur = (self.vision_model.currentText() or "").strip()
        if cur and cur in ranked_found:
            chosen = cur
        else:
            chosen = ranked_found[0]
        self._vision_filling = True
        self._fill_vision_models(ranked_found, keep=chosen)
        self._vision_filling = False
        tip = "，可在上方选择" if n > 1 else ""
        self._set_vision_status("✓ 找到 %d 个可用视觉模型%s" % (n, tip), "ok")
        # 自动保存（不管几个）
        self._save_vision_cfg()
        # 真实试拍：报告耗时 + 输入/输出 tokens
        _vb, _vk, _vm = self.app._vision_cfg()
        self._vision_real_test(base or _vb, _vk or "", chosen, stale)
        log("===== 视觉识别完成：%d 个可用，已选 %s =====" % (n, chosen))


    def _vision_real_test(self, base, key, model, stale):
        # 试拍用独立 token：_on_vision_test_done 已把识别 token +1，
        # 若继续用它会让 stale() 立刻为真、结果被丢弃。
        _rtoken = getattr(self, "_vision_rtoken", 0) + 1
        self._vision_rtoken = _rtoken
        """用当前描述风格、单次上限、超时发一次真实请求，报告耗时与 tokens。"""
        import threading
        import time as _time

        suffix = vision_style_suffix(self._style_from_widget())
        mt = self._tokens_from_widget()
        _side, _qual, _tout = self._limits_from_widget()
        prompt = ("这是一张测试图。请按下面的要求描述它。" + chr(10) + suffix)
        self._set_vision_status(
            "识别完成，正在用当前风格试拍（上限 %d tokens）…" % mt, "info")

        def work():
            from petcore import vision as V
            from petcore.consts import vision_join
            import requests as _rq
            import base64 as _b64
            t0 = _time.time()
            try:
                png = V.make_test_png(64)
                b64 = _b64.b64encode(png).decode("ascii")
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": "data:image/png;base64," + b64}},
                    ]}],
                    "max_tokens": mt,
                }
                r = _rq.post(vision_join(base, "chat/completions"),
                             headers={"Authorization": "Bearer " + key,
                                      "Content-Type": "application/json"},
                             json=payload, timeout=_tout)
                elapsed = _time.time() - t0
                if r.status_code != 200:
                    self._vbus.real_done.emit(
                        stale, False, "HTTP %s" % r.status_code,
                        "", 0, mt, None, "", elapsed, None)
                    return
                d = r.json()
                ch = d["choices"][0]
                msg = ch.get("message") or {}
                text = (msg.get("content") or "").strip()
                reason = (msg.get("reasoning_content") or "").strip()
                finish = ch.get("finish_reason")
                usage = d.get("usage") or {}
                used = usage.get("completion_tokens", 0) or 0
                ptok = usage.get("prompt_tokens")
                self._vbus.real_done.emit(
                    stale, True, "", text, used, mt, finish, reason, elapsed, ptok)
            except Exception as ex:
                elapsed = _time.time() - t0
                self._vbus.real_done.emit(
                    stale, False, str(ex)[:80], "", 0, mt, None, "", elapsed, None)

        threading.Thread(target=work, daemon=True).start()


    def _vision_real_test_done(self, stale, ok, err, text, used, mt,
                               finish=None, reason="", elapsed=None,
                               prompt_tokens=None, _rtoken=None):
        """真实试拍结果：报告耗时、输入/输出 tokens（取不到就只报耗时）。"""
        if _rtoken is not None and getattr(self, "_vision_rtoken", 0) != _rtoken:
            return
        if not ok:
            self._set_vision_status("✗ 试拍失败：%s" % (err or "未知"), "err")
            return
        n = len(text or "")
        t_part = ("耗时 %.1fs" % elapsed) if elapsed else ""
        u_part = ""
        bits = []
        if prompt_tokens is not None:
            bits.append("输入 %d" % prompt_tokens)
        if used:
            bits.append("输出 %d" % used)
        if bits:
            u_part = " / ".join(bits) + " tokens"
        if n == 0 and finish == "length":
            self._set_vision_status(
                "⚠ 模型只输出了思考过程，没给出正文（%s）。请把「单次上限」调大。"
                % (t_part or "耗时未知"), "err")
            return
        if n == 0:
            self._set_vision_status(
                "⚠ 模型没返回内容（%s）。可换模型或调大上限。"
                % (t_part or "耗时未知"), "err")
            return
        parts = [x for x in (t_part, u_part) if x]
        tail = ("（" + "，".join(parts) + "）") if parts else ""
        self._set_vision_status(
            "✓ 识别完成。试拍输出 %d 字%s" % (n, tail), "ok")


    def _refresh_vision_hint(self):
        """兼容旧调用点。"""
        self._restore_vision_status()

    def _save_persona(self):
        """保存人设。空 = 无风格（台词走中性池）。

        只有点「保存人设」才触发生成，且仅在哈希变化时。
        """
        text = (self.persona_edit.toPlainText() or "").strip()
        old = (self.app.cfg.get("persona") or "").strip()
        self.app.cfg["persona"] = text[:4000]
        self.app.save_cfg()
        if text:
            self._persona_hint.setText(
                "已保存。前缀缓存会失效一次，建议新开一个对话。")
        else:
            self._persona_hint.setText("已清空人设：无风格，台词使用中性池。")
        self._refresh_persona_state()
        self.app.say_main("人设已保存")
        self._refresh_chat_hint()
        log("[聊天] 人设已更新（%d 字）" % len(text))

        # 人设为空：不调用模型，状态行显示「未设置人设」，池回落中性
        if not text:
            self._cancel_persona_gen()
            self._refresh_persona_lines_status(force_text="未设置人设，使用默认台词")
            return
        # 哈希一致：直接读缓存，不发起请求
        try:
            from petcore import persona_lines as PL
            if PL.persona_hash(text) == PL.persona_hash(old) and \
                    PL.get_persona_pools(APP_DIR, text):
                self._refresh_persona_lines_status()
                log("[台词] 人设未变，跳过生成")
                return
        except Exception:
            pass
        self._generate_persona_lines(text)

    def _generate_persona_lines(self, persona_text, only_pools=None):
        """后台按人设生成台词池，完成后写缓存并提示。

        only_pools 非空时只生成这些池（「重试失败项」用）。
        """
        import threading
        try:
            from petcore import persona_lines as PL
        except Exception as ex:
            self._persona_hint.setText("生成失败：%s" % ex)
            return
        _b, _k, _m = self.app._chat_cfg()
        if not (_b and _k and _m):
            self._set_persona_status(
                "未配置聊天模型 Key，请先到「聊天模型」填写", "#d93025")
            return

        # 生成中再次保存：取消上一次
        self._cancel_persona_gen()
        self._persona_gen_seq = getattr(self, "_persona_gen_seq", 0) + 1
        seq = self._persona_gen_seq
        self._persona_gen_busy = True
        self._persona_gen_only = list(only_pools or [])
        self._set_persona_status("生成中… 0/%d" % max(1, len(
            only_pools) if only_pools else len(PL.POOL_SPECS)), "#8a8f9c")
        self._refresh_persona_btns()

        def progress(done, total, msg):
            try:
                if seq != getattr(self, "_persona_gen_seq", 0):
                    return
                self._set_persona_status("生成中… %d/%d" % (done, total),
                                         "#8a8f9c")
            except Exception:
                pass

        def cancelled():
            return seq != getattr(self, "_persona_gen_seq", 0)

        def work():
            try:
                res = PL.generate_all(
                    persona_text, (_b, _k, _m), APP_DIR,
                    on_progress=progress, only_pools=only_pools,
                    cancel_check=cancelled)
            except Exception as ex:
                log("[台词] 生成线程异常: %s: %s"
                    % (type(ex).__name__, ex), logging.ERROR)
                res = {"ok": False, "status": "fail",
                       "reason": "生成失败，已使用默认台词",
                       "total": 0, "done": 0, "failed_pools": [], "pools": {}}
            if seq != getattr(self, "_persona_gen_seq", 0):
                return
            try:
                self._persona_gen_result.emit(res)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _cancel_persona_gen(self):
        """让进行中的生成作废（结果会被丢弃）。"""
        self._persona_gen_seq = getattr(self, "_persona_gen_seq", 0) + 1
        self._persona_gen_busy = False

    def _set_persona_status(self, text, color="#8a8f9c"):
        try:
            self._persona_lines_status.setText(text or "")
            self._persona_lines_status.setStyleSheet(
                "color:%s;font-size:11px;" % color)
        except Exception:
            pass

    def _refresh_persona_btns(self):
        busy = bool(getattr(self, "_persona_gen_busy", False))
        for name in ("_persona_regen_btn", "_persona_retry_btn",
                     "_persona_retry_failed_btn", "_persona_reason_btn"):
            b = getattr(self, name, None)
            if b is None:
                continue
            try:
                b.setEnabled(not busy)
            except Exception:
                pass
        failed = list(getattr(self, "_persona_failed_pools", []) or [])
        try:
            self._persona_retry_failed_btn.setVisible(bool(failed))
        except Exception:
            pass
        try:
            self._persona_reason_btn.setVisible(bool(failed))
            if not failed:
                self._persona_reasons.hide()
                self._persona_reason_btn.setChecked(False)
        except Exception:
            pass

    def _toggle_persona_reasons(self):
        """展开/收起失败原因列表。"""
        try:
            show = self._persona_reason_btn.isChecked()
            if not show:
                self._persona_reasons.hide()
                return
            self._persona_reasons.setText(self._persona_reasons_html())
            self._persona_reasons.show()
        except Exception as ex:
            log("[台词] 展开原因失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _persona_reasons_html(self):
        """把失败池渲染成每行一条的纯文本（带池中文名与原因）。"""
        from petcore import persona_lines as PL
        out = []
        for f in (getattr(self, "_persona_failed_pools", []) or []):
            if not isinstance(f, dict):
                continue
            name = f.get("pool") or ""
            label = PL.pool_label(name)
            txt = PL.format_failure(f)
            out.append("✕ %s：%s" % (label, txt))
        return "\n".join(out) if out else ""

    def _on_persona_gen_result(self, res):
        """生成完成（主线程）：更新状态行 + 一次性气泡。"""
        self._persona_gen_busy = False
        status = (res or {}).get("status") or "fail"
        done = int((res or {}).get("done") or 0)
        total = int((res or {}).get("total") or 0)
        failed = list((res or {}).get("failed_pools") or [])
        self._persona_failed_pools = failed
        self._refresh_persona_btns()

        if status == "ok":
            self._set_persona_status(
                "台词已按当前人设生成 · %d 个池 · 刚刚" % done, "#1a7f37")
        elif status == "partial":
            self._set_persona_status(
                "已生成 %d/%d 个池 · %d 个使用默认台词"
                % (done, total, len(failed)), "#d08a00")
        elif status == "nokey":
            self._set_persona_status(
                "未配置聊天模型 Key，请先到「聊天模型」填写", "#d93025")
        elif status == "empty":
            self._set_persona_status("未设置人设，使用默认台词", "#8a8f9c")
        else:
            self._set_persona_status(
                res.get("reason") or "生成失败，已使用默认台词", "#d93025")

        # 失败原因列表若正展开，内容跟着刷新
        try:
            if self._persona_reason_btn.isChecked() and failed:
                self._persona_reasons.setText(self._persona_reasons_html())
                self._persona_reasons.show()
        except Exception:
            pass

        log("[台词] 生成结果：%s done=%d/%d failed=%d"
            % (status, done, total, len(failed)))

        # 一次性气泡：设置窗口开着且停在聊天页时不弹
        if status == "ok":
            msg = "新人设的台词准备好啦"
        elif status == "partial":
            msg = "大部分台词好了，有几个没成功"
        else:
            msg = "台词生成失败了，先用默认的"
        self._maybe_persona_bubble(msg)

    def _maybe_persona_bubble(self, msg):
        """按规则决定是否在桌宠上弹一次气泡（程序固定文字，0 token）。"""
        try:
            if not msg:
                return
            # 设置窗口打开且停在聊天页 → 只更新状态行
            if self.isVisible():
                try:
                    row = self.list.currentRow()
                    if 0 <= row < len(self.PAGES) and \
                            self.PAGES[row][0] == "chat":
                        return
                except Exception:
                    pass
            app = self.app
            # 免打扰 / 小游戏 / 全屏 → 不弹
            try:
                if app.game_active or app._in_dnd() or app._notification_paused():
                    return
            except Exception:
                pass
            pet = app.main_pet
            if pet is None:
                return
            pet.bubble_inner = False
            pet.bubble_text = msg
            pet.bubble_until = pet.t * TICK / 1000.0 + 5.0
            pet.last_line = msg
            pet.update()
        except Exception as ex:
            log("[台词] 气泡提示失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _refresh_persona_lines_status(self, force_text=None):
        """重新打开设置窗口时恢复上次结果。"""
        try:
            from petcore import persona_lines as PL
            from petcore.consts import persona_from_cfg
            p = persona_from_cfg(self.app.cfg)
            if force_text:
                self._set_persona_status(force_text, "#8a8f9c")
                self._persona_failed_pools = []
                self._refresh_persona_btns()
                return
            if not p:
                self._set_persona_status("未设置人设，使用默认台词", "#8a8f9c")
                self._persona_failed_pools = []
                self._refresh_persona_btns()
                return
            lr = PL.load_last_result(APP_DIR, p)
            if not lr:
                self._set_persona_status("尚未生成台词，点「重新生成」", "#8a8f9c")
                self._persona_failed_pools = []
                self._refresh_persona_btns()
                return
            st = lr.get("status")
            failed = list(lr.get("failed_pools") or [])
            self._persona_failed_pools = failed
            if st == "ok":
                n = len(PL.get_persona_pools(APP_DIR, p))
                self._set_persona_status(
                    "台词已按当前人设生成 · %d 个池 · %s"
                    % (n, lr.get("time") or ""), "#1a7f37")
            elif st == "partial":
                self._set_persona_status(
                    "已生成 %d/%d 个池 · %d 个使用默认台词"
                    % (lr.get("done") or 0, lr.get("total") or 0, len(failed)),
                    "#d08a00")
            elif st == "cleared":
                self._set_persona_status("已恢复默认台词", "#8a8f9c")
            else:
                self._set_persona_status(
                    lr.get("reason") or "生成失败，已使用默认台词", "#d93025")
            self._refresh_persona_btns()
        except Exception as ex:
            log("[台词] 恢复状态失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _retry_failed_persona_pools(self):
        """只重试失败的池，成功的池保持不动。"""
        failed = list(getattr(self, "_persona_failed_pools", []) or [])
        names = []
        for f in failed:
            if isinstance(f, dict) and f.get("pool"):
                names.append(f["pool"])
            elif isinstance(f, str):
                names.append(f)          # 兼容旧结构
        if not names:
            return
        text = (self.app.cfg.get("persona") or "").strip()
        if not text:
            return
        self._generate_persona_lines(text, only_pools=names)

    def _reset_persona_lines(self):
        """「恢复默认台词」：清缓存，回落中性池（不调模型）。"""
        try:
            from petcore import persona_lines as PL
            from petcore.consts import persona_from_cfg
            p = persona_from_cfg(self.app.cfg)
            if p:
                PL.clear_persona_pools(APP_DIR, p)
            self._persona_failed_pools = []
            self._set_persona_status("已恢复默认台词", "#8a8f9c")
            self._refresh_persona_btns()
            self.app.say_main("台词已恢复默认")
        except Exception as ex:
            log("[台词] 恢复默认失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _regen_persona_lines(self):
        """手动重新生成：用编辑框里的当前文本（不保存）。"""
        text = (self.persona_edit.toPlainText() or "").strip()
        if not text:
            self._set_persona_status("未设置人设，使用默认台词", "#8a8f9c")
            return
        self._generate_persona_lines(text)

    def _open_lines_dialog(self):
        """打开台词查看/编辑对话框。"""
        try:
            dlg = PersonaLinesDialog(self.app, self)
            dlg.exec()
        except Exception as ex:
            log("[台词] 打开对话框失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            QMessageBox.warning(self, "台词", "打开失败：%s" % ex)

    def _fill_example_persona(self):
        """填入示例人设（不保存，用户自己决定是否保存）。"""
        try:
            self.persona_edit.setPlainText(EXAMPLE_PERSONA)
            self._persona_hint.setText(
                "已填入示例人设。点「保存人设」才会生效。")
            self._refresh_persona_state()
        except Exception as ex:
            log("[聊天] 填入示例人设失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _reset_persona(self):
        """恢复默认 = 清空人设并切回中性池。"""
        self.app.cfg["persona"] = ""
        self.app.save_cfg()
        self.persona_edit.setPlainText("")
        self._persona_hint.setText(
            "已清空人设：无风格，台词使用中性池。")
        self._refresh_persona_state()
        self.app.say_main("人设已清空，台词切回中性池")
        self._refresh_chat_hint()

    def _set_max_tokens(self, value):
        v = max(MAX_TOKENS_MIN, min(MAX_TOKENS_MAX, int(value)))
        self.app.cfg["max_tokens"] = v
        self.app.save_cfg()
        self._refresh_chat_hint()

    def _refresh_chat_hint(self):
        """聊天页已无提示行，保留空方法供旧调用点使用。"""

    def _build_general_page(self, lay):
        self._title(lay, "通用", "生日、城市、开机自启与版本更新。")

        _BTN_W = 100

        # ① 设置生日（页内内联编辑）
        _r1 = QHBoxLayout()
        self._birthday_btn = QPushButton("设置生日")
        self._birthday_btn.setFixedWidth(_BTN_W)
        self._birthday_btn.clicked.connect(self._toggle_birthday_editor)
        _r1.addWidget(self._birthday_btn)
        self._birthday_state_lab = QLabel("")
        self._birthday_state_lab.setStyleSheet("color:#8a8f9c;font-size:11px;")
        _r1.addWidget(self._birthday_state_lab)
        _r1.addStretch(1)
        lay.addLayout(_r1)

        # 生日内联编辑区（默认隐藏，与按钮左对齐，上下各留 8px）
        self._birthday_editor = QWidget()
        _be = QHBoxLayout(self._birthday_editor)
        _be.setContentsMargins(0, 8, 0, 8)
        _be.setSpacing(8)
        _BD_H = 28
        self._bd_month = QSpinBox()
        self._bd_month.setRange(1, 12)
        self._bd_month.setSuffix(" 月")
        self._bd_month.setFixedWidth(84)      # 保证「12 月」完整显示
        self._bd_month.setFixedHeight(_BD_H)
        self._bd_month.setAlignment(Qt.AlignmentFlag.AlignRight
                                    | Qt.AlignmentFlag.AlignVCenter)
        self._bd_month.valueChanged.connect(self._on_bd_month_changed)
        _be.addWidget(self._bd_month)
        self._bd_day = QSpinBox()
        self._bd_day.setRange(1, 31)
        self._bd_day.setSuffix(" 日")         # 与月对称
        self._bd_day.setFixedWidth(84)        # 保证「12 日」完整显示
        self._bd_day.setFixedHeight(_BD_H)
        self._bd_day.setAlignment(Qt.AlignmentFlag.AlignRight
                                  | Qt.AlignmentFlag.AlignVCenter)
        _be.addWidget(self._bd_day)
        _bd_save = QPushButton("保存")
        _bd_save.setFixedWidth(64)
        _bd_save.setFixedHeight(_BD_H)        # 与数字框等高
        _bd_save.clicked.connect(self._save_birthday_inline)
        _be.addWidget(_bd_save)
        _bd_cancel = QPushButton("取消")
        _bd_cancel.setFixedWidth(64)
        _bd_cancel.setFixedHeight(_BD_H)      # 与数字框等高
        _bd_cancel.clicked.connect(self._hide_birthday_editor)
        _be.addWidget(_bd_cancel)
        _be.addStretch(1)
        self._birthday_editor.hide()
        # 键盘跳转：月在满两位或首位 2-9 时跳日；月回车跳日，日回车保存
        self._bd_month.lineEdit().textEdited.connect(self._on_bd_month_typed)
        self._bd_month.lineEdit().returnPressed.connect(self._focus_bd_day)
        self._bd_day.lineEdit().returnPressed.connect(self._save_birthday_inline)
        lay.addWidget(self._birthday_editor)

        # ② 设置城市
        _r2 = QHBoxLayout()
        b2 = QPushButton("设置城市")
        b2.setFixedWidth(_BTN_W)
        b2.clicked.connect(self._open_city_dialog)
        _r2.addWidget(b2)
        self._city_state_lab = QLabel("")
        self._city_state_lab.setStyleSheet("color:#8a8f9c;font-size:11px;")
        _r2.addWidget(self._city_state_lab)
        _r2.addStretch(1)
        lay.addLayout(_r2)

        # ---- 分隔线 ----
        lay.addWidget(self._hsep())

        # ③ 开机自启
        cfg = self.app.cfg
        self._check(lay, "开机自启", bool(cfg.get("autostart", False)),
                    lambda on: self.app.set_autostart(on))

        # ---- 分隔线 ----
        lay.addWidget(self._hsep())

        # ④ 检查更新
        _r4 = QHBoxLayout()
        self._update_btn = QPushButton("检查更新")
        self._update_btn.setFixedWidth(_BTN_W)
        self._update_btn.clicked.connect(self._check_update)
        _r4.addWidget(self._update_btn)
        self._version_lab = QLabel("")
        self._version_lab.setStyleSheet("color:#8a8f9c;font-size:11px;")
        _r4.addWidget(self._version_lab)
        _r4.addStretch(1)
        lay.addLayout(_r4)
        # 结果显示在按钮下方一行
        self._update_result = QLabel("")
        self._update_result.setWordWrap(True)
        self._update_result.setStyleSheet("color:#8a8f9c;font-size:11px;")
        lay.addWidget(self._update_result)
        # 「前往下载」链接（默认隐藏，有新版本时才显示）
        self._update_link = QLabel("")
        self._update_link.setOpenExternalLinks(False)
        self._update_link.setStyleSheet("font-size:11px;")
        self._update_link.linkActivated.connect(self._on_update_link)
        self._update_link.hide()
        lay.addWidget(self._update_link)

        lay.addStretch(1)
        self._refresh_general_values()

    def _hsep(self):
        """通用页用的分隔线。"""
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        return line

    # ---- 生日：页内内联编辑 ----
    def _toggle_birthday_editor(self):
        """展开/收起生日编辑区，展开时用当前值填好并聚焦月。"""
        if self._birthday_editor.isVisible():
            self._hide_birthday_editor()
            return
        self._fill_birthday_editor()
        self._birthday_editor.setVisible(True)
        # 自动聚焦月并全选
        try:
            self._bd_month.setFocus(Qt.FocusReason.OtherFocusReason)
            self._bd_month.lineEdit().selectAll()
        except Exception:
            pass

    def _hide_birthday_editor(self):
        self._birthday_editor.setVisible(False)

    def _fill_birthday_editor(self):
        """把配置里的生日解析进月/日控件（旧格式带年份时只取月和日）。"""
        raw = (self.app.cfg.get("birthday") or "").strip()
        _y, m, d = self._parse_birthday(raw)
        m = m if m and 1 <= m <= 12 else 1
        self._bd_month.blockSignals(True)
        self._bd_month.setValue(m)
        self._bd_month.blockSignals(False)
        # 先按月份夹好日范围，再填日
        self._apply_day_range(keep=d)

    def _days_in_month(self, month):
        """该月天数（2 月 29，4/6/9/11 月 30，其余 31）。"""
        try:
            m = int(month)
        except Exception:
            m = 1
        if m in (1, 3, 5, 7, 8, 10, 12):
            return 31
        if m in (4, 6, 9, 11):
            return 30
        return 29

    def _apply_day_range(self, keep=None):
        """日范围随月份联动；超出新范围时自动夹到上限（不弹错误）。"""
        try:
            if keep is None:
                keep = self._bd_day.value()
            n = self._days_in_month(self._bd_month.value())
            self._bd_day.blockSignals(True)
            self._bd_day.setRange(1, n)
            self._bd_day.setValue(min(max(1, int(keep or 1)), n))
            self._bd_day.blockSignals(False)
        except Exception as ex:
            log("[设置] 同步生日日数失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _on_bd_month_changed(self, _v):
        self._apply_day_range()

    def _on_bd_month_typed(self, text):
        """月输入满两位，或首位输入 2-9 → 自动跳到日。"""
        t = (text or "").strip()
        if not t:
            return
        if len(t) >= 2 or (len(t) == 1 and t[0] in "23456789"):
            self._focus_bd_day()

    def _focus_bd_day(self):
        try:
            self._bd_day.setFocus(Qt.FocusReason.OtherFocusReason)
            self._bd_day.lineEdit().selectAll()
        except Exception:
            pass

    def _save_birthday_inline(self):
        """保存生日（只存月日，不存年份）。存储格式 MM-DD，与旧读取方兼容。"""
        try:
            m = int(self._bd_month.value())
            d = int(self._bd_day.value())
            text = "%02d-%02d" % (m, d)
            self.app.cfg["birthday"] = text
            self.app.save_cfg()
            self._refresh_general_values()
            self._hide_birthday_editor()
            self.app.say_main("生日记住啦！到那天我会准时说生日快乐")
        except Exception as ex:
            log("[设置] 保存生日失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def keyPressEvent(self, e):
        """生日编辑区打开时，Esc 收起。"""
        try:
            if (e.key() == Qt.Key.Key_Escape
                    and getattr(self, "_birthday_editor", None) is not None
                    and self._birthday_editor.isVisible()):
                self._hide_birthday_editor()
                e.accept()
                return
        except Exception:
            pass
        super().keyPressEvent(e)

    # ---- 城市：子对话框（父窗口 = 设置窗口）----
    def _open_city_dialog(self):
        """设置城市：子对话框，居中于设置窗口，中文按钮。"""
        try:
            cur = self.app.cfg.get("city", "汕头")
            dlg = _CityDialog(self, cur)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                city = (dlg.edit.text() or "").strip()
                self.app.cfg["city"] = city
                self.app.save_cfg()
                self._refresh_general_values()
                self.app.say_main("城市已设置为%s" % city)
        except Exception as ex:
            log("[设置] 打开城市对话框失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    # ---- 检查更新（只在点击时联网，0 token）----
    def _check_update(self):
        """检查 GitHub Releases latest，比较 tag 与 APP_VERSION。"""
        if getattr(self, "_updating", False):
            return
        self._updating = True
        self._update_btn.setEnabled(False)
        self._update_btn.setText("检查中…")
        self._update_result.setText("")
        self._update_result.setStyleSheet("color:#8a8f9c;font-size:11px;")

        def work():
            tag = page = err = ""
            try:
                import requests
                r = requests.get(
                    UPDATE_LATEST_API,
                    headers={"User-Agent": "dafeiyu-pet/%s" % APP_VERSION,
                             "Accept": "application/vnd.github+json"},
                    timeout=UPDATE_TIMEOUT_SEC)
                if r.status_code == 200:
                    d = r.json() or {}
                    tag = (d.get("tag_name") or "").strip()
                    page = (d.get("html_url") or "").strip()
                else:
                    err = "HTTP %d" % r.status_code
            except Exception as ex:
                err = "%s: %s" % (type(ex).__name__, ex)
            try:
                self._update_done.emit(tag, page, err)
            except Exception:
                pass

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _on_update_done(self, tag, page, err):
        """检查更新结果回主线程。"""
        self._updating = False
        try:
            self._update_btn.setEnabled(True)
            self._update_btn.setText("检查更新")
        except Exception:
            pass
        self._update_link.hide()
        if err or not tag:
            self._update_result.setText("检查失败，请稍后再试")
            self._update_result.setStyleSheet("color:#d93025;font-size:11px;")
            return
        if self._version_newer(tag, APP_VERSION):
            self._update_result.setText("发现新版本 %s" % tag)
            self._update_result.setStyleSheet("color:#1a7f37;font-size:11px;")
            self._update_url = page or UPDATE_RELEASES_PAGE
            self._update_link.setText(
                '<a href="%s">前往下载</a>' % self._update_url)
            self._update_link.show()
        else:
            self._update_result.setText("已是最新版本")
            self._update_result.setStyleSheet("color:#8a8f9c;font-size:11px;")

    def _on_update_link(self, url):
        """点「前往下载」：用默认浏览器打开发布页。"""
        self._open_url(url or getattr(self, "_update_url", UPDATE_RELEASES_PAGE))

    @staticmethod
    def _version_tuple(v):
        """'v1.2.0' → (1, 2, 0)；无法解析返回 ()。"""
        try:
            s = (v or "").strip().lstrip("vV")
            parts = [int(x) for x in re.findall(r"\d+", s)[:4]]
            return tuple(parts)
        except Exception:
            return ()

    def _version_newer(self, remote, local):
        """remote 是否比 local 新。"""
        a, b = self._version_tuple(remote), self._version_tuple(local)
        if not a or not b:
            return False
        n = max(len(a), len(b))
        a = a + (0,) * (n - len(a))
        b = b + (0,) * (n - len(b))
        return a > b

    def _open_url(self, url):
        try:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(url))
        except Exception as ex:
            log("[设置] 打开链接失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _refresh_general_values(self):
        """通用页当前值：生日、城市、版本号。"""
        try:
            self._birthday_state_lab.setText(self._birthday_display())
        except Exception:
            self._birthday_state_lab.setText("未设置")
        try:
            city = (self.app.cfg.get("city") or "").strip()
            self._city_state_lab.setText(city if city else "未设置")
        except Exception:
            self._city_state_lab.setText("未设置")
        try:
            self._version_lab.setText(APP_VERSION)
        except Exception:
            pass

    def _birthday_display(self):
        """生日显示成「3 月 12 日」（旧配置带年份时只取月和日）。"""
        raw = (self.app.cfg.get("birthday") or "").strip()
        if not raw:
            return "未设置"
        _y, m, d = self._parse_birthday(raw)
        if m is None:
            return raw          # 认不出的旧格式原样显示，不丢数据
        return "%d 月 %d 日" % (m, d)

    # 生日解析：带年份 / 只有月日 两种格式分开匹配
    _BD_FULL_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
    _BD_MD_RE = re.compile(r"^(\d{1,2})[-/.](\d{1,2})$")

    def _parse_birthday(self, raw):
        """解析生日字符串 → (year 或 None, month 或 None, day 或 None)。"""
        s = (raw or "").strip()
        if not s:
            return None, None, None
        m = self._BD_FULL_RE.match(s)
        if m:
            return m.group(1), int(m.group(2)), int(m.group(3))
        m = self._BD_MD_RE.match(s)
        if m:
            return None, int(m.group(1)), int(m.group(2))
        return None, None, None

    # ---------- 语音页 ----------
    def _build_voice_page(self, lay):
        self._title(lay, "语音", "叫它唱歌或念出来时才会出声，日常聊天不出声。\n"
                    "使用前双击 tools\\启动语音服务.bat，看到 Uvicorn running 即就绪。")
        cfg = self.app.voice_cfg()

        self._voice_enabled = self._check(
            lay, "启用语音", bool(cfg.get("enabled")),
            lambda on: self._set_voice({"enabled": bool(on)}),
            "关闭时完全不请求语音服务，只显示气泡。")

        # 引擎选择
        erow = QHBoxLayout()
        erow.setContentsMargins(22, 0, 0, 0)
        elab = QLabel("合成引擎")
        elab.setFixedWidth(76)
        erow.addWidget(elab)
        self._voice_engine = QComboBox()
        self._voice_engine.addItem("Edge-TTS + RVC（快，约 2 秒）", "edge")
        self._voice_engine.addItem(
            "VoxCPM2（慢，约 3 分钟，48kHz 高音质）", "voxcpm")
        self._voice_engine.addItem(
            "GPT-SoVITS / yin1（离线直出，CPU 约为音频时长 1.5 倍）", "yin1")
        _eng = cfg.get("engine") or "edge"
        for i in range(self._voice_engine.count()):
            if self._voice_engine.itemData(i) == _eng:
                self._voice_engine.setCurrentIndex(i)
                break
        self._voice_engine.currentIndexChanged.connect(
            lambda _i: self._set_voice({
                "engine": self._voice_engine.currentData()}))
        self._voice_engine.setFixedHeight(30)
        erow.addWidget(self._voice_engine, 1)
        lay.addLayout(erow)
        lay.addWidget(self._sub_hint(
            "Edge：快，普通音色。VoxCPM2：音质好，能克隆音色。"
            "但 CPU 下一句话要几分钟；\n"
            "GPT-SoVITS(yin1) 离线直出、音色稳定，CPU 下约为音频时长的 1.5 倍，"
            "不需要 Edge-TTS 也不联网。"))

        # 服务地址
        row = QHBoxLayout()
        row.setContentsMargins(22, 0, 0, 0)
        lab = QLabel("服务地址")
        lab.setFixedWidth(76)
        row.addWidget(lab)
        self._voice_url = QLineEdit(str(cfg.get("url") or ""))
        self._voice_url.setFixedHeight(30)
        self._voice_url.editingFinished.connect(
            lambda: self._set_voice({"url": self._voice_url.text().strip()}))
        row.addWidget(self._voice_url, 1)
        lay.addLayout(row)
        lay.addWidget(self._sub_hint(
            "默认 127.0.0.1:9880。服务在别的机器上就填那台的 IP。"))

        # GPT-SoVITS（yin1）服务地址：仅当引擎选 yin1 时使用
        grow = QHBoxLayout()
        grow.setContentsMargins(22, 0, 0, 0)
        glab = QLabel("GPT 地址")
        glab.setFixedWidth(76)
        grow.addWidget(glab)
        self._voice_gpt_url = QLineEdit(str(cfg.get("gpt_url") or ""))
        self._voice_gpt_url.setFixedHeight(30)
        self._voice_gpt_url.editingFinished.connect(
            lambda: self._set_voice(
                {"gpt_url": self._voice_gpt_url.text().strip()}))
        grow.addWidget(self._voice_gpt_url, 1)
        lay.addLayout(grow)
        lay.addWidget(self._sub_hint(
            "默认 127.0.0.1:9882。GPT-SoVITS 专用。"
            "只在引擎选它时生效；本机离线推理，不需要 Edge-TTS。"))

        # 音量
        vrow = QHBoxLayout()
        vrow.setContentsMargins(22, 0, 0, 0)
        vlab = QLabel("音量")
        vlab.setFixedWidth(76)
        vrow.addWidget(vlab)
        self._voice_vol = QSlider(Qt.Orientation.Horizontal)
        self._voice_vol.setRange(0, 100)
        self._voice_vol.setValue(int(cfg.get("volume") or 80))
        self._voice_vol.setMinimumWidth(200)
        vrow.addWidget(self._voice_vol, 1)
        self._voice_vol_box = QSpinBox()
        self._voice_vol_box.setRange(0, 100)
        self._voice_vol_box.setValue(int(cfg.get("volume") or 80))
        self._voice_vol_box.setFixedWidth(80)
        vrow.addWidget(self._voice_vol_box)
        lay.addLayout(vrow)
        lay.addWidget(self._sub_hint("0 静音，100 最大。"))

        def from_slider(v):
            if self._voice_vol_box.value() != v:
                self._voice_vol_box.blockSignals(True)
                self._voice_vol_box.setValue(int(v))
                self._voice_vol_box.blockSignals(False)
            self._set_voice({"volume": int(v)})

        def from_box(v):
            if self._voice_vol.value() != v:
                self._voice_vol.blockSignals(True)
                self._voice_vol.setValue(int(v))
                self._voice_vol.blockSignals(False)
            self._set_voice({"volume": int(v)})

        self._voice_vol.valueChanged.connect(from_slider)
        self._voice_vol_box.valueChanged.connect(from_box)

        # 服务管理 + 测试
        lay.addSpacing(8)
        trow = QHBoxLayout()
        trow.setContentsMargins(22, 0, 0, 0)
        self._voice_start_btn = QPushButton("启动语音服务")
        self._voice_start_btn.setToolTip(
            "按当前引擎拉起对应的本机语音服务（独立窗口）。"
            "启动后语音功能才能用。\n"
            "edge → RVC(9880)；voxcpm → VoxCPM2(9881)；"
            "yin1 → GPT-SoVITS(9882)。")
        self._voice_start_btn.clicked.connect(self._voice_start_server)
        trow.addWidget(self._voice_start_btn)
        self._voice_test_btn = QPushButton("测试语音")
        self._voice_test_btn.setToolTip("让宠物念一句测试台词")
        self._voice_test_btn.clicked.connect(self._voice_test)
        trow.addWidget(self._voice_test_btn)
        trow.addStretch(1)
        lay.addLayout(trow)

        self._voice_status = QLabel("")
        self._voice_status.setWordWrap(True)
        self._voice_status.setStyleSheet(
            "color:#8a8f9c;font-size:11px;margin-left:22px;")
        lay.addWidget(self._voice_status)
        self._refresh_voice_status()
        lay.addStretch(1)
        # 测试结果从子线程切回主线程
        try:
            self._voice_test_done.connect(self._on_voice_test_done)
        except Exception:
            pass

    def _set_voice(self, patch):
        try:
            self.app.save_voice_cfg(patch)
            self._refresh_voice_status()
        except Exception as ex:
            log("[设置] 保存语音配置失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _refresh_voice_status(self):
        try:
            cfg = self.app.voice_cfg()
            if not cfg.get("enabled"):
                self._voice_status.setText("未启用")
                return
            _eng = cfg.get("engine") or "edge"
            if _eng == "yin1":
                _addr = cfg.get("gpt_url")
            elif _eng == "voxcpm":
                _addr = cfg.get("vox_url")
            else:
                _addr = cfg.get("url")
            self._voice_status.setText(
                "已启用 · %s · %s · 音量 %d%%"
                % (_eng, _addr, cfg.get("volume", 80)))
        except Exception:
            pass

    def _voice_start_server(self):
        """启动语音服务：子线程拉起 + 轮询，结果写状态栏。

        按当前引擎选对应服务，避免选 yin1 却去拉 RVC。
        """
        btn = getattr(self, "_voice_start_btn", None)
        if btn is not None:
            btn.setEnabled(False)
        _eng = (self.app.voice_cfg().get("engine") or "edge")
        self._voice_status.setText("…正在启动语音服务（首次需加载模型）")

        import threading
        import pet_voice as V

        result = {}
        cfg = self.app.voice_cfg()

        def work():
            try:
                if _eng == "yin1":
                    ok, msg = V.start_gpt_server(
                        cfg.get("gpt_url") or "http://127.0.0.1:9882",
                        wait=60)
                elif _eng == "voxcpm":
                    ok, msg = V.start_vox_server(
                        cfg.get("vox_url") or "http://127.0.0.1:9881",
                        wait=180)
                else:
                    ok, msg = V.start_server(
                        cfg.get("url") or "http://127.0.0.1:9880",
                        wait=40)
                result["ok"] = ok
                result["msg"] = msg
            except Exception as ex:
                result["ok"] = False
                result["msg"] = "%s: %s" % (type(ex).__name__, ex)
            # 轮询标志位（QTimer 只能在主线程用）
            self._voice_srv_result = result

        threading.Thread(target=work, daemon=True).start()

        # 用现有测试结果信号轮询（1 秒一次，直到拿到结果）
        self._voice_srv_poll = self.startTimer(1000)

    def timerEvent(self, ev):
        """轮询语音服务启动结果。"""
        res = getattr(self, "_voice_srv_result", None)
        if res is not None:
            self.killTimer(self._voice_srv_poll)
            self._voice_srv_result = None
            btn = getattr(self, "_voice_start_btn", None)
            if btn is not None:
                btn.setEnabled(True)
            if res.get("ok"):
                self._voice_status.setText(
                    "✓ %s——现在可以点「测试语音」了" % res.get("msg"))
            else:
                self._voice_status.setText("✗ %s" % res.get("msg"))
        super().timerEvent(ev)

    def _voice_test(self):
        """测试语音：子线程走完整链路，成功则真的播放，失败给明确原因。

        按当前引擎选对应服务，避免选 yin1 却去测 RVC。
        """
        cfg = self.app.voice_cfg()
        if not cfg.get("enabled"):
            self._voice_status.setText("先勾选「启用语音」再测试")
            return
        _eng = cfg.get("engine") or "edge"
        if _eng == "yin1":
            url = (cfg.get("gpt_url") or "").strip()
            wait_hint = "首次约 20 秒"
        elif _eng == "voxcpm":
            url = (cfg.get("vox_url") or "").strip()
            wait_hint = "VoxCPM 较慢，可能要几分钟"
        else:
            url = (cfg.get("url") or "").strip()
            wait_hint = "首次约 12 秒"
        if not url:
            self._voice_status.setText("✗ 请先填服务地址")
            return
        btn = getattr(self, "_voice_test_btn", None)
        if btn is not None:
            btn.setEnabled(False)
        self._voice_status.setText("…正在生成（%s，请稍等）" % wait_hint)

        import threading

        def work():
            import pet_voice as V
            t0 = time.time()
            # 走当前引擎的合成路径，与日常使用完全一致
            out, err = V.synthesize(cfg, "哼，笨蛋主人，听见了吗？")
            dt = time.time() - t0
            self._voice_test_done.emit(out, err, dt)

        threading.Thread(target=work, daemon=True).start()

    def _on_voice_test_done(self, out, err, dt):
        """测试结果回主线程：成功播放，失败显示原因。"""
        btn = getattr(self, "_voice_test_btn", None)
        if btn is not None:
            btn.setEnabled(True)
        if err:
            hint = ""
            if "refused" in err or "ConnectionError" in err:
                hint = "语音服务没启动？请运行 tools\\启动语音服务.bat"
            elif "edge-tts" in err or "bing" in err:
                hint = "Edge-TTS 联网失败，检查网络/代理"
            # 错误本体可能已带提示，不重复拼接
            if hint and hint not in err:
                err = "%s（%s）" % (err.split("（")[0].rstrip("：: "), hint)
            self._voice_status.setText("✗ 失败：%s" % err[:140])
            return
        try:
            self.app._voice_player._play_bytes(
                out, self.app.voice_cfg().get("volume", 80))
            self._voice_status.setText(
                "✓ 成功（%.1f 秒，%d 字节）——应该能听到声音了" % (dt, len(out)))
        except Exception as ex:
            self._voice_status.setText("✓ 生成成功，但播放失败：%s" % ex)

    # ---------- 主动搭话页 ----------
    def _build_proactive_page(self, lay):
        self._title(lay, "主动搭话")
        cfg = self.app.proactive_cfg()

        # ================= 顶部：开关 + 状态摘要 =================
        _top = QHBoxLayout()
        _top.setSpacing(10)
        self._pro_enable_box = QCheckBox("启用主动搭话")
        self._pro_enable_box.setChecked(bool(cfg.get("enabled")))
        self._pro_enable_box.toggled.connect(
            lambda on: self._pro_save({"enabled": bool(on)}))
        _top.addWidget(self._pro_enable_box)
        self._pro_status = QLabel("")
        self._pro_status.setStyleSheet("color:#8a8f9c;font-size:11px;")
        _top.addWidget(self._pro_status)
        _top.addStretch(1)
        lay.addLayout(_top)
        lay.addWidget(self._sub_hint(
            "工作时间段内发现你发呆，宠物会主动说一句。"
            "关闭后不检测、不调用模型"))

        # ================= 工作时间段 =================
        self._pro_line(lay)
        _sec = self._pro_section(lay, "工作时间段", expanded=True)
        self._pro_sessions_host = QWidget()
        self._pro_sessions_lay = QVBoxLayout(self._pro_sessions_host)
        self._pro_sessions_lay.setContentsMargins(0, 0, 0, 0)
        self._pro_sessions_lay.setSpacing(6)
        _sec.body_add(self._pro_sessions_host)
        self._pro_render_sessions(cfg.get("sessions") or [])
        _btn = QPushButton("添加时间段")
        _btn.clicked.connect(self._pro_add_session)
        _sec.body_add(_btn)
        _sec.body_add(self._sub_hint(
            "只在选中的星期、选中的时段内才检测。可加多段。"))

        # ================= 触发与频率 =================
        self._pro_line(lay)
        _sec2 = self._pro_section(lay, "触发与频率", expanded=True)
        self._pro_idle, self._pro_away = self._pro_threshold_pair(_sec2, cfg)
        _sec2.body_add(self._sub_hint(
            "多久没动算发呆（实际有 ±20% 浮动）"))
        # 原「频率控制」控件原样放在下方
        _sec2.body_add(self._pro_sub_title("冷却与上限"))
        self._pro_cool = self._pro_spin_row(
            _sec2, "冷却时间", int(cfg.get("cooldown_min")), 1, 180, "分钟",
            "两次搭话之间至少隔多久", "cooldown_min")
        self._pro_daily = self._pro_spin_row(
            _sec2, "每日上限", int(cfg.get("daily_max")), 1, 30, "次",
            "一天最多主动几次，跨天自动重置", "daily_max")
        # 掷骰概率：满足条件后实际去问模型的概率，不中就完全不花 token
        self._pro_fire_prob = self._pro_pct_row(
            _sec2, "触发概率", float(cfg.get("fire_prob", 0.65)),
            "满足所有条件后再掷一次骰子；没中就直接跳过，不调用模型、不花 token。"
            "调低更省钱，调高更爱说话。")

        # ================= 内容（三个标签页）=================
        self._pro_line(lay)
        _sec3 = self._pro_section(lay, "内容", expanded=False)
        _tabs = QTabWidget()
        _tabs.setDocumentMode(True)

        # ① 搭话提示词
        _p1 = QWidget()
        _l1 = QVBoxLayout(_p1)
        _l1.setContentsMargins(8, 8, 8, 8)
        _l1.setSpacing(6)
        _l1.addWidget(self._sub_hint(
            "只写场景规则，说话风格由「聊天」页的人设决定"))
        self._pro_prompt = QTextEdit()
        self._pro_prompt.setMinimumHeight(110)
        self._pro_prompt.setPlainText(cfg.get("prompt") or "")
        self._pro_prompt.textChanged.connect(self._pro_on_prompt_edited)
        _l1.addWidget(self._pro_prompt)
        _prow = QHBoxLayout()
        _pb = QPushButton("恢复默认提示词")
        _pb.clicked.connect(self._pro_reset_prompt)
        _prow.addWidget(_pb)
        _prow.addStretch(1)
        _l1.addLayout(_prow)
        _l1.addStretch(1)
        _tabs.addTab(_p1, "搭话提示词")

        # ② 回来打招呼
        _p2 = QWidget()
        _l2 = QVBoxLayout(_p2)
        _l2.setContentsMargins(8, 8, 8, 8)
        _l2.setSpacing(6)
        self._pro_welcome = QCheckBox("从离开回来时打一声招呼")
        self._pro_welcome.setChecked(bool(cfg.get("welcome_enabled")))
        self._pro_welcome.toggled.connect(
            lambda on: self._pro_save({"welcome_enabled": bool(on)}))
        _l2.addWidget(self._pro_welcome)
        _l2.addWidget(self._sub_hint(
            "只调一次模型，同样受冷却与每日上限约束。"
            "说话风格由「聊天」页的人设决定"))
        self._pro_welcome_prompt = QTextEdit()
        self._pro_welcome_prompt.setMinimumHeight(72)
        self._pro_welcome_prompt.setPlainText(cfg.get("welcome_prompt") or "")
        self._pro_welcome_prompt.textChanged.connect(
            lambda: self._pro_save({"welcome_prompt":
                                    self._pro_welcome_prompt.toPlainText()}))
        _l2.addWidget(self._pro_welcome_prompt)
        _wrow = QHBoxLayout()
        _wb = QPushButton("恢复默认欢迎语")
        _wb.clicked.connect(self._pro_reset_welcome)
        _wrow.addWidget(_wb)
        _wrow.addStretch(1)
        _l2.addLayout(_wrow)
        _l2.addStretch(1)
        _tabs.addTab(_p2, "回来打招呼")

        # ③ 隐私与过滤
        _p3 = QWidget()
        _l3 = QVBoxLayout(_p3)
        _l3.setContentsMargins(8, 8, 8, 8)
        _l3.setSpacing(6)
        self._pro_send_title = QCheckBox("把前台窗口标题发给模型")
        self._pro_send_title.setChecked(bool(cfg.get("send_title")))
        self._pro_send_title.toggled.connect(
            lambda on: self._pro_save({"send_title": bool(on)}))
        _l3.addWidget(self._pro_send_title)
        _l3.addWidget(self._sub_hint("关闭后只发进程名，不发标题。"))
        _l3.addWidget(self._pro_sub_title("隐私黑名单"))
        _l3.addWidget(self._sub_hint("这些词的窗口不搭话，标题也不外发。"))
        self._pro_private = self._pro_word_editor(_l3, cfg.get("private_words") or [],
                                                 self._pro_set_private)
        _l3.addWidget(self._pro_sub_title("跳过关键词"))
        _l3.addWidget(self._sub_hint(
            "这些场景不搭话，也不调模型。"))
        self._pro_skip = self._pro_word_editor(_l3, cfg.get("skip_words") or [],
                                               self._pro_set_skip)
        _rst = QPushButton("恢复默认跳过词")
        _rst.clicked.connect(self._pro_reset_skip)
        _l3.addWidget(_rst)
        _l3.addStretch(1)
        _tabs.addTab(_p3, "隐私与过滤")

        _sec3.body_add(_tabs)

        # ================= 测试（默认折叠，页面最底）=================
        self._pro_line(lay)
        _sec4 = self._pro_section(lay, "测试", expanded=False)
        _sec4.body_add(self._sub_hint(
            "跳过时段、阈值和冷却，立刻调一次模型。不计次数。"))
        _trow = QHBoxLayout()
        self._pro_test_btn = QPushButton("测试搭话")
        self._pro_test_btn.setToolTip("立刻调一次模型，结果显示在下方")
        self._pro_test_btn.clicked.connect(
            lambda: self._pro_run_test(welcome=False))
        _trow.addWidget(self._pro_test_btn)
        self._pro_test_welcome_btn = QPushButton("测试回来打招呼")
        self._pro_test_welcome_btn.setToolTip("用欢迎语提示词调一次模型")
        self._pro_test_welcome_btn.clicked.connect(
            lambda: self._pro_run_test(welcome=True))
        _trow.addWidget(self._pro_test_welcome_btn)
        self._pro_test_cancel_btn = QPushButton("取消")
        self._pro_test_cancel_btn.clicked.connect(self._pro_cancel_test)
        self._pro_test_cancel_btn.hide()
        _trow.addWidget(self._pro_test_cancel_btn)
        _trow.addStretch(1)
        _sec4.body_add_layout(_trow)

        # 是否真实弹气泡
        self._pro_test_real = self._check(
            _sec4.body_layout, "同时在桌宠上弹气泡",
            bool((self.app.cfg.get("proactive_test_real") if
                  isinstance(self.app.cfg.get("proactive_test_real"), bool) else True)),
            lambda on: self._pro_set_test_real(bool(on)),
            "勾选时气泡真的会出现在桌宠上（8 秒后消失）。")

        # 结果区
        self._pro_test_out = QLabel("")
        self._pro_test_out.setWordWrap(True)
        self._pro_test_out.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._pro_test_out.setStyleSheet(
            "color:#2f3542;font-size:11px;background:#f5f7fb;"
            "border-radius:6px;padding:8px;")
        self._pro_test_out.hide()
        _sec4.body_add(self._pro_test_out)

        self._pro_refresh_status()
        lay.addStretch(1)

    # ---- 主动搭话页的小工具 ----
    def _pro_line(self, lay):
        f = QFrame()
        f.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(f)

    def _pro_section(self, lay, title, expanded=True):
        sec = CollapsibleSection(title, expanded=expanded)
        lay.addWidget(sec)
        return sec

    def _pro_sub_title(self, text):
        w = QLabel(text)
        w.setStyleSheet("color:#2f3542;font-size:11px;font-weight:bold;")
        return w

    def _pro_threshold_pair(self, sec, cfg):
        """发呆 / 离开阈值：只保留数字框，两者联动。

        离开阈值必须大于发呆阈值：改发呆导致冲突时把离开顶到 发呆+5；
        改离开导致冲突时把发呆压到 离开-5（不小于 1）。
        返回 (发呆框, 离开框)。
        """
        idle = QSpinBox()
        idle.setRange(1, 60)
        idle.setSuffix(" 分钟")
        idle.setFixedWidth(110)
        idle.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        idle.setValue(max(1, int(cfg.get("idle_min") or 10)))
        sec.body_add(self._pro_form_row("发呆阈值", idle))

        away = QSpinBox()
        away.setRange(2, 180)
        away.setSuffix(" 分钟")
        away.setFixedWidth(110)
        away.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        away.setValue(max(2, int(cfg.get("away_min") or 20)))
        sec.body_add(self._pro_form_row("离开阈值", away))

        GAP = 5
        guard = {"busy": False}

        def on_idle(v):
            if guard["busy"]:
                return
            guard["busy"] = True
            try:
                if away.value() <= v:
                    away.setValue(min(away.maximum(), v + GAP))
                self._pro_save({"idle_min": int(v),
                                "away_min": int(away.value())})
            finally:
                guard["busy"] = False

        def on_away(v):
            if guard["busy"]:
                return
            guard["busy"] = True
            try:
                if v <= idle.value():
                    idle.setValue(max(1, v - GAP))
                self._pro_save({"idle_min": int(idle.value()),
                                "away_min": int(v)})
            finally:
                guard["busy"] = False

        idle.valueChanged.connect(on_idle)
        away.valueChanged.connect(on_away)
        return idle, away

    def _pro_pct_row(self, sec, label, value, desc):
        """百分比滑块（0~100%）+ 说明。用于 fire_prob 这类 0~1 的小数配置。"""
        wrap = QWidget()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(0, 100)
        s.setSingleStep(5)
        s.setPageStep(10)
        s.setValue(int(round(float(value) * 100)))
        s.setMinimumWidth(160)
        lab = QLabel("%d%%" % s.value())
        lab.setFixedWidth(44)
        lab.setAlignment(Qt.AlignmentFlag.AlignRight
                         | Qt.AlignmentFlag.AlignVCenter)
        lab.setStyleSheet("color:#5686fe;font-size:12px;")
        h.addWidget(s, 1)
        h.addWidget(lab, 0)
        sec.body_add(self._pro_form_row(label, wrap))
        sec.body_add(self._sub_hint(desc))

        def _on(v):
            lab.setText("%d%%" % v)
            self._pro_save({"fire_prob": max(0.0, min(1.0, v / 100.0))})

        s.valueChanged.connect(_on)
        return s

    def _pro_spin_row(self, sec, label, value, lo, hi, suffix, desc, key):
        """数字框 + 说明（原「频率控制」控件，去掉滑条）。返回数字框。"""
        box = QSpinBox()
        box.setRange(lo, hi)
        box.setSingleStep(1)
        box.setValue(int(value))
        box.setFixedWidth(110)
        box.setSuffix(" " + suffix)
        box.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        sec.body_add(self._pro_form_row(label, box))
        sec.body_add(self._sub_hint(desc))
        box.valueChanged.connect(lambda v: self._pro_save({key: int(v)}))
        return box

    def _pro_form_row(self, label, widget):
        """一行：左标签（固定宽）+ 右控件。"""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        lab = QLabel(label)
        lab.setFixedWidth(96)
        lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        h.addWidget(lab)
        h.addWidget(widget, 1)
        return row

    def _pro_word_editor(self, lay, words, on_save):
        """多行关键词编辑（一行一个）。lay 可以是布局或 CollapsibleSection。

        返回 QPlainTextEdit。
        """
        ed = QTextEdit()
        ed.setMinimumHeight(64)
        ed.setPlainText("\n".join(str(w) for w in words))
        ed.textChanged.connect(lambda: on_save(
            [x.strip() for x in ed.toPlainText().split("\n") if x.strip()]))
        if hasattr(lay, "body_add"):
            lay.body_add(ed)
        else:
            lay.addWidget(ed)
        return ed

    # ---- 时间段渲染 ----
    def _pro_render_sessions(self, sessions):
        while self._pro_sessions_lay.count():
            it = self._pro_sessions_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._pro_sessions = [dict(s) for s in sessions]
        for idx, s in enumerate(self._pro_sessions):
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(4)

            # ---- 时间框（固定宽度，保证 09:00 完整显示）----
            st = QLineEdit(str(s.get("start") or "09:00"))
            st.setFixedWidth(72)
            st.setAlignment(Qt.AlignmentFlag.AlignCenter)
            en = QLineEdit(str(s.get("end") or "18:00"))
            en.setFixedWidth(72)
            en.setAlignment(Qt.AlignmentFlag.AlignCenter)
            h.addWidget(QLabel("从"))
            h.addWidget(st)
            h.addWidget(QLabel("到"))
            h.addWidget(en)

            # ---- 跨夜 / 非法提示（灰字「次日」或标红）----
            st_txt = (st.text() or "").strip()
            en_txt = (en.text() or "").strip()
            oob = self._pro_time_state(st_txt, en_txt)
            tip = QLabel(oob[1])
            tip.setFixedWidth(46)
            if oob[0] == "cross":
                tip.setStyleSheet("color:#8a8f9c;font-size:11px;")
            elif oob[0] == "equal":
                tip.setStyleSheet("color:#d93025;font-size:11px;")
            else:
                tip.setStyleSheet("color:#8a8f9c;font-size:11px;")
            h.addWidget(tip)
            if oob[0] == "equal":
                for w2 in (st, en):
                    w2.setStyleSheet("border:1px solid #d93025;border-radius:4px;")
            else:
                for w2 in (st, en):
                    w2.setStyleSheet("")

            # ---- 星期：7 个紧凑切换按钮 ----
            days = set(s.get("days") or [])
            for di, dn in enumerate(("一", "二", "三", "四", "五", "六", "日")):
                b = QPushButton(dn)
                b.setCheckable(True)
                b.setFixedSize(28, 24)
                b.setChecked(di in days)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setStyleSheet(
                    "QPushButton{border:1px solid #dfe3ec;border-radius:4px;"
                    "font-size:11px;background:#ffffff;color:#5a6070;}"
                    "QPushButton:checked{background:#5686fe;color:#ffffff;"
                    "border:1px solid #5686fe;}")
                b.toggled.connect(
                    lambda on, i=idx, d=di: self._pro_toggle_day(i, d, on))
                h.addWidget(b)

            h.addStretch(1)
            rm = QPushButton("删除")
            rm.setFixedWidth(56)
            rm.clicked.connect(lambda _=False, i=idx: self._pro_del_session(i))
            h.addWidget(rm)

            st.editingFinished.connect(
                lambda i=idx, w=st: self._pro_set_session_time(i, "start", w.text()))
            en.editingFinished.connect(
                lambda i=idx, w=en: self._pro_set_session_time(i, "end", w.text()))
            self._pro_sessions_lay.addWidget(row)

    @staticmethod
    def _pro_time_state(start, end):
        """判断时间段状态：('cross', '次日') / ('equal', '时间相同') / ('ok', '')。

        检测逻辑（proactive.in_work_session）已支持跨夜：a > b 时按
        「cur >= a or cur < b」判定。所以这里只做提示，不改逻辑。
        """
        def _min(t):
            m = re.match(r"^(\d{1,2}):(\d{2})$", (t or "").strip())
            if not m:
                return -1
            hh, mm = int(m.group(1)), int(m.group(2))
            if hh > 23 or mm > 59:
                return -1
            return hh * 60 + mm
        a, b = _min(start), _min(end)
        if a < 0 or b < 0:
            return "ok", ""
        if a == b:
            return "equal", "时间相同"
        if a > b:
            return "cross", "次日"
        return "ok", ""

    def _pro_toggle_day(self, idx, day, on):
        if idx >= len(getattr(self, "_pro_sessions", [])):
            return
        days = set(self._pro_sessions[idx].get("days") or [])
        if on:
            days.add(day)
        else:
            days.discard(day)
        self._pro_sessions[idx]["days"] = sorted(days)
        self._pro_save({"sessions": self._pro_sessions})

    def _pro_set_session_time(self, idx, key, text):
        if idx >= len(getattr(self, "_pro_sessions", [])):
            return
        t = (text or "").strip()
        if not re.match(r"^\d{1,2}:\d{2}$", t):
            # 还原
            self._pro_render_sessions(self._pro_sessions)
            return
        hh, mm = t.split(":")
        t = "%02d:%02d" % (int(hh), int(mm))
        # 结束时间 == 开始时间：禁止保存（检测逻辑会跳过该段）
        other_key = "end" if key == "start" else "start"
        other = str(self._pro_sessions[idx].get(other_key) or "")
        if t == other:
            log("[设置] 时间段开始/结束相同，已还原")
            self._pro_render_sessions(self._pro_sessions)
            return
        self._pro_sessions[idx][key] = t
        self._pro_save({"sessions": self._pro_sessions})
        self._pro_render_sessions(self._pro_sessions)

    def _pro_add_session(self):
        cur = list(getattr(self, "_pro_sessions", []) or [])
        cur.append({"start": "09:00", "end": "18:00", "days": [0, 1, 2, 3, 4]})
        self._pro_save({"sessions": cur})
        self._pro_render_sessions(cur)

    def _pro_del_session(self, idx):
        cur = list(getattr(self, "_pro_sessions", []) or [])
        if 0 <= idx < len(cur):
            cur.pop(idx)
        if not cur:
            cur = [{"start": "09:00", "end": "18:00", "days": [0, 1, 2, 3, 4]}]
        self._pro_save({"sessions": cur})
        self._pro_render_sessions(cur)

    # ---- 保存 ----
    def _pro_save(self, patch):
        try:
            self.app.save_proactive_cfg(patch)
            self._pro_refresh_status()
        except Exception as ex:
            log("[设置] 保存主动搭话配置失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _pro_set_private(self, words):
        self._pro_save({"private_words": words})

    def _pro_set_skip(self, words):
        self._pro_save({"skip_words": words})

    def _pro_on_prompt_edited(self):
        self._pro_save({"prompt": self._pro_prompt.toPlainText()})

    def _pro_reset_skip(self):
        import proactive as _P
        self._pro_save({"skip_words": list(_P.DEFAULT_SKIP_WORDS)})
        self._pro_skip.blockSignals(True)
        self._pro_skip.setPlainText("\n".join(_P.DEFAULT_SKIP_WORDS))
        self._pro_skip.blockSignals(False)

    def _pro_reset_prompt(self):
        import proactive as _P
        self._pro_prompt.blockSignals(True)
        self._pro_prompt.setPlainText(_P.DEFAULT_PROMPT)
        self._pro_prompt.blockSignals(False)
        self._pro_save({"prompt": _P.DEFAULT_PROMPT})

    def _pro_reset_welcome(self):
        import proactive as _P
        self._pro_welcome_prompt.blockSignals(True)
        self._pro_welcome_prompt.setPlainText(_P.DEFAULT_WELCOME_PROMPT)
        self._pro_welcome_prompt.blockSignals(False)
        self._pro_save({"welcome_prompt": _P.DEFAULT_WELCOME_PROMPT})

    # ---- 测试按钮 ----
    def _pro_set_test_real(self, on):
        try:
            self.app.cfg["proactive_test_real"] = bool(on)
            self.app.save_cfg()
        except Exception as ex:
            log("[设置] 保存测试弹气泡开关失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _pro_set_test_out(self, text, color="#2f3542"):
        try:
            self._pro_test_out.setText(text)
            self._pro_test_out.setStyleSheet(
                "color:%s;font-size:11px;background:#f5f7fb;"
                "border-radius:6px;padding:8px;" % color)
            self._pro_test_out.setVisible(bool(text))
        except Exception:
            pass

    def _pro_run_test(self, welcome=False):
        """测试：忽略时间段/阈值/冷却，直接调一次模型。"""
        if getattr(self, "_pro_testing", False):
            return
        cfg = self.app.proactive_cfg()
        ready, base, key, model = self.app._proactive_chat_ready()
        if not ready:
            self._pro_set_test_out(
                "✗ 聊天 Key / 地址 / 模型还没配好，先去「聊天」页配置并点「测试」。",
                "#c0392b")
            return

        # 取当前前台窗口（测试不看时间段）
        try:
            import proactive as _P
            _h, _proc, _title = _P.foreground_window()
            info = {"proc": _proc, "title": _title, "hwnd": _h}
        except Exception:
            info = {"proc": "", "title": "", "hwnd": 0}

        # 组装消息：用当前配置的提示词与阈值（假装刚发呆到阈值）
        idle_sec = max(60, int(cfg.get("idle_min") or 10) * 60)
        recent = []
        try:
            recent = list(getattr(self.app._pro_state, "recent", []) or [])
        except Exception:
            pass
        import proactive as _P
        if welcome:
            system, user = _P.build_welcome_messages(cfg, info, recent)
        else:
            system, user = _P.build_messages(cfg, info, idle_sec, recent)

        self._pro_testing = True
        self._pro_test_btn.setEnabled(False)
        self._pro_test_welcome_btn.setEnabled(False)
        self._pro_test_cancel_btn.show()
        self._pro_test_cancel_btn.setEnabled(True)
        self._pro_set_test_out("…正在调用模型（前台：%s）" % (info.get("proc") or "未知"),
                               "#8a8f9c")

        # 预览要发送的内容（可折叠显示在结果里）
        preview = user if len(user) <= 300 else user[:300] + "…"

        # 截全屏（与正式触发一致）：视觉模型先看，观察结果并进 user
        shot_img = None
        try:
            from PySide6.QtCore import QBuffer, QIODevice
            from PySide6.QtWidgets import QApplication as _QA
            screen = _QA.screenAt(_QA.primaryScreen().geometry().center()) or \
                _QA.primaryScreen()
            pix = screen.grabWindow(0)
            if pix and not pix.isNull():
                buf = QBuffer()
                buf.open(QIODevice.OpenModeFlag.WriteOnly)
                pix.save(buf, "PNG")
                from PIL import Image as _PIL
                shot_img = _PIL.open(io.BytesIO(bytes(buf.data()))).convert("RGB")
                buf.close()
        except Exception:
            shot_img = None

        import threading
        self._pro_test_seq = getattr(self, "_pro_test_seq", 0) + 1
        seq = self._pro_test_seq

        def work():
            t0 = time.time()
            observe_text = ""
            if shot_img is not None:
                # 视觉模型观察（用视觉页配置）
                try:
                    _ls = self.app.cfg.get("look_screen") or {}
                    _vb = (_ls.get("vision_base") or "").strip()
                    _vk = (_ls.get("vision_key") or "").strip()
                    _vm = (_ls.get("vision_model") or "").strip()
                    if _vb and _vk and _vm:
                        from petcore.consts import LOOK_SCREEN_QUALITY_DEFAULT
                        du = _P.shrink_for_vision(
                            shot_img, max_side=1024,
                            quality=int(_ls.get("quality")
                                        or LOOK_SCREEN_QUALITY_DEFAULT))
                        omsg = _P.build_observe_messages(info, du)
                        if omsg:
                            obs, _oerr = _P.call_chat(
                                _vb, _vk, _vm, "", omsg,
                                timeout=30, max_tokens=200, temperature=0.2)
                            if obs:
                                observe_text = obs.strip()
                except Exception:
                    observe_text = ""
            user2 = user + ("\n\n【屏幕观察】\n" + observe_text
                            if observe_text else "")
            text, err = _P.call_chat(base, key, model, system, user2, timeout=45)
            dt = time.time() - t0
            if seq != getattr(self, "_pro_test_seq", 0):
                return          # 已取消/作废
            self._pro_test_done.emit(seq, text, err, dt, preview, welcome,
                                     bool(self._pro_test_real.isChecked()))

        threading.Thread(target=work, daemon=True).start()

    def _on_pro_test_done(self, seq, text, err, dt, preview, welcome, real):
        if seq != getattr(self, "_pro_test_seq", 0):
            return
        self._pro_testing = False
        self._pro_test_btn.setEnabled(True)
        self._pro_test_welcome_btn.setEnabled(True)
        self._pro_test_cancel_btn.hide()

        if err:
            self._pro_set_test_out(
                "✗ 调用失败（%.1fs）：%s\n\n【发送内容】\n%s" % (dt, err, preview),
                "#c0392b")
            return
        line = ""
        try:
            import proactive as _P
            line = _P.clean_reply(text)
        except Exception:
            line = (text or "").strip()
        if not line:
            self._pro_set_test_out(
                "模型返回 [SKIP]（表示这次没什么值得说的，实际运行时会静默）\n"
                "耗时 %.1fs\n\n【原始返回】\n%s\n\n【发送内容】\n%s"
                % (dt, (text or "（空）")[:200], preview),
                "#d08a00")
            return
        self._pro_set_test_out(
            "✓ %.1fs\n\n【宠物会说】\n%s\n\n【发送内容】\n%s"
            % (dt, line, preview), "#1a7f37")
        # 真实弹气泡（走测试专用路径，不计入每日次数、不写搭话记忆）
        if real:
            try:
                pet = self.app.main_pet
                if pet is not None:
                    pet.say(line)
                    # 与正式主动搭话一致：8 秒
                    pet.bubble_until = pet.t * TICK / 1000.0 + 8.0
                    pet.update()
            except Exception:
                pass
            # 同步写进聊天（与正式搭话一致）
            try:
                label = "[主动打招呼]" if welcome else "[主动搭话]"
                pairs = [("user", label), ("assistant", line)]
                cw = self.app.chat_window
                if cw is not None:
                    for role, content in pairs:
                        cw.append_external(role, content)
                else:
                    self.app._append_pairs_to_chat_file(pairs)
            except Exception:
                pass

    def _pro_cancel_test(self):
        self._pro_test_seq = getattr(self, "_pro_test_seq", 0) + 1
        self._pro_testing = False
        self._pro_test_btn.setEnabled(True)
        self._pro_test_welcome_btn.setEnabled(True)
        self._pro_test_cancel_btn.hide()
        self._pro_set_test_out("已取消（已发出的请求无法中断，结果会被丢弃）",
                               "#8a8f9c")

    def _pro_refresh_status(self):
        """顶部状态摘要：已关闭 / 当前不在工作时间段 / 工作时间内 · 今日 N/M 次。"""
        try:
            cfg = self.app.proactive_cfg()
            if not cfg.get("enabled"):
                self._pro_status.setText("已关闭")
                self._pro_status.setStyleSheet("color:#8a8f9c;font-size:11px;")
                return
            inside = False
            try:
                import proactive as _P
                inside, _seg = _P.in_work_session(cfg)
            except Exception:
                pass
            if not inside:
                self._pro_status.setText("当前不在工作时间段")
                self._pro_status.setStyleSheet("color:#8a8f9c;font-size:11px;")
                return
            st = getattr(self.app, "_pro_state", None)
            used = int(getattr(st, "used", 0) or 0)
            mx = int(cfg.get("daily_max") or 0)
            self._pro_status.setText("工作时间内 · 今日 %d/%d 次" % (used, mx))
            self._pro_status.setStyleSheet("color:#1a7f37;font-size:11px;")
        except Exception:
            pass

    # ---------- 刷新 ----------
    def reload(self):
        """重新读取配置并刷新所有控件（外部改了配置后调用）。"""
        for key, cb in getattr(self, "_feature_boxes", {}).items():
            cb.blockSignals(True)
            cb.setChecked(self.app.is_enabled(key))
            cb.blockSignals(False)

        # 宠物页统一走 refresh_pet_page（列表与按钮共用同一套状态逻辑）
        self.refresh_pet_page()
        # 宠物页物理组联动（甩抛物理 / 互撞 置灰状态）
        self._refresh_pet_physics_state()
        # 通用页三个当前值
        try:
            self._refresh_general_values()
        except Exception:
            pass

        # 提醒页：列表 / 标签条数 / 每行今日次数 / 间隔框
        try:
            for kind, sp in getattr(self, "_rem_spins", {}).items():
                sp.blockSignals(True)
                sp.setValue(self.app._rem_minutes(kind))
                sp.blockSignals(False)
            self._refresh_remind_lists()
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "reload", type(ex).__name__, ex),
                logging.WARNING)

        # 聊天页三个折叠组的标题摘要
        self._refresh_memory_summary()
        self._refresh_persona_summary()
        # 台词池状态行（重新打开窗口时恢复上次结果）
        try:
            self._refresh_persona_lines_status()
        except Exception:
            pass

        # 视觉字段：复用窗口时重新填，避免输入框变空
        try:
            if not getattr(self, "_vision_testing", False):
                self._vision_filling = True
                self._vision_fill_from_cfg()
                self._vision_filling = False
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "reload", type(ex).__name__, ex),
                logging.WARNING)
        # 单次输出上限跟着配置走
        try:
            mt = int(self.app.cfg.get("max_tokens", MAX_TOKENS_DEFAULT))
            self.max_tokens_spin.blockSignals(True)
            self.max_tokens_spin.setValue(mt)
            self.max_tokens_spin.blockSignals(False)
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "reload", type(ex).__name__, ex),
                logging.WARNING)
        self._refresh_chat_hint()

    def show_page(self, page=None):
        if page:
            self._select_page(page)
        self.reload()
        self.show()
        self.raise_()
        self.activateWindow()

    # ---------- 生命周期：识别状态必须复位 ----------
    def hideEvent(self, e):
        """隐藏（含关闭）时取消识别 + 记住窗口位置。"""
        try:
            if getattr(self, "_vision_testing", False):
                self._reset_vision_test_ui()
                self._set_vision_status("已取消识别（窗口已关闭）", "info")
        except Exception as ex:
            log("[设置] hideEvent 复位视觉识别失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
        try:
            if getattr(self, "_chat_testing", False):
                self._reset_chat_test_ui()
                self._set_chat_status("已取消识别（窗口已关闭）", "info")
        except Exception as ex:
            log("[设置] hideEvent 复位聊天识别失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
        self._remember_geometry()
        super().hideEvent(e)

    def closeEvent(self, e):
        """关闭时同样复位（视觉 + 聊天）+ 记住窗口位置。"""
        try:
            if getattr(self, "_vision_testing", False):
                self._reset_vision_test_ui()
        except Exception as ex:
            log("[设置] closeEvent 复位视觉识别失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
        try:
            if getattr(self, "_chat_testing", False):
                self._reset_chat_test_ui()
        except Exception as ex:
            log("[设置] closeEvent 复位聊天识别失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
        self._remember_geometry()
        super().closeEvent(e)

class PersonaLinesDialog(QDialog):
    """台词查看/编辑：按池列出当前人设生成出的台词，可手动修改。"""

    def __init__(self, app, parent=None):
        super().__init__(parent or app.main_pet)
        self.app = app
        self.setWindowTitle("台词查看 / 编辑")
        # 恢复上次位置/尺寸
        restore_window_geometry(app.cfg, "win_persona_lines", self, 720, 560,
                                min_w=560, min_h=400)
        lay = QVBoxLayout(self)
        tip = QLabel(
            "这里显示当前人设已生成的台词池。人设为空或某池未生成时，"
            "该池使用中性台词。修改后点「保存修改」写入缓存。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#777;font-size:11px;")
        lay.addWidget(tip)

        row = QHBoxLayout()
        row.addWidget(QLabel("台词池"))
        self.pool_box = QComboBox()
        self.pool_box.setMinimumWidth(320)          # 池名较长，避免被压成乱码
        self.pool_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.pool_box.setMinimumContentsLength(28)
        self.pool_box.currentIndexChanged.connect(
            lambda _i: self._load_pool(self.pool_box.currentData()))
        row.addWidget(self.pool_box, 1)
        lay.addLayout(row)

        # 失败池原因（选中失败池时显示）
        self.fail_tip = QLabel("")
        self.fail_tip.setWordWrap(True)
        self.fail_tip.setStyleSheet("color:#d93025;font-size:11px;")
        self.fail_tip.hide()
        lay.addWidget(self.fail_tip)

        self.edit = QTextEdit()
        self.edit.setStyleSheet(
            "QTextEdit{background:#fff;border:1px solid #dfe3ec;"
            "border-radius:8px;padding:8px;font-family:'Microsoft YaHei UI';"
            "font-size:12px;}")
        lay.addWidget(self.edit, 1)

        btn_row = QHBoxLayout()
        save = QPushButton("保存修改")
        save.clicked.connect(self._save_pool)
        btn_row.addWidget(save)
        reset = QPushButton("该池恢复中性")
        reset.clicked.connect(self._reset_pool)
        btn_row.addWidget(reset)
        btn_row.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        btn_row.addWidget(close)
        lay.addLayout(btn_row)

        self._fill_pools()

    def closeEvent(self, e):
        """关闭时记住窗口位置/尺寸。"""
        self._remember_geometry()
        super().closeEvent(e)

    def hideEvent(self, e):
        """隐藏也记住（accept() 在已隐藏时不触发 closeEvent）。"""
        self._remember_geometry()
        super().hideEvent(e)

    def _remember_geometry(self):
        try:
            save_window_geometry(self.app.cfg, "win_persona_lines", self)
            self.app.save_cfg()
        except Exception:
            pass

    def _persona(self):
        try:
            from petcore.consts import persona_from_cfg
            return persona_from_cfg(self.app.cfg)
        except Exception:
            return ""

    def _fill_pools(self):
        try:
            from petcore import persona_lines as PL
            self.pool_box.clear()
            names = sorted(PL.POOL_SPECS.keys())
            # 失败池用红色标记，并显示中文名
            failed = self._failed_map()
            for n in names:
                label = PL.pool_label(n)
                if n in failed:
                    self.pool_box.addItem("✕ %s（%s）" % (label, n), n)
                else:
                    self.pool_box.addItem(label, n)
            # 失败项标红：QComboBox 不支持逐项着色，用 tooltip + 上方提示
            self._refresh_fail_marks(failed)
        except Exception as ex:
            log("[台词] 载入池列表失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _failed_map(self):
        """当前人设的失败池 → 失败项 dict。"""
        try:
            from petcore import persona_lines as PL
            p = self._persona()
            if not p:
                return {}
            lr = PL.load_last_result(APP_DIR, p) or {}
            out = {}
            for f in (lr.get("failed_pools") or []):
                if isinstance(f, dict) and f.get("pool"):
                    out[f["pool"]] = f
                elif isinstance(f, str):
                    out[f] = {"pool": f, "reason_code": "",
                              "detail": {}, "fail_count": 1}
            return out
        except Exception:
            return {}

    def _refresh_fail_marks(self, failed):
        """给失败池的下拉项加 tooltip（原因文案）。"""
        try:
            from petcore import persona_lines as PL
            for i in range(self.pool_box.count()):
                n = self.pool_box.itemData(i)
                if n in failed:
                    self.pool_box.setItemData(
                        i, PL.format_failure(failed[n]),
                        Qt.ItemDataRole.ToolTipRole)
        except Exception:
            pass

    def _load_pool(self, _text=None):
        name = self.pool_box.currentData() or self.pool_box.currentText()
        if not name:
            return
        try:
            from petcore import persona_lines as PL
            p = self._persona()
            pools = PL.get_persona_pools(APP_DIR, p) if p else {}
            lines = pools.get(name)
            if not isinstance(lines, list) or not lines:
                from petcore.consts import neutral_pool
                lines = neutral_pool(name)
                self.edit.setPlaceholderText(
                    "（该池尚未按人设生成，当前显示中性台词）")
            else:
                self.edit.setPlaceholderText("")
            self.edit.setPlainText("\n".join(lines))
            # 失败池：显示原因
            f = self._failed_map().get(name)
            if f:
                self.fail_tip.setText("该池生成失败：%s" % PL.format_failure(f))
                self.fail_tip.show()
            else:
                self.fail_tip.hide()
        except Exception as ex:
            log("[台词] 载入池失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _save_pool(self):
        name = self.pool_box.currentData() or self.pool_box.currentText()
        if not name:
            return
        p = self._persona()
        if not p:
            QMessageBox.information(
                self, "台词", "人设为空，台词固定使用中性池，无需保存。")
            return
        try:
            from petcore import persona_lines as PL
            raw_lines = (self.edit.toPlainText() or "").split("\n")
            okk, why, clean = PL.validate_pool(name, raw_lines, min_count=1)
            if not clean:
                QMessageBox.warning(self, "台词", "没有合格台词：%s" % why)
                return
            # 手写保存视为已生成：从失败列表移除、fail_count 清零
            PL.save_pool_manually(APP_DIR, p, name, clean)
            self._fill_pools()
            self.pool_box.setCurrentIndex(
                self.pool_box.findData(name))
            QMessageBox.information(
                self, "台词", "已保存 %d 句（原 %d 句，已过滤不合规的）。"
                % (len(clean), len(raw_lines)))
        except Exception as ex:
            QMessageBox.warning(self, "台词", "保存失败：%s" % ex)

    def _reset_pool(self):
        name = self.pool_box.currentData() or self.pool_box.currentText()
        p = self._persona()
        if not name or not p:
            return
        try:
            from petcore import persona_lines as PL
            pools = PL.get_persona_pools(APP_DIR, p)
            pools.pop(name, None)
            PL.save_persona_pools(APP_DIR, p, pools)
            self._load_pool(name)
            QMessageBox.information(self, "台词", "已恢复该池为中性台词。")
        except Exception as ex:
            QMessageBox.warning(self, "台词", "恢复失败：%s" % ex)

