#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一入口 —— 超声波测距图形界面（Quectel Pi M2 / Debian）

用法
----
    python -m src.main                      # 图形界面（默认 /dev/i2c-7 @0x2D + Pin33 舵机扫描）
    python -m src.main --sim                # 仿真模式：无硬件也能验证界面与扫描流程
    python -m src.main --bus /dev/i2c-3 --addr 0x2D --range 80
    python -m src.main --no-servo           # 只用测距，不碰 PWM
    python -m src.main --auto-scan          # 启动即持续往复扫描（默认关闭）
    python -m src.main --scan-step 15       # 15° 步进扫描
    python -m src.main --view side          # 左上图切回侧视声锥视图

舵机权限
--------
板载 PWM 的 sysfs 节点默认仅 root 可写，首次使用需执行一次：

    sudo bash deploy/setup_servo_pwm.sh

未授权时程序不会崩：自动转仿真（界面照常演示扫描与方位判定），
并在右侧日志里打印完整修复步骤。

退出码
------
    0  正常结束
    1  未安装 PySide6 或硬件访问失败

注意
----
本入口**只负责启动 UI**。命令行连续测距功能已移除；如需非图形化测距，
可用项目中自行编写短脚本调用 ``src.sonar_i2c.SonarI2C``，或运行本程序
的仿真/真机图形界面进行观察。
"""

import argparse
import sys

from . import config


def run_gui(args):
    """启动图形界面。"""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        sys.exit(
            "ERROR: PySide6 is not installed. On the board, create a virtual "
            "environment and install the dependency:\n"
            "    cd ~/projects/demo-ultrasonic-sensing\n"
            "    python3 -m venv .venv\n"
            "    .venv/bin/pip install -r requirements.txt \\\n"
            "        -i https://mirrors.aliyun.com/pypi/simple/ \\\n"
            "        --trusted-host mirrors.aliyun.com\n"
            "Or just run ./run_scan.py (it prints the same hint when no environment "
            "is found).")

    from .ui.main_window import MainWindow          # 同样的惰性导入

    app = QApplication(sys.argv)
    window = MainWindow(args.bus, args.addr, args.range, sim=args.sim,
                        servo=not args.no_servo, scan=args.auto_scan,
                        scan_step=args.scan_step, view=args.view)
    window.show()
    return app.exec()


# ---------------------------------------------------------------------------
# 参数解析（仅用于 UI 启动配置）
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Quectel Pi M2 ultrasonic scanner (graphical interface)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--bus", default=config.BUS,
                        help="I2C device node (40-pin Pin27/Pin28)")
    parser.add_argument("--addr", type=lambda x: int(x, 0),
                        default="0x%02X" % config.ADDR, dest="addr",
                        help="module I2C address, accepts 0x2D or 45")
    parser.add_argument("--range", type=float, default=config.DEFAULT_RANGE_CM,
                        dest="range",
                        help="initial detection threshold in cm (%.0f~%.0f)"
                             % (config.RANGE_MIN_CM, config.RANGE_MAX_CM))
    parser.add_argument("--sim", action="store_true",
                        help="simulation mode: no hardware access, synthetic readings")
    parser.add_argument("--no-servo", action="store_true", dest="no_servo",
                        help="do not use the servo at all (ranging only)")
    parser.add_argument("--auto-scan", action="store_true", dest="auto_scan",
                        help="start continuous scanning right away (off by default; "
                             "the Rotate & Scan button performs a single sweep)")
    parser.add_argument("--scan-step", type=float, default=config.SCAN_STEP_DEG,
                        dest="scan_step",
                        help="scan step in degrees (%.0f~%.0f)"
                             % (config.SCAN_STEP_MIN_DEG, config.SCAN_STEP_MAX_DEG))
    parser.add_argument("--view", choices=("scan", "side"),
                        default=config.VIEW_MODE, dest="view",
                        help="top-left view: scan (top-down polar) / side (elevation)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return run_gui(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
