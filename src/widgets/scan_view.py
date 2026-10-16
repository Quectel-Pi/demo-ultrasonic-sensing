#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视图层 —— 俯视扫描视图（QPainter 自绘极坐标）

为什么换成俯视图
----------------
加了舵机以后，"传感器朝哪边"本身就是主要信息：侧视图只能表达"前方多远"，
俯视图能把整圈方位（0~180°）与距离画在同一张图里，左中右一眼可辨。

画面
----
圆心 = 舵机转轴（画成可旋转的探头：R/T 两颗换能器 + 两颗 RGB 指示灯），
半径 = 距离；上半圆 0~180° 就是舵机的行程：

    左侧 0°  ←  正前方 90°  →  右侧 180°

每个采样点画成一个小方块，**上一整圈**用暗色、**本轮进行中**用亮色，
最靠近物体的那个点加一圈高亮。扇区（左 / 中 / 右）用底色与分界线区分，
虚线弧是探测范围（阈值）。判定结论直接写在左上角。

θ 约定：视图收到的是**观察角**（0 = 左，90 = 前，180 = 右）。
舵机实际角与观察角的差异（``SERVO_INVERT``）在控制层换算好再送进来，
视图本身不做任何方向判断，保持"只显示"。
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
PAD_L = PAD_R = 52.0        # 左右留白：给 0°/180° 的角度标注
PAD_T = 56.0                # 顶部留白：给 30°~150° 的角度标注与标题
PAD_B = 30.0                # 底部留白
HEAD_R = 20.0               # 探头本体（转轴）半径


class ScanView(QWidget):
    """俯视极坐标扫描视图（纯显示，不含业务逻辑）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 320)

        self._cm = None                     # 当前读数
        self._view_deg = 90.0               # 当前观察角
        self._range = config.DEFAULT_RANGE_CM
        self._in_range = False
        self._led = {"r": config.COLOR_OK, "t": config.COLOR_OK}
        self._direction = ""
        self._detail = {}
        self._live = {}                     # 本轮扫描 {观察角: cm}
        self._last = {}                     # 上一整圈 {观察角: cm}

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(ANIM_MS)

    # ------------------------------------------------------------------
    # 外部接口
    # ------------------------------------------------------------------
    def set_distance(self, cm):
        self._cm = cm
        self.update()

    def set_angle(self, view_deg):
        """设置当前观察角（0 = 左，90 = 前，180 = 右）。"""
        self._view_deg = float(view_deg)
        self.update()

    def push_point(self, view_deg, cm):
        """本轮扫描的一个采样点。

        "一圈结束"由 ``set_sweep()`` 显式告知，这里**不**根据角度回绕去猜：
        ``SERVO_INVERT`` 会让观察角单调**递减**（180→0），靠角度方向判断新一圈
        会在每个点上误判，结果只画得出最后一个点。靠显式通知则与方向无关。
        """
        self._live[view_deg] = cm
        self.update()

    def set_sweep(self, sweep):
        """接收一整圈的结果：本轮结束，把结果转为底色参照。"""
        self._last = dict(sweep or {})
        self._live = {}
        self.update()

    def set_direction(self, text, detail=None):
        self._direction = text or ""
        self._detail = dict(detail or {})
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

    def clear(self):
        self._live.clear()
        self._last.clear()
        self.update()

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        p.fillRect(self.rect(), QColor("#0f1116"))
        p.setPen(QPen(QColor("#262b33"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 10, 10)

        radius = min((w - PAD_L - PAD_R) / 2.0, h - PAD_T - PAD_B)
        if radius < 70:
            return
        cx, cy = w / 2.0, h - PAD_B

        scale = self._scale(radius)
        font = p.font()
        font.setFamily("Consolas")
        font.setPointSize(9)
        p.setFont(font)

        def polar(view_deg, cm, r_override=None):
            """(观察角, 距离) → 屏幕坐标；r_override 直接给像素半径。"""
            r = radius if r_override is None else r_override
            if cm is not None:
                r = min(1.0, cm / scale) * radius
            a = math.radians(view_deg)
            return QPointF(cx - math.cos(a) * r, cy - math.sin(a) * r)

        self._draw_sectors(p, cx, cy, radius)
        self._draw_grid(p, cx, cy, radius, scale, polar)
        self._draw_range_arc(p, cx, cy, radius, scale)
        self._draw_points(p, self._last, polar, dim=True)
        self._draw_points(p, self._live, polar, dim=False)
        self._draw_beam(p, cx, cy, radius, polar)
        self._draw_head(p, cx, cy)
        self._draw_readout(p, w)

    # --- 距离比例：至少覆盖阈值，数据超了就把量程顶上去 ---
    def _scale(self, radius):
        peak = max([cm for cm in list(self._last.values())
                    + list(self._live.values()) if cm is not None] or [0.0])
        return max(self._range, peak * 1.08, 20.0)

    # --- 扇区底色 + 分界线 + 名称 ---
    @staticmethod
    def _draw_sectors(p, cx, cy, radius):
        rect = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
        for name, (lo, hi) in config.SECTORS:
            colour = QColor(config.SECTOR_COLOR[name])
            colour.setAlpha(20)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(colour))
            # Qt 角度 0 = 三点钟方向、逆时针为正：观察角 v ↔ 180 - v
            p.drawPie(rect, int((180 - hi) * 16), int((hi - lo) * 16))

        p.setBrush(Qt.NoBrush)
        # 分界线画在每个扇区（除最后一个）的上边界
        for _name, (_lo, hi) in config.SECTORS[:-1]:
            pt = QPointF(cx - math.cos(math.radians(hi)) * radius,
                         cy - math.sin(math.radians(hi)) * radius)
            p.setPen(QPen(QColor("#2f3947"), 1, Qt.DashLine))
            p.drawLine(QPointF(cx, cy), pt)

        label_font = p.font()
        label_font.setPointSize(10)
        label_font.setBold(True)
        p.setFont(label_font)
        for name, (lo, hi) in config.SECTORS:
            mid = (lo + hi) / 2.0
            pos = QPointF(cx - math.cos(math.radians(mid)) * radius * 0.86,
                          cy - math.sin(math.radians(mid)) * radius * 0.86)
            colour = QColor(config.SECTOR_COLOR[name])
            colour.setAlpha(150)
            p.setPen(colour)
            # 扇区名是英文（LEFT/CENTER/RIGHT），标签框要留够宽度
            p.drawText(QRectF(pos.x() - 34, pos.y() - 9, 68, 18),
                       Qt.AlignCenter, name.upper())

    # --- 等距弧 + 角度刻度 ---
    @staticmethod
    def _draw_grid(p, cx, cy, radius, scale, polar):
        p.setBrush(Qt.NoBrush)
        for frac in (0.25, 0.5, 0.75, 1.0):
            r = radius * frac
            p.setPen(QPen(QColor("#222834"), 1, Qt.DotLine))
            p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), 0, 180 * 16)
            if frac == 1.0:
                continue
            # 距离刻度标在 90°（正前方）那条射线上，最直观
            label = polar(90.0, None, r)
            p.setPen(QColor("#4a5568"))
            p.drawText(QRectF(label.x() + 4, label.y() - 8, 46, 16),
                       Qt.AlignLeft | Qt.AlignVCenter, "%.0f" % (scale * frac))

        p.setPen(QPen(QColor("#262b33"), 1.2))
        p.drawLine(QPointF(cx - radius, cy), QPointF(cx + radius, cy))

        for deg in range(0, 181, 30):
            outer = polar(float(deg), None, radius)
            inner = polar(float(deg), None, radius * 0.95)
            p.setPen(QPen(QColor("#39424f"), 1.2))
            p.drawLine(inner, outer)

        # 角度标注（0=左、90=前、180=右）
        tag = p.font()
        tag.setPointSize(8)
        p.setFont(tag)
        for deg in range(0, 181, 30):
            pos = polar(float(deg), None, radius + 13)
            p.setPen(QColor("#55627a"))
            p.drawText(QRectF(pos.x() - 18, pos.y() - 8, 36, 16),
                       Qt.AlignCenter, "%d°" % deg)

    # --- 探测范围（阈值）虚弧 ---
    def _draw_range_arc(self, p, cx, cy, radius, scale):
        frac = min(1.0, self._range / scale)
        r = radius * frac
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 107, 94, 200), 1.6, Qt.DashLine))
        p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), 0, 180 * 16)
        # 标签跟着弧的左端走；范围占满量程时弧半径 ≈ 画布半径，必须把标签
        # 夹回可见区域，否则会被裁到控件外面（只剩半个字）
        box_w, left = 130.0, cx - r - 6.0
        left = min(max(4.0, left - box_w), max(4.0, self.width() - box_w - 4.0))
        p.setPen(QColor("#ff6b5e"))
        p.drawText(QRectF(left, cy - 22, box_w, 15),
                   Qt.AlignRight | Qt.AlignVCenter,
                   "threshold %.0f cm" % self._range)

    # --- 采样点 ---
    def _draw_points(self, p, points, polar, dim):
        if not points:
            return
        nearest = None
        for view_deg, cm in points.items():
            if cm is None:
                continue
            if nearest is None or cm < nearest[1]:
                nearest = (view_deg, cm)
        for view_deg, cm in points.items():
            if cm is None:                  # 无回波：在弧上画一个小叉
                pos = polar(view_deg, None, 8)
                p.setPen(QPen(QColor(90, 100, 115, 120 if dim else 200), 1.2))
                p.drawLine(QPointF(pos.x() - 3, pos.y() - 3),
                           QPointF(pos.x() + 3, pos.y() + 3))
                p.drawLine(QPointF(pos.x() - 3, pos.y() + 3),
                           QPointF(pos.x() + 3, pos.y() - 3))
                continue

            in_range = cm <= self._range
            pos = polar(view_deg, cm)
            base = QColor("#ff6b5e") if in_range else QColor("#5d6d7e")
            if dim:
                base.setAlpha(110)
            p.setPen(Qt.NoPen)
            p.setBrush(base)
            size = 7.0 if in_range else 5.0
            p.drawRect(QRectF(pos.x() - size / 2, pos.y() - size / 2, size, size))

            if not dim and nearest is not None and \
                    abs(view_deg - nearest[0]) < 1e-6:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor("#eaf6ff"), 1.6))
                p.drawEllipse(pos, 8.5, 8.5)
                p.setPen(QColor("#eaf6ff"))
                p.drawText(QRectF(pos.x() + 11, pos.y() - 9, 120, 17),
                           Qt.AlignLeft | Qt.AlignVCenter, "%.1f cm" % cm)

    # --- 当前方向的声锥 ---
    def _draw_beam(self, p, cx, cy, radius, polar):
        a = self._view_deg
        near = polar(a - BEAM_HALF_DEG, None, HEAD_R * 0.7)
        far1 = polar(a - BEAM_HALF_DEG, None, radius * 0.92)
        far2 = polar(a + BEAM_HALF_DEG, None, radius * 0.92)
        grad = QLinearGradient(cx, cy, far1.x(), far1.y())
        top, bottom = QColor(config.ACCENT), QColor(config.ACCENT)
        top.setAlpha(60)
        bottom.setAlpha(6)
        grad.setColorAt(0.0, top)
        grad.setColorAt(1.0, bottom)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawPolygon(QPolygonF([near, far1, far2]))

        # 波纹：相位随时间前推，表示"正在持续测距"（只画在当前方向上）
        t = time.time()
        p.setBrush(Qt.NoBrush)
        for i in range(3):
            phase = (t * 0.7 + i / 3.0) % 1.0
            r = HEAD_R + phase * (radius - HEAD_R)
            p.setPen(QPen(QColor(41, 211, 255, int(70 * (1.0 - phase))), 1.4))
            half = 18.0
            p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r),
                      int((180.0 - (a + half)) * 16), int(2 * half * 16))

    # --- 探头本体（随角度旋转） ---
    def _draw_head(self, p, cx, cy):
        p.save()
        p.translate(cx, cy)
        p.rotate(self._view_deg - 90.0)      # 让"局部向上"对齐当前观察角

        p.setPen(QPen(QColor("#39424f"), 1.4))
        p.setBrush(QColor("#1b212b"))
        p.drawEllipse(QPointF(0, 0), HEAD_R, HEAD_R)

        # R（接收）/ T（发射）两颗换能器，分居声轴两侧
        for dx in (-7.0, 7.0):
            p.setPen(QPen(QColor("#4a5568"), 1.1))
            p.setBrush(QColor("#242b36"))
            p.drawEllipse(QPointF(dx, -6), 6.0, 6.0)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#8a97a8"))
            p.drawEllipse(QPointF(dx, -6), 3.4, 3.4)

        # 两颗 RGB 指示灯
        for dy, key in ((3.0, "r"), (10.0, "t")):
            r, g, b = self._led.get(key, config.COLOR_OK)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(r, g, b, 60))
            p.drawEllipse(QPointF(0, dy), 6.0, 6.0)
            p.setBrush(QColor(r, g, b))
            p.drawEllipse(QPointF(0, dy), 2.8, 2.8)

        # 指向线（舵机臂），让朝向更醒目
        p.setPen(QPen(QColor(config.ACCENT), 1.6))
        p.drawLine(QPointF(0, -HEAD_R + 1), QPointF(0, -HEAD_R - 7))
        p.restore()

    # --- 左上角结论 + 右上角当前角度 ---
    def _draw_readout(self, p, w):
        font = p.font()
        font.setPointSize(11)
        font.setBold(True)
        p.setFont(font)
        text = self._direction or "waiting for scan…"
        if text and text != "No target":
            colour = QColor("#eaf6ff")
        elif text == "No target":
            colour = QColor("#8a95a3")
        else:
            colour = QColor("#55627a")
        p.setPen(colour)
        p.drawText(QRectF(14, 8, w - 200, 20), Qt.AlignLeft | Qt.AlignVCenter,
                   "Nearest object: %s" % text)

        detail = self._detail or {}
        if detail.get("nearest_cm") is not None:
            small = p.font()
            small.setPointSize(9)
            small.setBold(False)
            p.setFont(small)
            p.setPen(QColor("#8a95a3"))
            p.drawText(QRectF(14, 27, w - 200, 18),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       "%.1f cm @ %.0f° (threshold %.0f cm)"
                       % (detail["nearest_cm"], detail.get("nearest_angle", 0.0),
                          self._range))
        elif detail.get("any_cm") is not None:
            small = p.font()
            small.setPointSize(9)
            small.setBold(False)
            p.setFont(small)
            p.setPen(QColor("#8a95a3"))
            p.drawText(QRectF(14, 27, w - 200, 18),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       "no object in range · closest %.1f cm" % detail["any_cm"])

        small = p.font()
        small.setPointSize(9)
        small.setBold(False)
        small.setFamily("Consolas")
        p.setFont(small)
        p.setPen(QColor("#7fe8ff"))
        p.drawText(QRectF(w - 190, 8, 176, 17), Qt.AlignRight | Qt.AlignVCenter,
                   "angle %3.0f°" % self._view_deg)
        p.setPen(QColor("#8a95a3"))
        p.drawText(QRectF(w - 190, 25, 176, 17), Qt.AlignRight | Qt.AlignVCenter,
                   "dist %s" % ("--.- cm" if self._cm is None
                                else "%.1f cm" % self._cm))
