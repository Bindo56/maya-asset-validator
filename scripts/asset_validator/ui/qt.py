"""The one place the UI imports Qt from.

Every UI module imports ``QtCore``, ``QtGui``, ``QtWidgets`` and the helpers
below from here. PySide6 comes first (Maya 2025 and later); PySide2 is the
fallback for Maya 2023-2024 (Qt 5.15).
"""

from __future__ import annotations

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    import shiboken6 as shiboken
    IS_PYSIDE6 = True
except ImportError:  # Maya 2023-2024 ship PySide2 / Qt 5.15
    from PySide2 import QtCore, QtGui, QtWidgets
    import shiboken2 as shiboken
    IS_PYSIDE6 = False


def maya_main_window() -> QtWidgets.QWidget | None:
    """Maya's main window as a QWidget, or None without one (mayapy)."""

    try:
        import maya.OpenMayaUI as omui
    except ImportError:
        return None
    pointer = omui.MQtUtil.mainWindow()
    if pointer is None:
        return None
    return shiboken.wrapInstance(int(pointer), QtWidgets.QWidget)


def is_alive(widget: QtCore.QObject | None) -> bool:
    """True while the C++ object behind ``widget`` still exists."""

    return widget is not None and shiboken.isValid(widget)


def exec_dialog(dialog: QtWidgets.QDialog) -> int:
    """Run a modal dialog: ``exec()`` in PySide6, ``exec_()`` in PySide2."""

    run = getattr(dialog, "exec", None) or dialog.exec_
    return int(run())


__all__ = [
    "IS_PYSIDE6", "QtCore", "QtGui", "QtWidgets", "exec_dialog", "is_alive", "maya_main_window",
]
