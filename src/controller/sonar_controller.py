#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制层 —— 采集 / 滤波 / 判定 / 指示灯状态机（与界面解耦）

为什么必须后台线程
------------------
模块单次测距是「写命令 → 等 ≥100ms → 读结果」，实测约 160ms。若放在界面线程里
执行，窗口会明显卡顿。因此本层继承 ``QThread``，在 ``run()`` 里跑采集循环，
结果通过信号投递回界面线程（跨线程信号由 Qt 自动排队，无需手动加锁）。

两种工作模式
------------
    静态模式  舵机停在某个角度（默认 90° 正前方），按目标周期轮询测距。
              与"没有舵机"时的行为完全一致。
    扫描模式  舵机按 ``SCAN_STEP_DEG`` 从 0° 走到 180°，每个角度"先到位、再测量"，
              走完一轮把整圈读数交给方位判定，得出物体在左 / 中 / 右。

一轮扫描的节拍 = 角度数 ×（机械稳定 SERVO_SETTLE_S + 单次测距 ~160ms）。
以默认 5° 步进为例：37 点 × 0.31s ≈ 11.5s 一整圈（10° 则 19 点 ≈ 6s）。

方位判定（``_evaluate_direction``）
----------------------------------
1. 只统计**落在探测范围内**（≤ 阈值）的有效读数，无回波的 ``None`` 直接丢弃；
2. 按观察角把整圈读数分到左 / 中 / 右三个扇区（扇区边界见 ``config.SECTOR_*``）；
3. 每个扇区取**最近**的那一点作为该侧的距离；
4. 取三个扇区里距离最小的作为结论；与结论相差不到 ``DIRECTION_MARGIN_CM``
   的扇区一并列出（例如「左 / 中」），避免物体正好卡在扇区边界时给出过于自信
   的结论；
5. 三个扇区都没有范围内读数 → 「无目标」。

注意：超声波波束有 ±15° 张角，物体在左前方时"中"扇区边缘也会读到它。取"各扇区
最近点"并由扇区边界切分，能把结论稳定在最靠近物体的那一侧。

野值抑制（两层）
----------------
第 1 层在驱动：``raw == 0``（无回波）与越界（<2cm / >400cm）直接判为无效。
第 2 层在本层：**静态模式**下对最近 ``config.MEDIAN_WINDOW`` 个有效值取中值。
扫描模式下**刻意不做**这层滤波：整圈里相邻两点来自不同方位，跨角度取中值会把
左右两侧的读数混在一起，反而制造出根本不存在的位置。

线程安全
--------
界面线程会调 ``set_color()`` 点灯、``set_angle()`` 转舵机，采集线程同时在读距离并转
同一个舵机。所有 I2C 访问由驱动层 ``SonarI2C.lock`` 串行化，所有 PWM 访问由
``ServoPWM.lock`` 串行化，本层不再重复加锁。
"""

import statistics
import time
from collections import deque

from PySide6.QtCore import QThread, Signal

from .. import config
from ..driver.servo_pwm import ServoPWM, slew_seconds
from ..driver.sonar_i2c import SonarI2C


class SonarController(QThread):
    """超声波采集 + 舵机扫描控制器。

    典型用法（界面层）::

        ctrl = SonarController(bus, addr, detect_range)
        ctrl.distanceChanged.connect(on_distance)
        ctrl.start()                     # 启动后台采集
        ...
        ctrl.stop_and_wait()             # 关闭窗口时收尾
    """

    # --- 对外信号（全部从采集线程发出，Qt 自动排队投递到界面线程） ---
    distanceChanged = Signal(object, bool)  # (距离 cm 或 None, 本次是否新测到)
                                            # 丢点保持时 cm 为上次值、fresh=False
    obstacleChanged = Signal(bool)          # 是否落在探测范围内
    rangeChanged = Signal(float)            # 探测范围 cm
    linkChanged = Signal(bool, str)         # (是否真实连上硬件, 说明)
    ledAutoChanged = Signal(bool)           # 自动障碍指示开关状态
    statsChanged = Signal(float, float, int)  # (实测刷新率 Hz, 单次耗时 ms, 累计丢点)
    messageLogged = Signal(str)             # 给界面的日志行

    # --- 舵机与扫描 ---
    servoLinkChanged = Signal(bool, bool, bool, str)   # (可操作, 仿真, 可扫描, 说明)
    servoEnableChanged = Signal(bool)       # 舵机使能开关状态
    scanChanged = Signal(bool)              # **自动**扫描（持续往复）开关状态
    sweepStateChanged = Signal(bool, int, int)  # 单次扫描 (进行中, 已完成, 总数)
    angleChanged = Signal(float)            # 当前**观察角**（0=左 / 90=前 / 180=右）
    scanPoint = Signal(float, object)       # (观察角, 距离 cm 或 None)
    sweepFinished = Signal(object)          # {观察角: 距离} 一整圈的结果
    directionChanged = Signal(str, object)  # ("左 / 中 / 右" 或 "无目标", 详情)

    def __init__(self, bus=config.BUS, addr=config.ADDR,
                 detect_range=config.DEFAULT_RANGE_CM, sim=False,
                 servo=True, scan=config.SCAN_ENABLED_DEFAULT,
                 scan_step=config.SCAN_STEP_DEG, parent=None):
        super().__init__(parent)
        self.detect_range = float(detect_range)
        self.led_auto = True
        self.in_range = False
        self.last_cm = None          # 对外读数（静态模式下为中值滤波结果）
        self.last_cm_raw = None      # 最近一次原始读数
        self.refresh_hz = 0.0
        self.led_rgb = {"r": config.COLOR_OK, "t": config.COLOR_OK}

        self.sonar = SonarI2C(bus, addr, sim=sim)

        # --- 舵机与扫描状态 ---
        # 注意"可操作"与"真有硬件"是两件事：没有 /sys/class/pwm（开发机）或
        # 没有写权限（板上未授权）时 ServoPWM 会转仿真 —— 此时界面照常演示整条
        # 扫描链路，只是舵机不动。真正是否驱动硬件看 self.servo.connected。
        self.servo = ServoPWM(sim=sim)
        self.servo_wanted = bool(servo)      # --no-servo 时为 False（用户主动关闭）
        self.servo_on = self.servo_wanted and self.servo_available
        # 两种扫描：auto_scan = 持续往复（默认关）；_single_scan = 按钮触发的一次性 180°
        self.auto_scan = bool(scan) and self.servo_on and self.scan_allowed
        self._single_scan = False
        self.scan_step = self._clamp_step(scan_step)
        self.target_angle = config.SERVO_PARK_DEG       # 静态模式的目标角
        self.sweep = {}                                 # 本轮扫描 {观察角: cm}
        self.last_sweep = {}                            # 上一整圈（改阈值时重判用）
        self.direction = ""                             # 方位结论文本
        self.direction_detail = {}
        self._scan_index = 0
        self._angles = self._build_angles()
        self._announced_angle = None

        self._hist = deque(maxlen=config.MEDIAN_WINDOW)
        self._fail = 0
        self.drop_total = 0          # 累计丢点（驱动重读后仍无结果），界面显示用
        self._last_ts = None
        self._hz = 0.0
        self._running = True

        if self.sonar.connected:
            self.sonar.set_color(config.COLOR_OK)

    # ------------------------------------------------------------------
    # 状态查询（界面绑定信号后，用它补发一次初始状态）
    # ------------------------------------------------------------------
    def link_state(self):
        """``(是否连上硬件, 说明)`` —— 构造函数里 UI 还没接上信号，需要补发。"""
        if self.sonar.connected:
            return True, self.sonar.describe()
        return False, "SIMULATION · %s" % (self.sonar.open_error()
                                           or "I2C bus not opened")

    def servo_link_state(self):
        """``(可操作, 是否仿真, 是否允许扫描, 说明)``。

        "可操作" = 能真正驱动（PWM 通道就绪）**或** 处于仿真态（界面可完整演示
        扫描与方位判定，只是舵机不动，方便开发机上先验证逻辑）。
        真正有没有硬件看 ``self.servo.connected``；权限不足时的完整修复步骤在
        ``self.servo.open_error()``（多行）；"是否允许扫描"的判据见
        ``scan_blocked_reason()``。
        """
        if not self.servo_wanted:
            return False, False, False, "disabled by --no-servo"
        if self.servo.connected:
            return True, False, True, self.servo.describe()
        why = (self.servo.open_error()
               or "hardware not accessed (--sim or no /sys/class/pwm)").split("\n")[0]
        if self.servo_on:
            return True, True, self.scan_allowed, "SIMULATION · %s" % why
        return False, False, False, "not enabled · %s" % why

    def poll_hint(self):
        """轮询参数说明（界面副标题用）。"""
        return "target %.0f Hz · ~%d ms/read" % (
            config.POLL_HZ, int((config.MEASURE_WAIT_S + 0.06) * 1000))

    @property
    def median_window(self):
        """中值滤波窗口点数（界面显示用）。"""
        return self._hist.maxlen

    @property
    def servo_available(self):
        """舵机是否可操作：真的能驱动，或处于仿真（界面可完整演示）。"""
        return bool(self.servo.connected or self.servo.sim)

    def scan_blocked_reason(self):
        """自动扫描不可用的原因（``None`` 表示可用）。

        为什么要有这道闸：扫描的意义在于"探头真的转到了不同方位"。若是
        **真超声 + 假舵机**（板上未授权 PWM 的典型状态），探头根本没动，
        整圈读到的都是同一个方向的距离，判定逻辑却照样会输出"物体在左侧"
        —— 那是个凭空捏造的结论。因此这种混合状态下直接禁用扫描。

        而"声呐与舵机都是仿真"（开发机、``--sim``）不在拦截之列：那时仿真
        读数本身就随方位变化，整条链路是自洽的，演示有效。
        """
        if self.servo.connected:
            return None
        if self.sonar.sim:
            return None
        why = (self.servo.open_error() or "simulation").split("\n")[0]
        return ("Servo PWM is not actually driven (%s), so the probe never turns "
                "and any direction result would be meaningless — automatic "
                "scanning is disabled" % why)

    @property
    def scan_allowed(self):
        """当前状态下自动扫描是否有意义。"""
        return self.scan_blocked_reason() is None

    # ------------------------------------------------------------------
    # 参数设置（界面线程调用）
    # ------------------------------------------------------------------
    def set_range(self, cm):
        """调整探测范围(cm)；变化足够明显时立即按新阈值重判当前距离与方位。"""
        cm = max(config.RANGE_MIN_CM, min(config.RANGE_MAX_CM, float(cm)))
        if abs(cm - self.detect_range) < 0.5:      # 滑块拖动去抖
            return
        self.detect_range = cm
        self.rangeChanged.emit(cm)
        if self.last_cm is not None:
            self._judge(self.last_cm, force=True)
        if self.last_sweep:                        # 阈值变了，方位结论要跟着变
            self._reevaluate()

    def set_led_auto(self, on):
        """开关"LED 障碍指示"；开启时立即同步到当前状态颜色。"""
        on = bool(on)
        if on == self.led_auto:
            return
        self.led_auto = on
        if on:
            self._apply_led(config.COLOR_ALERT if self.in_range else config.COLOR_OK)
        self.ledAutoChanged.emit(on)
        self.messageLogged.emit("Obstacle LEDs: %s" % ("ON" if on else "OFF"))

    def set_color(self, rgb, target="both"):
        """手动点灯。

        手动选色意味着用户要自己控制灯，因此会**自动关闭**"障碍指示"，
        避免下一次状态变化把颜色覆盖掉。

        :param rgb:    ``(r, g, b)``
        :param target: ``"r"`` / ``"t"`` / ``"both"``
        """
        if self.led_auto:
            self.led_auto = False
            self.ledAutoChanged.emit(False)
        self.sonar.set_color(rgb, target)
        if target in ("r", "both"):
            self.led_rgb["r"] = tuple(rgb)
        if target in ("t", "both"):
            self.led_rgb["t"] = tuple(rgb)
        self.messageLogged.emit("Manual LED: %s → RGB%s"
                                % ({"r": "R probe", "t": "T probe",
                                    "both": "both probes"}[target], tuple(rgb)))

    def _apply_led(self, rgb):
        """按当前状态给双探头同色（自动指示用）。"""
        self.led_rgb["r"] = self.led_rgb["t"] = tuple(rgb)
        self.sonar.set_color(rgb, "both")

    # ------------------------------------------------------------------
    # 舵机与扫描（界面线程调用）
    # ------------------------------------------------------------------
    @staticmethod
    def _clamp_step(step):
        return max(config.SCAN_STEP_MIN_DEG,
                   min(config.SCAN_STEP_MAX_DEG, float(step)))

    @staticmethod
    def _clamp_angle(deg):
        return max(config.SERVO_ANGLE_MIN,
                   min(config.SERVO_ANGLE_MAX, float(deg)))

    def _build_angles(self):
        """本轮扫描要停留的角度序列（含两端；步进不整除时补上 180°）。

        必须用**向下取整**再补末端：``round(180/7)=26`` 会得到 182°，
        超出机械行程 —— 虽然下发时会被钳到 180°，但那会让两个角度落到同一
        观察角上，扫描表里出现互相覆盖的重复点。
        """
        step = self.scan_step
        lo, hi = config.SERVO_ANGLE_MIN, config.SERVO_ANGLE_MAX
        n = int((hi - lo) / step)
        angles = [lo + i * step for i in range(n + 1)]
        if angles[-1] < hi - 1e-6:
            angles.append(hi)
        return angles

    def sweep_seconds(self):
        """一轮扫描的预计耗时（界面提示用）。

        要把**斜坡耗时**算进去：大角度/小角度都走梯形速度曲线，漏掉它估时永远偏短。
        扫描内相邻两点只差一个步进，所以按步进估算即可。
        """
        per_point = (config.SERVO_SETTLE_S + config.MEASURE_WAIT_S + 0.06
                     + slew_seconds(self.scan_step))
        return len(self._angles) * per_point

    @property
    def scanning(self):
        """采集循环此刻是否应该在扫描（持续往复或一次性单圈都算）。"""
        return bool(self.servo_on and (self.auto_scan or self._single_scan))

    def set_servo_enabled(self, on):
        """总开关：关掉即停止 PWM 输出（舵机掉力），但通道保持导出。"""
        on = bool(on) and self.servo_available
        if on == self.servo_on:
            if not on and not self.servo_available:
                self.messageLogged.emit("Servo unavailable: %s"
                                        % (self.servo.open_error() or "unknown"))
            return
        self.servo_on = on
        if on:
            self.target_angle = config.SERVO_PARK_DEG
            self._announced_angle = None
            self.messageLogged.emit("Servo enabled · %s" % self.servo.describe())
        else:
            if self.auto_scan:
                self.auto_scan = False
                self.scanChanged.emit(False)
            self._cancel_single_sweep()
            self.servo.release()
            self.messageLogged.emit("Servo disabled (PWM off, horn moves freely)")
        self.servoEnableChanged.emit(on)

    def set_scan(self, on):
        """开关**持续往复**的自动扫描；关闭时舵机回中位。"""
        on = bool(on)
        if on and not self.servo_on:
            self.messageLogged.emit("Servo unavailable, cannot scan: %s"
                                    % (self.servo.open_error() or "unknown"))
            self.scanChanged.emit(False)
            return
        blocked = self.scan_blocked_reason()
        if on and blocked:
            self.messageLogged.emit(blocked)
            self.scanChanged.emit(False)
            return
        if on == self.auto_scan:
            return
        self.auto_scan = on
        if on:
            self._scan_index = 0
            self.sweep = {}
            self.messageLogged.emit(
                "Continuous scan started: %.0f° step · 0-180° · %d points per "
                "sweep, ~%.1fs" % (self.scan_step, len(self._angles),
                                   self.sweep_seconds()))
        else:
            self.target_angle = self._clamp_angle(config.SERVO_PARK_DEG)
            self.messageLogged.emit("Continuous scan stopped (probe parked at %.0f°)"
                                    % self.target_angle)
        self.scanChanged.emit(on)

    def start_single_sweep(self):
        """按钮入口：**扫一整圈就停**（0→180° → 方位判定 → 回停靠角）。

        与"自动扫描"的区别只是扫完不接着下一圈，所以两者共用同一套
        ``_scan_step`` / ``_finish_sweep``；重复点击会被忽略，避免排队重扫。
        """
        if not self.servo_on:
            self.messageLogged.emit("Servo unavailable, cannot scan: %s"
                                    % (self.servo.open_error() or "unknown"))
            return
        blocked = self.scan_blocked_reason()
        if blocked:
            self.messageLogged.emit(blocked)
            self.sweepStateChanged.emit(False, 0, len(self._angles))
            return
        if self._single_scan:
            return                                   # 正在扫，忽略重复点击
        self._single_scan = True
        self._scan_index = 0
        self.sweep = {}
        total = len(self._angles)
        self.sweepStateChanged.emit(True, 0, total)
        self.messageLogged.emit("Rotate & scan started: 0-180° · %d angles · ~%.1fs"
                                % (total, self.sweep_seconds()))

    def _cancel_single_sweep(self):
        if self._single_scan:
            self._single_scan = False
            self.sweepStateChanged.emit(False, 0, len(self._angles))

    def set_scan_step(self, step):
        """改扫描步进；下一轮生效（不打断当前轮，避免半圈数据混两种步进）。"""
        step = self._clamp_step(step)
        if abs(step - self.scan_step) < 1e-6:
            return
        self.scan_step = step
        self._angles = self._build_angles()
        self.messageLogged.emit("Scan step set to %.0f° → %d angles per sweep, ~%.1fs"
                                % (step, len(self._angles), self.sweep_seconds()))

    def set_angle(self, view_deg):
        """手动指定**观察角**（0=左 / 90=前 / 180=右）：暂停自动扫描、取消单次扫描。

        界面只讲观察角，舵机角在这里换算 —— 免得"探头朝向"在滑块和扫描图上
        显示成两个不同的数字（``SERVO_INVERT`` 一开就会差 180-x）。
        """
        view = self._clamp_angle(view_deg)
        self.target_angle = self._clamp_angle(config.servo_angle(view))
        self._cancel_single_sweep()
        if self.auto_scan:
            self.auto_scan = False
            self.scanChanged.emit(False)
            self.messageLogged.emit("Manual direction → continuous scan paused")
        self.angleChanged.emit(view)

    # ------------------------------------------------------------------
    # 采集循环（运行在子线程）
    # ------------------------------------------------------------------
    def run(self):
        period = 1.0 / config.POLL_HZ
        while self._running:
            t0 = time.time()
            if self.scanning:
                self._scan_step()
            else:
                self._static_step()
                delay = period - (time.time() - t0)
                if delay > 0:
                    self.msleep(int(delay * 1000))

    def _static_step(self):
        """静态模式一步：舵机到位（若需要）→ 测一次距。丢点允许"保持显示"。"""
        if self.servo_on:
            self._goto(self.target_angle)
        self.sonar.set_scan_angle(None)         # 仿真回到"非扫描"读数模型
        t0 = time.time()
        cm = self.sonar.read_distance_cm()
        self._tick(cm, time.time() - t0, hold=True)

    def _scan_step(self):
        """扫描模式一步：到下一个角度 → 稳定 → 测一次 → 入表；扫满一圈出结论。"""
        if self._scan_index >= len(self._angles):       # 上一圈刚结束
            self._scan_index = 0
        if self._scan_index == 0:
            self.sweep = {}                             # 新的一圈从空表开始

        angle = self._angles[self._scan_index]
        self._goto(angle)

        view = config.view_angle(angle)
        self.sonar.set_scan_angle(view)                 # 仅仿真器使用
        t0 = time.time()
        cm = self.sonar.read_distance_cm()
        cost = time.time() - t0

        self.sweep[view] = cm
        self.scanPoint.emit(view, cm)
        self._tick(cm, cost, smooth=False)              # 跨角度不做中值滤波；也不保持丢点

        self._scan_index += 1
        self.sweepStateChanged.emit(self._single_scan, self._scan_index,
                                    len(self._angles))
        if self._scan_index >= len(self._angles):
            self._finish_sweep()

    def _goto(self, deg):
        """把舵机转到指定角度（**舵机角**），必要时等机械稳定（角度没变就不再等）。

        用 ``set_angle_smooth`` 走梯形斜坡，而不是把目标角一次写下去 —— 一次写
        等于位置阶跃，舵机会全速冲过去，又响又冲（停靠位 90° → 扫描起点 0° 尤其明显）。

        对外发的 ``angleChanged`` 是**观察角**，界面只认这一个坐标系。
        """
        deg = self._clamp_angle(deg)
        cur = self.servo.angle
        moved = cur is None or abs(deg - cur) >= 0.5
        self.servo.set_angle_smooth(deg)
        if moved:
            self.msleep(int(config.SERVO_SETTLE_S * 1000))
        view = config.view_angle(deg)
        if moved or self._announced_angle != view:
            self._announced_angle = view
            self.angleChanged.emit(view)

    def _finish_sweep(self):
        """一圈扫完：投递完整结果并给出方位结论。

        若是按钮触发的**单次**扫描，就在这里收工：回停靠角、复位进度。
        自动扫描则什么都不做，循环会接着扫下一圈。
        """
        self.last_sweep = dict(self.sweep)
        self.sweepFinished.emit(dict(self.last_sweep))
        text, detail = self._evaluate_direction(self.last_sweep)
        self._publish_direction(text, detail)

        if self._single_scan:
            self._single_scan = False
            self.target_angle = self._clamp_angle(config.SERVO_PARK_DEG)
            self.sweepStateChanged.emit(False, len(self._angles),
                                        len(self._angles))
            self.messageLogged.emit("Rotate & scan finished, probe parked at %.0f°"
                                    % self.target_angle)

    def _reevaluate(self):
        """阈值/扇区参数变化后，用上一圈数据重算结论（不重新扫描）。"""
        text, detail = self._evaluate_direction(self.last_sweep)
        self._publish_direction(text, detail)

    def _publish_direction(self, text, detail):
        changed = (text != self.direction)
        self.direction, self.direction_detail = text, detail
        self.directionChanged.emit(text, detail)
        if not changed:
            return
        near = detail.get("nearest_cm")
        if near is None:
            self.messageLogged.emit("Scan result: no object within threshold")
            return
        extra = ""
        if detail.get("tied"):
            extra = " (also close: %s)" % ", ".join(detail["tied"])
        self.messageLogged.emit(
            "Scan result: nearest object is in the %s sector "
            "(%.1f cm @ %.0f°, threshold %.0f cm)%s"
            % (text, near, detail.get("nearest_angle", 0.0),
               self.detect_range, extra))

    def _evaluate_direction(self, sweep):
        """按扇区归纳整圈读数 → ``(结论文本, 详情)``。

        **结论就是"最近物体所在的那一扇区"**：先按扇区各自取最近点，再取三者中
        距离最小的那个扇区。全局最小值必然落在它自己的扇区里，所以这条结论
        与"整圈最近的那个点在哪边"是同一件事 —— 但按扇区归纳后，即使最近点正好
        落在扇区边界上，结论也稳定（不会在两个相邻角度之间来回跳）。

        :param sweep: ``{观察角: 距离 cm 或 None}``
        :return: ``("Left" / "Center" / "Right" / "No target",
                 {"nearest_cm", "nearest_angle", "nearest_sector", "sectors",
                  "tied", "any_cm", "any_angle"})``
        """
        valid = {a: cm for a, cm in sweep.items() if cm is not None}
        if not valid:
            return "No target", {"nearest_cm": None, "nearest_angle": None,
                                 "nearest_sector": None, "sectors": {},
                                 "tied": [], "any_cm": None, "any_angle": None}

        any_angle, any_cm = min(valid.items(), key=lambda p: p[1])

        best = {}
        for name in config.SECTOR_ORDER:
            hits = [(a, cm) for a, cm in valid.items()
                    if cm <= self.detect_range and config.sector_of(a) == name]
            if len(hits) >= config.DIRECTION_MIN_HITS:
                best[name] = min(hits, key=lambda p: p[1])

        detail = {"sectors": {n: best[n][1] for n in best},
                  "any_cm": any_cm, "any_angle": any_angle,
                  "nearest_cm": None, "nearest_angle": None,
                  "nearest_sector": None, "tied": []}
        if not best:
            return "No target", detail

        sector = min(best, key=lambda n: best[n][1])      # 最近物体所在扇区
        angle, cm = best[sector]
        detail.update(nearest_cm=cm, nearest_angle=angle, nearest_sector=sector)
        # 次近扇区只作为"也靠近这边"的提示，不再混进结论 —— 结论只回答"最近的在哪边"
        detail["tied"] = [n for n in config.SECTOR_ORDER
                          if n in best and n != sector
                          and best[n][1] <= cm + config.DIRECTION_MARGIN_CM]
        return sector, detail

    def _tick(self, cm, cost, smooth=True, hold=False):
        """处理一次采样结果：统计 → 滤波 → 投递 → 判定。

        :param smooth: 是否做中值滤波。扫描模式传 False —— 相邻采样来自不同方位，
                       跨角度取中值会把左右读数混成一个假位置。
        :param hold:   丢点时是否保持上次读数（只给**静态模式**用）。
                       扫描模式下每个点都是另一个方位，"保持"等于编造数据，必须关掉。
        """
        self._update_hz()
        if cm is None:
            self.drop_total += 1
        self.statsChanged.emit(self.refresh_hz, cost * 1000.0, self.drop_total)

        if cm is None:
            self._fail += 1
            # 丢点保持：模块有 5~7% 的偶发丢点，一个点不该把界面打成 `--.-`。
            # 保持的只是**显示值**，曲线那边一律按未测到处理（见 main_window）。
            if hold and self.last_cm is not None and \
                    self._fail < max(1, config.HOLD_FAILS):
                self.distanceChanged.emit(self.last_cm, False)
                return
            if self._fail == 3:             # 连续三次才算"真丢了"，报一次日志
                self.messageLogged.emit(
                    "Repeated read failures — check module wiring / power / bus %s"
                    % self.sonar.bus)
            self._hist.clear()              # 链路异常时清窗，避免用陈旧值误导判定
            self.last_cm = None
            self.distanceChanged.emit(None, False)
            return

        if self._fail >= 3:
            self.messageLogged.emit("Reads recovered")
        self._fail = 0

        self.last_cm_raw = cm
        if smooth:
            self._hist.append(cm)
            self.last_cm = round(statistics.median(self._hist), 1)
            if len(self._hist) == self._hist.maxlen and \
                    abs(cm - self.last_cm) > config.OUTLIER_LOG_CM:
                self.messageLogged.emit(
                    "Outlier rejected: raw %.1f cm → median %.1f cm"
                    % (cm, self.last_cm))
        else:
            self.last_cm = round(cm, 1)

        self.distanceChanged.emit(self.last_cm, True)
        self._judge(self.last_cm)

    def _update_hz(self):
        """按实际采样间隔估算刷新率（指数平滑，避免数字乱跳）。"""
        now = time.time()
        if self._last_ts is not None:
            dt = now - self._last_ts
            if dt > 1e-6:
                hz = 1.0 / dt
                self._hz = hz if self._hz <= 0.0 else 0.75 * self._hz + 0.25 * hz
        self._last_ts = now
        self.refresh_hz = self._hz

    def _judge(self, cm, force=False):
        """范围内/外判定：进入用阈值，退出用 阈值 + 回滞，防止边缘抖动。"""
        if force or not self.in_range:
            new_state = cm <= self.detect_range
        else:
            new_state = cm <= self.detect_range + config.HYSTERESIS_CM

        if new_state == self.in_range:
            return

        self.in_range = new_state
        self.obstacleChanged.emit(new_state)
        if self.led_auto:
            self._apply_led(config.COLOR_ALERT if new_state else config.COLOR_OK)
        if new_state:
            self.messageLogged.emit("● Obstacle detected at %.1f cm (threshold %.0f cm)"
                                    % (cm, self.detect_range))
        else:
            self.messageLogged.emit("○ Obstacle left the detection range")

    # ------------------------------------------------------------------
    # 退出
    # ------------------------------------------------------------------
    def stop_and_wait(self, timeout_ms=2000):
        """停止采集线程并释放总线／PWM（关闭窗口时调用）。"""
        self._running = False
        self.wait(timeout_ms)
        self.servo.close()
        self.sonar.close()
