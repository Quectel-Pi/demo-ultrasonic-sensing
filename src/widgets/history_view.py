#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视图层 —— 距离历史曲线（QPainter 自绘）

自己画曲线是为了把 pyqtgraph / numpy 这两个依赖从工程里去掉：板上 ARM 环境
`pip install` 一个 numpy 动辄几分钟，而这个画面只需要一条折线加一层网格。
"""

import time
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from .. import config

WINDOW_S = 30.0             # 曲线时间窗口
REPAINT_MS = 100            # 重绘节拍


class HistoryView(QWidget):
    """最近 30 秒的距离曲线；读取失败处断线，不做插值以免误读。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(180)
        self._pts = deque()          # [(时间戳, 距离cm 或 None), ...]
        self._range = config.DEFAULT_RANGE_CM

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(REPAINT_MS)

    # ------------------------------------------------------------------
    # 外部接口
    # ------------------------------------------------------------------
    def push(self, cm):
        """追加一个采样点（``cm=None`` 表示本次读取失败）。"""
        now = time.time()
        self._pts.append((now, cm))
        while self._pts and now - self._pts[0][0] > WINDOW_S + 1.0:
            self._pts.popleft()

    def set_range(self, cm):
        self._range = float(cm)
        self.update()

    def clear(self):
        self._pts.clear()
        self.update()

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 54, 16, 16, 24
        plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
        if plot_w < 60 or plot_h < 50:
            return

        p.fillRect(self.rect(), QColor("#0f1116"))
        p.setPen(QPen(QColor("#262b33"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 10, 10)

        now = time.time()
        top_cm = self._top_cm()
        x_of = lambda t: pad_l + plot_w * (1.0 - (now - t) / WINDOW_S)
        y_of = lambda cm: pad_t + plot_h * (1.0 - cm / top_cm)

        font = p.font()
        font.setFamily("Consolas")
        font.setPointSize(9)
        p.setFont(font)

        self._draw_grid(p, pad_l, pad_r, pad_t, plot_w, plot_h, top_cm, x_of, y_of)
        self._draw_range_line(p, pad_l, w, pad_r, y_of)

        # 曲线数据
        series = [(x_of(t), (None if cm is None else y_of(cm)))
                  for t, cm in self._pts if t >= now - WINDOW_S]
        if not series:
            p.setPen(QColor("#55627a"))
            p.drawText(QRectF(pad_l, pad_t, plot_w, plot_h),
                       Qt.AlignCenter, "waiting for data…")
            return

        # 断点切分：读取失败处不连线，但**短缺口**用虚线轻轻搭一下，
        # 免得偶尔丢一个点就把曲线切成一堆碎段（0 个缺口 = 一律断开）
        segments, bridges, current = [], [], []
        prev_last, gap = None, 0
        for x, y in series:
            if y is None:
                if current:
                    prev_last = current[-1]
                    segments.append(current)
                    current = []
                gap += 1
                continue
            if gap and prev_last is not None and gap <= config.HISTORY_GAP_BRIDGE:
                bridges.append((prev_last, QPointF(x, y)))
            gap = 0
            current.append(QPointF(x, y))
        if current:
            segments.append(current)

        # 曲线下方渐变填充（用最后一段，视觉上够用且开销小）
        fill = segments[-1] if segments else []
        if len(fill) > 1:
            area = QPolygonF([QPointF(fill[0].x(), pad_t + plot_h)]
                             + fill
                             + [QPointF(fill[-1].x(), pad_t + plot_h)])
            grad = QLinearGradient(0, pad_t, 0, pad_t + plot_h)
            grad.setColorAt(0.0, QColor(41, 211, 255, 60))
            grad.setColorAt(1.0, QColor(41, 211, 255, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(grad)
            p.drawPolygon(area)

        p.setBrush(Qt.NoBrush)
        # 短缺口先画：虚线 + 暗淡色，与真实数据明显区分（不让人误当成实测值）
        if bridges:
            pen = QPen(QColor(41, 211, 255, 110), 1.2, Qt.DotLine)
            p.setPen(pen)
            for a, b in bridges:
                p.drawLine(a, b)
        p.setPen(QPen(QColor(config.ACCENT), 2))
        for seg in segments:
            p.drawPolyline(QPolygonF(seg))

        # 最新点
        if current:
            last = current[-1]
            p.setPen(QPen(QColor("#0f1116"), 1.5))
            p.setBrush(QColor("#eaf6ff"))
            p.drawEllipse(last, 3.5, 3.5)

    # ------------------------------------------------------------------
    def _top_cm(self):
        """纵轴上限：至少覆盖探测范围，数据超了就把量程顶上去。"""
        peak = max([cm for _, cm in self._pts if cm is not None] or [0.0])
        base = max(self._range * 1.15, 30.0)
        return max(base, peak * 1.1)

    def _draw_grid(self, p, pad_l, pad_r, pad_t, plot_w, plot_h, top_cm,
                   x_of, y_of):
        # 纵轴：4 等分刻度
        for i in range(5):
            cm = top_cm * i / 4.0
            y = y_of(cm)
            p.setPen(QPen(QColor("#222834"), 1, Qt.DotLine))
            p.drawLine(pad_l, y, pad_l + plot_w, y)
            p.setPen(QColor("#4a5568"))
            p.drawText(QRectF(6, y - 8, pad_l - 12, 16),
                       Qt.AlignRight | Qt.AlignVCenter, "%.0f" % cm)

        # 横轴：-30s ~ 0s
        now = time.time()
        for i in range(7):
            t = now - WINDOW_S + WINDOW_S * i / 6.0
            x = x_of(t)
            p.setPen(QPen(QColor("#222834"), 1, Qt.DotLine))
            p.drawLine(x, pad_t, x, pad_t + plot_h)
            p.setPen(QColor("#4a5568"))
            p.drawText(QRectF(x - 20, pad_t + plot_h + 3, 40, 16),
                       Qt.AlignHCenter | Qt.AlignTop, "%ds" % (t - now))

        p.setPen(QColor("#55627a"))
        p.drawText(QRectF(6, pad_t - 2, pad_l - 12, 14),
                   Qt.AlignRight | Qt.AlignTop, "cm")
        p.drawText(QRectF(pad_l + 6, pad_t + 4, 220, 14),
                   Qt.AlignLeft | Qt.AlignTop,
                   "Distance history (last %.0f s)" % WINDOW_S)

    def _draw_range_line(self, p, pad_l, w, pad_r, y_of):
        y = y_of(self._range)
        p.setPen(QPen(QColor("#e0a93c"), 1.4, Qt.DashLine))
        p.drawLine(pad_l, y, w - pad_r, y)
        p.setPen(QColor("#e0a93c"))
        p.drawText(QRectF(w - pad_r - 160, y - 17, 156, 14),
                   Qt.AlignRight | Qt.AlignVCenter,
                   "threshold %.0f cm" % self._range)
