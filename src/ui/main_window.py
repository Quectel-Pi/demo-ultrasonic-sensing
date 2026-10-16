#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主窗口 —— 负责布局、信号绑定与主题

分工：
    左列  扫描视图（俯视极坐标 ScanView，默认）/ 侧视声锥视图（DistanceView，
          由 ``config.VIEW_MODE`` 选择） + HistoryView（历史曲线）
    右列  ControlPanel（读数 / 统计 / 阈值 / 舵机扫描 / 灯光 / 日志）

本文件是界面层里唯一"知道控制器存在"的地方：面板只抛信号，由这里转给控制层。

右侧面板整体放进 QScrollArea：加了舵机区以后控件变多，小屏（1024×600 一类）
上必须能滚动，否则底部的日志与灯控会被挤出窗口。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QMainWindow, QScrollArea,
                               QStatusBar, QVBoxLayout, QWidget)

from .. import config
from ..controller.sonar_controller import SonarController
from ..widgets.control_panel import ControlPanel
from ..widgets.distance_view import DistanceView
from ..widgets.history_view import HistoryView
from ..widgets.scan_view import ScanView

WINDOW_TITLE = "Ultrasonic Distance Measurement Console"


class MainWindow(QMainWindow):
    """测距 + 舵机扫描监控主窗口。

    :param bus:          I2C 设备节点
    :param addr:         模块地址
    :param detect_range: 初始探测范围 cm
    :param sim:          强制仿真模式
    :param servo:        是否启用舵机（``False`` = 只用测距，完全不碰 PWM）
    :param scan:         启动即自动扫描
    :param scan_step:    扫描步进（度）
    :param view:         左上视图 ``"scan"``（默认）/ ``"side"``
    """

    def __init__(self, bus, addr, detect_range, sim=False, servo=True,
                 scan=config.SCAN_ENABLED_DEFAULT,
                 scan_step=config.SCAN_STEP_DEG, view=config.VIEW_MODE):
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1320, 860)

        self.view_mode = view
        self.controller = SonarController(bus, addr, detect_range, sim=sim,
                                          servo=servo, scan=scan,
                                          scan_step=scan_step)
        self._build_ui()
        self._wire()
        self.controller.start()

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        left = QVBoxLayout()
        left.setSpacing(10)
        self.beam = DistanceView() if self.view_mode == "side" else ScanView()
        self.history = HistoryView()
        left.addWidget(self.beam, 3)
        left.addWidget(self.history, 2)
        root.addLayout(left, 1)

        self.panel = ControlPanel(self.controller.sonar.bus,
                                  self.controller.sonar.addr,
                                  self.controller.detect_range,
                                  self.controller.poll_hint())
        root.addWidget(self._scrollable(self.panel), 0)

        bar = QStatusBar()
        self.setStatusBar(bar)
        bar.showMessage("Initialising…")

        self.setStyleSheet("QMainWindow{background:#0d0f13;}"
                           "QStatusBar{color:#8a95a3;background:#0d0f13;"
                           "border-top:1px solid #262b33;}")

    @staticmethod
    def _scrollable(widget):
        """把右侧面板放进滚动区（面板比小屏高时仍可操作）。"""
        scroll = QScrollArea()
        scroll.setWidget(widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(widget.width() + 14)
        scroll.setStyleSheet(
            "QScrollArea{background:#0d0f13;border:none;}"
            "QScrollBar:vertical{background:#14161a;width:8px;margin:0;}"
            "QScrollBar::handle:vertical{background:#333a45;border-radius:4px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical"
            "{height:0;}")
        return scroll

    # ------------------------------------------------------------------
    # 信号绑定
    # ------------------------------------------------------------------
    def _wire(self):
        c, p = self.controller, self.panel

        c.distanceChanged.connect(self._on_distance)
        c.obstacleChanged.connect(self._on_obstacle)
        c.rangeChanged.connect(self._on_range)
        c.linkChanged.connect(self._on_link)
        c.statsChanged.connect(p.set_stats)
        c.messageLogged.connect(p.log)
        c.ledAutoChanged.connect(p.set_led_auto)

        c.servoLinkChanged.connect(self._on_servo_link)
        c.servoEnableChanged.connect(p.set_servo_enabled)
        c.scanChanged.connect(p.set_scan)
        c.scanChanged.connect(self._on_scan_changed)
        c.sweepStateChanged.connect(p.set_sweep_state)
        c.sweepStateChanged.connect(self._on_sweep_state)
        c.angleChanged.connect(self._on_angle)
        c.scanPoint.connect(self._on_scan_point)
        c.sweepFinished.connect(self._on_sweep)
        c.directionChanged.connect(self._on_direction)

        p.rangeRequested.connect(c.set_range)
        p.ledAutoRequested.connect(c.set_led_auto)
        p.colorRequested.connect(c.set_color)
        p.servoEnableRequested.connect(c.set_servo_enabled)
        p.scanRequested.connect(c.set_scan)
        p.singleSweepRequested.connect(c.start_single_sweep)
        p.angleRequested.connect(c.set_angle)
        p.scanStepRequested.connect(c.set_scan_step)

        # 控制器在构造函数里就开始采集了，此时界面还没接上信号，这里补发初始状态
        self._on_link(*c.link_state())
        self._on_range(c.detect_range)
        self._on_obstacle(c.in_range)
        p.set_angle(c.target_angle)
        p.set_scan_step(c.scan_step)
        self._on_angle(c.target_angle)
        self._on_direction(c.direction, c.direction_detail)
        p.set_servo_enabled(c.servo_on)
        p.set_scan(c.auto_scan)
        self._on_servo_link(*c.servo_link_state())    # 放最后：它会按可用性禁用控件

    # ------------------------------------------------------------------
    # 控制器 → 界面
    # ------------------------------------------------------------------
    def _on_distance(self, cm, fresh=True):
        self.panel.set_distance(cm, self.controller.last_cm_raw, fresh)
        self.beam.set_distance(cm)
        self.beam.set_led(self.controller.led_rgb)      # 灯光由状态驱动，随采样同步
        # 曲线只画**真实测到**的点：保持值不进曲线，否则等于画出没测过的距离
        self.history.push(cm if fresh else None)

    def _on_obstacle(self, flag):
        self.panel.set_obstacle(flag)
        self.beam.set_obstacle(flag)

    def _on_range(self, cm):
        self.panel.set_range(cm)
        self.beam.set_range(cm)
        self.history.set_range(cm)
        self._refresh_status()

    def _on_link(self, ok, text):
        self.panel.set_link(ok, text)
        self.panel.log(("I2C connected: %s" % text) if ok else text)
        self._refresh_status()

    def _on_servo_link(self, ok, sim, scan_ok, text):
        self.panel.set_servo_link(ok, sim, scan_ok, text,
                                  needs_root=self.controller.servo.needs_root)
        if not ok:
            self.panel.log("Servo unavailable: %s" % text)
        elif sim:
            self.panel.log("Servo simulated: %s" % text)
        else:
            self.panel.log("Servo ready: %s" % text)
        error = self.controller.servo.open_error()
        if error and not self.controller.servo.connected:
            for line in str(error).splitlines():        # 把完整修复步骤写进日志
                self.panel.log("  " + line)
        blocked = self.controller.scan_blocked_reason()
        if blocked:                                     # 扫描不可信时明确说清
            self.panel.log(blocked)
        self._refresh_status()

    def _on_angle(self, view_deg):
        """观察角 → 面板滑块 + 扫描视图（两者都用观察角，无需再换算）。"""
        self.panel.set_angle(view_deg)
        self.beam.set_angle(view_deg)

    def _on_scan_point(self, view_deg, cm):
        if isinstance(self.beam, ScanView):
            self.beam.push_point(view_deg, cm)

    def _on_sweep(self, sweep):
        if isinstance(self.beam, ScanView):
            self.beam.set_sweep(sweep)

    def _on_direction(self, text, detail):
        self.panel.set_direction(text, detail)
        if isinstance(self.beam, ScanView):
            self.beam.set_direction(text, detail)

    def _on_scan_changed(self, _on):
        """自动扫描开关变化也要刷新状态栏（否则它一直写着「静态」）。"""
        self._refresh_status()

    def _on_sweep_state(self, _running, _done, _total):
        """单次扫描起止同样要刷新状态栏 —— 状态栏是这个程序里最该说真话的一行。"""
        self._refresh_status()

    def _refresh_status(self):
        c = self.controller
        if c.servo.connected:
            servo = "servo ready"
        elif c.servo_available and c.servo_on:
            servo = "servo simulated"
        else:
            servo = "servo off"
        mode = "scanning" if c.scanning else "static"
        self.statusBar().showMessage(
            "%s · %s · %s · %s · threshold %.0f cm"
            % (c.sonar.describe(), servo, mode, c.poll_hint(), c.detect_range))

    # ------------------------------------------------------------------
    # 退出
    # ------------------------------------------------------------------
    def closeEvent(self, event):
        self.controller.stop_and_wait()
        super().closeEvent(event)
