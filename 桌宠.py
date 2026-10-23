# -*- coding: utf-8 -*-
"""
大肥鱼桌宠 —— 三视图透明桌宠 + DeepSeek AI 对话
左键单击：弹出功能列表（🗨️图标）→ 点击🗨️弹出聊天框
聊天时只禁用移动，呼吸/摇摆/小动作正常
"""
import base64
import ctypes
import datetime
import html
import io
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
from PySide6.QtCore import (Qt, QTimer, QPoint, QPointF, QRect, QRectF, QThread, Signal,
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

# ===== 常量 / 配置 / 工具（已拆分到 petcore.consts）=====
from petcore.consts import *          # noqa: F401,F403
# import * 不会导入下划线开头的名字，这里显式补全
from petcore.consts import mask_secrets, is_default_vision_system  # noqa: F401

# 看看屏幕增强模块（场景分类 / OCR / 预处理 / 看屏记忆 / 视觉模型排序）
import screen_vision as SV   # noqa: E402
from weather_cn import weather_cn, weather_tip  # noqa: E402
import pet_voice as VOICE  # noqa: E402
VOICE.log = log          # 让 pet_voice 的日志写进本程序的 error.log
import proactive as PRO      # noqa: E402  主动搭话

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
                            _setup_logging, _thread_excepthook, _valid_hhmm,
                            _user_data_dir, _extract_bundled_sprites)


# ===== 宠物窗口（已拆分到 petcore.pet_window）=====
from petcore.pet_window import PetWindow, FoodPanel   # noqa: F401


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


class BalanceWorker(QThread):
    """后台查询余额：DeepSeek（GET /user/balance）或 智谱（GET /api/biz/account/...）"""
    done = Signal(float, dict)
    failed = Signal(str)

    def __init__(self, api_key, provider="deepseek"):
        super().__init__()
        self.api_key = api_key
        self.provider = (provider or "deepseek").lower()

    # ---------- DeepSeek ----------
    def _query_deepseek(self):
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
        self.done.emit(total[0], {"sign": total[1], "provider": "deepseek",
                                  "raw": data})

    # ---------- 智谱 ----------
    def _query_zhipu(self):
        headers = {"Authorization": "Bearer " + self.api_key,
                   "Accept": "application/json"}
        r = requests.get(BALANCE_URL_ZHIPU, headers=headers, timeout=15)
        if r.status_code != 200:
            self.failed.emit(_http_error_message(r.status_code, r.text))
            return
        try:
            data = r.json()
        except Exception:
            self.failed.emit("智谱余额接口返回的不是 JSON")
            return
        # 业务错误：{"code":404,"msg":"...","success":false}
        if isinstance(data, dict) and data.get("success") is False:
            self.failed.emit("智谱余额查询失败：%s" % (data.get("msg") or "未知错误"))
            return
        d = data.get("data") if isinstance(data, dict) else None
        if not isinstance(d, dict):
            self.failed.emit("智谱余额接口没有返回数据")
            return
        # 优先 availableBalance，退回 balance
        amount = None
        for k in ("availableBalance", "balance"):
            try:
                v = d.get(k)
                if v is not None:
                    amount = float(v)
                    break
            except Exception:
                continue
        if amount is None:
            self.failed.emit("智谱余额接口没有返回可用金额")
            return
        meta = {
            "sign": "¥",
            "provider": "zhipu",
            "raw": data,
            "recharge": _safe_float(d.get("rechargeAmount")),
            "give": _safe_float(d.get("giveAmount")),
            "spent": _safe_float(d.get("totalSpendAmount")),
        }
        self.done.emit(amount, meta)

    def run(self):
        try:
            if self.provider == "zhipu":
                self._query_zhipu()
            else:
                self._query_deepseek()
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


def _http_error_message(status, body, kind="chat"):
    """把 HTTP 错误转成用户可读文案。**所有输出都经过 mask_secrets 脱敏。**

    kind="chat"  → 提示去「设置 → 聊天」（聊天 Key）
    kind="vision"→ 提示去「设置 → 视觉」（视觉 Key）
    """
    msg = ""
    try:
        data = json.loads(body)
        err = data.get("error") or {}
        msg = err.get("message") or data.get("message") or ""
    except Exception:
        msg = (body or "")[:200]
    low = (msg or "").lower()

    def _key_hint():
        if kind == "vision":
            return "请到 设置 →「视觉」重新填写视觉 Key。"
        return "请到 设置 →「聊天」→「聊天模型」重新填写聊天 Key。"

    if status == 401 or "authentication" in low or "invalid api key" in low or "api key" in low:
        return "API Key 无效或已失效。" + _key_hint()
    if status == 402 or "insufficient" in low or "balance" in low:
        return "账户余额不足（HTTP 402）。请先充值。"
    if status == 429:
        return "请求太频繁或额度用尽（HTTP 429），稍等一会儿再试。"
    if status and status >= 500:
        return mask_secrets("服务器出错了（HTTP %s）：%s" % (status, msg or "稍后再试"))
    return mask_secrets("请求失败（HTTP %s）：%s" % (status, msg or "未知错误"))


# ===== 聊天（已拆分到 petcore.chat）=====
from petcore.chat import (MemoryStore, ChatWorker, SummaryWorker,   # noqa: F401
                          ChatBubble, ChatWindow, MemoryDialog)

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


# ===== 设置窗口（已拆分到 petcore.settings）=====
from petcore.settings import SettingsWindow   # noqa: F401


class PetApp(QObject):
    # 宠物显示状态变化（pet_id, state, reason）——设置窗口监听后刷新列表
    pet_state_changed = Signal(str, str, str)

    # 看看屏幕 → 聊天窗口（跨线程安全：worker 线程只 emit）
    look_to_chat = Signal(str, str, str)

    # 天气查询结果回主线程（worker 线程只 emit）
    weather_done = Signal(str, int)

    # 主动搭话：模型返回结果回主线程（line, 是否回来打招呼）
    pro_active_say = Signal(str, bool)
    """全局唯一的东西：配置、托盘、菜单、聊天、记忆、提醒、节日、截屏守卫、
    系统监控、小游戏、用量。持有 1~N 个 PetWindow。"""

    def __init__(self):
        self.cfg, migrated = load_config_safe()
        super().__init__()
        self.pets = []
        # 看屏记忆（放用户数据目录，不放项目目录）
        try:
            self._screen_memory = SV.ScreenMemory(
                os.path.join(APP_DIR, "screen_memory.json"), keep=3)
        except Exception as ex:
            log("[看看屏幕] 初始化 ScreenMemory 失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            self._screen_memory = None
        # OCR 引擎首次加载很慢，后台预热（不阻塞界面线程）
        try:
            if SV.ocr_available():
                SV.warmup_ocr(blocking=False)
                log("[看看屏幕] OCR 后台预热已启动")
            else:
                log("[看看屏幕] 未安装 rapidocr，OCR 已跳过（功能照常）")
        except Exception as ex:
            log("[看看屏幕] OCR 预热失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

        # 旧配置迁移（一次性写入 chat_base / chat_model，运行时不再回落）
        try:
            self._migrate_chat_cfg()
        except Exception as ex:
            log("[聊天] 迁移失败: %s: %s" % (type(ex).__name__, ex), logging.WARNING)
        # 清除过期的默认规则副本（默认规则变化时自动跟随新版）
        try:
            self._purge_default_vision_system()
        except Exception as ex:
            log("[视觉] 清除默认规则副本失败: %s: %s" % (type(ex).__name__, ex), logging.WARNING)
        # 看看屏幕结果回主线程写聊天（worker 线程只 emit）
        self.look_to_chat.connect(self._on_look_to_chat)
        self.weather_done.connect(self._on_weather_done)
        self.pro_active_say.connect(self._on_proactive_say)

        # 语音播放器（外部 RVC 服务，未启用时不请求）
        try:
            self._voice_player = VOICE.VoicePlayer(self.voice_cfg)
        except Exception as ex:
            log("[语音] 初始化失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)
            self._voice_player = None        # 气泡点击 → 主动搭话反馈（在 PetWindow 里判断是否点中气泡）
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
        # 主动陪伴状态
        # ---- 主动搭话 ----
        self._pro_tracker = PRO.StateTracker()
        self._pro_inside = False
        self._pro_pending = None
        self._pro_last_info = {}
        self._pro_req_id = 0
        try:
            self._pro_state = PRO.RuntimeState(
                os.path.join(APP_DIR, "proactive_state.json"))
        except Exception as ex:
            log("[主动] 初始化状态失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)
            self._pro_state = PRO.RuntimeState(os.path.join(APP_DIR, "_pro_tmp.json"))
        self._proc_last_name = ""
        self._proc_since = time.time()
        self._last_festival_hour = None
        self._proc_name_cache = {}
        self._last_pick = {}
        self._rem_next = {}
        self._t = 0
        self.last_system_check = 0
        # 余额查询 / 时节特效状态
        self.balance_workers = []      # 同时查询多个服务商
        self._balance_results = {}     # provider -> (ok, amount/meta/err)
        self._balance_pending = 0
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

        # 主动陪伴（每 PROACTIVE_CHECK_SEC 检查一次）
        self._proactive_timer = QTimer()
        self._proactive_timer.timeout.connect(self._proactive_tick)
        self._proactive_timer.start(PROACTIVE_CHECK_SEC * 1000)

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

    def say_main(self, text, inner=False, voice=False):
        """聊天/提醒/节日/系统播报只由 main 显示。

        语音默认**关闭**（日常一律文字气泡）。只有显式 voice=True
        （叫它唱歌/念出来）时才走语音链路。
        """
        if self.main_pet is not None:
            self.main_pet.say(text, inner=inner)
        if voice and not inner:
            try:
                self.speak_voice(text)
            except Exception:
                pass

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

    # ---------- 主动搭话（工作时间段内、发呆时主动说一句）----------
    def proactive_cfg(self):
        """读「主动搭话」配置（补齐/夹紧，旧配置也兼容）。"""
        try:
            return PRO.normalize_cfg(self.cfg.get("proactive_ai"))
        except Exception as ex:
            log("[主动] 读配置失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)
            return PRO.default_cfg()

    def save_proactive_cfg(self, patch=None):
        """写「主动搭话」配置（patch 为要合并的字段）。"""
        cur = self.proactive_cfg()
        if isinstance(patch, dict):
            cur.update(patch)
        self.cfg["proactive_ai"] = PRO.normalize_cfg(cur)
        self.save_cfg()
        return self.cfg["proactive_ai"]

    def _proactive_own_hwnds(self):
        """桌宠自己的窗口句柄集合（这些不算「用户切换窗口」）。"""
        out = []
        try:
            for w in self.pets:
                try:
                    out.append(int(w.winId()))
                except Exception:
                    pass
            for attr in ("settings_window", "chat_window", "memory_dialog",
                         "rps_panel", "catch_win"):
                w = getattr(self, attr, None)
                if w is not None:
                    try:
                        out.append(int(w.winId()))
                    except Exception:
                        pass
        except Exception:
            pass
        return tuple(out)

    def _proactive_chat_ready(self):
        """聊天 Key/地址/模型是否可用。返回 (ok, base, key, model)。"""
        try:
            base, key, model = self._chat_cfg()
        except Exception:
            return False, "", "", ""
        ok = bool((key or "").strip() and (base or "").strip()
                  and (model or "").strip())
        return ok, base, key, model

    def _proactive_cooldown_left(self, cfg):
        """冷却剩余秒数（含「无视翻倍」的倍率）。"""
        st = getattr(self, "_pro_state", None)
        if st is None:
            return 0.0
        base = float(cfg.get("cooldown_min") or 0) * 60.0
        mult = float(getattr(st, "interval_mult", 1.0) or 1.0)
        need = base * mult
        elapsed = time.time() - float(getattr(st, "last_fire_ts", 0.0) or 0.0)
        return max(0.0, need - elapsed)

    def _proactive_tick(self):
        """定时轮询（只在工作时间段内真正工作，段外直接返回）。"""
        cfg = self.proactive_cfg()
        if not cfg.get("enabled"):
            return
        # ① 时间段门控：段外完全不轮询
        inside, _seg = PRO.in_work_session(cfg)
        if not inside:
            if getattr(self, "_pro_inside", False):
                # 刚离开时间段：进行中的计时作废
                self._pro_inside = False
                if getattr(self, "_pro_tracker", None) is not None:
                    self._pro_tracker.reset()
                log("[主动] 离开工作时间段，计时作废")
            return
        if not getattr(self, "_pro_inside", False):
            # 刚进入时间段：计时清零
            self._pro_inside = True
            if getattr(self, "_pro_tracker", None) is None:
                self._pro_tracker = PRO.StateTracker()
            else:
                self._pro_tracker.reset()
            log("[主动] 进入工作时间段，计时清零")

        # ② 其他暂停条件
        if self.game_active or self.chat_paused or self._in_dnd():
            return
        pet = self.main_pet
        if pet is None or not pet.isVisible():
            return

        # ③ 更新状态
        tracker = self._pro_tracker
        state, idle_sec, info = tracker.tick(
            cfg["idle_min"], cfg["away_min"], self._proactive_own_hwnds())
        self._pro_last_info = dict(info or {})

        # ④ 回来打招呼
        if (cfg.get("welcome_enabled")
                and state == PRO.ACTIVE
                and tracker.was_away()):
            if self._pro_can_welcome(cfg):
                _, pil = self._pro_grab_fullscreen_jpg()
                self._pro_fire(cfg, info, 0, welcome=True, shot_img=pil)
                return

        if state != PRO.IDLE:
            return
        if not tracker.can_fire():
            return          # 这次发呆已经搭过话

        # ⑤ 本地过滤（命中就不调模型）
        ready, base, key, model = self._proactive_chat_ready()
        st = self._pro_state
        st.roll_day()
        reason = PRO.local_filter(
            info, cfg, self._proactive_cooldown_left(cfg),
            st.used, ready)
        if reason:
            log("[主动] 跳过：%s" % reason)
            return

        # ⑥ 调模型（先在主线程截全屏，发给视觉模型观察）
        tracker.mark_fired()
        _, pil = self._pro_grab_fullscreen_jpg()
        self._pro_fire(cfg, info, idle_sec, welcome=False, shot_img=pil)

    def _pro_can_welcome(self, cfg):
        """回来打招呼是否允许（每日上限/冷却同样适用）。"""
        ready, _b, _k, _m = self._proactive_chat_ready()
        if not ready:
            return False
        st = self._pro_state
        st.roll_day()
        if st.used >= int(cfg.get("daily_max") or 0):
            return False
        if self._proactive_cooldown_left(cfg) > 0:
            return False
        return True

    def _pro_grab_fullscreen_jpg(self):
        """截当前屏幕全屏，返回 (QPixmap, PIL.Image)。失败返回 (None, None)。

        只在内存里，不落盘。截图前桌宠会短暂隐藏，避免拍到宠物自己。
        """
        try:
            screen = self._screen_under_cursor()
            pix, dpr = self._grab_screen_pixmap(screen)
            if pix is None or pix.isNull():
                return None, None
            buf = QBuffer()
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            pix.save(buf, "PNG")
            raw = bytes(buf.data())
            buf.close()
            from PIL import Image as _PILImage
            img = _PILImage.open(io.BytesIO(raw)).convert("RGB")
            return pix, img
        except Exception as ex:
            log("[主动] 截屏失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)
            return None, None

    def _pro_fire(self, cfg, info, idle_sec, welcome=False, shot_img=None):
        """真正调用模型（后台线程），成功后弹气泡。

        shot_img: PIL.Image（主线程截好的全屏图）。None 则不发截图。
        流程：视觉模型先「看」→ 把观察结果并进聊天消息 → 聊天模型说话。
        """
        ready, base, key, model = self._proactive_chat_ready()
        if not ready:
            return
        st = self._pro_state
        recent = list(getattr(st, "recent", []) or [])
        if welcome:
            system, user = PRO.build_welcome_messages(cfg, info, recent)
        else:
            system, user = PRO.build_messages(cfg, info, idle_sec, recent)

        # 截图观察（聊天场景/黑名单在 _proactive_tick 的本地过滤已拦，
        # 这里再兜底一次：微信/QQ 等聊天进程不发截图）
        observe_msgs = None
        vbase, vkey, vmodel = "", "", ""
        try:
            proc_low = (info.get("proc") or "").lower()
            private_hit = PRO._hit("%s %s" % (info.get("proc") or "",
                                              info.get("title") or ""),
                                   cfg.get("private_words") or [])
            if shot_img is not None and not private_hit and \
                    not any(k in proc_low for k in
                            ("wechat", "weixin", "qq", "telegram",
                             "dingtalk", "feishu", "lark", "discord")):
                _ls = self.cfg.get("look_screen") or {}
                vbase = (vbase or (_ls.get("vision_base") or "")).strip()
                vkey = (vkey or (_ls.get("vision_key") or "")).strip()
                vmodel = (vmodel or (_ls.get("vision_model") or "")).strip()
                if vbase and vkey and vmodel:
                    from petcore.consts import LOOK_SCREEN_QUALITY_DEFAULT
                    data_url = PRO.shrink_for_vision(
                        shot_img, max_side=1024,
                        quality=int(_ls.get("quality")
                                    or LOOK_SCREEN_QUALITY_DEFAULT))
                    observe_msgs = PRO.build_observe_messages(info, data_url)
        except Exception as ex:
            log("[主动] 组装截图观察失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            observe_msgs = None

        timeout = 45
        vtimeout = min(45, int((self.cfg.get("look_screen") or {})
                               .get("timeout") or 30))
        self._pro_req_id = getattr(self, "_pro_req_id", 0) + 1
        rid = self._pro_req_id

        def work():
            observe_text = ""
            if observe_msgs:
                obs, oerr = PRO.call_chat(vbase, vkey, vmodel, "", "",
                                          timeout=vtimeout, max_tokens=200,
                                          temperature=0.2, top_p=1.0,
                                          messages=observe_msgs)
                if obs:
                    observe_text = obs.strip()
                elif oerr:
                    log("[主动] 截图观察失败（降级为无观察继续）：%s" % oerr)
            # 把观察并进聊天 user 消息
            user2 = user
            if observe_text:
                user2 = user + "\n\n【屏幕观察】\n" + observe_text
            text, err = PRO.call_chat(base, key, model, system, user2,
                                      timeout=timeout)
            if err:
                log("[主动] 调用失败（静默）：%s" % err)
                return
            line = PRO.clean_reply(text)
            if not line:
                log("[主动] 模型返回 SKIP（静默）")
                return
            # 防幻觉：书名号里的专有名词若不在观察/窗口信息里，重生成一次
            bad, words = PRO.looks_hallucinated(line, user2)
            if bad:
                log("[主动] 疑似幻觉（%s），重生成一次" % "、".join(words[:3]))
                retry_hint = ("\n\n【重要】上一次你提到了"
                              + "、".join(words[:3])
                              + "，但提供的画面信息里没有这些。"
                              "不要编造作品名/歌名/游戏名，只用上面出现过的内容，"
                              "或者只描述你确实看到的部分。")
                text2, err2 = PRO.call_chat(base, key, model, system,
                                            user2 + retry_hint, timeout=timeout)
                if not err2:
                    line2 = PRO.clean_reply(text2)
                    if line2:
                        bad2, _w2 = PRO.looks_hallucinated(line2, user2)
                        if not bad2:
                            line = line2        # 重试干净就采用
                        else:
                            # 还编：把可疑书名号换成中性词
                            line = PRO.strip_hallucinated(line2, _w2)
                            log("[主动] 重试仍有幻觉，已做中性化处理")
                    else:
                        log("[主动] 重试返回 SKIP，本次不搭话")
                        return
                else:
                    # 重试失败：保留原回复并中性化
                    line = PRO.strip_hallucinated(line, words)
            if not line:
                return
            if rid != getattr(self, "_pro_req_id", 0):
                return
            self.pro_active_say.emit(line, bool(welcome))

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _on_proactive_say(self, line, welcome):
        """主线程：显示气泡 + 记录。"""
        pet = self.main_pet
        if pet is None:
            return
        try:
            pet.say(line)
            pet.bubble_until = pet.t * TICK / 1000.0 + 8.0   # 8 秒
            pet.update()
        except Exception as ex:
            log("[主动] 显示失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)
            return
        st = self._pro_state
        st.mark_fire(line)
        self._pro_pending = {"line": line, "info": self._pro_last_info,
                             "ts": time.time(), "welcome": welcome}
        # 写入聊天（与「看看屏幕」一致：聊天窗口开着就实时追加，否则写文件）
        try:
            label = "[主动打招呼]" if welcome else "[主动搭话]"
            pairs = [("user", label), ("assistant", line)]
            cw = self.chat_window
            if cw is not None:
                for role, content in pairs:
                    cw.append_external(role, content)
            else:
                self._append_pairs_to_chat_file(pairs)
        except Exception as ex:
            log("[主动] 写聊天失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)
        log("[主动] %s -> %s（今日第 %d 次）"
            % ("打招呼" if welcome else "搭话", line[:30], st.used))

    def _on_proactive_clicked(self):
        """用户点了主动搭话的气泡：进聊天页并带上上下文。"""
        pend = getattr(self, "_pro_pending", None)
        if not pend:
            return False
        try:
            self._pro_state.mark_clicked()
        except Exception:
            pass
        self._pro_pending = None
        # 带进聊天上下文
        try:
            self.open_chat()
            cw = self.chat_window
            if cw is not None:
                info = pend.get("info") or {}
                extra = ""
                if info.get("proc"):
                    extra = "（刚才在看 %s）" % info.get("proc")
                cw.append_external("user", "[宠物主动搭话] " + pend.get("line", ""))
                cw.append_external("assistant", pend.get("line", ""))
                log("[主动] 已带入聊天上下文%s" % extra)
        except Exception as ex:
            log("[主动] 带入聊天失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)
        return True

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
    def _mood_action(self, user_text, reply_text):
        """按聊天内容判断情绪，播一个对应动作（带冷却，不打断工作状态动画）。

        user_text: 用户这轮说的话
        reply_text: 宠物这轮的回复
        返回命中的情绪名或 ""。
        """
        try:
            # 只在「真正在干活」时让路；success/error 是终态，情绪可以覆盖
            if getattr(self, "_work_state", "idle") in ("thinking", "working"):
                return ""
            now = time.time()
            last = getattr(self, "_mood_last", 0.0)
            text = ((user_text or "") + " " + (reply_text or "")).lower()
            if not text.strip():
                return ""
            for name, keys, act, cool in MOOD_ACTIONS:
                if not any(k.lower() in text for k in keys):
                    continue
                # 该情绪自己的冷却
                cd = getattr(self, "_mood_cd", {})
                if now - float(cd.get(name, 0.0)) < cool:
                    return ""
                # 全局冷却
                if now - last < MOOD_COOLDOWN_DEFAULT:
                    return ""
                pet = self.main_pet
                if pet is None or act not in ACTIONS:
                    return ""
                # 停掉 success/error 的回 idle 定时器，避免 2.2s 后把情绪动作打断
                try:
                    self._work_hold_timer.stop()
                except Exception:
                    pass
                self._work_state = "idle"
                pet._start_chain_action(act)
                # 情绪动作要「一眼看出来」：跳过与待机几乎一样的开头帧，
                # 直接从 25% 处起播（害羞的蹲下捂脸、跳舞的抬手都在前中段）
                frames = pet._frame_list(act) or []
                if frames:
                    try:
                        pet._start_frame_anim(act, start_index=len(frames) // 4)
                    except Exception:
                        pass
                # 情绪动作播短一点（6 秒），尽快回到待机，避免拖沓
                pet.chain_dur = min(pet.chain_dur, 6.0)
                cd[name] = now
                self._mood_cd = cd
                self._mood_last = now
                log("[情绪] %s -> %s" % (name, act))
                return name
        except Exception as ex:
            log("[情绪] 判断失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)
        return ""

    # ---------- 语音 ----------
    def voice_cfg(self):
        """读语音配置（补齐/夹紧）。"""
        try:
            return VOICE.normalize_voice_cfg(self.cfg.get("voice"))
        except Exception:
            return dict(VOICE.DEFAULT_VOICE_CFG)

    def save_voice_cfg(self, patch=None):
        """写语音配置。"""
        cur = self.voice_cfg()
        if isinstance(patch, dict):
            cur.update(patch)
        self.cfg["voice"] = VOICE.normalize_voice_cfg(cur)
        self.save_cfg()
        return self.cfg["voice"]

    def speak_voice(self, text):
        """让宠物把这句话说出来（语音服务不可用时静默）。"""
        try:
            p = getattr(self, "_voice_player", None)
            if p is None:
                return False
            return p.speak(text)
        except Exception as ex:
            log("[语音] 说话失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)
            return False

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
        prov = str(b.get("provider") or "auto").lower()
        if prov not in ("auto", "deepseek", "zhipu"):
            prov = "auto"
        return {
            "auto": bool(b.get("auto", False)),
            "interval_min": max(BALANCE_MIN_INTERVAL_MIN,
                                int(b.get("interval_min", BALANCE_MIN_INTERVAL_MIN) or 0)),
            "last_value": b.get("last_value"),
            "last_time": b.get("last_time"),
            "provider": prov,
        }

    def _balance_provider(self):
        """决定这次查哪家：显式配置优先，auto 时看哪个 Key 有值。"""
        cfg = self._balance_cfg()
        p = cfg.get("provider") or "auto"
        if p in ("deepseek", "zhipu"):
            return p
        # auto：优先智谱（若配了视觉 Key），否则 DeepSeek
        ls = self.cfg.get("look_screen") or {}
        if (ls.get("vision_key") or "").strip():
            return "zhipu"
        return "deepseek"

    def _balance_key(self, provider):
        """取对应服务商的 Key。"""
        if provider == "zhipu":
            ls = self.cfg.get("look_screen") or {}
            return (ls.get("vision_key") or "").strip()
        return (self.cfg.get("ds_api_key") or "").strip()


    def _save_balance_cfg(self, patch):
        b = self.cfg.get("balance")
        if not isinstance(b, dict):
            b = {}
        b.update(patch)
        self.cfg["balance"] = b
        self.save_cfg()

    def query_balance(self, manual=True):
        """查询余额：**同时**查 DeepSeek 与智谱，两边都回来后一起播报。"""
        running = [w for w in self.balance_workers if w is not None and w.isRunning()]
        if running:
            if manual:
                self.say_main("正在查余额，稍等一下…")
            return
        # 收集要查的服务商（有 Key 的才查）
        targets = []
        scope = (self._balance_cfg().get("provider") or "auto")
        if scope == "deepseek":
            order = ("deepseek",)
        elif scope == "zhipu":
            order = ("zhipu",)
        else:
            order = ("deepseek", "zhipu")   # 两家同时查
        for prov in order:
            key = self._balance_key(prov)
            if key:
                targets.append((prov, key))
        # DeepSeek 余额接口只对 api.deepseek.com 有效；
        # 换了别家就跳过（不报错），只查智谱或给出说明。
        _cb = (self.cfg.get("chat_base") or "").strip().lower()
        _deepseek_ok = ("deepseek" in _cb) if _cb else True
        if not _deepseek_ok:
            targets = [(pv, k) for (pv, k) in targets if pv != "deepseek"]
            log("[余额] 聊天地址非 DeepSeek（%s），跳过 DeepSeek 余额" % _cb)


        if not targets:
            if not _deepseek_ok:
                self.say_main("当前聊天服务商不支持查询余额哦~"
                              "（只有 DeepSeek 和智谱能查）")
            else:
                self.say_main("还没设置 API Key：右键桌宠 →「设置 Key」"
                              "（智谱 Key 在 设置 →「视觉」）")
            return

        self.balance_workers = []
        self._balance_results = {}
        self._balance_pending = len(targets)
        for prov, key in targets:
            w = BalanceWorker(key, provider=prov)
            w.done.connect(lambda amt, meta, pv=prov: self._on_one_balance(pv, True, amt, meta))
            w.failed.connect(lambda msg, pv=prov: self._on_one_balance(pv, False, None, msg))
            self.balance_workers.append(w)
            w.start()

    def _on_one_balance(self, provider, ok, amount, payload):
        """单家结果回来：先存着，等全部回来再统一播报。"""
        self._balance_results[provider] = (ok, amount, payload)
        self._balance_pending = max(0, self._balance_pending - 1)
        if self._balance_pending > 0:
            return
        self._report_balances()

    def _report_balances(self):
        """把各家的余额合并成一条播报：只显示「模型 + 金额」。"""
        results = self._balance_results or {}
        if not results:
            return
        parts = []
        total = 0.0
        got_any = False
        for prov in ("deepseek", "zhipu"):
            if prov not in results:
                continue
            ok, amount, payload = results[prov]
            label = "智谱" if prov == "zhipu" else "DeepSeek"
            if not ok:
                parts.append("%s 查询失败" % label)
                continue
            got_any = True
            try:
                total += float(amount)
            except Exception as ex:
                log("[%s] %s 失败: %s: %s"
                    % (__name__, "_report_balances", type(ex).__name__, ex),
                    logging.WARNING)
            parts.append("%s ¥%.2f" % (label, amount))

        if not got_any:
            self.say_main("余额查询失败：" + "；".join(parts))
            return

        text = " / ".join(parts)
        self._save_balance_cfg({"last_value": float(total),
                                "last_time": time.time()})
        self.say_main(text)
        # 档位动画按合计判断
        tier = self._balance_tier(total)
        pet = self.main_pet
        if pet is not None and tier is not None:
            name = tier[2]
            if name in ACTIONS:
                pet._start_chain_action(name)
            elif name in CLICK_RESPONSES:
                pet._play_temp(name, total=CLICK_RESPONSES[name]["duration"], squash=True)
            line = self._pick_line(tier[3], "balance_" + tier[1])
            QTimer.singleShot(3200, lambda t=line: self.say_main(t))


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

    # ---------- 看看屏幕 ----------
    def _foreground_title(self):
        """取前台窗口「程序名 | 标题」，失败返回空串。"""
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            h = user32.GetForegroundWindow()
            if not h:
                return ""
            # 标题
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(h, buf, 512)
            title = (buf.value or "").strip()
            # 进程名
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            name = ""
            try:
                import psutil
                name = psutil.Process(pid.value).name()
            except Exception:
                name = ""
            if name and title:
                return "%s | %s" % (name, title)
            return title or name
        except Exception as ex:
            log("取前台窗口失败: %s" % ex, logging.DEBUG)
            return ""

    # ---------- 看看屏幕：框选截图 ----------
    def _purge_default_vision_system(self):
        """启动时：如果配置里的规则副本等于任一历史默认值，清除它。

        这样默认规则变化时，未自定义过的用户自动跟随新版。
        """
        ls = self.cfg.get("look_screen")
        if not isinstance(ls, dict):
            return
        cur = (ls.get("vision_system") or "").strip()
        if not cur:
            return
        if is_default_vision_system(cur):
            ls["vision_system"] = ""
            self.cfg["look_screen"] = ls
            try:
                self.save_cfg()
                log("[视觉] 已清除过期的默认规则副本，将跟随新版默认")
            except Exception as ex:
                log("[视觉] 清除规则副本后保存失败: %s: %s"
                    % (type(ex).__name__, ex), logging.WARNING)


    def _migrate_chat_cfg(self):
        """旧配置迁移：**只做一次**。

        用 chat_migrated 标记保证：迁移后即使手动清空 chat_base，
        重启也不会再根据 ds_api_key 重新写入。
        """
        if self.cfg.get("chat_migrated"):
            return   # 已迁移过，不再动
        key = (self.cfg.get("ds_api_key") or "").strip()
        if not key:
            return   # 没 Key，无需迁移（下次有 Key 时再做）
        self.cfg["chat_base"] = DS_BASE_URL
        self.cfg["chat_model"] = DS_MODEL
        self.cfg["chat_migrated"] = True
        try:
            self.save_cfg()
            log("[聊天] 旧配置迁移完成（chat_migrated=True）")
        except Exception as ex:
            log("[聊天] 迁移保存失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

    def _chat_cfg(self):
        """聊天配置：返回 (base_url, api_key, model)。

        运行时**不回落**：base/model 为空就是空，由调用方判断未配置。
        """
        base = (self.cfg.get("chat_base") or "").strip()
        key = (self.cfg.get("ds_api_key") or "").strip()
        model = (self.cfg.get("chat_model") or "").strip()
        return base, key, model

    def _chat_cfg_raw(self):
        """界面上要显示的值（迁移后应始终有值）。"""
        return self._chat_cfg()


    def _vision_cfg(self):
        """视觉配置（独立于聊天 Key）：返回 (base_url, api_key, model_name)。

        模型名为空时**不做任何回落**，按「未配置」处理
        （由 _vision_ready_check 提示用户去点「测试」）。
        """
        ls = self.cfg.get("look_screen") or {}
        base = (ls.get("vision_base") or "").strip()
        key = (ls.get("vision_key") or "").strip()
        model = (ls.get("vision_model") or "").strip()
        return base, key, model


    def _vision_style_cfg(self):
        """描述风格与单次上限（独立于聊天页）。返回 (后缀句, max_tokens)。"""
        ls = self.cfg.get("look_screen") or {}
        style = ls.get("style") or VISION_STYLE_DEFAULT
        suffix = vision_style_suffix(style)
        try:
            mt = int(ls.get("max_tokens") or VISION_TOKENS_DEFAULT)
        except Exception:
            mt = VISION_TOKENS_DEFAULT
        mt = max(VISION_TOKENS_MIN, min(VISION_TOKENS_MAX, mt))
        return suffix, mt

    def _vision_system_cfg(self):
        """视觉请求的 system 文本。

        结构：识别规则（可编辑）+ 空行 + 描述风格长度句（自动追加）。
        配置里只在用户自定义时才存副本；副本等于历史默认值时视为默认。
        """
        ls = self.cfg.get("look_screen") or {}
        body = (ls.get("vision_system") or "").strip()
        if not body or is_default_vision_system(body):
            body = VISION_SYSTEM_DEFAULT
        suffix = vision_style_suffix(ls.get("style") or VISION_STYLE_DEFAULT)
        if suffix:
            return body + chr(10) + chr(10) + suffix
        return body

    def _vision_limits(self):
        """视觉请求限制：返回 (max_side, quality, timeout)。"""
        ls = self.cfg.get("look_screen") or {}

        def _int(key, lo, hi, dv):
            try:
                v = int(ls.get(key) or dv)
            except Exception:
                v = dv
            return max(lo, min(hi, v))

        side = _int("max_side", LOOK_SCREEN_SIDE_MIN, LOOK_SCREEN_SIDE_MAX,
                    LOOK_SCREEN_SIDE_DEFAULT)
        qual = _int("quality", LOOK_SCREEN_QUALITY_MIN, LOOK_SCREEN_QUALITY_MAX,
                    LOOK_SCREEN_QUALITY_DEFAULT)
        tout = _int("timeout", LOOK_SCREEN_TIMEOUT_MIN, LOOK_SCREEN_TIMEOUT_MAX,
                    LOOK_SCREEN_TIMEOUT_DEFAULT)
        return side, qual, tout


    def _vision_ready_check(self):
        """进入框选前的检查：三项任一为空就不进入。

        返回 (ok, 提示文字)。
        """
        base, key, model = self._vision_cfg()
        if not key:
            return False, "还没填视觉 Key 呢，笨蛋主人——去「设置 → 视觉」填一个吧。"
        if not base or not model:
            return False, ("只有 Key 还不行哦，得在「设置 → 视觉」里"
                           "点一下「测试」，让我认出服务商和模型。")
        return True, ""

    def _screen_under_cursor(self):
        """返回鼠标所在屏幕（没有则用主屏）。"""
        try:
            pos = QCursor.pos()
            sc = QApplication.screenAt(pos)
            if sc is not None:
                return sc
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_screen_under_cursor", type(ex).__name__, ex),
                logging.WARNING)
        return QApplication.primaryScreen()

    def _grab_screen_pixmap(self, screen):
        """截指定屏幕，返回 (QPixmap, 设备像素比)。只在内存里。"""
        try:
            if screen is None:
                return None, 1.0
            geo = screen.geometry()          # 逻辑像素
            dpr = float(screen.devicePixelRatio() or 1.0)
            # 按物理尺寸截，避免高 DPI 模糊
            pw = int(round(geo.width() * dpr))
            ph = int(round(geo.height() * dpr))
            shot = screen.grabWindow(0, 0, 0, pw, ph)
            if shot is None or shot.isNull():
                return None, dpr
            return shot, dpr
        except Exception as ex:
            log("[看看屏幕] 截图失败: %s" % ex, logging.WARNING)
            return None, 1.0

    def _begin_region_select(self, pet=None):
        """右键「看看屏幕」入口：记录前台窗口 → 检查 → 隐藏 → 框选 → 后续流程。

        关键：get_fg_info() 必须在框选开始前调用，否则拿到的是截图工具窗口。
        """
        speaker = pet or self.main_pet

        # ① 先记前台窗口（此刻焦点还在用户的应用上）
        try:
            self._look_fg_info = SV.get_fg_info()
            log("[看看屏幕] 前台窗口: %s / %s"
                % (self._look_fg_info[0] or "无", self._look_fg_info[1] or "无"))
        except Exception as ex:
            self._look_fg_info = ("", "")
            log("[看看屏幕] 取前台窗口失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)

        # ② 视觉配置检查
        ok, tip = self._vision_ready_check()
        if not ok:
            self.say_main(tip)
            return

        # ③ 冷却（防止连点反复弹遮罩）
        now = time.time()
        if now - getattr(self, "_look_last", 0.0) < LOOK_SCREEN_COOLDOWN:
            self.say_main("别急，%d 秒后再看～" % int(LOOK_SCREEN_COOLDOWN))
            return
        self._look_last = now

        # ④ 记住原本可见的宠物，稍后恢复
        self._look_was_visible = [w for w in self.pets if w.isVisible()]
        for w in self._look_was_visible:
            self.set_visible(w.pet_id, False, "capture")

        # ⑤ 等窗口真正消失后再截底图（否则会拍到桌宠）
        QTimer.singleShot(150, lambda: self._do_region_select())

    def _do_region_select(self):
        """截底图 → 弹遮罩。"""
        try:
            screen = self._screen_under_cursor()
            pix, dpr = self._grab_screen_pixmap(screen)
            if pix is None or pix.isNull():
                self._restore_after_select()
                self.say_main("截图失败了……")
                return
            geo = screen.geometry()
            # 底图缩到逻辑尺寸显示，选区坐标即逻辑像素
            disp = pix.scaled(geo.width(), geo.height(),
                              Qt.AspectRatioMode.IgnoreAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
            from petcore.region_select import RegionSelectOverlay
            ov = RegionSelectOverlay(disp, geo)
            ov.selected.connect(lambda r, d=dpr, p=pix: self._on_region_selected(r, d, p))
            ov.cancelled.connect(self._on_region_cancelled)
            # 保留引用，防止被 GC
            self._region_overlay = ov
            ov.showFullScreen()
            ov.activateWindow()
            ov.setFocus()
        except Exception as ex:
            log("[看看屏幕] 打开框选失败: %s" % ex, logging.WARNING)
            self._restore_after_select()
            self.say_main("框选打不开呢……")

    def _restore_after_select(self):
        """框选结束后恢复桌宠显示。"""
        for w in getattr(self, "_look_was_visible", []) or []:
            try:
                self.set_visible(w.pet_id, True, "capture")
            except Exception as ex:
                log("[%s] %s 失败: %s: %s"
                    % (__name__, "_restore_after_select", type(ex).__name__, ex),
                    logging.WARNING)
        self._look_was_visible = []

    def _release_region_overlay(self):
        """释放遮罩与截图（不落盘）。"""
        ov = getattr(self, "_region_overlay", None)
        if ov is not None:
            try:
                ov._base = None
                ov.hide()
                ov.deleteLater()
            except Exception as ex:
                log("[%s] %s 失败: %s: %s"
                    % (__name__, "_release_region_overlay", type(ex).__name__, ex),
                    logging.WARNING)
        self._region_overlay = None

    def _on_region_cancelled(self):
        """取消：不发请求，用口吻回一句。"""
        self._release_region_overlay()
        self._restore_after_select()
        pool = ["不看了吗？", "哼，不看就不看。", "……那你叫我看干嘛。",
                "笨蛋主人，耍我玩呢？", "不看啦？那我自己玩去了。"]
        self.say_main(self._pick_line(pool, "look_cancel") or "不看了吗？")

    def _on_region_selected(self, rect, dpr, base_pix):
        """框选完成：裁剪 → 缩放 → 走视觉 + 聊天两步流程。"""
        try:
            region = None
            try:
                # 逻辑坐标 × dpr = 物理像素
                x = int(round(rect.x() * dpr))
                y = int(round(rect.y() * dpr))
                w = int(round(rect.width() * dpr))
                h = int(round(rect.height() * dpr))
                if w < LOOK_SCREEN_MIN_SELECTION or h < LOOK_SCREEN_MIN_SELECTION:
                    self._on_region_cancelled()
                    return
                img = base_pix.toImage()
                region = img.copy(QRect(x, y, w, h))
                if region.isNull():
                    self._on_region_cancelled()
                    return
                # 缩放到长边 ≤ 1024
                rw, rh = region.width(), region.height()
                mx = max(rw, rh)
                _side, _q, _t = self._vision_limits()
                if mx > _side:
                    k = _side / float(mx)
                    region = region.scaled(max(1, int(rw * k)), max(1, int(rh * k)),
                                           Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation)
                log("[看看屏幕] 发送了一张 %d×%d 的截图" % (region.width(), region.height()))
            finally:
                # 无论成功与否，立刻释放底图
                base_pix = None
            self._release_region_overlay()
            self._restore_after_select()
            if region is None or region.isNull():
                self.say_main("选区怪怪的，再试一次吧")
                return
            self._run_look_screen(region)
        except Exception as ex:
            log("[看看屏幕] 处理选区失败: %s" % ex, logging.WARNING)
            self._release_region_overlay()
            self._restore_after_select()
            self.say_main("瞧不清呢……")

    def _save_look_shot(self, pil_img):
        """把这次看屏的截图存到用户数据目录，返回路径（失败返回 ""）。

        只在「把截图原图也存进聊天记录」开启时调用。
        文件名带时间戳，最多保留最近 50 张，避免无限增长。
        """
        try:
            import os as _os
            d = _os.path.join(APP_DIR, "look_shots")
            _os.makedirs(d, exist_ok=True)
            fn = time.strftime("%Y%m%d_%H%M%S") + ".jpg"
            path = _os.path.join(d, fn)
            img = pil_img
            w, h = img.size
            mx = max(w, h)
            if mx > 1600:
                s = 1600.0 / mx
                img = img.resize((max(1, int(w * s)), max(1, int(h * s))))
            img.save(path, "JPEG", quality=85)
            # 只保留最近 50 张
            try:
                files = sorted(
                    (_os.path.join(d, f) for f in _os.listdir(d)
                     if f.lower().endswith((".jpg", ".png"))),
                    key=lambda x: _os.path.getmtime(x))
                for old in files[:-50]:
                    try:
                        _os.remove(old)
                    except Exception:
                        pass
            except Exception:
                pass
            return path
        except Exception as ex:
            log("[看看屏幕] 保存截图失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            return ""

    def _run_look_screen(self, image):
        """两步流程：analyze_screen（视觉+OCR+场景）→ 聊天模型按人设回复。

        analyze_screen 内部会：
          - 按前台窗口/标题分类场景，选对应提示词
          - 预处理图片（小选区放大）
          - 非聊天场景做 OCR，结果作为【参考文字】给视觉模型
          - 组装给聊天模型的消息（含上次看屏 + 避免重复的角度）
        """
        base, key, model = self._vision_cfg()
        _side, _quality, _timeout = self._vision_limits()
        _suffix, vis_tokens = self._vision_style_cfg()
        speaker = self.main_pet
        if speaker is not None:
            speaker.say("看看中…")

        # QImage → PIL.Image（只在内存）
        holder = {"pil": None, "fg": getattr(self, "_look_fg_info", ("", "")),
                  "shot_path": ""}
        try:
            buf = QBuffer()
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buf, "PNG")          # 无损转出，后面由 SV 统一压 JPEG
            raw_png = bytes(buf.data())
            buf.close()
            image = None
            from PIL import Image as _PILImage
            holder["pil"] = _PILImage.open(io.BytesIO(raw_png)).convert("RGB")
            # 「把截图原图也存进聊天记录」开启时，落盘一份
            _ls_cfg = self.cfg.get("look_screen") or {}
            if _ls_cfg.get("save_shot"):
                holder["shot_path"] = self._save_look_shot(holder["pil"])
        except Exception as ex:
            log("[看看屏幕] 图片转换失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            self.say_main("截图处理失败了呢……")
            return

        # 用户自定义视觉提示词（空则 None，交给场景提示词）
        _rule = (self.cfg.get("look_screen") or {}).get("vision_system") or ""
        _override = None
        if _rule.strip() and not is_default_vision_system(_rule):
            _override = _rule.strip()

        def vision_call(messages):
            """视觉请求（OpenAI 兼容 messages），返回字符串。temperature=0.2。"""
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": vis_tokens,
                "temperature": 0.2,
            }
            r = requests.post(
                vision_join(base, "chat/completions"),
                headers={"Authorization": "Bearer " + key,
                         "Content-Type": "application/json"},
                json=payload, timeout=_timeout)
            if r.status_code != 200:
                raise RuntimeError(_http_error_message(r.status_code, r.text,
                                                      kind="vision"))
            d = r.json()
            ch = (d.get("choices") or [{}])[0]
            msg = ch.get("message") or {}
            txt = (msg.get("content") or "").strip()
            if not txt and ch.get("finish_reason") == "length":
                # token 不够，加大重试一次
                payload["max_tokens"] = min(VISION_TOKENS_MAX, vis_tokens * 2)
                r2 = requests.post(
                    vision_join(base, "chat/completions"),
                    headers={"Authorization": "Bearer " + key,
                             "Content-Type": "application/json"},
                    json=payload, timeout=_timeout)
                if r2.status_code == 200:
                    m2 = (r2.json().get("choices") or [{}])[0].get("message") or {}
                    txt = (m2.get("content") or "").strip()
            if not txt:
                reason = (msg.get("reasoning_content") or "").strip()
                if reason:
                    txt = reason.split(chr(10))[-1].strip()[:80]
            return txt

        def run():
            try:
                pil = holder["pil"]
                fg = holder["fg"]
                if pil is None:
                    return
                chat_msg, scene, state = SV.analyze_screen(
                    pil, fg, self._screen_memory, vision_call,
                    is_selection=True,
                    vision_system_override=_override)
                holder["pil"] = None
                log("[看看屏幕] 场景=%s 状态=%s" % (scene, (state or "")[:30]))
                if not chat_msg:
                    self.say_main("我看了半天，没看出啥……")
                    return
                self._look_screen_to_chat_desc(
                    chat_msg, scene, state,
                    shot_path=holder.get("shot_path") or "")
            except Exception as ex:
                log("[看看屏幕] analyze_screen 失败: %s: %s"
                    % (type(ex).__name__, ex), logging.WARNING)
                self.say_main("瞧不清呢……%s" % str(ex)[:30])
            finally:
                holder["pil"] = None

        threading.Thread(target=run, daemon=True).start()

    def _look_screen_to_chat(self, title, text, ok=True, desc=None,
                             shot_path=""):
        """把「看看屏幕」的结果写进聊天（**线程安全入口**）。

        可能在 worker 线程被调用，因此只 emit 信号；
        真正的 UI/落盘在 _on_look_to_chat 里（主线程执行）。
        """
        try:
            self.look_to_chat.emit(str(title or ""), str(text or ""),
                                   str(desc or "") + "\x00" + str(shot_path or ""))
            return True
        except Exception as ex:
            log("[看看屏幕] 发信号失败: %s" % ex, logging.WARNING)
            return False

    def _on_look_to_chat(self, title, text, desc):
        """主线程：把结果写进聊天窗口/文件。

        默认**不写**「[屏幕解析] …」到历史（只用于本次回复）；
        高级设置里开启「保存屏幕描述到聊天记录」后才写描述；
        开启「把截图原图也存进聊天记录」后，还会带一张截图缩略图。
        """
        try:
            # desc 里可能带 \x00 分隔的截图路径
            shot_path = ""
            if isinstance(desc, str) and "\x00" in desc:
                desc, shot_path = desc.split("\x00", 1)
            user_line = "[看看屏幕] %s" % (title or "框选")
            save_desc = bool((self.cfg.get("look_screen") or {}).get("save_desc", False))
            save_shot = bool((self.cfg.get("look_screen") or {}).get("save_shot", False))
            if not save_shot:
                shot_path = ""
            pairs = []
            if desc and save_desc:
                pairs.append(("user", "[屏幕解析] " + desc))
            pairs.append(("user", user_line))
            pairs.append(("assistant", text))

            cw = self.chat_window
            if cw is not None:
                for role, content in pairs:
                    # 截图挂在第一条 user 消息上
                    img = shot_path if (role == "user"
                                        and content == user_line) else None
                    cw.append_external(role, content, image_path=img)
                log("[看看屏幕] 已同步到聊天（描述%s写入，截图%s）"
                    % ("已" if (desc and save_desc) else "未",
                       "已" if shot_path else "未"))
                return
            if self._append_pairs_to_chat_file(pairs, shot_path=shot_path):
                log("[看看屏幕] 已写入聊天记录（描述%s写入，截图%s）"
                    % ("已" if (desc and save_desc) else "未",
                       "已" if shot_path else "未"))
        except Exception as ex:
            log("[看看屏幕] 写聊天失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)


    def _append_pairs_to_chat_file(self, pairs, shot_path=""):
        """不开聊天窗口，把多条 (role, content) 追加到 chats.json 当前会话。

        文件不存在时自动创建（首次使用也能看到记录）。
        """
        try:
            data = None
            if os.path.exists(CHAT_PATH):
                try:
                    with open(CHAT_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = None
            if not isinstance(data, dict):
                data = {"sessions": [], "current": None}
            sessions = data.get("sessions")
            if not isinstance(sessions, list):
                sessions = []
                data["sessions"] = sessions
            # 没有会话就建一个
            if not sessions:
                sid = time.strftime("%Y%m%d%H%M%S") + "000"
                sessions.append({
                    "id": sid, "title": "看看屏幕", "messages": [],
                    "summary": "", "summarized_upto": 0,
                    "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                data["current"] = sid
            cur = data.get("current")
            sess = None
            for s in sessions:
                if s.get("id") == cur:
                    sess = s
                    break
            if sess is None:
                sess = max(sessions, key=lambda x: x.get("updated") or "")
                data["current"] = sess.get("id")
            msgs = sess.setdefault("messages", [])
            for role, content in pairs:
                content = (content or "").strip()
                if content:
                    item = {"role": role, "content": content}
                    # 截图挂在第一条 user 消息上
                    if (shot_path and role == "user"
                            and content.startswith("[看看屏幕]")):
                        item["image"] = shot_path
                    msgs.append(item)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            sess["updated"] = stamp
            data["updated"] = stamp
            tmp = CHAT_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CHAT_PATH)
            return True
        except Exception as ex:
            log("[看看屏幕] 写聊天记录失败: %s" % ex, logging.WARNING)
            return False


    def _append_to_chat_file(self, user_text, assistant_text):
        """不开聊天窗口，直接把两条消息追加到 chats.json 的当前会话。"""
        try:
            if not os.path.exists(CHAT_PATH):
                return False
            with open(CHAT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            sessions = data.get("sessions") or []
            if not sessions:
                return False
            cur = data.get("current")
            sess = None
            for s in sessions:
                if s.get("id") == cur:
                    sess = s
                    break
            if sess is None:
                sess = max(sessions, key=lambda x: x.get("updated") or "")
            msgs = sess.setdefault("messages", [])
            msgs.append({"role": "user", "content": user_text})
            msgs.append({"role": "assistant", "content": assistant_text})
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            sess["updated"] = stamp
            data["updated"] = stamp
            tmp = CHAT_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CHAT_PATH)
            return True
        except Exception as ex:
            log("[看看屏幕] 写聊天记录失败: %s" % ex, logging.WARNING)
            return False


    def _look_screen_to_chat_desc(self, chat_msg, scene="general", state="",
                                  shot_path=""):
        """把 analyze_screen 组装好的消息交给聊天模型，用大肥鱼人设回一句。"""
        try:
            self._ask_chat_for_look(chat_msg, chat_msg, scene=scene, state=state,
                                    shot_path=shot_path)
        except Exception as ex:
            log("[看看屏幕] 交给聊天失败: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            self.say_main(chat_msg[:80])

    def _ask_chat_for_look(self, line, desc, scene="general", state="",
                           shot_path=""):
        """把 [屏幕观察] 交给聊天模型，用大肥鱼人设回一句。

        - system 末尾追加 CHAT_LOOK_RULES（只在看屏路径）
        - temperature 0.9 / top_p 0.95（更活泼，减少模板化）
        - 反套路：新回复开头两字若与最近 3 次相同，重新生成一次（最多重试 1 次）
        - 回复经 postprocess_reply 处理
        - 成功后 mem.save(scene, state, reply)
        聊天 Key 不可用（未配置/失效/网络错误）时降级显示视觉描述。
        """
        key = (self.cfg.get("ds_api_key") or "").strip()
        if not key:
            self._look_fallback(desc, "未配置聊天 Key")
            return
        style = self.cfg.get("reply_style", REPLY_STYLE_DEFAULT)
        persona = persona_from_cfg(self.cfg)
        sys_text = build_system_prompt(style, persona)
        try:
            sys_text = self.memory.system_content(sys_text)
        except Exception as ex:
            log("[%s] %s 失败: %s: %s"
                % (__name__, "_ask_chat_for_look", type(ex).__name__, ex),
                logging.WARNING)
        # 只在看屏路径追加规则
        sys_text = sys_text + SV.CHAT_LOOK_RULES
        max_tokens = int(self.cfg.get("max_tokens", MAX_TOKENS_DEFAULT))
        base_url, _ck, model = self._chat_cfg()
        mem = self._screen_memory

        # 最近 3 次回复的开头两字（反套路比对用）
        recent_heads = set()
        try:
            for rp in (mem.recent_replies() or [])[-3:]:
                h = SV.reply_head(rp)
                if h:
                    recent_heads.add(h)
        except Exception:
            pass

        def _ask_once(extra_hint=""):
            """发一次聊天请求，返回 (text, why)。"""
            user_msg = line + (("\n" + extra_hint) if extra_hint else "")
            messages = [
                {"role": "system", "content": sys_text},
                {"role": "user", "content": user_msg},
            ]
            try:
                r = requests.post(
                    base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + key,
                             "Content-Type": "application/json"},
                    json={"model": model, "messages": messages,
                          "max_tokens": max_tokens,
                          "temperature": 0.9,
                          "top_p": 0.95},
                    timeout=120)
                if r.status_code == 200:
                    try:
                        return (r.json()["choices"][0]["message"]["content"] or "").strip(), ""
                    except Exception:
                        return "", ""
                why = "HTTP %s" % r.status_code
                log("[看看屏幕] 聊天模型不可用（%s），降级显示视觉描述" % why)
                return "", why
            except Exception as ex:
                why = str(ex)[:40]
                log("[看看屏幕] 聊天请求失败（%s），降级显示视觉描述" % why)
                return "", why

        def run():
            text, why = _ask_once()
            # 反套路：开头与最近 3 次撞了 → 重生成一次
            if text and recent_heads:
                head = SV.reply_head(text)
                if head and head in recent_heads:
                    log("[看看屏幕] 开头「%s」与最近重复，重新生成一次" % head)
                    hint = ("（上一次你的开头是「%s」，和之前重复了。"
                            "这次换一个完全不同的开头和句式，不要用“哼”“笨蛋主人”"
                            "“才不是”开头，直接说画面里的东西。）" % head)
                    text2, why2 = _ask_once(hint)
                    if text2:
                        head2 = SV.reply_head(text2)
                        # 新回复开头也不撞才采用；仍撞就保留第一次的
                        if head2 and head2 not in recent_heads:
                            text = text2
                        else:
                            log("[看看屏幕] 重试后开头「%s」仍重复，保留原回复" % head2)
                    elif why2:
                        why = why2

            if text:
                text = SV.postprocess_reply(text)
                self.say_main(text)
                self._look_screen_to_chat("(框选)", text, ok=True, desc=desc,
                                          shot_path=shot_path)
                try:
                    mem.save(scene, state, text)
                except Exception as ex:
                    log("[看看屏幕] 保存看屏记忆失败: %s: %s"
                        % (type(ex).__name__, ex), logging.WARNING)
            else:
                self._look_fallback(desc, why)

        threading.Thread(target=run, daemon=True).start()

    def _look_fallback(self, desc, why=""):
        """聊天模型不可用时：把视觉描述用人设口吻显示（不写历史）。"""
        if why:
            log("[看看屏幕] 降级原因：%s" % why)
        text = "唔……我看到了：" + (desc or "（看不清）")
        if len(text) > 120:
            text = text[:118] + "…"
        # 只显示气泡，不写聊天历史
        self.say_main(text)


    def _get_weather(self, pet=None):
        """查看天气：网络请求放子线程，结果通过信号回主线程。"""
        speaker = pet or self.main_pet
        city = self.cfg.get("city", "汕头")
        log("天气请求城市: %s" % city)
        if speaker is not None:
            speaker.say("让我看看天气…")
        self._weather_req_id = getattr(self, "_weather_req_id", 0) + 1
        rid = self._weather_req_id

        def work():
            text = ""
            try:
                r = requests.get(
                    "https://wttr.in/%s?format=j1" % city,
                    timeout=10,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                log("天气接口状态: %s" % r.status_code)
                data = r.json()
                weather = data["current_condition"][0]
                temp = weather["temp_C"]
                raw_weather = (weather.get("weatherDesc") or [{}])[0].get("value", "")
                desc = weather_cn(raw_weather)
                tip = weather_tip(raw_weather)
                # 认不出的天气名不硬塞英文，只说温度
                if desc:
                    text = "%s今天%s°，%s。%s" % (city, temp, desc, tip)
                else:
                    text = "%s今天%s°。%s" % (city, temp, tip)
            except Exception as ex:
                log("天气错误: %s: %s" % (type(ex).__name__, ex), logging.WARNING)
                text = "天气查不到了……不是我不肯说，是它坏了"
            self.weather_done.emit(text, rid)

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _on_weather_done(self, text, rid):
        """主线程：显示天气结果（丢弃过期的旧请求）。"""
        if rid != getattr(self, "_weather_req_id", 0):
            return
        if text:
            self.say_main(text)

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

        m.addAction("看看屏幕", lambda: self._begin_region_select(pet))
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
