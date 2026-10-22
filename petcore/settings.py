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
                               QAbstractItemView, QFormLayout)

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

    def __init__(self, title, parent=None, expanded=True):
        super().__init__(parent)
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

    def set_expanded(self, on, emit=True):
        on = bool(on)
        if on == self._expanded:
            self._apply_visibility()
            return
        self._expanded = on
        self._apply_visibility()
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
        ("visual", "视觉"),
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
        self._build_visual_page(self._pages["visual"])
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

        # ---- 隐藏时暂停 ----
        self._check(lay, "隐藏时暂停动画", bool(self.app.cfg.get("pause_when_hidden", True)),
                    lambda on: self._set_anim_cfg("pause_when_hidden", bool(on)),
                    "宠物隐藏后停止动画解码与定时器，CPU 接近 0；显示时立即恢复")

        # ---- 撞飞彩蛋 ----
        self._check(lay, "撞飞彩蛋", bool(self.app.cfg.get("smash_spin", True)),
                    lambda on: self._set_anim_cfg("smash_spin", bool(on)),
                    "高速甩飞撞到屏幕边缘时，头部跟随速度方向旋转，停稳后回正")

        # ---- 主动陪伴 ----
        line3 = QFrame()
        line3.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line3)
        self._check(lay, "主动陪伴", bool(self.app.cfg.get("proactive", True)),
                    lambda on: self._set_anim_cfg("proactive", bool(on)),
                    "宠物会自己找你说话：静默够久 + 距上次够久 → 主动冒一句。"
                    "深夜/饭点/久坐会说不同的话；睡觉、免打扰、小游戏中不打扰。")
        th = self.app.proactive_thresholds()
        self._pro_sliders = {}
        specs = (
            ("idle_min", "静默多久", 1, 180, 1, "分钟",
             "宠物多久没被理会时，才会主动说话"),
            ("same_app_min", "同应用时长", 1, 480, 5, "分钟",
             "同一个程序连续用多久算「坐太久」（会触发久坐台词）"),
            ("cooldown_min", "两次间隔", 1, 240, 5, "分钟",
             "两次主动说话之间至少隔多久，别太烦人"),
            ("daily_max", "每天最多", 1, 50, 1, "次",
             "一天最多主动几次，跨天自动重置"),
        )
        for key, label, lo, hi, step, suffix, desc in specs:
            self._pro_sliders[key] = self._slider_row(
                lay, label, th[key], lo, hi, step, None,
                lambda v, k=key: self._set_proactive_th(key, int(v)), suffix)
            hint = QLabel(desc)
            hint.setWordWrap(True)
            hint.setStyleSheet("color:#8a8f9c;font-size:11px;margin-left:22px;")
            lay.addWidget(hint)

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

    def _set_proactive_th(self, key, value):
        cfg = self.app.cfg.get("proactive_cfg")
        if not isinstance(cfg, dict):
            cfg = self.app.proactive_thresholds()
        cfg[key] = int(value)
        self.app.cfg["proactive_cfg"] = cfg
        self.app.save_cfg()

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
                    "支持 DeepSeek 与智谱 GLM；档位按「满额基准」折算已用百分比。")
        bc = self.app._balance_cfg()
        self._balance_provider_combo = self._combo_row(
            lay, "查询范围",
            [("两家同时查（推荐）", "auto"),
             ("只查 DeepSeek", "deepseek"),
             ("只查智谱 GLM", "zhipu")],
            bc.get("provider", "auto"),
            lambda v: self._set_balance_provider(str(v)))
        self._balance_provider_hint = QLabel("")
        self._balance_provider_hint.setWordWrap(True)
        self._balance_provider_hint.setStyleSheet(
            "color:#8a8f9c;font-size:11px;margin-left:22px;")
        lay.addWidget(self._balance_provider_hint)
        self._refresh_balance_provider_hint()

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
        self._refresh_balance_provider_hint()

    def _refresh_balance_provider_hint(self):
        """说明当前会查哪几家、各自的 Key 是否就绪。"""
        try:
            dk = (self.app._balance_key("deepseek") or "").strip()
            zk = (self.app._balance_key("zhipu") or "").strip()
            scope = (self.app._balance_cfg().get("provider") or "auto")
            bits = []
            if scope in ("auto", "deepseek"):
                bits.append("DeepSeek %s" % ("✓" if dk else "未填 Key"))
            if scope in ("auto", "zhipu"):
                bits.append("智谱 %s" % ("✓" if zk else "未填 Key"))
            self._balance_provider_hint.setText("会查询：" + "，".join(bits))
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_refresh_balance_provider_hint", type(ex).__name__, ex),
                logging.WARNING)


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

        # ---- 聊天模型（可收缩，默认展开）----
        _c_exp = bool((self.app.cfg.get("ui_sections") or {}).get("chat_model", True))
        self._chat_section = CollapsibleSection("聊天模型", expanded=_c_exp)
        self._chat_section.toggled.connect(
            lambda on: self._save_section_state("chat_model", on))

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

        self._chat_section.body_add_layout(_cf)

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
        self._chat_section.body_add_layout(_cbtn)

        # 状态行
        self._chat_status = QLabel("")
        self._chat_status.setWordWrap(True)
        self._chat_status.setStyleSheet(
            "color:#8a8f9c;font-size:11px;margin-left:22px;")
        self._chat_section.body_add(self._chat_status)

        # 缓存提示
        self._chat_section.body_add(self._hint_label("更换模型后缓存失效一次属正常"))

        lay.addWidget(self._chat_section)
        self._chat_fill_from_cfg()



        self._combo_row(
            lay, "回复风格",
            [(label, key) for label, key in REPLY_STYLE_CHOICES],
            self.app.cfg.get("reply_style", REPLY_STYLE_DEFAULT),
            lambda v: self._set_reply_style(str(v)))

        self._style_hint = QLabel("")
        self._style_hint.setWordWrap(True)
        self._style_hint.setStyleSheet("color:#8a8f9c;font-size:11px;margin-left:22px;")
        lay.addWidget(self._style_hint)

        # ---- 人设（可收缩）----
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line2)

        _p_exp = bool((self.app.cfg.get("ui_sections") or {}).get("persona", True))
        self._persona_section = CollapsibleSection("人设", expanded=_p_exp)
        self._persona_section.toggled.connect(
            lambda on: self._save_section_state("persona", on))
        _p_desc = QLabel(
            "这段会作为 system 的开头发给模型。固定不变才有利于前缀缓存；"
            "改完缓存会失效一次，建议新开一个对话。")
        _p_desc.setWordWrap(True)
        _p_desc.setStyleSheet("color:#8a8f9c;font-size:11px;")
        self._persona_section.body_add(_p_desc)

        self.persona_edit = QTextEdit()
        self.persona_edit.setFixedHeight(150)
        self.persona_edit.setPlaceholderText("留空则使用内置默认人设")
        self.persona_edit.setStyleSheet(
            "QTextEdit{background:#ffffff;border:1px solid #dfe3ec;border-radius:8px;"
            "padding:8px;font-family:'Microsoft YaHei UI';font-size:12px;}")
        self.persona_edit.setPlainText(self.app.cfg.get("persona") or DEFAULT_PERSONA)
        self.persona_edit.textChanged.connect(self._on_persona_edited)
        self._persona_section.body_add(self.persona_edit)

        prow = QHBoxLayout()
        save_btn = QPushButton("保存人设")
        save_btn.clicked.connect(self._save_persona)
        prow.addWidget(save_btn)
        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(self._reset_persona)
        prow.addWidget(reset_btn)
        prow.addStretch(1)
        self._persona_section.body_add_layout(prow)

        self._persona_hint = QLabel("")
        self._persona_hint.setWordWrap(True)
        self._persona_hint.setStyleSheet("color:#d08a00;font-size:11px;margin-left:2px;")
        self._persona_section.body_add(self._persona_hint)

        lay.addWidget(self._persona_section)
        self._refresh_persona_state()


        # 聊天 system 摘要（放在人设区下方）
        self._chat_preview = QLabel("")
        self._chat_preview.setWordWrap(True)
        self._chat_preview.setStyleSheet(
            "color:#5a6070;font-size:11px;margin-top:6px;"
            "background:#f5f7fb;border-radius:6px;padding:8px;")
        lay.addWidget(self._chat_preview)

        # ---- 单次输出上限（滑块 + 数字框）----
        self._max_tokens_field, self.max_tokens_slider, self.max_tokens_spin = \
            self._tokens_field(
                int(self.app.cfg.get("max_tokens", MAX_TOKENS_DEFAULT)),
                MAX_TOKENS_MIN, MAX_TOKENS_MAX, 50,
                lambda v: self._set_max_tokens(int(v)), "tokens")
        _mt_row = QHBoxLayout()
        _mt_row.addWidget(QLabel("单次输出上限"))
        _mt_row.addWidget(self._max_tokens_field, 1)
        _mt_row.addStretch(0)
        lay.addLayout(_mt_row)
        hint2 = QLabel("模型单次最多输出多少 token（中文约 1.5 字/token）。"
                       "「回复风格」决定说多长，这个值决定最多能说多长。")
        hint2.setWordWrap(True)
        hint2.setStyleSheet("color:#8a8f9c;font-size:11px;margin-left:2px;")
        lay.addWidget(hint2)


        lay.addStretch(1)
        self._refresh_chat_hint()

    # ---------- 看看屏幕：视觉配置 ----------
    def _build_visual_page(self, lay):
        self._title(lay, "视觉",
                    "「看看屏幕」用的视觉模型配置。填一个视觉 Key，点「测试」自动识别服务商与可用视觉模型。"
                    "高级设置里可调上限、图片大小、质量和超时。")

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

        # 风格灰字：紧贴「描述风格」控件下方
        self._vision_style_hint = QLabel("")
        self._vision_style_hint.setWordWrap(True)
        self._vision_style_hint.setStyleSheet(
            "color:#8a8f9c;font-size:11px;margin-left:22px;")
        lay.addWidget(self._vision_style_hint)

        # ---- 识别规则（视觉请求 system，可收缩，默认收起）----
        _r_exp = bool((self.app.cfg.get("ui_sections") or {}).get("vision_rule", False))
        self._rule_section = CollapsibleSection("识别规则", expanded=_r_exp)
        self._rule_section.toggled.connect(
            lambda on: self._save_section_state("vision_rule", on))

        _rule_hint = QLabel("这段会作为视觉请求的 system，长度句自动追加在末尾")
        _rule_hint.setWordWrap(True)
        _rule_hint.setStyleSheet("color:#8a8f9c;font-size:11px;")
        self._rule_section.body_add(_rule_hint)

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
        self._vision_rule_reset = QPushButton("恢复默认")
        self._vision_rule_reset.clicked.connect(self._reset_vision_rule)
        _rrow.addWidget(self._vision_rule_reset)
        _rrow.addStretch(1)
        self._rule_section.body_add_layout(_rrow)

        lay.addWidget(self._rule_section)
        self._refresh_rule_state()

        # ---- 高级设置（可收缩，默认收起）----
        _a_exp = bool((self.app.cfg.get("ui_sections") or {}).get("vision_advanced", False))
        self._advanced_section = CollapsibleSection("高级设置", expanded=_a_exp)
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
            "识别结果最长能写多长，太小会被截断。带思考过程的模型请调大。"))

        # 6) 图片最长边
        self._vision_side_field, self.vision_side_slider, self.vision_side_spin = \
            self._tokens_field(
                int((self.app.cfg.get("look_screen") or {}).get("max_side")
                    or LOOK_SCREEN_SIDE_DEFAULT),
                LOOK_SCREEN_SIDE_MIN, LOOK_SCREEN_SIDE_MAX, LOOK_SCREEN_SIDE_STEP,
                lambda v: self._on_vision_limit_changed("max_side", int(v)), "px")
        _af.addRow("图片最长边", self._vision_side_field)
        _af.addRow("", self._sub_hint(
            "截图缩放后的最长边。越大越清楚，也越慢越费钱；一般 1024 够用。"))

        # 7) 图片质量
        self._vision_quality_field, self.vision_quality_slider, self.vision_quality_spin = \
            self._tokens_field(
                int((self.app.cfg.get("look_screen") or {}).get("quality")
                    or LOOK_SCREEN_QUALITY_DEFAULT),
                LOOK_SCREEN_QUALITY_MIN, LOOK_SCREEN_QUALITY_MAX, 5,
                lambda v: self._on_vision_limit_changed("quality", int(v)), "")
        _af.addRow("图片质量", self._vision_quality_field)
        _af.addRow("", self._sub_hint(
            "越低上传越快，但字会变糊，一般不用动。"))

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
            "等多久没反应就放弃。带思考过程的模型建议调大。"))

        self._advanced_section.body_add_layout(_af)

        # 保存屏幕描述到聊天记录（默认关闭）
        self._vision_save_desc = self._check(
            self._advanced_section.body_layout, "保存屏幕描述到聊天记录",
            bool((self.app.cfg.get("look_screen") or {}).get("save_desc", False)),
            lambda on: self._set_vision_save_desc(bool(on)),
            "关闭时只用于这一次回复，不写入聊天记录。")


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
        self._vision_reset_btn.setToolTip("把高级设置四项恢复为默认值（不影响 Key）")
        self._vision_reset_btn.clicked.connect(self._reset_vision_advanced)
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

        lay.addStretch(1)


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
        cur_n = cur or DEFAULT_PERSONA.strip()
        saved_n = saved or DEFAULT_PERSONA.strip()
        if cur_n == saved_n:
            if saved_n == DEFAULT_PERSONA.strip():
                self._persona_section.set_state("默认", "#8a8f9c")
            else:
                self._persona_section.set_state("已保存", "#1a7f37")
        else:
            self._persona_section.set_state("未保存", "#d08a00")

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


    def _reset_vision_rule(self):
        """恢复默认 = 清除配置里的副本（之后自动跟随代码里的最新默认值）。"""
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
        self._set_vision_status("✓ 已恢复默认（副本已清除，将跟随新版默认）", "ok")


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
        """风格行下方灰字：显示当前追加句（和聊天页写法一致）。"""
        try:
            suffix = vision_style_suffix(self._style_from_widget())
            self._vision_style_hint.setText(
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


    def _reset_vision_advanced(self):
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
        """保存人设到 config.json。留空/与默认一致时存空串（用内置默认）。"""
        text = (self.persona_edit.toPlainText() or "").strip()
        if not text or text == DEFAULT_PERSONA.strip():
            self.app.cfg["persona"] = ""
        else:
            self.app.cfg["persona"] = text[:4000]
        self.app.save_cfg()
        self._persona_hint.setText("已保存。前缀缓存会失效一次，建议新开一个对话。")
        self._refresh_persona_state()
        self.app.say_main("人设已保存")
        self._refresh_chat_hint()
        log("[聊天] 人设已更新（%d 字）" % len(text))

    def _reset_persona(self):
        """恢复默认人设：清空配置字段 + 编辑框回填默认文本。"""
        self.app.cfg["persona"] = ""
        self.app.save_cfg()
        self.persona_edit.setPlainText(DEFAULT_PERSONA)
        self._persona_hint.setText("已恢复默认人设。前缀缓存会失效一次，建议新开一个对话。")
        self._refresh_persona_state()
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
        """刷新聊天页提示：风格句 + system 一行摘要。"""
        try:
            style = self.app.cfg.get("reply_style", REPLY_STYLE_DEFAULT)
            suffix = reply_style_suffix(style)
            mt = int(self.app.cfg.get("max_tokens", MAX_TOKENS_DEFAULT))
            self._style_hint.setText(
                "当前风格下，system 末尾会追加：%s"
                % (("「" + suffix + "」") if suffix else "（不追加，不限长度）"))
            persona_txt = persona_from_cfg(self.app.cfg)
            try:
                mem_n = len(self.app.memory.items)
            except Exception:
                mem_n = 0
            # 一行小字：不重复人设正文，只说明拼了什么
            if suffix:
                m = re.search(r"控制在\s*(\d+)\s*字", suffix)
                length_txt = "当前 %s 字以内" % (m.group(1) if m else "限")
            else:
                length_txt = "不限长度"
            self._chat_preview.setText(
                "system = 人设 + 回复长度句（%s）+ %d 条长期记忆"
                % (length_txt, mem_n))
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_refresh_chat_hint", type(ex).__name__, ex),
                logging.WARNING)

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
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "reload", type(ex).__name__, ex),
                logging.WARNING)
        self._refresh_balance_hint()

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
        """隐藏（含关闭）时取消识别，避免下次打开卡在「识别中」。"""
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
        super().hideEvent(e)

    def closeEvent(self, e):
        """关闭时同样复位（视觉 + 聊天）。"""
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
        super().closeEvent(e)
