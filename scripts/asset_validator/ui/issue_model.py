"""Tree model and filter proxy for a validation report (no Maya calls).

One parent row per check that ran (its verdict, label and issue count), with
one child row per Issue and per execution error. Columns: Severity, Issue,
Object, Count, Action. Each row keeps its Issue or CheckResult in a custom
role, so the window reads the record, not the text. O issues the artist
accepted as Intentional read "Accepted".
"""

from __future__ import annotations

from asset_validator.data_models import CheckResult, Issue, ReportState, Severity, ValidationReport
from asset_validator.ui.qt import QtCore, QtGui

COLUMNS = ("Severity", "Issue", "Object", "Count", "Action")
SEVERITY, ISSUE, OBJECT, COUNT, ACTION = range(len(COLUMNS))

_ROLE = QtCore.Qt.ItemDataRole.UserRole
ISSUE_ROLE = _ROLE + 1        # Issue for an issue row
RESULT_ROLE = _ROLE + 2       # CheckResult for every row of that check
KIND_ROLE = _ROLE + 3         # "check", "issue" or "error"
LEVEL_ROLE = _ROLE + 4        # "error", "incomplete", "review", "warning", "pass" or "other"

LEVEL_COLORS = {
    "error": "#e06c6c",
    "incomplete": "#c08ce0",
    "review": "#6cb4e0",
    "warning": "#e0c06c",
    "pass": "#7cc47c",
    "other": "#a0a0a0",
}
_VERDICT_LEVEL = {
    ReportState.FAILED: "error",
    ReportState.ERROR: "error",
    ReportState.INCOMPLETE: "incomplete",
    ReportState.NEEDS_REVIEW: "review",
    ReportState.PASS_WITH_WARNINGS: "warning",
    ReportState.PASS: "pass",
}


def issue_level(issue: Issue) -> str:
    if issue.severity == Severity.ERROR:
        return "error"
    if issue.needs_review:
        return "review"
    return "warning"


def issue_severity_text(issue: Issue) -> str:
    """Error, Review or Warning; an O issue the artist accepted reads "Accepted".

    Accepted overlaps stay Warning issues, so their level, colour
    and filter bucket are the warning ones; only the text differs.
    """

    if issue.check_id == "O" and dict(issue.details).get("intent") == "intentional":
        return "Accepted"
    return {"error": "Error", "review": "Review", "warning": "Warning"}[issue_level(issue)]


def _item(text: str, level: str, kind: str, result: CheckResult, *, colored: bool = False,
          tooltip: str = "") -> QtGui.QStandardItem:
    item = QtGui.QStandardItem(text)
    item.setEditable(False)
    item.setData(kind, KIND_ROLE)
    item.setData(level, LEVEL_ROLE)
    item.setData(result, RESULT_ROLE)
    if colored:
        item.setForeground(QtGui.QBrush(QtGui.QColor(LEVEL_COLORS[level])))
    if tooltip:
        item.setToolTip(tooltip)
    return item


class IssueModel(QtGui.QStandardItemModel):
    """Rows for one ValidationReport."""

    def __init__(self, parent: QtCore.QObject | None = None):
        super().__init__(0, len(COLUMNS), parent)
        self.setHorizontalHeaderLabels(COLUMNS)
        self.report: ValidationReport | None = None

    def clear_report(self) -> None:
        self.removeRows(0, self.rowCount())
        self.report = None

    def set_report(self, report: ValidationReport) -> None:
        self.clear_report()
        self.report = report
        for result in report.results:
            label = result.metadata.label if result.metadata else result.check_id
            level = _VERDICT_LEVEL.get(result.verdict, "other")
            count = len(result.issues)
            parent = [
                _item(result.verdict.value, level, "check", result, colored=True),
                _item(f"{result.check_id}  {label}", level, "check", result,
                      tooltip=result.metadata.hint if result.metadata else ""),
                _item("", level, "check", result),
                _item(str(count) if count else "", level, "check", result),
                _item("Select all" if count else "", level, "check", result),
            ]
            for issue in result.issues:
                issue_row = self._issue_row(result, issue)
                parent[0].appendRow(issue_row)
            for error in result.errors:
                error_level = "error" if result.execution.value == "Error" else "incomplete"
                parent[0].appendRow([
                    _item(result.execution.value, error_level, "error", result, colored=True),
                    _item(error, error_level, "error", result, tooltip=error),
                    _item("", error_level, "error", result),
                    _item("", error_level, "error", result),
                    _item("", error_level, "error", result),
                ])
            self.appendRow(parent)

    @staticmethod
    def _issue_row(result: CheckResult, issue: Issue) -> list[QtGui.QStandardItem]:
        level = issue_level(issue)
        short_name = issue.target_path.rsplit("|", 1)[-1]
        count = len(issue.component_indices) or 1
        row = [
            _item(issue_severity_text(issue), level, "issue", result, colored=True),
            _item(issue.message, level, "issue", result, tooltip=issue.message),
            _item(short_name, level, "issue", result, tooltip=issue.target_path),
            _item(str(count), level, "issue", result),
            _item("Select", level, "issue", result),
        ]
        for item in row:
            item.setData(issue, ISSUE_ROLE)
        return row


class IssueFilterProxy(QtCore.QSortFilterProxyModel):
    """Text and severity filter.

    A check row stays visible while any child matches; a check row whose own
    label matches the text shows all its children.
    """

    LEVELS = {
        "All": None,
        "Errors and incomplete": {"error", "incomplete"},
        "Review and warnings": {"review", "warning"},
    }

    def __init__(self, parent: QtCore.QObject | None = None):
        super().__init__(parent)
        self.setRecursiveFilteringEnabled(True)
        # Qt 6 shows every child of an accepted row; Qt 5.15 (Maya 2024)
        # has no such option, so filterAcceptsRow does the same there.
        self._auto_accept_children = hasattr(self, "setAutoAcceptChildRows")
        if self._auto_accept_children:
            self.setAutoAcceptChildRows(True)
        self._text = ""
        self._levels: set[str] | None = None

    def set_text(self, text: str) -> None:
        self._text = text.strip().casefold()
        self.invalidateFilter()

    def set_severity(self, name: str) -> None:
        self._levels = self.LEVELS[name]
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QtCore.QModelIndex) -> bool:
        model = self.sourceModel()
        first = model.index(source_row, SEVERITY, source_parent)
        kind = model.data(first, KIND_ROLE)
        if kind == "check":
            # Shown when no filter is active, or when its label matches the
            # text with no severity filter; otherwise only through children.
            if self._levels is not None:
                return False
            label = model.data(model.index(source_row, ISSUE, source_parent)) or ""
            return not self._text or self._text in label.casefold()
        level = model.data(first, LEVEL_ROLE)
        if self._levels is not None and level not in self._levels:
            return False
        if not self._text:
            return True
        issue = model.data(first, ISSUE_ROLE)
        haystack = [model.data(model.index(source_row, column, source_parent)) or ""
                    for column in (ISSUE, OBJECT)]
        if issue is not None:
            haystack.append(issue.target_path)
        if any(self._text in text.casefold() for text in haystack):
            return True
        # Qt 5 fallback for setAutoAcceptChildRows: a child of an accepted
        # row (a check whose label matches) stays visible.
        return (not self._auto_accept_children and source_parent.isValid()
                and self.filterAcceptsRow(source_parent.row(), source_parent.parent()))


__all__ = [
    "ACTION", "COLUMNS", "COUNT", "ISSUE", "ISSUE_ROLE", "IssueFilterProxy", "IssueModel",
    "KIND_ROLE", "LEVEL_COLORS", "LEVEL_ROLE", "OBJECT", "RESULT_ROLE", "SEVERITY",
    "issue_level", "issue_severity_text",
]
