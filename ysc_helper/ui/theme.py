"""浅色 / 深色主题（配色沿用 Android 版 ui/theme/Theme.kt）。"""
from __future__ import annotations

from dataclasses import dataclass

PRIMARY_BLUE = "#2563EB"
ACCENT_PURPLE = "#7C3AED"
STATUS_OK_GREEN = "#16A34A"
STATUS_WARN_AMBER = "#D97706"
STATUS_BAD_RED = "#DC2626"
NEUTRAL_GRAY = "#6B7280"


@dataclass(frozen=True)
class Palette:
    dark: bool
    primary: str
    background: str
    surface: str
    surface_variant: str
    on_surface: str
    muted: str
    border: str


LIGHT = Palette(False, PRIMARY_BLUE, "#F1F5F9", "#FFFFFF", "#F8FAFC", "#0F172A", "#64748B", "#E2E8F0")
DARK = Palette(True, "#60A5FA", "#0F172A", "#1E293B", "#16223A", "#E2E8F0", "#94A3B8", "#334155")


def stylesheet(p: Palette) -> str:
    return f"""
    QWidget {{ color: {p.on_surface}; font-size: 13px; }}
    QMainWindow, QWidget#Root, QScrollArea, QScrollArea > QWidget > QWidget#TaskInner {{ background: {p.background}; }}
    QFrame#Card {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 12px; }}
    QFrame#Tile {{ background: {p.surface_variant}; border: 1px solid {p.border}; border-radius: 10px; }}
    QFrame#Header {{ background: {p.surface}; border-bottom: 1px solid {p.border}; }}
    QLabel {{ background: transparent; }}
    QLabel#Title {{ font-size: 20px; font-weight: 800; }}
    QLabel#Subtitle, QLabel#Muted {{ color: {p.muted}; font-size: 12px; }}
    QLabel#CardTitle {{ font-size: 14px; font-weight: 700; }}
    QLabel#StatValue {{ font-size: 20px; font-weight: 800; }}
    QLabel#Accent {{ color: {p.primary}; font-weight: 700; }}
    QLabel#Badge {{ color: {p.primary}; background: {"#1E3A8A" if p.dark else "#DBEAFE"};
                   border-radius: 9px; padding: 2px 10px; font-size: 11px; font-weight: 600; }}
    QLabel#DemoBadge {{ color: white; background: {STATUS_WARN_AMBER}; border-radius: 9px; padding: 2px 10px;
                       font-size: 11px; font-weight: 600; }}
    QTabWidget::pane {{ border: none; background: {p.background}; }}
    QTabBar::tab {{ background: transparent; color: {p.muted}; padding: 8px 22px; font-size: 14px;
                   border-bottom: 3px solid transparent; }}
    QTabBar::tab:selected {{ color: {p.primary}; border-bottom: 3px solid {p.primary}; font-weight: 700; }}
    QPushButton {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 8px; padding: 6px 14px; }}
    QPushButton:hover {{ border-color: {p.primary}; }}
    QPushButton:disabled {{ color: {p.muted}; background: {p.surface_variant}; }}
    QPushButton#Primary {{ background: {p.primary}; color: white; border: none; font-weight: 700; }}
    QPushButton#Primary:disabled {{ background: {p.border}; color: {p.muted}; }}
    QPushButton#Danger {{ background: {STATUS_BAD_RED}; color: white; border: none; font-weight: 700; }}
    QPushButton#BigPrimary {{ background: {p.primary}; color: white; border: none; border-radius: 12px;
                             font-size: 15px; font-weight: 700; min-height: 46px; }}
    QPushButton#BigDanger {{ background: {STATUS_BAD_RED}; color: white; border: none; border-radius: 12px;
                            font-size: 15px; font-weight: 700; min-height: 46px; }}
    QPushButton#Chip {{ border-radius: 14px; padding: 4px 12px; font-size: 12px; }}
    QPushButton#Chip:checked {{ background: {"#1E3A8A" if p.dark else "#DBEAFE"}; border-color: {p.primary};
                               color: {p.primary}; font-weight: 600; }}
    QPushButton#Link {{ border: none; background: transparent; color: {p.primary}; padding: 4px 6px; }}
    QPushButton#LinkDanger {{ border: none; background: transparent; color: {STATUS_BAD_RED}; padding: 4px 6px; }}
    QLineEdit {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 10px; padding: 7px 10px; }}
    QLineEdit:focus {{ border-color: {p.primary}; }}
    QProgressBar {{ background: {p.surface_variant}; border: none; border-radius: 4px; height: 9px; }}
    QProgressBar::chunk {{ background: {p.primary}; border-radius: 4px; }}
    QTreeWidget, QTableWidget, QListWidget {{ background: {p.surface}; alternate-background-color: {p.surface_variant};
                   border: 1px solid {p.border}; border-radius: 10px; }}
    QTreeWidget::item, QTableWidget::item {{ padding: 4px 2px; }}
    QTreeWidget::item:selected, QTableWidget::item:selected {{ background: {"#1E3A8A" if p.dark else "#DBEAFE"};
                   color: {p.on_surface}; }}
    QHeaderView::section {{ background: {p.surface_variant}; color: {p.muted}; border: none;
                   border-bottom: 1px solid {p.border}; padding: 6px; font-weight: 600; }}
    QSlider::groove:horizontal {{ height: 6px; background: {p.border}; border-radius: 3px; }}
    QSlider::sub-page:horizontal {{ background: {p.primary}; border-radius: 3px; }}
    QSlider::handle:horizontal {{ background: {p.primary}; width: 18px; margin: -6px 0; border-radius: 9px; }}
    QCheckBox, QRadioButton {{ background: transparent; spacing: 6px; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; border: 2px solid {p.muted};
                   background: {p.surface}; }}
    QCheckBox::indicator {{ border-radius: 4px; }}
    QRadioButton::indicator {{ border-radius: 9px; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {p.primary};
                   border-color: {p.primary}; }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {p.border}; }}
    QStatusBar {{ background: {p.surface}; color: {p.on_surface}; border-top: 1px solid {p.border}; }}
    QMenu {{ background: {p.surface}; border: 1px solid {p.border}; }}
    QMenu::item:selected {{ background: {p.primary}; color: white; }}
    QToolTip {{ background: {p.surface}; color: {p.on_surface}; border: 1px solid {p.border}; }}
    """
