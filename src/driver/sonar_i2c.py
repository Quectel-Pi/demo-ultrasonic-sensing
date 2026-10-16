#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""驱动层 —— 幻彩 RGB 超声波模块（US01）@ Quectel Pi M2 / Debian

文件自上而下分三节，顺序即"从总线到器件到上层"：

    1. I2C 传输层   ``I2C`` / ``I2C.Message``   零依赖 ctypes 直调 /dev/i2c-N
    2. US01 器件层  ``US01`` / ``US01Error``    模块协议实现（寄存器见 config.py）
    3. 对外门面     ``SonarI2C``                仿真回退 + 线程锁，上层唯一入口

硬件
----
总线 : I2C7，40-Pin 的 Pin27(SDA) / Pin28(SCL)，设备节点 ``/dev/i2c-7``
模块 : 幻彩超声波模块（STC8H1K08 + RCWL-9616），I2C 地址 ``0x2D``
时序 : 写 ``0x10=0x01`` 启动一次测距 → 等待 ≥100ms → 读 2 字节（大端）
数值 : raw 是回波时间(us)，距离(cm) = raw × 0.017；量程 2~400cm，波束 ±15°
灯   : R 探头（接收）在 0x00~0x02，T 探头（发射）在 0x03~0x05

依赖
----
只用 Python 标准库，I2C 由第 1 节自行实现 —— 板上不装任何 pip 包也能测距
（``python -m src.main --cli``）。图形界面才需要 PySide6。

开发机注意
----------
``libc.so.6`` 延迟到 ``I2C.__init__`` 内才加载（见 ``_get_libc``），因此
在 Windows 上 ``import`` 本模块不会失败，``--help`` 与纯命令行模式可正常使用。
"""

import ctypes
import math
import os
import threading
import time

from .. import config


# ===========================================================================
# 1. I2C 传输层
# ===========================================================================
# 直接对 /dev/i2c-N 做 ioctl(fd, I2C_RDWR)，一次系统调用即可完成
# 「先写寄存器地址、再读数据」这类组合时序（对应 linux/i2c-dev.h 的两个结构体）。
# 出错时抛 OSError（errno 取自 C 层）。

I2C_M_RD = 0x0001        # i2c_msg.flags：本条消息为读方向
I2C_RDWR = 0x0707        # ioctl：组合读写传输

_libc = None             # libc 句柄，延迟加载（见模块 docstring）


class _I2CMsg(ctypes.Structure):
    """对应 linux/i2c-dev.h 的 struct i2c_msg。"""

    _fields_ = [
        ("addr", ctypes.c_uint16),                # 7-bit 从设备地址
        ("flags", ctypes.c_uint16),               # 方向标志
        ("len", ctypes.c_uint16),                 # 数据字节数
        ("buf", ctypes.POINTER(ctypes.c_uint8)),  # 数据缓冲
    ]


class _I2CRdwrData(ctypes.Structure):
    """对应 linux/i2c-dev.h 的 struct i2c_rdwr_ioctl_data。"""

    _fields_ = [
        ("msgs", ctypes.POINTER(_I2CMsg)),
        ("nmsgs", ctypes.c_uint32),
    ]


def _get_libc():
    """加载 libc 并声明 ioctl 原型（只在首次真正访问总线时执行）。"""
    global _libc
    if _libc is None:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        libc.ioctl.restype = ctypes.c_int
        _libc = libc
    return _libc


class I2C:
    """最小 i2c-dev 封装（纯标准库，无第三方依赖）。

    用法::

        bus = I2C("/dev/i2c-7")
        bus.transfer(0x2D, [I2C.Message([0x10, 0x01])])      # 写 0x10=0x01
        msg = I2C.Message([0, 0], read=True)
        bus.transfer(0x2D, [msg])                            # 读 2 字节
        data = bytes(msg.data)
        bus.close()
    """

    class Message:
        """一条 I2C 消息；``read=True`` 表示读方向，结果会回填进 ``data``。"""

        def __init__(self, data, read=False):
            self.data = list(data)      # list[int]
            self.read = bool(read)

        def __repr__(self):
            return "I2C.Message(data=%r, read=%r)" % (self.data, self.read)

    def __init__(self, devpath):
        self._libc = _get_libc()        # 延迟加载：非 Linux 平台 import 本模块也安全
        self.devpath = devpath
        self.fd = os.open(devpath, os.O_RDWR)       # 失败抛 OSError

    def transfer(self, addr, msgs):
        """执行一次 i2c_rdwr 组合传输。

        :param addr: 7-bit 从设备地址
        :param msgs: ``list[I2C.Message]``
        :return: None（读消息的结果回填进各自的 ``msg.data``）
        """
        msgs = list(msgs)
        n = len(msgs)
        if n == 0:
            return

        arr = (_I2CMsg * n)()
        bufs, lengths = [], []
        for i, m in enumerate(msgs):
            data = list(m.data)
            length = len(data)
            buf = (ctypes.c_uint8 * max(length, 1))()   # 长度 0 时退化为空数组，兜 1 字节
            if not m.read:
                for j, b in enumerate(data):
                    buf[j] = b & 0xFF
            bufs.append(buf)
            lengths.append(length)

            arr[i].addr = addr & 0xFFFF
            arr[i].flags = I2C_M_RD if m.read else 0
            arr[i].len = length
            arr[i].buf = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))

        rd = _I2CRdwrData(msgs=arr, nmsgs=n)
        if self._libc.ioctl(self.fd, I2C_RDWR, ctypes.byref(rd)) < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))

        # 读消息：把内核填好的字节回填给调用方
        for i, m in enumerate(msgs):
            if m.read:
                m.data = list(bufs[i][:lengths[i]])

    def close(self):
        """关闭设备节点（可重复调用）。"""
        if getattr(self, "fd", None) is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None


# ===========================================================================
# 2. US01 器件层
# ===========================================================================
class US01Error(Exception):
    """模块访问异常：总线打开失败、读写重试耗尽、返回数据长度异常等。"""


class US01:
    """幻彩超声波模块协议驱动。

    只负责"总线 ↔ 寄存器"，不含仿真、不含界面逻辑；内部对 I2C 失败做 3 次重试
    （实测该链路存在偶发 NACK，单次失败不代表器件掉线）。

    用法::

        with US01("/dev/i2c-7") as us:
            us.set_rgb(255, 0, 0)
            cm = us.get_distance_cm()        # None 表示无有效回波
    """

    def __init__(self, bus=config.BUS, addr=config.ADDR):
        self.bus = bus
        self.addr = addr
        try:
            self._i2c = I2C(bus)
        except OSError as exc:
            raise US01Error("cannot open I2C bus %s: %s" % (bus, exc)) from exc

    # ------------------------------------------------------------------
    # 基础读写
    # ------------------------------------------------------------------
    def _write(self, reg, value):
        self._i2c.transfer(self.addr, [I2C.Message([reg & 0xFF, value & 0xFF])])

    def _read(self, length):
        msg = I2C.Message([0] * length, read=True)
        self._i2c.transfer(self.addr, [msg])
        return bytes(msg.data)

    def _retry(self, action, what):
        """执行 action，失败重试 config.RETRY 次后统一抛 US01Error。"""
        last = None
        for _ in range(config.RETRY):
            try:
                return action()
            except OSError as exc:
                last = exc
                time.sleep(0.005)
        raise US01Error("%s failed after %d retries: %s"
                        % (what, config.RETRY, last))

    def _write_reg(self, reg, value):
        self._retry(lambda: self._write(reg, value), "write reg 0x%02X" % reg)

    def _read_bytes(self, length):
        return self._retry(lambda: self._read(length), "read %d bytes" % length)

    # ------------------------------------------------------------------
    # 在位探测
    # ------------------------------------------------------------------
    def ping(self):
        """器件是否应答。

        按**真实读路径**读 2 字节并复用同样的 3 次重试，避免偶发一次 NACK
        就误判器件不在位。

        :return: True 应答 / False 无应答
        """
        try:
            self._read_bytes(2)
            return True
        except US01Error:
            return False

    # ------------------------------------------------------------------
    # 测距
    # ------------------------------------------------------------------
    def get_distance_cm(self, no_echo_retry=None):
        """一次完整测距：写命令 → 等待 → 读结果（阻塞，约 100ms/次）。

        ``raw == 0`` 时会**立刻重读**（最多 ``config.NO_ECHO_RETRY`` 次）：
        板端实测 60 次采样里 76.7% 是 raw=0，但立刻重读能救回其中 85% ——
        这些 0 绝大多数不是"真的没回波"，而是模块没在本次等待窗口内测完。
        因此重读既能把掉点压下去，又不会把"真的没东西"伪装成有读数
        （真空旷方向重读多少次都是 0，最终仍返回 None）。

        :return: 距离 cm（float）；重读后仍无回波 / 超量程则返回 None
        """
        if no_echo_retry is None:
            no_echo_retry = config.NO_ECHO_RETRY
        attempts = max(1, int(no_echo_retry) + 1)

        for _ in range(attempts):
            self._write_reg(config.REG_CMD, config.CMD_START_MEASURE)
            time.sleep(config.MEASURE_WAIT_S)

            data = self._read_bytes(2)
            if len(data) != 2:
                raise US01Error("unexpected distance payload: expected 2 bytes, "
                                "got %d" % len(data))

            raw = (data[0] << 8) | data[1]          # 大端
            if raw == 0:
                continue                            # 这次没结果 → 重读
            cm = raw * config.RAW_TO_CM
            if cm < config.DIST_MIN_CM or cm > config.DIST_MAX_CM:
                continue                            # 盲区 / 超量程（含总线野值）→ 重读
            return cm
        return None

    # ------------------------------------------------------------------
    # RGB 灯
    # ------------------------------------------------------------------
    def _write_rgb(self, base_reg, r, g, b):
        for offset, value in enumerate((r, g, b)):
            self._write_reg(base_reg + offset, value)
            time.sleep(config.REG_WRITE_GAP_S)

    def set_rgb_r(self, r, g, b):
        """设置 R（接收）探头颜色，各分量 0~255。"""
        self._write_rgb(config.REG_R_RED, r, g, b)

    def set_rgb_t(self, r, g, b):
        """设置 T（发射）探头颜色，各分量 0~255。"""
        self._write_rgb(config.REG_T_RED, r, g, b)

    def set_rgb(self, r, g, b):
        """双探头同色。"""
        self.set_rgb_r(r, g, b)
        self.set_rgb_t(r, g, b)

    # ------------------------------------------------------------------
    # 资源管理
    # ------------------------------------------------------------------
    def close(self):
        if getattr(self, "_i2c", None) is not None:
            try:
                self._i2c.close()
            finally:
                self._i2c = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


# ===========================================================================
# 3. 对外门面
# ===========================================================================
class SonarI2C:
    """上层唯一入口：把 US01 包装成"带仿真回退 + 线程安全"的门面。

    为什么需要它
    ------------
    * **仿真回退**：PC 上没有 ``/dev/i2c-N``，界面与逻辑仍要能跑起来验证；
      打开失败时自动转仿真并记录原因（``open_error()``），而不是直接崩。
    * **线程安全**：界面线程写灯、采集线程读距离，共用同一个 I2C 文件描述符，
      所有总线访问在这里统一由 ``lock`` 串行化。

    :param bus:  设备节点，如 ``"/dev/i2c-7"``；也接受 int（``7`` → ``/dev/i2c-7``）
    :param addr: 7-bit 从机地址
    :param sim:  True 表示强制仿真，不打开总线
    """

    def __init__(self, bus=config.BUS, addr=config.ADDR, sim=False):
        self.bus = self._normalize(bus)
        self.addr = addr
        self.lock = threading.Lock()
        self.last_error = None
        self.sim = bool(sim)
        self._dev = None
        self._t0 = time.time()
        self._sim_angle = None      # 仿真扫描时的当前观察角（None = 非扫描）
        if not self.sim:
            self._open()

    @staticmethod
    def _normalize(bus):
        """把 int / 纯数字字符串统一成 ``/dev/i2c-N``。"""
        if isinstance(bus, int):
            return "/dev/i2c-%d" % bus
        text = str(bus).strip()
        return "/dev/i2c-%s" % text if text.isdigit() else text

    def _open(self):
        try:
            self._dev = US01(self.bus, self.addr)
        except US01Error as exc:
            self._dev = None
            self.sim = True
            self.last_error = str(exc)

    @property
    def connected(self):
        """总线是否已打开（打开成功不代表器件在线，读取失败由上层处理）。"""
        return self._dev is not None

    def open_error(self):
        """打开失败的说明；正常连接时为 None。"""
        return self.last_error

    def probe(self):
        """器件是否应答。

        真机上做一次带重试的读探测；``0x2D`` 无 ACK 时返回 False
        （典型报错 ``[Errno 6] No such device or address``，多为接线或供电问题）。
        仿真模式下恒为 False。

        :return: True 应答 / False 无应答或仿真
        """
        if self._dev is None:
            return False
        with self.lock:
            try:
                return bool(self._dev.ping())
            except US01Error as exc:
                self.last_error = str(exc)
                return False

    # ------------------------------------------------------------------
    # 测距
    # ------------------------------------------------------------------
    def read_distance_cm(self):
        """读取一次距离（阻塞，约 160ms）。

        :return: 距离 cm（保留 1 位）；读取失败 / 无回波 / 超量程均返回 None

        调用方的轮询周期必须不小于本调用的实际耗时，否则总线会排队积压。
        """
        if self.sim:
            return self._sim_cm()
        if self._dev is None:
            return None

        with self.lock:
            try:
                cm = self._dev.get_distance_cm()
            except US01Error as exc:
                self.last_error = str(exc)
                return None
        return None if cm is None else round(cm, 1)

    def set_scan_angle(self, view_deg):
        """告知仿真器"当前探头朝向"（**只对仿真有效**，真机为空操作）。

        控制器在扫描时每步调用一次，仿真读数才会随方位变化，
        从而在没有硬件时也能看到左中右判定；传 ``None`` 表示回到非扫描仿真。
        """
        self._sim_angle = None if view_deg is None else float(view_deg)

    def _sim_cm(self):
        """仿真读数。

        * **非扫描**：在 10~100cm 之间缓慢往返并带一点抖动（原有行为）；
        * **扫描**：在 ``SIM_TARGET_DEG`` 方位摆一个 ``SIM_TARGET_CM`` 的物体，
          其余方向是 ``SIM_BACK_CM`` 的远背景墙 —— 这样"转一圈判左右"在仿真里
          也能真正跑出结论，而不是每个角度都读到同一个数。

        这里**真实 sleep** 出真机的单次耗时，让仿真态的刷新率与耗时统计
        和真机一致 —— 仿真器的价值就在于让开发期就能预估界面表现。
        """
        time.sleep(config.SIM_SHOT_S)
        t = time.time() - self._t0

        if self._sim_angle is None:
            mid = (config.SIM_NEAR_CM + config.SIM_FAR_CM) / 2.0
            amp = (config.SIM_FAR_CM - config.SIM_NEAR_CM) / 2.0
            cm = mid + amp * math.sin(2.0 * math.pi * t / config.SIM_PERIOD_S) \
                + 0.6 * math.sin(3.7 * t)
        else:
            delta = abs((self._sim_angle - config.SIM_TARGET_DEG + 180.0) % 360.0
                        - 180.0)
            base = (config.SIM_TARGET_CM
                    if delta <= config.SIM_TARGET_WIDTH_DEG
                    else config.SIM_BACK_CM)
            cm = base + 1.5 * math.sin(0.9 * t) + 0.4 * math.sin(4.3 * t)

        return round(max(config.DIST_MIN_CM,
                         min(config.DIST_MAX_CM, cm)), 1)

    # ------------------------------------------------------------------
    # RGB 灯
    # ------------------------------------------------------------------
    def set_color(self, rgb, target="both"):
        """设置探头颜色。

        :param rgb:    ``(r, g, b)``，各分量 0~255
        :param target: ``"r"`` 接收探头 / ``"t"`` 发射探头 / ``"both"`` 双探头
        :return: True 成功 / False 失败（仿真下恒为 True）
        """
        r, g, b = (int(rgb[0]) & 0xFF, int(rgb[1]) & 0xFF, int(rgb[2]) & 0xFF)
        wanted = {"r": (0,), "t": (1,), "both": (0, 1)}[target]

        if self.sim:
            return True
        if self._dev is None:
            return False

        ok = True
        with self.lock:
            for index in wanted:
                try:
                    if index == 0:
                        self._dev.set_rgb_r(r, g, b)
                    else:
                        self._dev.set_rgb_t(r, g, b)
                except US01Error as exc:
                    self.last_error = str(exc)
                    ok = False
        return ok

    # ------------------------------------------------------------------
    # 描述与释放
    # ------------------------------------------------------------------
    def describe(self):
        """链路一句话描述（状态栏 / 日志用）。"""
        if self.connected:
            return "%s @0x%02X" % (self.bus, self.addr)
        return "SIMULATION (%s not opened)" % self.bus

    def close(self):
        """熄灯并释放总线（可重复调用）。"""
        with self.lock:
            if self._dev is None:
                return
            try:
                self._dev.set_rgb(0, 0, 0)
            except Exception:
                pass
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None

    def __repr__(self):
        return "<SonarI2C %s addr=0x%02X %s>" % (
            self.bus, self.addr, "connected" if self.connected else "sim")
