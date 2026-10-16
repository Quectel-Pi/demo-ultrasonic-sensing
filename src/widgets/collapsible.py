#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视图层 —— 可折叠分区（点标题展开 / 收起）

为什么需要它
------------
右侧面板控件越加越多：把「双探头 RGB 灯 / 探头朝向与步进 / 运行日志」全部摊开时，
日志会被挤到可视区外面（必须往右滚动才看得到），而这几项大部分时间并不需要操作。
折叠后首屏只留高频内容：实时读数、方位结论、旋转扫描按钮。

两个实现要点
------------
* 标题就是一个 ``QToolButton``（checkable），箭头随状态翻转，用户一眼看出可点；
* 收起时**隐藏内容**（而不是 ``setEnabled(False)``）：Qt 的 ``QGroupBox`` 勾选只把
  子控件变灰、照样占地方，起不到紧凑的作用。

body 可选带边框（``framed``）：用来**替代**原来的 ``QGroupBox`` 时加边框保持视觉分区；
嵌在别的分组框里时不加，免得出现双层框。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QSizePolicy, QToolButton, QVBoxLayout,
                               QWidget)


class CollapsibleSection(QWidget):
    """标题可点击的分区；内容加到 ``self.body_layout``（或用 ``add_widget``）。

    用法::

        sec = CollapsibleSection("Event log", expanded=False)
        sec.add_widget(self.log_box, stretch=1)
        root.addWidget(sec, 1)

    标题里的 ``&`` 会被 Qt 当成快捷键标记（显示成下划线，如 "Rotate _Scan"），
    这里统一转义成 ``&&``，调用方按普通文本传即可。
    """

    toggled = Signal(bool)          # True = 展开

    def __init__(self, title, expanded=False, framed=False, parent=None):
        super().__init__(parent)
        self.header = QToolButton(self)
        self.header.setObjectName("sectionHeader")
        self.header.setText(self._escape(title))
        self.header.setCheckable(True)
        self.header.setChecked(bool(expanded))
        self.header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setAutoRaise(True)
        # 占满整行：点标题栏任意位置都能展开，而不用精确点中文字
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.header.toggled.connect(self._on_toggled)

        self.body = QWidget(self)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(6)

        if framed:
            self.frame = QFrame(self)
            self.frame.setObjectName("sectionFrame")
            outer = QVBoxLayout(self.frame)
            outer.setContentsMargins(10, 8, 10, 10)
            outer.addWidget(self.body)
        else:
            self.frame = self.body          # 无边框时 body 自己就是显隐目标

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)
        root.addWidget(self.header)
        root.addWidget(self.frame)

        # 收起时本体不占高度；body 在有边框时保持"可见"，显隐交给外框
        if framed:
            self.body.setVisible(True)
        self.frame.setVisible(bool(expanded))

    # ------------------------------------------------------------------
    def add_widget(self, widget, stretch=0):
        self.body_layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout):
        self.body_layout.addLayout(layout)
        return layout

    def set_title(self, text):
        """改标题（用于给日志加未读计数之类的角标）。"""
        self.header.setText(self._escape(text))

    @staticmethod
    def _escape(text):
        """``&`` → ``&&``：Qt 把单个 & 当快捷键标记，会吃掉一个字符并加下划线。"""
        return str(text).replace("&", "&&")

    def set_expanded(self, on):
        self.header.setChecked(bool(on))        # 会触发 _on_toggled

    def is_expanded(self):
        return self.header.isChecked()

    def _on_toggled(self, on):
        self.frame.setVisible(bool(on))
        self.header.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.toggled.emit(bool(on))
