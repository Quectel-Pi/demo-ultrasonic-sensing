#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""驱动层 —— 板载 PWM 舵机（内核 sysfs ``/sys/class/pwm``）

硬件
----
引脚 : 40-Pin **Pin33**（丝印 PWM）= GPIO2_D3 = **PWM2_CH3**，控制器设备 ``2ade3000.pwm``
节点 : ``/sys/class/pwm/pwmchipN/pwmM/{period,duty_cycle,enable}``
波形 : 50Hz（周期 20ms），脉宽 500~2500µs ↔ 舵机 0~180°，90° = 1500µs 中位

为什么用 sysfs 而不是第三方库或软件 PWM
--------------------------------------
内核已经把 PWM 控制器暴露成"写两个文件"，写 ``period`` / ``duty_cycle`` 得到的就是
**硬件 PWM**：没有用户态翻转 GPIO 的抖动（软件 PWM 在 Python 里抖动可达上百 µs，
换算成舵机就是 ±10° 的抖），也不需要 smbus2 / periphery 之类的新依赖。

权限（重要）
------------
``/sys/class/pwm/pwmchipN/export`` 与导出后的 ``pwmM/*`` 默认**仅 root 可写**，
且 export 出来的通道目录同样是 root 所有 —— 所以"先 export 再 chmod"只能由 root
一次做完。首次使用请执行一次：

    sudo bash deploy/setup_servo_pwm.sh

该脚本会导出通道、把权限放开给 ``dialout`` 组，并装一个开机自启的 systemd 单元
保证重启后依然可用。权限不足时本驱动**不抛异常**，而是转仿真并给出可执行的修复
提示（``open_error()``），界面照常可用，只是舵机不动。

仿真
----
非 Linux、找不到该 PWM 控制器、或没有写权限 → 自动进入仿真：只记录角度，
不写 sysfs，``connected`` 为 False。开发机上无需任何硬件即可演示整条链路。
"""

import math
import os
import threading
import time

from .. import config

PWM_ROOT = "/sys/class/pwm"         # sysfs PWM 根目录
NS_PER_US = 1000                    # 1µs = 1000ns（sysfs 用 ns）
_EXPORT_TIMEOUT_S = 0.5             # export 后等待 pwmN 目录出现的上限
_SLEW_EPS_DEG = 0.05                # 斜坡收敛判定（度）

PERMISSION_HINT = (
    "No write access to /sys/class/pwm (kernel default: root only).\n"
    "Run this ONCE on the board:\n"
    "    sudo bash deploy/setup_servo_pwm.sh\n"
    "It exports the PWM channel and grants the dialout group access, after which\n"
    "this program needs no root. To just run the GUI without a servo, pass --no-servo.")


class ServoPWMError(Exception):
    """舵机 PWM 访问异常：控制器不存在、通道导出失败、属性写入失败等。"""


# ---------------------------------------------------------------------------
# 角度 ↔ 脉宽（显示、标定与驱动共用同一处换算）
# ---------------------------------------------------------------------------
def angle_to_pulse(deg):
    """舵机角 0~180° → 脉宽 µs（默认 500~2500，90° = 1500 中位）。"""
    lo, hi = config.SERVO_ANGLE_MIN, config.SERVO_ANGLE_MAX
    span = (hi - lo) or 1.0
    a = max(lo, min(hi, float(deg)))
    p0, p1 = config.SERVO_PULSE_MIN_US, config.SERVO_PULSE_MAX_US
    return p0 + (a - lo) / span * (p1 - p0)


def pulse_to_angle(us):
    """脉宽 µs → 舵机角 0~180°（标定时反查用）。"""
    p0, p1 = config.SERVO_PULSE_MIN_US, config.SERVO_PULSE_MAX_US
    span = (p1 - p0) or 1.0
    us = max(min(p0, p1), min(max(p0, p1), float(us)))
    return config.SERVO_ANGLE_MIN + \
        (us - p0) / span * (config.SERVO_ANGLE_MAX - config.SERVO_ANGLE_MIN)


def slew_seconds(delta_deg):
    """走完 ``delta_deg`` 需要多久（秒）—— 梯形/三角形速度曲线的解析解。

    界面上的"一轮约 xx 秒"就靠它把斜坡耗时算进去，否则估时永远偏短。
    ``SERVO_SLEW_DPS = 0``（关闭斜坡）时返回 0。
    """
    v = float(config.SERVO_SLEW_DPS)
    if v <= 0:
        return 0.0
    d = abs(float(delta_deg))
    if d <= 0:
        return 0.0
    a = float(config.SERVO_SLEW_ACCEL_DPS2)
    if a <= 0:                      # 匀速：立即达到 v，速度阶跃
        return d / v
    d_ramp = v * v / (2.0 * a)      # 加速段走的距离
    if d >= 2.0 * d_ramp:           # 梯形：加速 + 匀速 + 减速
        return 2.0 * (v / a) + (d - 2.0 * d_ramp) / v
    v_peak = math.sqrt(a * d)       # 三角形：还没到巡航速度就该减速了
    return 2.0 * v_peak / a


class ServoPWM:
    """板载 PWM 舵机驱动（sysfs），线程安全、写失败自愈、无硬件自动仿真。

    典型用法::

        servo = ServoPWM()
        if servo.connected:
            servo.set_angle(90)      # 回中位
        print(servo.describe())
    """

    def __init__(self, chip_device=config.PWM_CHIP_DEVICE,
                 channel=config.PWM_CHANNEL, freq=config.PWM_FREQ_HZ,
                 sim=False, chip_path=None):
        self.chip_device = chip_device
        self.channel = int(channel)
        self.freq = float(freq)
        self.lock = threading.RLock()      # 界面线程与扫描线程都会下发角度
        self.last_error = None
        self.needs_root = False            # 失败原因是"没权限"（而不是"没这个硬件"）
        self.sim = bool(sim)
        self._chip = chip_path             # 显式指定时跳过自动发现
        self._ch_dir = None
        self._angle = None                 # 最近一次下发的舵机角（仿真时也记）
        self._pulse_us = None
        self._slew_abort = False           # 让正在跑的斜坡立刻停手（release 用）
        if not self.sim:
            self._open()

    # ------------------------------------------------------------------
    # 打开 / 发现 / 导出
    # ------------------------------------------------------------------
    def _open(self):
        """发现控制器 → 确保通道已导出 → 配置周期与初始脉宽。任一步失败即转仿真。"""
        try:
            if self._chip is None:
                self._chip = self._find_chip()
            if self._chip is None:
                raise ServoPWMError(
                    "PWM controller %s not found (no matching device under "
                    "/sys/class/pwm)" % self.chip_device)
            self._ch_dir = self._ensure_channel()
            self._configure()
        except (ServoPWMError, OSError) as exc:
            self._ch_dir = None
            self.sim = True
            self.needs_root = (isinstance(exc, PermissionError)
                               or getattr(exc, "errno", None) in (1, 13))
            self.last_error = self._explain(exc)

    def _explain(self, exc):
        """把底层异常翻译成"下一步该做什么"。"""
        if isinstance(exc, PermissionError) or getattr(exc, "errno", None) in (1, 13):
            return "%s\n(original error: %s)" % (PERMISSION_HINT, exc)
        return str(exc)

    def _find_chip(self):
        """在 /sys/class/pwm 里按**设备名**找控制器，不写死 pwmchipN。

        为什么按设备名：pwmchipN 的编号取决于内核探测顺序，换内核/改设备树就会变；
        ``2ade3000.pwm`` 这类 platform 设备名才是稳定的。
        """
        if not os.path.isdir(PWM_ROOT):
            raise ServoPWMError("this system has no %s (not Linux, or the PWM "
                                "subsystem is disabled)" % PWM_ROOT)
        for name in sorted(os.listdir(PWM_ROOT)):
            if not name.startswith("pwmchip"):
                continue
            dev = os.path.realpath(os.path.join(PWM_ROOT, name, "device"))
            if os.path.basename(dev) == self.chip_device:
                return os.path.join(PWM_ROOT, name)
        return None

    def _ensure_channel(self):
        """通道已导出则直接用；未导出则先 export（需要 root 权限）。"""
        ch_dir = os.path.join(self._chip, "pwm%d" % self.channel)
        if os.path.isdir(ch_dir):
            return ch_dir

        self._write(os.path.join(self._chip, "export"), self.channel)
        deadline = time.time() + _EXPORT_TIMEOUT_S
        while not os.path.isdir(ch_dir) and time.time() < deadline:
            time.sleep(config.PWM_EXPORT_WAIT_S)
        if not os.path.isdir(ch_dir):
            raise ServoPWMError(
                "after writing export=%d the directory %s never appeared — the "
                "channel number may exceed npwm, or the controller does not "
                "support it" % (self.channel, ch_dir))
        return ch_dir

    def _configure(self):
        """配置周期/极性并给出一个合法初始脉宽。

        **顺序是这块控制器的硬约束**：通道刚从 export 出来时 ``period = 0``，
        此时任何 apply（包括写 ``enable``）都会被内核判 ``EINVAL``（实测
        rk3576 的 ``2ade3000.pwm`` 就是如此）。因此必须：

            先 period（让状态第一次合法） → 再 polarity → 再 duty → 最后 enable

        只有在通道**已经处于使能态**时才需要先写 ``enable=0`` —— 那种情况下
        period 本来就是合法的，停用不会失败；而"改周期前必须先停用"也是内核要求。
        polarity 是本板特有的坑：导出后默认 ``inversed``，而舵机要正相。
        """
        period_ns = int(round(1.0e9 / self.freq))

        if self._read(os.path.join(self._ch_dir, "enable")) == "1":
            self._write(os.path.join(self._ch_dir, "enable"), 0)   # 改周期前先停
        self._write(os.path.join(self._ch_dir, "period"), period_ns)

        pol_path = os.path.join(self._ch_dir, "polarity")
        want = config.PWM_POLARITY
        try:
            if self._read(pol_path) != want:
                self._write(pol_path, want)
        except OSError as exc:
            # 个别控制器不支持改极性：不因此判定整条链路失败，但要让上层看得见
            self.last_error = ("failed to set polarity %s: %s (if the servo does "
                               "not move or jitters, check whether this pin needs "
                               "an external inverter)" % (want, exc))

        self.set_angle(config.SERVO_PARK_DEG)          # 内部会写 duty 并 enable
        if self._read(os.path.join(self._ch_dir, "enable")) != "1":
            self._write(os.path.join(self._ch_dir, "enable"), 1)

    def _write(self, path, value):
        """写一个 sysfs 属性（sysfs 不支持 seek，必须一次 write 完）。"""
        with open(path, "w") as fh:
            fh.write("%d" % int(value) if isinstance(value, int) else str(value))

    @staticmethod
    def _read(path):
        with open(path, "r") as fh:
            return fh.read().strip()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def connected(self):
        """sysfs 通道是否已就绪（就绪不代表舵机一定接线正确）。"""
        return self._ch_dir is not None

    def open_error(self):
        """不可用原因；正常时为 None。"""
        return self.last_error

    def describe(self):
        """一句话链路描述（状态栏 / 日志用），含极性 —— 反相是舵机不动的高频原因。"""
        if self.connected:
            pol = "?"
            try:
                pol = self._read(os.path.join(self._ch_dir, "polarity"))
            except OSError:
                pass
            mark = {"normal": "normal polarity", "inversed": "INVERTED"}.get(pol, pol)
            warn = "" if pol == config.PWM_POLARITY else " ⚠ polarity mismatch"
            return "Pin33 PWM2_CH3 · %s/pwm%d · %.0fHz · %s%s" % (
                os.path.basename(self._chip), self.channel, self.freq, mark, warn)
        return "SIMULATION · %s" % (self.last_error or "not initialised").split("\n")[0]

    # ------------------------------------------------------------------
    # 角度
    # ------------------------------------------------------------------
    def set_angle(self, deg):
        """下发舵机角（0~180°，含 ``SERVO_TRIM_DEG`` 补偿）—— **瞬变，不做任何缓动**。

        只在"确知舵机已在目标位附近"或"需要立刻到位"时用；正常移动请用
        :meth:`set_angle_smooth`。

        :return: True 真的写了 sysfs / False 仿真或写入失败
        """
        lo, hi = config.SERVO_ANGLE_MIN, config.SERVO_ANGLE_MAX
        trimmed = max(lo, min(hi, float(deg) + config.SERVO_TRIM_DEG))
        us = angle_to_pulse(trimmed)
        self._angle = max(lo, min(hi, float(deg)))
        return self.set_pulse_us(us)

    def set_angle_smooth(self, deg):
        """**平滑**转到目标角：按梯形速度曲线分步逼近，避免位置阶跃。

        为什么必须这么做：PWM 舵机没有片上缓动，"把目标角一次写下去"等于让它
        瞬间跨越整个角度 —— 电机全速冲刺、齿轮撞击声很大、电流尖峰明显
        （从停靠位 90° 到扫描起点 0° 就是 90° 的阶跃）。

        曲线形状：先按 ``SERVO_SLEW_ACCEL_DPS2`` 加速，到 ``SERVO_SLEW_DPS``
        后匀速，接近目标时再按"剩余距离内刹得住"减速到 0。大角度（90°/180°）
        因此变成零点几秒的柔和移动；扫描里 5° 的小步只走一小段三角形，耗时可忽略。

        ``SERVO_SLEW_DPS = 0`` 时退化为直接跳变（老行为）。
        :return: True 真的写了 sysfs / False 仿真或写入失败
        """
        lo, hi = config.SERVO_ANGLE_MIN, config.SERVO_ANGLE_MAX
        target = max(lo, min(hi, float(deg)))
        cur = self._angle

        # 仿真、从未下发过角度（上电首次，本来也不知道舵机停在哪）、或关闭斜坡 → 直接跳
        if not self.connected or cur is None or config.SERVO_SLEW_DPS <= 0:
            return self.set_angle(target)

        tick = max(0.002, float(config.SERVO_SLEW_TICK_S))
        v_max = float(config.SERVO_SLEW_DPS)
        accel = max(0.0, float(config.SERVO_SLEW_ACCEL_DPS2))
        self._slew_abort = False
        v = 0.0
        ok = True
        while not self._slew_abort:
            delta = target - cur
            if abs(delta) <= _SLEW_EPS_DEG:
                break
            # 剩余距离内刹得住的速度上限（v² = 2as）；加速度为 0 时不做限制
            v_cap = math.sqrt(2.0 * accel * abs(delta)) if accel > 0 else v_max
            v_want = min(v_max, v_cap) * (1.0 if delta > 0 else -1.0)
            if accel > 0:
                dv = accel * tick
                v += max(-dv, min(dv, v_want - v))     # 加速度受限地逼近目标速度
            else:
                v = v_want
            step = v * tick
            if abs(step) > abs(delta):
                step = delta
            cur += step
            ok = self.set_angle(cur) and ok
            time.sleep(tick)

        if self._slew_abort:
            # 被 release() 打断（用户关了舵机）：停在当前位置就走，**绝不能**再写目标角
            # —— 那等于重新使能通道，舵机会突然又动起来，而且正好是我们想避免的阶跃。
            return ok
        return self.set_angle(target) and ok

    def set_pulse_us(self, us):
        """直接下发脉宽（µs）—— 标定舵机行程时用。"""
        us = max(0.0, min(1.0e6 / self.freq, float(us)))
        if self._pulse_us is not None and abs(us - self._pulse_us) < 0.5:
            return self.connected                 # 没变就不写总线/文件
        self._pulse_us = us

        if not self.connected:
            return False                            # 仿真：只记录，不驱动

        duty_ns = int(round(us * NS_PER_US))
        period_ns = int(round(1.0e9 / self.freq))
        duty_ns = max(0, min(period_ns - 1, duty_ns))
        with self.lock:
            try:
                self._write(os.path.join(self._ch_dir, "duty_cycle"), duty_ns)
                if self._read(os.path.join(self._ch_dir, "enable")) != "1":
                    self._write(os.path.join(self._ch_dir, "enable"), 1)
                self.last_error = None
                return True
            except OSError as exc:
                # 偶发失败不把 connected 永久置假：sysfs 掉节点才是真故障，
                # 单次 EINVAL/EBUSY 下次仍有机会写进去。
                self.last_error = "failed to write duty_cycle: %s" % exc
                return False

    @property
    def angle(self):
        """最近一次下发的舵机角（None = 尚未下发过）。"""
        return self._angle

    def pulse_us(self):
        """最近一次下发的脉宽（µs）。"""
        return self._pulse_us

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    def park(self):
        """平滑回到停靠角（默认 90° 正前方），保证退出后探头朝向可预期。"""
        return self.set_angle_smooth(config.SERVO_PARK_DEG)

    def release(self):
        """关闭 PWM 输出：舵机掉力（省电、不发热，但会被外力推动）。

        会先让正在跑的斜坡停手 —— 否则"关舵机"之后斜坡还会继续把通道写回使能态。
        """
        self._slew_abort = True
        if not self.connected:
            return False
        with self.lock:
            try:
                self._write(os.path.join(self._ch_dir, "enable"), 0)
                self._pulse_us = None      # 下次下发必须重写
                return True
            except OSError as exc:
                self.last_error = "failed to disable PWM: %s" % exc
                return False

    def close(self):
        """释放前按 ``SERVO_RELEASE_ON_EXIT`` 决定"掉力"还是"保持停靠角"。

        刻意**不 unexport**：通道目录一旦销毁，重新 export 又只有 root 能做，
        会把程序推进"必须再 sudo 一次"的坑；保持导出最省事。
        """
        if not self.connected:
            return
        if config.SERVO_RELEASE_ON_EXIT:
            self.release()
        else:
            self.park()

    def __repr__(self):
        return "<ServoPWM %s pwm%d %s>" % (
            self.chip_device, self.channel,
            "connected" if self.connected else "sim")
