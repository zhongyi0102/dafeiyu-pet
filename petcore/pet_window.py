# -*- coding: utf-8 -*-
"""大肥鱼桌宠 —— 宠物窗口（PetWindow）+ 喂食面板（FoodPanel）。

从 桌宠.py 拆分（第 23 批重构）。本模块依赖 petcore.consts，
并通过 `self.app`（PetApp 实例）访问应用级功能，**不反向 import 桌宠.py**。
"""
import ctypes
import datetime
import json
import logging
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
        self.setFixedSize(self.win_w, self.cur_h + self._bubble_area_h() + MARGIN * 2 + 10)

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
        # 撞飞彩蛋：头部跟随速度方向旋转
        self._smash_spin = 0.0
        self._smash_until = 0.0
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
        if self.pet.get("display", True) and not TEST_MODE:
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
            try:
                if (self.app.cfg.get("smash_spin", True)
                        and info["impact"] > SMASH_SPIN_MIN_SPEED):
                    self._trigger_smash_spin()
            except Exception:
                pass
        if info["landed"]:
            landed = True
            self.body.last_impact = 0.0
        return landed

    def _trigger_smash_spin(self):
        """撞飞彩蛋：按速度方向给头部一个旋转角，停稳后回正。"""
        try:
            vx, vy = float(self.body.vx), float(self.body.vy)
        except Exception:
            return
        if abs(vx) > 120:
            ang = SMASH_SPIN_MAX_DEG if vx > 0 else -SMASH_SPIN_MAX_DEG
        elif abs(vy) > 400:
            ang = SMASH_SPIN_MAX_DEG * 0.6 if vy > 0 else -SMASH_SPIN_MAX_DEG * 0.6
        else:
            return
        self._smash_spin = ang
        self._smash_until = self.t * TICK / 1000.0 + SMASH_SETTLE_SEC
        log("[撞飞] 头部旋转 %.0f°" % ang)

    def _step_smash_spin(self):
        """推进：停稳 SMASH_SETTLE_SEC 秒后平滑回正。"""
        if not getattr(self, "_smash_spin", 0.0):
            return
        if self.t * TICK / 1000.0 >= getattr(self, "_smash_until", 0.0):
            self._smash_spin *= 0.85
            if abs(self._smash_spin) < 0.4:
                self._smash_spin = 0.0

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

    def _start_frame_anim(self, action, start_index=0):
        """开始播放某动作的帧序列（返回是否成功）。

        start_index: 从第几帧开始播（情绪动作用来跳过与待机几乎相同的
        开头帧，直接呈现最有表现力的部分）。
        """
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
        try:
            self._frame_index = max(0, min(int(start_index), len(frames) - 1))
        except Exception:
            self._frame_index = 0
        self._frame_elapsed = 0.0
        self._frame_fps = fps
        self._frame_loop = bool(spec.get("loop", True))
        self._frame_hold = bool(spec.get("hold_last", True))
        return True

    def _bubble_scale(self):
        """当前气泡文字缩放系数。"""
        try:
            return max(BUBBLE_SCALE_MIN,
                       min(BUBBLE_SCALE_MAX,
                           float(self.app.cfg.get("bubble_scale", 1.0))))
        except Exception:
            return 1.0

    def _bubble_area_h(self):
        """气泡区高度：随缩放等比放大（BUBBLE_H 是 100% 时的基准）。

        额外留 18px 余量：QFontMetrics.height() 不含行距与边框，
        实测 250% 时需要再多一点才不压到宠物头顶。
        """
        return int(BUBBLE_H * self._bubble_scale()) + 18

    def apply_bubble_scale(self):
        """缩放变化后重设窗口尺寸（气泡区跟着变高）。"""
        try:
            total_h = self.cur_h + self._bubble_area_h() + MARGIN * 2 + 10
            self.setFixedSize(self.win_w, total_h)
            self.update()
        except Exception:
            pass

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
        # 播放速率：倍速越大，帧推进越快
        rate = 1.0
        try:
            rate = max(0.25, min(8.0, float(self.app.cfg.get("anim_rate", 1.0))))
        except Exception:
            rate = 1.0
        self._frame_elapsed += dt * self._frame_fps * rate
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
            # 气泡文字缩放系数（50%~300%，列宽/字号同比放大）
            bscale = 1.0
            try:
                bscale = max(BUBBLE_SCALE_MIN, min(BUBBLE_SCALE_MAX,
                             float(self.app.cfg.get("bubble_scale", 1.0))))
            except Exception:
                bscale = 1.0
            # 基准字号：优先 pixelSize，为 -1 时退回 pointSizeF
            base_px = self.bubble_font.pixelSize()
            if base_px <= 0:
                base_px = max(9, int(self.bubble_font.pointSizeF() * 1.34))
            size_px = max(6, int(base_px * bscale))
            if self.bubble_inner:
                bfont = QFont(self.bubble_font)
                bfont.setPixelSize(size_px)
                bfont.setItalic(True)
                bg, fg = QColor(232, 232, 238, 235), QColor(125, 125, 138)
            else:
                bfont = QFont(self.bubble_font)
                bfont.setPixelSize(size_px)
                bg, fg = QColor(255, 255, 255, 235), QColor(60, 60, 80)
            fm = QFontMetrics(bfont)
            # 气泡宽度上限：不超出窗口（留 8px 边）
            max_w = max(60, self.width() - 16)
            # 高度上限：不超出气泡区（防止画到宠物身上）
            max_h = max(20, self._bubble_area_h() - 8)
            words = self.bubble_text
            lines = []
            cur = ""
            # 避头尾：这些标点不能出现在行首（否则出现「句号掉行」）
            NO_LINE_START = "。，、；：？！）】》」』…—·.,;:?!)]}>"
            for ch in words:
                if fm.horizontalAdvance(cur + ch) > max_w - 20:
                    # 若当前字符是标点、且上一行还有字，把上一行末字挪下来
                    if ch in NO_LINE_START and len(cur) > 1:
                        lines.append(cur[:-1])
                        cur = cur[-1] + ch
                    else:
                        lines.append(cur)
                        cur = ch
                else:
                    cur += ch
            if cur:
                lines.append(cur)
            # 行数上限：超出气泡区就截断（末尾加省略号）
            max_lines = max(1, (max_h - 14) // max(1, fm.height()))
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                if lines:
                    lines[-1] = lines[-1][:-1] + "…" if len(lines[-1]) > 1 else "…"
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
            p.rotate(sway + act_rot + getattr(self, "_smash_spin", 0.0))
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
            p.rotate(sway + act_rot + getattr(self, "_smash_spin", 0.0))
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
        # 隐藏时暂停：连 t 都不推进（行为完全冻结，省 CPU）
        if getattr(self, "_paused_for_hidden", False):
            return
        self.t += 1
        dt = TICK / 1000.0

        # 提醒走动优先：期间不做普通散步/跟随
        if self._step_remind_walk():
            self._step_frame_anim(dt)
            self.update()
            return

        self._step_smash_spin()
        if self.jump_t > 0:
            self.jump_t = max(0.0, self.jump_t - 0.06)
        if self.cross_t > 0:
            self.cross_t = max(0.0, self.cross_t - dt * 1000.0 / ANIM_CROSS_FADE_MS)
        if self.squash_t > 0:
            self.squash_t = max(0.0, self.squash_t - dt * 2.2)
        if self._action_fade > 0:
            self._action_fade = max(0.0, self._action_fade - dt * 1000.0 / ANIM_CROSS_FADE_MS)

        # 临时动作倒计时（受播放速率影响）
        if self.action_t > 0:
            try:
                _rate = max(0.25, min(8.0, float(self.app.cfg.get("anim_rate", 1.0))))
            except Exception:
                _rate = 1.0
            self.action_t = max(0.0, self.action_t - 0.03 * _rate)
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
    def _in_bubble_area(self, pt):
        """点是否落在气泡区域（顶部 BUBBLE_H 高度内）。"""
        try:
            return 0 <= pt.y() <= BUBBLE_H and self.bubble_text and \
                self.t * TICK / 1000.0 < self.bubble_until
        except Exception:
            return False

    def mousePressEvent(self, e):
        # 主动搭话气泡：点一下 → 进聊天页并把这句话带进上下文
        if (e.button() == Qt.MouseButton.LeftButton
                and self._in_bubble_area(e.position().toPoint())):
            try:
                if self.app._on_proactive_clicked():
                    return      # 已处理，不再触发拖拽/戳脸
            except Exception:
                pass
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
        self.setFixedSize(self.win_w, self.cur_h + self._bubble_area_h() + MARGIN * 2 + 10)
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

    def hideEvent(self, e):
        """隐藏：暂停动画解码与 tick（省 CPU，隐藏时接近 0 占用）。"""
        super().hideEvent(e)
        try:
            if self.app.cfg.get("pause_when_hidden", True) and not self.dragging:
                self._paused_for_hidden = True
                if self.timer.isActive():
                    self.timer.stop()
                self._stop_frame_anim()
        except Exception:
            pass

    def showEvent(self, e):
        super().showEvent(e)
        self.app.apply_capture_exclude(self)
        # 显示：恢复 tick 与解码
        try:
            if getattr(self, "_paused_for_hidden", False):
                self._paused_for_hidden = False
                if not self.timer.isActive():
                    self.timer.start(TICK)
        except Exception:
            pass
