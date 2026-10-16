#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板上启动入口（桌面快捷方式 / 开机自启调用）。

职责：检测含 PySide6 的 Python 解释器，并以 ``python -m src.main`` 拉起
超声波测距图形界面。

检测顺序：

    1. <项目根>/.venv/bin/python     优先，独立环境
    2. 当前解释器 / python3          依赖已装在系统里时

``python -m src.main`` 要求项目根在 cwd（sys.path[0]）上 ``src`` 包才可导入，
因此这里以 ``cwd=ROOT`` 拉起 —— 与从哪个目录双击、从哪条路径调用都无关。

找不到含 PySide6 的解释器时，会给出创建隔离环境并安装依赖的具体命令，
而不是直接崩溃或进入非图形模式。
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# 板上优先使用项目自身的虚拟环境
VENV_CANDIDATES = (
    os.path.join(ROOT, ".venv", "bin", "python"),
)

INSTALL_HINT = """\
No Python with PySide6 was found, so the GUI cannot start.

Create an isolated environment and install the dependency (once):

    cd {root}
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt \\
        -i https://mirrors.aliyun.com/pypi/simple/ \\
        --trusted-host mirrors.aliyun.com

Then run again:

    ./run_scan.py

Note: this program only provides the graphical interface.
"""


def has_pyside(python):
    """该解释器能否 import PySide6。"""
    try:
        return subprocess.call(
            [python, "-c", "import PySide6"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def pick_python():
    """挑一个含 PySide6 的解释器；没有则返回 None。"""
    for cand in VENV_CANDIDATES:
        if os.path.isfile(cand) and os.access(cand, os.X_OK) and has_pyside(cand):
            return cand
    for cand in (sys.executable, "python3"):
        if cand and has_pyside(cand):
            return cand
    return None


def main():
    python = pick_python()
    if python is None:
        sys.stderr.write(INSTALL_HINT.format(root=ROOT))
        return 1

    print("using python:", python)
    return subprocess.call([python, "-m", "src.main"] + sys.argv[1:], cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
