#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视图层 —— 测距侧视视图（QPainter 自绘）

画面自下而上是：超声波模块（双探头 R 接收 / T 发射，各带 RGB 灯）→ 向上发散的
声锥 → 按真实距离定位的障碍物方块。虚线是探测范围界线，范围内的区域会着色。

两个来自手册的细节：
  * 声锥发散角按 **±15°** 换算成实际像素宽度，距离越远越宽，符合真实声锥；
  * 量程自适应：障碍物超出探测范围时自动扩展刻度，保证始终可见。
"""

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QLinearGradient, QPainter, QPen,
                           QPolygonF)
from PySide6.QtWidgets import QWidget

from .. import config

BEAM_HALF_DEG = 15.0        # 模块波束半角（手册指标）
ANIM_MS = 40                # 波纹动画刷新间隔


class DistanceView(QWidget):
    """测距可视化视图（纯显示，不含业务逻辑）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 300)
        self._cm = None
        self._range = config.DEFAULT_RANGE_CM
        self._in_range = False
        self._led = {"r": config.COLOR_OK, "t": config.COLOR_OK}
        self._view_deg = 90.0       # 当前观察角（0=左, 90=前, 180=右）

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(ANIM_MS)

    # ------------------------------------------------------------------
    # 外部接口
    # ------------------------------------------------------------------
    def set_angle(self, view_deg):
        """记录当前观察角，只在图上标注朝向。

        侧视图画的是"正前方有多远"，舵机转开后本视图仍然有效（只是这一次读数
        来自另一个方位），因此不旋转画面，仅把方位写出来，避免误导。
        """
        self._view_deg = float(view_deg)
        self.update()

    def set_distance(self, cm):
        self._cm = cm
        self.update()

    def set_range(self, cm):
        self._range = float(cm)
        self.update()

    def set_obstacle(self, flag):
        self._in_range = bool(flag)
        self.update()

    def set_led(self, led):
        self._led = dict(led)
        self.update()

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # 布局参数
        pad_l, pad_r, pad_t = 58, 22, 30
        mod_h, pad_b = 62, 16
        y_base = h - pad_b                  # 模块底边
        y_face = y_base - mod_h             # 声锥起点（模块顶面）
        axis_h = y_face - pad_t             # 量程对应的像素高度
        if axis_h < 80 or w < 240:
            return
        cx = pad_l + (w - pad_l - pad_r) / 2.0

        dist, rng = self._cm, self._range
        scale = max(rng, 20.0)
        if dist is not None and dist > scale:
            scale = math.ceil(dist / 10.0) * 10.0

        def y_of(cm):
            """距离(cm) → 屏幕 y 坐标。"""
            return y_face - cm / scale * axis_h

        # 背景
        p.fillRect(self.rect(), QColor("#0f1116"))
        p.setPen(QPen(QColor("#262b33"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 10, 10)

        font = p.font()
        font.setFamily("Consolas")
        font.setPointSize(9)
        p.setFont(font)

        self._draw_zones(p, w, pad_l, pad_r, rng, y_of)
        self._draw_grid(p, w, pad_l, pad_r, pad_t, scale, y_of, cx, axis_h)
        self._draw_beam(p, cx, y_face, pad_t, axis_h, w, pad_l, pad_r)
        self._draw_obstacle(p, cx, y_of, dist, rng, scale)
        self._draw_module(p, cx, y_base, mod_h)

    # --- 分区着色 + 范围界线 ---
    @staticmethod
    def _draw_zones(p, w, pad_l, pad_r, rng, y_of):
        y_rng = y_of(rng)
        p.fillRect(QRectF(pad_l, y_rng, w - pad_l - pad_r, y_of(0.0) - y_rng),
                   QColor(255, 96, 82, 28))
        colour = QColor("#ff6b5e")
        p.setPen(QPen(colour, 1.6, Qt.DashLine))
        p.drawLine(pad_l, y_rng, w - pad_r, y_rng)
        p.setPen(colour)
        p.drawText(QRectF(w - pad_r - 160, y_rng - 19, 156, 15),
                   Qt.AlignRight | Qt.AlignVCenter, "range %.0f cm" % rng)

    # --- 刻度网格 ---
    def _draw_grid(self, p, w, pad_l, pad_r, pad_t, scale, y_of, cx, axis_h):
        step = 10 if scale <= 60 else (20 if scale <= 150 else 50)
        grid = QPen(QColor("#222834"), 1, Qt.DotLine)
        cm = step
        while cm <= scale:
            y = y_of(cm)
            p.setPen(grid)
            p.drawLine(pad_l, y, w - pad_r, y)
            p.setPen(QColor("#4a5568"))
            p.drawText(QRectF(6, y - 8, pad_l - 12, 16),
                       Qt.AlignRight | Qt.AlignVCenter, str(cm))
            cm += step
        p.setPen(QColor("#55627a"))
        p.drawText(QRectF(6, pad_t - 20, pad_l - 12, 14),
                   Qt.AlignRight | Qt.AlignVCenter, "cm")
        p.drawText(QRectF(pad_l, 8, 300, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   "scale 0 – %d cm (module range 2~%d cm)"
                   % (scale, int(config.DIST_MAX_CM)))
        # 探头当前朝向（侧视图不旋转画面，只标注这一次读数来自哪个方位）
        p.setPen(QColor("#7fe8ff"))
        p.drawText(QRectF(w - pad_r - 230, 8, 230, 14),
                   Qt.AlignRight | Qt.AlignVCenter,
                   "probe %.0f° · %s" % (self._view_deg,
                                         config.sector_of(self._view_deg)))

    # --- 声锥与扩散波纹 ---
    @staticmethod
    def _draw_beam(p, cx, y_face, pad_t, axis_h, w, pad_l, pad_r):
        near_w = 52.0
        far_w = min(w - pad_l - pad_r,
                    2.0 * axis_h * math.tan(math.radians(BEAM_HALF_DEG)))
        grad = QLinearGradient(0, y_face, 0, pad_t)
        top, bottom = QColor(config.ACCENT), QColor(config.ACCENT)
        top.setAlpha(56)
        bottom.setAlpha(4)
        grad.setColorAt(0.0, top)
        grad.setColorAt(1.0, bottom)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawPolygon(QPolygonF([
            QPointF(cx - near_w / 2, y_face), QPointF(cx + near_w / 2, y_face),
            QPointF(cx + far_w / 2, pad_t), QPointF(cx - far_w / 2, pad_t)]))

        # 扩散波纹：相位随时间前推，视觉上表示"正在持续测距"
        t = time.time()
        p.setBrush(Qt.NoBrush)
        for i in range(3):
            phase = (t * 0.55 + i / 3.0) % 1.0
            r = phase * axis_h * 0.9 + 6
            p.setPen(QPen(QColor(41, 211, 255, int(80 * (1.0 - phase))), 1.6))
            p.drawArc(QRectF(cx - r, y_face - r, 2 * r, 2 * r), 180 * 16, 180 * 16)

    # --- 障碍物 ---
    def _draw_obstacle(self, p, cx, y_of, dist, rng, scale):
        if dist is None:
            return
        y = y_of(min(dist, scale))
        bw, bh = 70.0, 40.0
        box = QRectF(cx - bw / 2, y - bh, bw, bh)

        if self._in_range:
            fill, border = QColor("#8c2f28"), QColor("#ff6b5e")
        else:
            fill, border = QColor("#2a3441"), QColor("#5d6d7e")

        p.setBrush(fill)
        p.setPen(QPen(border, 1.8))
        p.drawRoundedRect(box, 6, 6)
        p.setPen(QPen(QColor(border.red(), border.green(), border.blue(), 90), 1.2))
        for dx in (-15, 15):
            p.drawLine(cx + dx, box.top() + 8, cx + dx, box.bottom() - 8)

        bold = p.font()
        bold.setPointSize(12)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(QColor("#eaf6ff") if self._in_range else QColor("#9fb0c0"))
        p.drawText(QRectF(box.right() + 10, box.top(), 160, 18),
                   Qt.AlignLeft | Qt.AlignVCenter, "%.1f cm" % dist)

        if dist > rng:
            small = p.font()
            small.setPointSize(9)
            small.setBold(False)
            p.setFont(small)
            p.setPen(QColor("#7c8794"))
            p.drawText(QRectF(box.right() + 10, box.top() + 18, 170, 16),
                       Qt.AlignLeft | Qt.AlignVCenter, "beyond range")

    # --- 传感器模块本体 ---
    def _draw_module(self, p, cx, y_base, mod_h):
        mw = 200.0
        mod = QRectF(cx - mw / 2, y_base - mod_h, mw, mod_h)
        p.setBrush(QColor("#1b212b"))
        p.setPen(QPen(QColor("#39424f"), 1.4))
        p.drawRoundedRect(mod, 8, 8)
        cy = mod.center().y()

        tag_font = p.font()
        tag_font.setPointSize(8)
        tag_font.setBold(False)

        # 左右两颗换能器：左 R（接收）、右 T（发射）
        for dx, tag in ((-62, "R"), (62, "T")):
            pos = QPointF(cx + dx, cy - 4)
            p.setPen(QPen(QColor("#4a5568"), 1.2))
            p.setBrush(QColor("#242b36"))
            p.drawEllipse(pos, 15, 15)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#8a97a8"))
            p.drawEllipse(pos, 9.5, 9.5)
            p.setBrush(QColor("#141a22"))
            p.drawEllipse(pos, 3.5, 3.5)
            p.setFont(tag_font)
            p.setPen(QColor("#55627a"))
            p.drawText(QRectF(pos.x() - 10, pos.y() + 14, 20, 12),
                       Qt.AlignHCenter | Qt.AlignTop, tag)

        # 两颗 RGB 指示灯（带光晕），与模块实际灯位对应
        for dx, key in ((-18, "r"), (18, "t")):
            r, g, b = self._led[key]
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(r, g, b, 60))
            p.drawEllipse(QPointF(cx + dx, cy - 4), 10, 10)
            p.setBrush(QColor(r, g, b))
            p.drawEllipse(QPointF(cx + dx, cy - 4), 4.5, 4.5)

        p.setFont(tag_font)
        p.setPen(QColor("#55627a"))
        p.drawText(QRectF(mod.left(), mod.bottom() - 18, mw, 13),
                   Qt.AlignHCenter, "ULTRASONIC · 0x%02X" % config.ADDR)
