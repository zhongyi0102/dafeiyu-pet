# -*- coding: utf-8 -*-
"""看看屏幕 —— 框选截图遮罩。

设计要点：
  - 底图**只在内存里**（QPixmap），不写任何文件
  - 全屏无边框置顶，显示底图并略微变暗，十字光标
  - 左键拖拽框选：选中区域保持原亮度 + 显示尺寸
  - 松开左键确认；Esc / 右键取消
  - 兼容多显示器与高 DPI：遮罩只覆盖鼠标所在那块屏幕，
    选区坐标（逻辑像素）× 设备像素比 = 物理像素，用于裁剪
"""

from PySide6.QtCore import Qt, QRect, QPoint, Signal
from PySide6.QtGui import QPainter, QPixmap, QColor, QPen, QFont, QCursor
from PySide6.QtWidgets import QWidget


class RegionSelectOverlay(QWidget):
    """框选遮罩：拿到用户选中的矩形（逻辑像素，相对本屏幕左上角）。"""

    # 选中区域（逻辑像素）；取消时发 cancelled
    selected = Signal(QRect)
    cancelled = Signal()

    DIM_ALPHA = 110          # 非选区变暗程度
    MIN_SIDE = 20            # 小于这个边长视为取消

    def __init__(self, base_pixmap, screen_geo, parent=None):
        super().__init__(parent)
        self._base = base_pixmap          # 底图（内存）
        self._geo = screen_geo            # 该屏幕的逻辑几何
        self._origin = None               # 拖拽起点（逻辑坐标）
        self._cur = None                  # 当前点
        self._done = False
        self._cancelled = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        # 覆盖鼠标所在屏幕
        self.setGeometry(screen_geo)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ---------- 绘制 ----------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # 1) 底图（铺满整屏）
        if self._base is not None and not self._base.isNull():
            p.drawPixmap(self.rect(), self._base)

        # 2) 整体压暗
        p.fillRect(self.rect(), QColor(0, 0, 0, self.DIM_ALPHA))

        r = self._selection()
        if r.isNull() or r.width() <= 0 or r.height() <= 0:
            return

        # 3) 选区恢复原亮度：把底图对应部分再画一次
        if self._base is not None and not self._base.isNull():
            src = QRect(r)
            # base 已缩放到逻辑尺寸，直接按同尺寸取源
            p.drawPixmap(r, self._base, src)

        # 4) 白框 + 尺寸文字
        pen = QPen(QColor(255, 255, 255, 230))
        pen.setWidth(2)
        p.setPen(pen)
        p.drawRect(r.adjusted(0, 0, -1, -1))

        tip = "%d × %d" % (r.width(), r.height())
        f = QFont()
        f.setPointSize(10)
        p.setFont(f)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(tip) + 12
        th = fm.height() + 6
        tx = r.left()
        ty = r.top() - th - 4
        if ty < 0:
            ty = r.bottom() + 4
        p.fillRect(QRect(tx, ty, tw, th), QColor(0, 0, 0, 170))
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRect(tx, ty, tw, th), Qt.AlignmentFlag.AlignCenter, tip)

    # ---------- 选区 ----------
    def _selection(self):
        if self._origin is None or self._cur is None:
            return QRect()
        return QRect(self._origin, self._cur).normalized()

    # ---------- 鼠标 ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self._cancel()
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self._origin = QPoint(int(e.position().x()), int(e.position().y()))
        self._cur = QPoint(self._origin)
        self.update()

    def mouseMoveEvent(self, e):
        if self._origin is None:
            return
        self._cur = QPoint(int(e.position().x()), int(e.position().y()))
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self._origin is None:
            return
        r = self._selection()
        if r.width() < self.MIN_SIDE or r.height() < self.MIN_SIDE:
            # 选区太小 → 视为取消
            self._cancel()
            return
        self._finish(r)

    # ---------- 键盘 ----------
    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(e)

    # ---------- 结束 ----------
    def _finish(self, rect):
        if self._done:
            return
        self._done = True
        # 立即释放底图（不再需要）
        self._base = None
        self.hide()
        self.selected.emit(rect)
        self.close()

    def _cancel(self):
        if self._done:
            return
        self._done = True
        self._cancelled = True
        self._base = None
        self.hide()
        self.cancelled.emit()
        self.close()
