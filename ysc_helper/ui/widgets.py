"""通用卡片 / 指标块组件。"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


class Card(QFrame):
    def __init__(self, parent: Optional[QWidget] = None, margins: int = 16, spacing: int = 8) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(margins, margins, margins, margins)
        self.lay.setSpacing(spacing)


class MetricTile(QFrame):
    def __init__(self, label: str, value: str = "0", tile: bool = True, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Tile" if tile else "Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 8, 6, 8)
        lay.setSpacing(2)
        self.value = QLabel(value)
        self.value.setObjectName("StatValue")
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel(label)
        self.label.setObjectName("Muted")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.value)
        lay.addWidget(self.label)

    def set_value(self, v: str, color: Optional[str] = None) -> None:
        self.value.setText(v)
        self.value.setStyleSheet(f"color: {color};" if color else "")


def muted(text: str = "", wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("Muted")
    lb.setWordWrap(wrap)
    return lb


def title(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("CardTitle")
    return lb
