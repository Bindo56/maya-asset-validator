"""Modal dialogs of the window's fixes (no Maya calls).

``ConfirmDialog`` asks before New Identity. ``RenameDialog`` previews a
duplicate-name rename (``fixes.rename.RenamePlan``): one row per clashing
node, a tick box per renamable node, and an editable new name. Every edit
re-plans through ``replan`` (``functools.partial(plan_rename, issue,
context, handles=..., snapshot=plan.snapshot)``), which never captures the
window: in PySide6 6.5 a callable that holds the window and is connected to
a child's signal keeps a closed window alive, so only bound methods and Qt
slots are connected here.

The window runs these with ``ValidatorWindow.run_dialog`` (the one ``exec()``
call; headless tests patch it) and reads ``plan()`` and ``choice()`` after.
"""

from __future__ import annotations

from collections.abc import Callable
import html
from typing import TYPE_CHECKING

from asset_validator.ui.issue_model import LEVEL_COLORS
from asset_validator.ui.qt import QtCore, QtGui, QtWidgets

if TYPE_CHECKING:
    from asset_validator.fixes.rename import RenameMember, RenamePlan

RENAME_COLUMNS = ("Rename", "Node", "Scope", "New name", "Also renamed", "Status")
TICK, NODE, SCOPE, NEW_NAME, ALSO, STATUS = range(len(RENAME_COLUMNS))

_FLAG = QtCore.Qt.ItemFlag
_CHECKED = QtCore.Qt.CheckState.Checked
_UNCHECKED = QtCore.Qt.CheckState.Unchecked
_BUTTON = QtWidgets.QDialogButtonBox.StandardButton


def _bare(absolute: str) -> str:
    """``":a:mesh_02"`` -> ``"a:mesh_02"`` (the form Maya and D print)."""

    return absolute[1:] if absolute.startswith(":") else absolute


def _leaf(absolute: str) -> str:
    """The name without any namespace."""

    return absolute[absolute.rfind(":") + 1:]


class ConfirmDialog(QtWidgets.QDialog):
    """A word-wrapped question with OK (renamed) and Cancel."""

    def __init__(self, title: str, text: str, ok_text: str = "OK",
                 parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        layout = QtWidgets.QVBoxLayout(self)
        self.label = QtWidgets.QLabel(text)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.buttons = QtWidgets.QDialogButtonBox(_BUTTON.Ok | _BUTTON.Cancel)
        self.ok_button = self.buttons.button(_BUTTON.Ok)
        self.ok_button.setText(ok_text)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)


class RenameDialog(QtWidgets.QDialog):
    """Preview of one D clash: tick rows, edit names, then Rename.

    ``plan()`` is the latest plan (re-planned after every name edit) and
    ``choice()`` the ticked member indices. Neither touches Maya.
    """

    def __init__(self, plan: "RenamePlan", replan: Callable[..., "RenamePlan"],
                 parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self._plan = plan
        self._replan = replan
        self._choice: set[int] = {
            index for index in plan.default_choice if self._member(index).refusal is None
        }
        self._names: dict[int, str] = {}
        self._error: str | None = None

        self.setWindowTitle("Rename duplicate names")
        self.setModal(True)
        self.resize(940, 340)
        layout = QtWidgets.QVBoxLayout(self)
        self.header = QtWidgets.QLabel(
            f"'{plan.short_name}' is used by {len(plan.members)} nodes. Tick the nodes to "
            "rename; their derived shapes follow. One Undo (Ctrl+Z) restores every renamed node."
        )
        self.header.setWordWrap(True)
        layout.addWidget(self.header)

        self.table = QtWidgets.QTableWidget(len(plan.members), len(RENAME_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(RENAME_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.DoubleClicked
            | QtWidgets.QAbstractItemView.EditTrigger.EditKeyPressed
            | QtWidgets.QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)          # Status takes the spare width
        # Maya's dark style draws an unticked item-view box almost invisibly
        # (#262626 on #292929): draw the boxes ourselves.
        self.table.setStyleSheet(
            "QTableWidget::indicator { width: 13px; height: 13px; border: 1px solid #a0a0a0;"
            " border-radius: 2px; background: #1e1e1e; }"
            "QTableWidget::indicator:checked { background: #5285a6; border-color: #d0d0d0; }"
            "QTableWidget::indicator:disabled { border-color: #555555; background: #2a2a2a; }"
        )
        self._build_rows()
        self.table.itemChanged.connect(self._edited)
        layout.addWidget(self.table, 1)

        self.footer = QtWidgets.QLabel("")
        self.footer.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.footer.setWordWrap(True)
        layout.addWidget(self.footer)
        self.buttons = QtWidgets.QDialogButtonBox(_BUTTON.Ok | _BUTTON.Cancel)
        self.ok_button = self.buttons.button(_BUTTON.Ok)
        self.ok_button.setText("Rename")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._refresh()
        # Fit every column but Status to its contents (long or namespaced
        # paths and the derived shapes' new names must not be cut off), then
        # widen the dialog to show them, up to most of the screen.
        for column in (TICK, NODE, SCOPE, NEW_NAME, ALSO):
            self.table.resizeColumnToContents(column)
        wanted = sum(self.table.columnWidth(column) for column in range(len(RENAME_COLUMNS) - 1))
        screen = self.screen().availableGeometry().width() if self.screen() is not None else 1600
        self.resize(min(max(wanted + 260, 760), int(screen * 0.9)), self.sizeHint().height())

    # ------------------------------------------------------------ public
    def plan(self) -> "RenamePlan":
        return self._plan

    def choice(self) -> frozenset[int]:
        return frozenset(self._choice)

    def set_checked(self, index: int, on: bool) -> None:
        """Tick or untick a row, as a click on its box would."""

        self.table.item(index, TICK).setCheckState(_CHECKED if on else _UNCHECKED)

    def set_new_name(self, index: int, leaf: str) -> None:
        """Type a new namespace-free name into a row, as an edit would."""

        self.table.item(index, NEW_NAME).setText(leaf)

    def ok_enabled(self) -> bool:
        if self._error is not None or not self._choice:
            return False
        for index in self._choice:
            member = self._member(index)
            if member.refusal is not None or member.problem is not None or not member.ops:
                return False
        return True

    def accept(self) -> None:
        if self.ok_enabled():        # Enter on a disabled Rename button does nothing
            super().accept()

    # ----------------------------------------------------------- private
    def _member(self, index: int) -> "RenameMember":
        return self._plan.members[index]

    def _build_rows(self) -> None:
        self.table.blockSignals(True)
        try:
            for row, member in enumerate(self._plan.members):
                tick = QtWidgets.QTableWidgetItem("")
                if member.refusal is None:
                    tick.setFlags(_FLAG.ItemIsEnabled | _FLAG.ItemIsUserCheckable | _FLAG.ItemIsSelectable)
                else:
                    tick.setFlags(_FLAG.ItemIsSelectable)          # disabled: cannot be ticked
                    tick.setToolTip(member.refusal)
                tick.setCheckState(_CHECKED if row in self._choice else _UNCHECKED)
                node = QtWidgets.QTableWidgetItem(member.canonical_path)
                tooltip = "\n".join(member.paths)
                if member.promoted_from:
                    tooltip += f"\nRenamed through its transform; clashing shape: {member.promoted_from}"
                node.setToolTip(tooltip)
                scope = QtWidgets.QTableWidgetItem("in scope" if member.in_scope else "outside scope")
                name = QtWidgets.QTableWidgetItem("")
                if member.refusal is None:
                    name.setFlags(_FLAG.ItemIsEnabled | _FLAG.ItemIsSelectable | _FLAG.ItemIsEditable)
                    name.setToolTip("The new name without a namespace; the node keeps its namespace.")
                else:
                    name.setFlags(_FLAG.ItemIsSelectable)
                also = QtWidgets.QTableWidgetItem("")
                status = QtWidgets.QTableWidgetItem("")
                for item in (node, scope, also, status):
                    item.setFlags(_FLAG.ItemIsEnabled | _FLAG.ItemIsSelectable)
                for column, item in ((TICK, tick), (NODE, node), (SCOPE, scope), (NEW_NAME, name),
                                     (ALSO, also), (STATUS, status)):
                    self.table.setItem(row, column, item)
        finally:
            self.table.blockSignals(False)

    def _edited(self, item: QtWidgets.QTableWidgetItem) -> None:
        row, column = item.row(), item.column()
        if not 0 <= row < len(self._plan.members):
            return
        member = self._member(row)
        if column == TICK:
            if member.refusal is None and item.checkState() == _CHECKED:
                self._choice.add(member.index)
            else:
                self._choice.discard(member.index)
        elif column == NEW_NAME:
            if member.refusal is not None:
                return
            self._names[member.index] = item.text().strip()
            self._run_replan()
        else:
            return
        self._refresh()

    def _run_replan(self) -> None:
        try:
            # ``names`` is keyword-only in plan_rename.
            self._plan = self._replan(names=dict(self._names))
            self._error = None
        except Exception as exc:     # RenameRefused (the clash changed) or a Maya error
            self._error = str(exc) or type(exc).__name__

    def _refresh(self) -> None:
        """Rewrite every cell from the current plan without re-entering ``_edited``."""

        plan = self._plan
        self.table.blockSignals(True)
        try:
            for row, member in enumerate(plan.members):
                ticked = member.index in self._choice
                self.table.item(row, TICK).setCheckState(_CHECKED if ticked else _UNCHECKED)
                name_item = self.table.item(row, NEW_NAME)
                also_parts: list[str] = []
                if member.refusal is None and member.ops:
                    name_item.setText(_leaf(member.ops[0].target))
                    also_parts.extend(_bare(op.target) for op in member.ops[1:])
                else:
                    name_item.setText("")
                if member.kept:
                    also_parts.append("keeps: " + ", ".join(
                        f"{path.rsplit('|', 1)[-1]} ({reason})" for path, reason in member.kept
                    ))
                also_item = self.table.item(row, ALSO)
                also_item.setText("; ".join(also_parts))
                also_item.setToolTip("\n".join(also_parts))
                status_item = self.table.item(row, STATUS)
                if member.refusal is not None:
                    status, level = member.refusal, "error"
                elif member.problem is not None:
                    status, level = member.problem, ("error" if ticked else "warning")
                elif not ticked:
                    status, level = "keeps its name", "other"
                else:
                    status, level = "", "other"
                status_item.setText(status)
                status_item.setToolTip(status)
                status_item.setForeground(QtGui.QBrush(QtGui.QColor(LEVEL_COLORS[level])))
        finally:
            self.table.blockSignals(False)
        for column in (NEW_NAME, ALSO):          # an edit or re-plan can lengthen them
            self.table.resizeColumnToContents(column)

        lines = []
        prefix = f"{plan.short_name!r} will still be used by"
        lines.extend(warning for warning in plan.warnings if not warning.startswith(prefix))
        kept = [member.canonical_path for member in plan.members if member.index not in self._choice]
        if len(kept) >= 2:
            lines.append(f"'{plan.short_name}' will still be used by {len(kept)} nodes: {', '.join(kept)}")
        text = "<br>".join(html.escape(line, quote=False) for line in lines)
        if self._error is not None:
            error = f"<span style='color:{LEVEL_COLORS['error']}'>{html.escape(self._error, quote=False)}</span>"
            text = error + ("<br>" + text if text else "")
        self.footer.setText(text)
        self.footer.setVisible(bool(text))
        self.ok_button.setEnabled(self.ok_enabled())


__all__ = ["ConfirmDialog", "RENAME_COLUMNS", "RenameDialog"]
