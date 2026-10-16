#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视图层 —— 右侧控制面板（界面文案全英文）

只负责"显示 + 收集用户操作"，不含任何业务判断：用户动作一律以信号抛出，
由主窗口转给控制层。

版面约定（为了一屏放得下）
------------------------
**常显的只有三个分组框**：``Distance``（实时距离）、``Detection threshold``（探测阈值）、
``Rotate & Scan``（扫描与结论）；它们上方是标题、I2C 副标题与链路状态三行文本。
其余**四个分区默认收起**，折进可点开的 ``CollapsibleSection``：``Probe direction &
scan step``（嵌在 ``Rotate & Scan`` 内部）、``Link statistics``、``Indicator LEDs``、
``Event log``。这样在 1280×800 甚至更矮的屏幕上也不需要滚动，而"从来不动"的参数
仍然一点就开。

改动版面时的同步点：**"常显几块 / 收起几块"这个数字写在这里，也写在移远开发者文档平台的
资料文档（"界面说明"一节）里**，改版面要一并同步。两份 README 已精简，界面只讲到三个
常显分组框，不列分区明细，一般不需要跟着改。

结论的措辞
----------
扫描回答的是"**最近的物体在哪一扇区**"，所以标题写 ``NEAREST OBJECT``，值只给
``LEFT / CENTER / RIGHT``；次近扇区只作为 ``also close`` 的补充，不混进结论。
"""

from PySide6.QtCore import Qt, QTime, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QPushButton,
                               QSlider, QTextEdit, QVBoxLayout, QWidget)

from .. import config
from .collapsible import CollapsibleSection

# 按钮文案里的 "&" 是 Qt 快捷键标记，必须写 "&&" 才会显示成 `Rotate & Scan`
SWEEP_LABEL = "⟳  Rotate && Scan (180°)"

# Distance 下面那行状态：说的是"**探测范围内**有没有障碍物"，所以文案里必须带范围，
# 否则"No obstacle"会被读成"整圈都没有东西"。
OBSTACLE_TEXT = "● Obstacle within detection range"
NO_OBSTACLE_TEXT = "○ No obstacle within detection range"


class ControlPanel(QWidget):
    """控制面板：链路状态 / 实时距离 / 扫描与结论 + 四个折叠设置区。"""

    rangeRequested = Signal(float)          # 用户调整探测范围
    ledAutoRequested = Signal(bool)         # 用户开关"障碍指示"
    colorRequested = Signal(object, str)    # 用户手动点灯：(rgb, target)

    servoEnableRequested = Signal(bool)     # 用户开关舵机
    scanRequested = Signal(bool)            # 用户开关**自动**（持续往复）扫描
    singleSweepRequested = Signal()         # 用户点「Rotate & Scan」：扫一整圈
    angleRequested = Signal(float)          # 用户手动指定朝向
    scanStepRequested = Signal(float)       # 用户改扫描步进

    def __init__(self, bus, addr, detect_range, poll_hint, parent=None):
        super().__init__(parent)
        self.setFixedWidth(384)
        self._detect_range = float(detect_range)
        self._servo_ok = True       # 舵机是否可操作（由 set_servo_link 更新）
        self._scan_ok = True        # 当前状态下扫描结论是否可信
        self._auto_scan = False     # 自动（持续往复）扫描开关
        self._sweep_running = False # 单次 180° 扫描是否进行中
        self._sweep_done = 0
        self._sweep_total = 0
        self._log_count = 0
        self._build(bus, addr, poll_hint)
        self.apply_theme()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build(self, bus, addr, poll_hint):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        title = QLabel("Ultrasonic Distance Measurement Console")
        title.setObjectName("title")
        title.setWordWrap(True)
        sub = QLabel("Quectel Pi M2 · %s @0x%02X · %s" % (bus, addr, poll_hint))
        sub.setObjectName("sub")
        sub.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(sub)

        self.lbl_link = QLabel("○ connecting…")
        self.lbl_link.setObjectName("link")
        self.lbl_link.setWordWrap(True)
        root.addWidget(self.lbl_link)

        # --- 实时距离（常显）---
        box = QGroupBox("Distance")
        lay = QVBoxLayout(box)
        self.lbl_dist = QLabel("--.- cm")
        self.lbl_dist.setObjectName("dist")
        self.lbl_dist.setAlignment(Qt.AlignCenter)
        self.lbl_state = QLabel(NO_OBSTACLE_TEXT)
        self.lbl_state.setObjectName("state")
        self.lbl_state.setAlignment(Qt.AlignCenter)
        self.lbl_state.setWordWrap(True)
        lay.addWidget(self.lbl_dist)
        lay.addWidget(self.lbl_state)
        root.addWidget(box)

        # --- 探测阈值（常显，放在扫描之前：它决定"范围内"的门槛）---
        box = QGroupBox("Detection threshold")
        lay = QVBoxLayout(box)
        self.lbl_range = QLabel("%.0f cm" % self._detect_range)
        self.lbl_range.setObjectName("readout")
        self.lbl_range.setAlignment(Qt.AlignCenter)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(int(config.RANGE_MIN_CM), int(config.RANGE_MAX_CM))
        self.slider.setValue(int(self._detect_range))
        self.slider.setTickPosition(QSlider.TicksBelow)
        self.slider.setTickInterval(10)
        self.slider.valueChanged.connect(self._on_slider)
        lay.addWidget(self.lbl_range)
        lay.addWidget(self.slider)
        hint = QLabel("Readings closer than this count as an obstacle, and the "
                      "direction result only uses readings inside this range.")
        hint.setObjectName("mono2")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        root.addWidget(box)

        # --- 扫描与结论（常显，主操作区）---
        # 注意 QGroupBox / QPushButton 的 "&" 是快捷键标记，要写成 "&&"
        box = QGroupBox("Rotate && Scan")
        lay = QVBoxLayout(box)

        self.lbl_servo = QLabel("○ connecting servo…")
        self.lbl_servo.setObjectName("link")
        self.lbl_servo.setWordWrap(True)
        lay.addWidget(self.lbl_servo)

        # 不可用时的"下一步做什么"必须直接可见 —— 日志在面板底部，要滚动才看得到
        self.lbl_servo_hint = QLabel("")
        self.lbl_servo_hint.setObjectName("hint")
        self.lbl_servo_hint.setWordWrap(True)
        self.lbl_servo_hint.setVisible(False)
        lay.addWidget(self.lbl_servo_hint)

        # 主操作：点一次 = 转一整圈 0~180° 并给出最近物体的方位
        self.btn_sweep = QPushButton(SWEEP_LABEL)
        self.btn_sweep.setObjectName("primary")
        self.btn_sweep.clicked.connect(self.singleSweepRequested)
        lay.addWidget(self.btn_sweep)

        self.chk_scan = QCheckBox("Continuous scan (repeat)")
        self.chk_scan.setChecked(config.SCAN_ENABLED_DEFAULT)
        self.chk_scan.toggled.connect(self.scanRequested)
        lay.addWidget(self.chk_scan)

        self.chk_servo = QCheckBox("Enable servo")
        self.chk_servo.setChecked(True)
        self.chk_servo.toggled.connect(self.servoEnableRequested)
        lay.addWidget(self.chk_servo)

        # 结论：先说清"这是最近物体的方位"，再给扇区
        self.lbl_dir_cap = QLabel("NEAREST OBJECT")
        self.lbl_dir_cap.setObjectName("cap")
        self.lbl_dir_cap.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.lbl_dir_cap)
        self.lbl_dir = QLabel("--")
        self.lbl_dir.setObjectName("dir")
        self.lbl_dir.setAlignment(Qt.AlignCenter)
        self.lbl_dir.setWordWrap(True)
        lay.addWidget(self.lbl_dir)
        self.lbl_dir_detail = QLabel("Press “Rotate & Scan” to measure")
        self.lbl_dir_detail.setObjectName("mono2")
        self.lbl_dir_detail.setAlignment(Qt.AlignCenter)
        self.lbl_dir_detail.setWordWrap(True)
        lay.addWidget(self.lbl_dir_detail)

        # ---- 探头朝向 + 扫描步进：设置一次就不常动，整体折进一个分区 ----
        self.sec_aim = CollapsibleSection("Probe direction & scan step")

        row = QHBoxLayout()
        row.addWidget(QLabel("Probe direction"))
        row.addStretch(1)
        self.lbl_angle = QLabel("%.0f°" % config.SERVO_PARK_DEG)
        self.lbl_angle.setObjectName("readout")
        row.addWidget(self.lbl_angle)
        self.sec_aim.add_layout(row)

        self.sld_angle = QSlider(Qt.Horizontal)
        self.sld_angle.setRange(int(config.SERVO_ANGLE_MIN),
                                int(config.SERVO_ANGLE_MAX))
        self.sld_angle.setValue(int(config.SERVO_PARK_DEG))
        self.sld_angle.setTickPosition(QSlider.TicksBelow)
        self.sld_angle.setTickInterval(30)
        self.sld_angle.valueChanged.connect(self._on_angle)
        self.sec_aim.add_widget(self.sld_angle)

        row = QHBoxLayout()
        row.addWidget(QLabel("Scan step"))
        self.cmb_step = QComboBox()
        for step in (5, 10, 15, 20, 30, 45):
            self.cmb_step.addItem("%d°" % step, float(step))
        # 默认与 config.SCAN_STEP_DEG 对齐（列表第一项就是 5°）
        index = self.cmb_step.findData(float(config.SCAN_STEP_DEG))
        self.cmb_step.setCurrentIndex(index if index >= 0 else 0)
        self.cmb_step.currentIndexChanged.connect(self._on_step)
        row.addWidget(self.cmb_step, 1)
        self.sec_aim.add_layout(row)

        hint = QLabel("Angles follow the scan map: 0° left · 90° ahead · 180° right. "
                      "Dragging pauses the scan and parks the probe there.")
        hint.setObjectName("mono2")
        hint.setWordWrap(True)
        self.sec_aim.add_widget(hint)
        lay.addWidget(self.sec_aim)
        root.addWidget(box)

        # --- 以下三块默认收起（用户要求：不需要的栏目可以隐藏）---

        # 链路统计
        self.sec_stats = CollapsibleSection("Link statistics")
        self.lbl_stats = QLabel("rate --.- Hz · --- ms/read · drops -")
        self.lbl_stats.setObjectName("mono")
        self.lbl_raw = QLabel("raw --.- cm · median window %d" % config.MEDIAN_WINDOW)
        self.lbl_raw.setObjectName("mono2")
        self.sec_stats.add_widget(self.lbl_stats)
        self.sec_stats.add_widget(self.lbl_raw)
        root.addWidget(self.sec_stats)

        # 指示灯
        self.sec_led = CollapsibleSection("Indicator LEDs", framed=True)
        self.chk_auto = QCheckBox("Obstacle indicator (red in range / green out)")
        self.chk_auto.setChecked(True)
        self.chk_auto.toggled.connect(self.ledAutoRequested)
        self.sec_led.add_widget(self.chk_auto)

        row = QHBoxLayout()
        row.addWidget(QLabel("Target"))
        self.cmb_target = QComboBox()
        self.cmb_target.addItem("Both probes", "both")
        self.cmb_target.addItem("R probe (receiver)", "r")
        self.cmb_target.addItem("T probe (transmitter)", "t")
        row.addWidget(self.cmb_target, 1)
        self.sec_led.add_layout(row)

        grid = QGridLayout()
        grid.setSpacing(4)
        for i, (name, rgb) in enumerate(config.PALETTE):
            btn = QPushButton(name)
            btn.setObjectName("swatch")
            btn.setStyleSheet("QPushButton#swatch{background:%s;color:%s;}"
                              % (QColor(*rgb).name(),
                                 "#ffffff" if max(rgb) > 110 else "#c8d2dc"))
            btn.clicked.connect(lambda _=False, c=rgb: self._emit_color(c))
            grid.addWidget(btn, i // 3, i % 3)
        self.sec_led.add_layout(grid)

        self.btn_custom = QPushButton("Custom colour…")
        self.btn_custom.clicked.connect(self._pick_color)
        self.sec_led.add_widget(self.btn_custom)
        root.addWidget(self.sec_led)

        # 日志
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setObjectName("log")
        self.log_box.setMinimumHeight(96)
        self.sec_log = CollapsibleSection("Event log")
        self.sec_log.add_widget(self.log_box, stretch=1)
        self.sec_log.toggled.connect(self._on_log_toggled)
        root.addWidget(self.sec_log, 1)          # 展开时吃掉剩余高度

    # ------------------------------------------------------------------
    # 用户动作 → 信号
    # ------------------------------------------------------------------
    def _on_slider(self, value):
        self.lbl_range.setText("%d cm" % value)
        self.rangeRequested.emit(float(value))

    def _on_angle(self, value):
        self.lbl_angle.setText("%d°" % value)
        self.angleRequested.emit(float(value))

    def _on_step(self, index):
        step = self.cmb_step.itemData(index)
        if step is not None:
            self.scanStepRequested.emit(float(step))

    def _on_log_toggled(self, on):
        """展开日志时把未读计数清掉（收起状态下标题会带条数）。"""
        if on:
            self._log_count = 0
            self.sec_log.set_title("Event log")

    def _target(self):
        return self.cmb_target.currentData()

    def _emit_color(self, rgb):
        self.colorRequested.emit(tuple(rgb), self._target())

    def _pick_color(self):
        current = QColor(*config.PALETTE[0][1])
        colour = QColorDialog.getColor(current, self, "Choose LED colour")
        if colour.isValid():
            self._emit_color((colour.red(), colour.green(), colour.blue()))

    # ------------------------------------------------------------------
    # 外部刷新接口（由主窗口调用）
    # ------------------------------------------------------------------
    def set_distance(self, cm, raw, fresh=True):
        """``fresh=False`` 表示本次没测到、显示的是保持值：把数字调暗并标注，
        免得把"上次的读数"当成"这次的读数"。"""
        self.lbl_dist.setProperty("stale", bool(cm is not None and not fresh))
        if cm is None:
            self.lbl_dist.setText("--.- cm")
            self.lbl_raw.setText("raw --.- cm · median window %d"
                                 % config.MEDIAN_WINDOW)
        else:
            self.lbl_dist.setText("%.1f cm" % cm)
            self.lbl_raw.setText("%sraw %s cm · median window %d"
                                 % ("" if fresh else "held · ",
                                    "--.-" if raw is None else "%.1f" % raw,
                                    config.MEDIAN_WINDOW))
        self._repolish(self.lbl_dist)

    def set_obstacle(self, flag):
        self.lbl_state.setProperty("alert", bool(flag))
        self.lbl_state.setText(OBSTACLE_TEXT if flag else NO_OBSTACLE_TEXT)
        self._repolish(self.lbl_state)

    def set_range(self, cm):
        self._detect_range = float(cm)
        self.lbl_range.setText("%.0f cm" % cm)
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(cm)))
        self.slider.blockSignals(False)

    def set_link(self, ok, text):
        self.lbl_link.setProperty("ok", bool(ok))
        self.lbl_link.setText(("● connected · %s" % text) if ok else ("○ %s" % text))
        self._repolish(self.lbl_link)

    def set_stats(self, hz, cost_ms, drops=0):
        """丢点计数直接摆在链路统计里 —— 掉点是否真的变少了，一眼能看出来。"""
        self.lbl_stats.setText("rate %.1f Hz · %.0f ms/read · drops %d"
                               % (hz, cost_ms, drops))

    def set_led_auto(self, on):
        self.chk_auto.blockSignals(True)
        self.chk_auto.setChecked(bool(on))
        self.chk_auto.blockSignals(False)

    # --- 舵机 / 扫描（由主窗口调用） ---
    def set_servo_link(self, ok, sim, scan_ok, text, needs_root=False):
        """``ok=False`` 表示舵机不可操作（``--no-servo``）；``sim=True`` 表示在仿真
        演示（能操作，但没有真正驱动硬件）；``scan_ok=False`` 表示当前状态下扫描
        结论不可信（真超声 + 假舵机）；``needs_root=True`` 表示失败原因是权限，
        这时才把"在板上执行一次 sudo"摊出来（开发机上没意义，不显示）。"""
        ok = bool(ok)
        self._servo_ok = ok
        self._scan_ok = bool(scan_ok)
        self.lbl_servo.setProperty("ok", ok and not sim)
        self.lbl_servo.setProperty("sim", bool(sim))
        if not ok:
            self.lbl_servo.setText("○ %s" % text)
        elif sim:
            self.lbl_servo.setText("◐ %s" % text)
        else:
            self.lbl_servo.setText("● servo ready · %s" % text)
        self._repolish(self.lbl_servo)
        if needs_root:
            self.lbl_servo_hint.setText(
                "Run this ONCE on the board (asks for the root password):\n"
                "    cd ~/projects/demo-ultrasonic-sensing\n"
                "    sudo bash deploy/setup_servo_pwm.sh")
            self.lbl_servo_hint.setVisible(True)
        else:
            self.lbl_servo_hint.setVisible(False)
        for widget in (self.chk_servo, self.cmb_step, self.lbl_angle):
            widget.setEnabled(ok)
        self.chk_scan.setEnabled(ok and self._scan_ok)
        self.chk_scan.setToolTip(
            "" if scan_ok else "Servo is not actually driven, so the probe never "
                               "turns and any direction result would be wrong")
        self._sync_sweep_button()
        self.sld_angle.setEnabled(ok and not self._auto_scan)

    def _sync_sweep_button(self):
        """「Rotate & Scan」按钮的唯一状态出口 —— 三处条件（舵机可用 / 未在自动扫描 /
        未在单次扫描）都要满足才可点，写成一处避免几个 setter 互相覆盖。"""
        busy = self._sweep_running
        self.btn_sweep.setEnabled(self._servo_ok and self._scan_ok
                                  and not self._auto_scan and not busy)
        if busy:
            self.btn_sweep.setText("Scanning… %d/%d"
                                   % (self._sweep_done, self._sweep_total or 1))
        else:
            self.btn_sweep.setText(SWEEP_LABEL)

    def set_sweep_state(self, running, done, total):
        """单次扫描的进度：按钮文字跟着走，扫描期间禁用以免排队重扫。"""
        self._sweep_running = bool(running)
        self._sweep_done = int(done)
        self._sweep_total = int(total)
        self._sync_sweep_button()

    def set_servo_enabled(self, on):
        self.chk_servo.blockSignals(True)
        self.chk_servo.setChecked(bool(on))
        self.chk_servo.blockSignals(False)
        self.sld_angle.setEnabled(bool(on) and self._servo_ok
                                  and not self._auto_scan)

    def set_scan(self, on):
        self._auto_scan = bool(on)
        self.chk_scan.blockSignals(True)
        self.chk_scan.setChecked(self._auto_scan)
        self.chk_scan.blockSignals(False)
        self.sld_angle.setEnabled(not self._auto_scan and self._servo_ok)
        self._sync_sweep_button()

    def set_angle(self, view_deg):
        """同步朝向滑块（观察角，不触发反向信号，避免与采集线程打架）。"""
        self.lbl_angle.setText("%.0f°" % view_deg)
        self.sld_angle.blockSignals(True)
        self.sld_angle.setValue(int(round(view_deg)))
        self.sld_angle.blockSignals(False)

    def set_scan_step(self, step):
        index = self.cmb_step.findData(float(step))
        if index >= 0:
            self.cmb_step.blockSignals(True)
            self.cmb_step.setCurrentIndex(index)
            self.cmb_step.blockSignals(False)

    def set_direction(self, text, detail=None):
        """方位结论：**最近物体在哪一扇区**。标题已写明 NEAREST OBJECT，
        这里只给扇区名，避免一句话里塞太多信息。"""
        detail = detail or {}
        none = (not text) or text == "No target"
        self.lbl_dir.setProperty("none", none)
        if not text:
            self.lbl_dir.setText("--")
            self.lbl_dir_detail.setText("Press “Rotate & Scan” to measure")
        elif text == "No target":
            self.lbl_dir.setText("NONE IN RANGE")
            cm = detail.get("any_cm")
            self.lbl_dir_detail.setText(
                "closest in sweep %.1f cm (beyond threshold)" % cm if cm
                else "no echo from any angle")
        else:
            self.lbl_dir.setText(text.upper())
            extra = (" · also close: %s" % ", ".join(detail["tied"])
                     if detail.get("tied") else "")
            self.lbl_dir_detail.setText(
                "%.1f cm @ %.0f° · threshold %.0f cm%s"
                % (detail.get("nearest_cm", 0.0),
                   detail.get("nearest_angle", 0.0), self._detect_range, extra))
        self._repolish(self.lbl_dir)

    def log(self, text):
        self.log_box.append(
            QTime.currentTime().toString("HH:mm:ss") + "  " + text)
        # 日志收起时用标题上的条数提示"有新内容"，免得悄悄攒一堆没人看
        if not self.sec_log.is_expanded():
            self._log_count += 1
            self.sec_log.set_title("Event log (%d)" % self._log_count)

    @staticmethod
    def _repolish(widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------
    def apply_theme(self):
        self.setStyleSheet("""
            QWidget { background:#14161a; color:#dfe5ec; font-size:13px; }
            QLabel#title { font-size:15px; font-weight:700; color:#eaf6ff; }
            QLabel#sub { color:#7c8794; font-size:11px; }
            QLabel#dist { font-family:Consolas,monospace; font-size:32px;
                          font-weight:700; color:#7fe8ff; }
            QLabel#dist[stale="true"] { color:#4d7f92; }
            QLabel#state { font-size:14px; font-weight:700; color:#3ddc84;
                           padding:2px 0 4px 0; }
            QLabel#state[alert="true"] { color:#ff6b5e; }
            QLabel#cap { color:#7c8794; font-size:11px; letter-spacing:1px;
                         padding-top:2px; }
            QLabel#dir { font-size:22px; font-weight:700; color:#7fe8ff;
                         padding:0 0 2px 0; }
            QLabel#dir[none="true"] { color:#8a95a3; font-size:16px; }
            QLabel#readout { font-family:Consolas,monospace; font-size:16px;
                             color:#7fe8ff; font-weight:700; }
            QLabel#mono { font-family:Consolas,monospace; font-size:12px;
                          color:#7fe8ff; }
            QLabel#mono2 { font-family:Consolas,monospace; font-size:11px;
                           color:#7c8794; }
            QLabel#hint { font-family:Consolas,monospace; font-size:11px;
                          color:#e0a93c; }
            QLabel#link { font-size:12px; color:#e0a93c; padding:2px 0; }
            QLabel#link[ok="true"] { color:#3ddc84; }
            QLabel#link[sim="true"] { color:#e0a93c; }
            QGroupBox { border:1px solid #2a2f38; border-radius:10px;
                        margin-top:14px; padding:10px 10px 8px 10px;
                        font-weight:600; color:#aebccd; }
            QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 4px; }
            QSlider::groove:horizontal { height:6px; border-radius:3px;
                                         background:#262b33; }
            QSlider::sub-page:horizontal { border-radius:3px; background:
                qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 #16688c, stop:1 #29d3ff); }
            QSlider::handle:horizontal { width:18px; height:18px; margin:-7px 0;
                border-radius:9px; background:#e8f6ff; border:2px solid #29d3ff; }
            QCheckBox { color:#aebccd; }
            QComboBox { background:#1d2229; border:1px solid #333a45;
                        border-radius:6px; padding:4px 6px; color:#dfe5ec; }
            QComboBox QAbstractItemView { background:#1d2229; color:#dfe5ec;
                        selection-background-color:#2a4152; }
            QPushButton { background:#1d2229; border:1px solid #333a45;
                          border-radius:8px; padding:7px 6px; color:#dfe5ec; }
            QPushButton:hover { background:#242b34; border-color:#3d4753; }
            QPushButton:pressed { background:#2c3a3f; }
            QPushButton#primary { background:#16688c; border:1px solid #29d3ff;
                                  color:#eaf6ff; font-weight:700; padding:9px 6px; }
            QPushButton#primary:hover { background:#1b7ba6; }
            QPushButton#primary:disabled { background:#1d2229; border-color:#333a45;
                                           color:#5d6d7e; font-weight:600; }
            QToolButton { border:none; background:transparent; color:#7fe8ff;
                          font-size:12px; padding:3px 0; }
            QToolButton:hover { color:#b9f0ff; }
            QToolButton#sectionHeader { color:#aebccd; font-weight:600;
                                        font-size:13px; padding:5px 2px;
                                        text-align:left; }
            QToolButton#sectionHeader:hover { color:#eaf6ff; }
            QFrame#sectionFrame { border:1px solid #2a2f38; border-radius:10px; }
            QTextEdit#log { background:#101318; color:#9db2c5;
                            border:1px solid #262b33; border-radius:8px;
                            font-family:Consolas,monospace; font-size:11px; }
        """)
