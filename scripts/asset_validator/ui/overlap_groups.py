"""Table model for the window's Overlap groups tab (no Maya calls).

One row per overlap group of the current O issue's mesh, as returned by
``intent.describe_groups``: the group's vertices, how many there are, the
largest pair distance inside it, and the artist's decision. The model is
virtual (``QAbstractTableModel``), so meshes with 10^5 to 10^6 groups stay
usable: nothing is built per row until the view asks for it.
"""

from __future__ import annotations

from collections.abc import Sequence

from asset_validator.ui.issue_model import LEVEL_COLORS
from asset_validator.ui.qt import QtCore, QtGui

HEADERS = ("#", "Vertices", "Count", "Largest distance (cm)", "Decision")
NUMBER, VERTICES, COUNT, DISTANCE, DECISION = range(len(HEADERS))
MAX_SHOWN_VERTICES = 20          # the Vertices cell lists this many, then "..."
MAX_TOOLTIP_VERTICES = 1000

_DECISION_LEVEL = {"Error": "error", "Intentional": "warning", "Not classified": "review"}
_RIGHT = QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter


def _vertex_text(vertices: Sequence[int], limit: int) -> str:
    shown = ", ".join(str(vertex) for vertex in vertices[:limit])
    return shown + (", \u2026" if len(vertices) > limit else "")


class OverlapGroupsModel(QtCore.QAbstractTableModel):
    """Rows of ``GroupRow`` (vertices, largest_distance, decision)."""

    HEADERS = HEADERS

    def __init__(self, parent: QtCore.QObject | None = None):
        super().__init__(parent)
        self._rows: tuple = ()

    def set_rows(self, rows: Sequence) -> None:
        self.beginResetModel()
        self._rows = tuple(rows)
        self.endResetModel()

    def rows(self) -> tuple:
        return self._rows

    def group(self, row: int) -> tuple[int, ...]:
        """The vertex indices of one group (a sorted tuple)."""

        return tuple(self._rows[row].vertices)

    def rows_of(self, groups: Sequence[Sequence[int]]) -> list[int]:
        """The sorted rows showing any of ``groups`` (groups not listed are skipped)."""

        wanted = {tuple(group) for group in groups}
        return [number for number, row in enumerate(self._rows) if tuple(row.vertices) in wanted]

    # ------------------------------------------------------- Qt model API
    def rowCount(self, parent: QtCore.QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QtCore.QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(HEADERS)

    def data(self, index: QtCore.QModelIndex, role: int = QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        column = index.column()
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            if column == NUMBER:
                return str(index.row() + 1)
            if column == VERTICES:
                return _vertex_text(row.vertices, MAX_SHOWN_VERTICES)
            if column == COUNT:
                return str(len(row.vertices))
            if column == DISTANCE:
                return f"{row.largest_distance:.6g}"
            if column == DECISION:
                return row.decision
            return None
        if role == QtCore.Qt.ItemDataRole.ToolTipRole and column == VERTICES:
            if len(row.vertices) > MAX_SHOWN_VERTICES:
                return _vertex_text(row.vertices, MAX_TOOLTIP_VERTICES)
            return None
        if role == QtCore.Qt.ItemDataRole.ForegroundRole and column == DECISION:
            level = _DECISION_LEVEL.get(row.decision)
            return QtGui.QBrush(QtGui.QColor(LEVEL_COLORS[level])) if level else None
        if role == QtCore.Qt.ItemDataRole.TextAlignmentRole and column in (NUMBER, COUNT, DISTANCE):
            return _RIGHT
        return None

    def headerData(self, section: int, orientation: QtCore.Qt.Orientation,
                   role: int = QtCore.Qt.ItemDataRole.DisplayRole):
        if (role == QtCore.Qt.ItemDataRole.DisplayRole
                and orientation == QtCore.Qt.Orientation.Horizontal and 0 <= section < len(HEADERS)):
            return HEADERS[section]
        return None


__all__ = ["HEADERS", "MAX_SHOWN_VERTICES", "OverlapGroupsModel"]
