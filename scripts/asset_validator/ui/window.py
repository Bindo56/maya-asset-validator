"""The artist window: run the checks, browse issues, select targets, rename
duplicate names and classify overlaps.

Layout: a splitter with the check list on the left (a coloured label per
check, an enable box, a Run button, and Uncheck All / Invert / Check Failed
Only / Check All below) and the report on the right. The report is a
QTreeView over IssueModel with a filter proxy and a Details / Overlap groups
tab pair, and every Select is verified first (ui.scene).

Reopening raises and activates the open window; settings live in an optionVar.
Avoided: a main-window lookup in a default argument (it crashes without a GUI),
class-level widget dictionaries shared by every instance, reconnecting the
select button's signal on every report, and a window reference that outlives
its window.

The run is the shared generator stepped by single-shot QTimers, a time slice
per tick. Scene edits during or after a run mark the report Stale.

Fixes. The action row under the report holds Select, Rename...,
Mark Intentional, Mark Error, Clear Mark and New Identity.... A fix that edits
the scene (Rename, New Identity) runs in its own named undo chunk, which the
SceneWatcher does not filter, so Undo of a fix makes the report Stale. After
such a fix the window re-runs the last check set and rebuilds every row
(it never removes a row optimistically); the status then reports the check's
new verdict. Marks change no node: they update the overlap decisions and
re-annotate the last run in place, and the report stays fresh. Every modal
dialog runs through ``run_dialog``, the one ``exec()`` call, which headless
tests patch.
"""

from __future__ import annotations

from dataclasses import replace
import functools
import html
import json
import os
import sys
import time
import traceback

from asset_validator.core import ValidationControl, ValidationStepper, default_rules, iter_validation
from asset_validator.data_models import ReportState, ScopeMode, ValidationReport
from asset_validator.fixes.identity import IdentityRefused, apply_new_identity, plan_new_identity
from asset_validator.fixes.rename import RenameRefused, apply_rename, plan_rename
from asset_validator.intent import session as intent_session
from asset_validator.intent.apply import (
    ClassifyRefused, GroupsView, apply_intent, classify, describe_groups,
)
from asset_validator.intent.store import IntentStore, sidecar_path
from asset_validator.maya_adapter import resolve_scope
from asset_validator.ui.dialogs import ConfirmDialog, RenameDialog
from asset_validator.ui.issue_model import (
    ACTION, COLUMNS, ISSUE_ROLE, KIND_ROLE, LEVEL_COLORS, RESULT_ROLE, IssueFilterProxy, IssueModel,
    issue_severity_text,
)
from asset_validator.ui.overlap_groups import OverlapGroupsModel
from asset_validator.ui.qt import QtCore, QtGui, QtWidgets, exec_dialog, is_alive, maya_main_window
from asset_validator.ui.scene import SceneWatcher, SelectRefused, select_issues

OBJECT_NAME = "AssetValidatorWindow"
SETTINGS_VAR = "assetValidatorWindowSettings"
STEP_SECONDS = 0.03          # work per QTimer tick before the UI repaints
DETAILS_TAB, GROUPS_TAB = 0, 1

# Verdict colours for the check labels.
VERDICT_BACKGROUND = {
    ReportState.PASS: "#446644",
    ReportState.PASS_WITH_WARNINGS: "#6b6233",
    ReportState.NEEDS_REVIEW: "#335a6b",
    ReportState.FAILED: "#664444",
    ReportState.ERROR: "#5a3d66",
    ReportState.INCOMPLETE: "#55555f",
}
STATE_COLORS = {
    ReportState.PASS: LEVEL_COLORS["pass"],
    ReportState.PASS_WITH_WARNINGS: LEVEL_COLORS["warning"],
    ReportState.NEEDS_REVIEW: LEVEL_COLORS["review"],
    ReportState.FAILED: LEVEL_COLORS["error"],
    ReportState.ERROR: LEVEL_COLORS["error"],
    ReportState.INCOMPLETE: LEVEL_COLORS["incomplete"],
    ReportState.STALE: LEVEL_COLORS["other"],
    ReportState.NO_TARGETS: LEVEL_COLORS["other"],
}

_TIPS = {
    "select": "Select the current row's verified target in Maya",
    "rename": "Rename the nodes of this duplicate name (a preview opens first; one Undo restores them)",
    "intentional": "Accept the row's overlap groups (or the groups selected in Overlap groups) as Intentional",
    "error": "Mark the row's overlap groups (or the groups selected in Overlap groups) as Error",
    "clear": "Remove the Intentional/Error marks of the row's groups (or the selected groups)",
    "identity": "Give this imported copy its own Maya UUID so its overlaps can be marked (one Undo restores it)",
}

_WINDOW: "ValidatorWindow | None" = None


def _close_watcher(watcher: SceneWatcher, *_args) -> None:
    watcher.close()


def _dag_paths(paths) -> tuple:
    """Live MDagPaths for the selected DAG objects (non-DAG entries are skipped)."""
    import maya.api.OpenMaya as om

    found = []
    for path in paths:
        try:
            selection = om.MSelectionList()
            selection.add(path)
            found.append(selection.getDagPath(0))
        except (RuntimeError, TypeError):
            continue
    return tuple(found)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


class CheckRow(QtWidgets.QWidget):
    """One check: coloured label, enable box, Run button and issue count."""

    # Signals and bound methods only: in PySide6 6.5 a lambda that captures
    # the window, connected to a child's signal, keeps the closed window alive.
    run_requested = QtCore.Signal(str)

    def __init__(self, rule, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.check_id = rule.check_id
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.label = QtWidgets.QLabel(f"{rule.check_id}  {rule.metadata.label}")
        self.label.setMinimumWidth(190)
        self.label.setToolTip(rule.metadata.hint)
        self.enabled = QtWidgets.QCheckBox()
        self.enabled.setChecked(True)
        self.enabled.setToolTip("Include this check in Run Checked")
        self.run_button = QtWidgets.QPushButton("Run")
        self.run_button.setMaximumWidth(48)
        self.run_button.setToolTip("Run only this check (a partial run)")
        self.run_button.clicked.connect(self._request_run)
        self.count = QtWidgets.QLabel("")
        self.count.setMinimumWidth(48)
        for widget in (self.label, self.enabled, self.run_button, self.count):
            layout.addWidget(widget)
        self.verdict: ReportState | None = None
        self.set_result(None)

    def _request_run(self, *_args) -> None:
        self.run_requested.emit(self.check_id)

    def set_result(self, result) -> None:
        self.verdict = result.verdict if result is not None else None
        color = VERDICT_BACKGROUND.get(self.verdict)
        self.label.setStyleSheet(
            f"background-color: {color}; padding: 2px;" if color else "padding: 2px;"
        )
        self.count.setText(str(len(result.issues)) if result is not None and result.issues else "")


class ValidatorWindow(QtWidgets.QWidget):
    """Non-modal tool window parented to Maya's main window."""

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setWindowFlags(QtCore.Qt.WindowType.Window)
        self.setObjectName(OBJECT_NAME)
        self.setWindowTitle("Asset Validator")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(1100, 720)

        self._rules = default_rules()
        self._results: dict[str, object] = {}
        self._stepper: ValidationStepper | None = None
        self._control: ValidationControl | None = None
        self._running = False
        self._closed = False
        self._hidden_with_report = False
        self._stale_stepper: ValidationStepper | None = None
        self.report: ValidationReport | None = None
        # The unannotated report of the last run (decisions are
        # applied to it, never to the shown one), the check set to re-run
        # after a fix, the fix whose verdict the next run reports, and the
        # O measurements cached while the report stays fresh.
        self._base_report: ValidationReport | None = None
        self._last_check_ids: tuple[str, ...] = ()
        # (scope mode, MDagPaths of the selected objects) of the last run: a
        # renamed node keeps its MDagPath, so a re-run after a fix finds it.
        self._last_scope: tuple[str, tuple] | None = None
        self._after_fix: tuple[str, str | None, str, str] | None = None
        self._measure_cache: dict = {}
        self._intent_notes: dict[str, list[str]] = {}
        self._report_serial = 0
        self._groups_target: str | None = None
        self._groups_key: tuple[int, str] | None = None
        self._defer_groups = False
        self._watcher = SceneWatcher(self._on_scene_edit)
        # Observe scene events (imports, saves, reference edits) from the moment
        # the window opens, so decisions are checked against them; the session
        # holds no reference to the window. A userSetup.py install() covers
        # the time before the window ever opens.
        try:
            intent_session.get_session(create=True)
        except Exception as exc:          # decisions are optional; never block the window
            print(f"Asset Validator: overlap decisions are unavailable ({exc}).")
        # Destroyed without a close event (parent deleted, deleteUI): still
        # remove the callbacks. The closure must not capture the window.
        self.destroyed.connect(functools.partial(_close_watcher, self._watcher))

        self._build()
        self._load_settings()
        self._connect_settings()
        self._show_state(None)
        # A save made from Maya's own window (Ctrl+S there) changes the
        # decisions summary without activating this window; poll it cheaply.
        self._label_timer = QtCore.QTimer(self)
        self._label_timer.setInterval(1000)
        self._label_timer.timeout.connect(self._update_intent_label)        # bound method: no leak
        self._label_timer.start()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        main = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Scope:"))
        self.scope = QtWidgets.QComboBox()
        self.scope.addItems([mode.value for mode in ScopeMode])
        self.scope.setToolTip(
            "Selection: selected meshes and the meshes of selected transforms. "
            "Hierarchy: every mesh under the selection. Scene: every mesh."
        )
        top.addWidget(self.scope)
        self.run_all_button = QtWidgets.QPushButton("Run All")
        self.run_checked_button = QtWidgets.QPushButton("Run Checked")
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.clear_button = QtWidgets.QPushButton("Clear")
        self.run_all_button.clicked.connect(self.run_all)
        self.run_checked_button.clicked.connect(self.run_checked)
        self.cancel_button.clicked.connect(self.cancel)
        self.clear_button.clicked.connect(self.clear)
        for button in (self.run_all_button, self.run_checked_button, self.cancel_button, self.clear_button):
            top.addWidget(button)
        top.addStretch(1)
        self.state_label = QtWidgets.QLabel()
        self.partial_label = QtWidgets.QLabel("Partial")
        self.partial_label.setToolTip("Not every required check ran")
        self.partial_label.setStyleSheet(f"color: {LEVEL_COLORS['warning']}; font-weight: bold;")
        top.addWidget(self.state_label)
        top.addWidget(self.partial_label)
        main.addLayout(top)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_checks())
        splitter.addWidget(self._build_report())
        splitter.setStretchFactor(1, 1)
        main.addWidget(splitter, 1)

        bottom = QtWidgets.QHBoxLayout()
        self.progress = QtWidgets.QProgressBar()
        self.progress.setTextVisible(True)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        bottom.addWidget(self.progress, 1)
        bottom.addWidget(self.status_label, 2)
        main.addLayout(bottom)

    def _build_checks(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        header = QtWidgets.QLabel("CHECKS")
        header.setStyleSheet("background-color: grey; color: black; font-size: 16px; padding: 4px;")
        header.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(header)
        self.check_rows: dict[str, CheckRow] = {}
        for rule in self._rules:
            row = CheckRow(rule, panel)
            row.run_requested.connect(self.run_single)
            self.check_rows[rule.check_id] = row
            layout.addWidget(row)
        layout.addStretch(1)
        buttons = QtWidgets.QGridLayout()
        for position, (text, slot) in enumerate((
            ("Uncheck All", self.uncheck_all), ("Invert", self.invert_checks),
            ("Check Failed Only", self.check_failed_only), ("Check All", self.check_all),
        )):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button, position // 2, position % 2)
        layout.addLayout(buttons)
        return panel

    def _build_report(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        filters = QtWidgets.QHBoxLayout()
        self.filter_text = QtWidgets.QLineEdit()
        self.filter_text.setPlaceholderText("Filter issues or objects")
        self.filter_text.setClearButtonEnabled(True)
        self.filter_severity = QtWidgets.QComboBox()
        self.filter_severity.addItems(list(IssueFilterProxy.LEVELS))
        filters.addWidget(self.filter_text, 1)
        filters.addWidget(self.filter_severity)
        layout.addLayout(filters)

        self.model = IssueModel(self)
        self.proxy = IssueFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.filter_text.textChanged.connect(self.proxy.set_text)
        self.filter_severity.currentTextChanged.connect(self.proxy.set_severity)

        self.tree = QtWidgets.QTreeView()
        self.tree.setModel(self.proxy)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.tree.clicked.connect(self._clicked)
        self.tree.doubleClicked.connect(self.select_index)
        self.tree.selectionModel().currentRowChanged.connect(self._current_changed)

        self.details = QtWidgets.QTextBrowser()
        self.details.setOpenLinks(False)
        self.detail_tabs = QtWidgets.QTabWidget()
        self.detail_tabs.addTab(self.details, "Details")
        self.detail_tabs.addTab(self._build_groups_panel(), "Overlap groups")
        self.detail_tabs.setTabEnabled(GROUPS_TAB, False)
        self.detail_tabs.currentChanged.connect(self._tab_changed)

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        split.addWidget(self.tree)
        split.addWidget(self.detail_tabs)
        # 3:2, so the Overlap groups tab shows several groups at Maya's
        # default size (3:1 leaves about two rows).
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)
        layout.addLayout(self._build_actions())
        return panel

    def _build_groups_panel(self) -> QtWidgets.QWidget:
        self.groups_panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(self.groups_panel)
        layout.setContentsMargins(2, 2, 2, 2)
        self.groups_note = QtWidgets.QLabel("")
        self.groups_note.setWordWrap(True)
        self.groups_model = OverlapGroupsModel(self)
        self.groups_view = QtWidgets.QTableView()
        self.groups_view.setModel(self.groups_model)
        self.groups_view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.groups_view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.groups_view.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.groups_view.verticalHeader().setVisible(False)
        self.groups_view.horizontalHeader().setStretchLastSection(True)
        self.groups_view.setToolTip("Select groups to mark only those; double-click a group to select its vertices")
        self.groups_view.doubleClicked.connect(self._group_double_clicked)
        self.groups_view.selectionModel().selectionChanged.connect(self._groups_selection_changed)
        # At least five rows at the style's row height (30 px in Maya 2025).
        frame = 2 * self.groups_view.frameWidth()
        self.groups_view.setMinimumHeight(
            self.groups_view.horizontalHeader().sizeHint().height()
            + 5 * self.groups_view.verticalHeader().defaultSectionSize() + frame
        )
        layout.addWidget(self.groups_note)
        layout.addWidget(self.groups_view, 1)
        return self.groups_panel

    def _build_actions(self) -> QtWidgets.QVBoxLayout:
        rows = QtWidgets.QVBoxLayout()
        actions = QtWidgets.QHBoxLayout()
        self.select_button = QtWidgets.QPushButton("Select")
        self.select_button.setToolTip(_TIPS["select"])
        self.select_button.clicked.connect(self._select_current)
        self.rename_button = QtWidgets.QPushButton("Rename\u2026")
        self.rename_button.clicked.connect(self.rename_current)
        self.intentional_button = QtWidgets.QPushButton("Mark Intentional")
        self.intentional_button.clicked.connect(self._mark_intentional_clicked)
        self.error_button = QtWidgets.QPushButton("Mark Error")
        self.error_button.clicked.connect(self._mark_error_clicked)
        self.clear_mark_button = QtWidgets.QPushButton("Clear Mark")
        self.clear_mark_button.clicked.connect(self.clear_current_mark)
        self.new_identity_button = QtWidgets.QPushButton("New Identity\u2026")
        self.new_identity_button.clicked.connect(self.new_identity_current)
        separator = QtWidgets.QFrame()
        separator.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        separator.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
        actions.addWidget(self.select_button, 1)
        actions.addWidget(self.rename_button)
        actions.addWidget(separator)
        for button in (self.intentional_button, self.error_button, self.clear_mark_button,
                       self.new_identity_button):
            actions.addWidget(button)
        rows.addLayout(actions)

        labels = QtWidgets.QHBoxLayout()
        self.acts_on_label = QtWidgets.QLabel("")
        self.intent_label = QtWidgets.QLabel("")
        self.intent_label.setWordWrap(True)
        self.intent_label.setStyleSheet(f"color: {LEVEL_COLORS['other']};")
        labels.addWidget(self.acts_on_label)
        labels.addStretch(1)
        labels.addWidget(self.intent_label, 2)
        rows.addLayout(labels)
        return rows

    # --------------------------------------------------------------- settings
    def _load_settings(self) -> None:
        try:
            import maya.cmds as cmds
            if not cmds.optionVar(exists=SETTINGS_VAR):
                return
            settings = json.loads(cmds.optionVar(query=SETTINGS_VAR))
            enabled = set(settings.get("enabled", self.check_rows))
            for check_id, row in self.check_rows.items():
                row.enabled.setChecked(check_id in enabled)
            scope = settings.get("scope")
            if scope in [mode.value for mode in ScopeMode]:
                self.scope.setCurrentText(scope)
        except Exception:
            pass        # settings are a convenience; never block the window

    def _connect_settings(self) -> None:
        # Save on every change too: a window destroyed without a close event
        # never reaches closeEvent.
        for row in self.check_rows.values():
            row.enabled.toggled.connect(self._save_settings)
        self.scope.currentTextChanged.connect(self._save_settings)

    def _save_settings(self, *_args) -> None:
        try:
            import maya.cmds as cmds
            settings = {
                "enabled": [check_id for check_id, row in self.check_rows.items() if row.enabled.isChecked()],
                "scope": self.scope.currentText(),
            }
            cmds.optionVar(stringValue=(SETTINGS_VAR, json.dumps(settings)))
        except Exception:
            pass

    # ----------------------------------------------------------- check toggles
    def check_all(self) -> None:
        for row in self.check_rows.values():
            row.enabled.setChecked(True)

    def uncheck_all(self) -> None:
        for row in self.check_rows.values():
            row.enabled.setChecked(False)

    def invert_checks(self) -> None:
        for row in self.check_rows.values():
            row.enabled.setChecked(not row.enabled.isChecked())

    def check_failed_only(self) -> None:
        """Enable only checks whose last result has an issue or did not complete."""
        for check_id, row in self.check_rows.items():
            result = self._results.get(check_id)
            row.enabled.setChecked(
                result is not None and (bool(result.issues) or result.verdict != ReportState.PASS)
            )

    # -------------------------------------------------------------------- run
    @property
    def is_running(self) -> bool:
        return self._running

    def run_all(self) -> None:
        self.run_checks([rule.check_id for rule in self._rules])

    def run_checked(self) -> None:
        self.run_checks([check_id for check_id, row in self.check_rows.items() if row.enabled.isChecked()])

    def run_single(self, check_id: str) -> None:
        self.run_checks([check_id])

    def run_checks(self, check_ids, *, rerun_scope: tuple[str, list[str]] | None = None) -> None:
        """Run ``check_ids`` on the Scope combo and the live selection.

        ``rerun_scope`` (mode, selected paths) re-runs a previous run's scope
        instead, so the re-run after a fix covers the meshes it was made from.
        """
        if self._running:
            return
        rules = [rule for rule in default_rules() if rule.check_id in set(check_ids)]
        if not rules:
            self._status("No checks are enabled.", "warning")
            return
        self._last_check_ids = tuple(rule.check_id for rule in rules)
        # Reset the visible state before the first step, so a failure during
        # setup never shows the previous run's result.
        self._watcher.clear_report_callbacks()
        self.model.clear_report()
        self.report = None
        self._results = {}
        self._reset_fix_state()
        for row in self.check_rows.values():
            row.set_result(None)
        self.details.clear()
        self._running = True
        self._set_running_ui(True)
        self.state_label.setText("Running...")
        self.state_label.setStyleSheet("font-weight: bold;")
        self.partial_label.setVisible(False)
        self._status("", "other")
        try:
            if rerun_scope is not None:
                context = resolve_scope(rerun_scope[0], selection=rerun_scope[1])
            else:
                context = resolve_scope(self.scope.currentText())
            self._last_scope = (context.mode.value, _dag_paths(context.selection))
            self._control = ValidationControl()
            self._stepper = ValidationStepper(
                iter_validation(context, rules=rules, control=self._control), control=self._control,
            )
            # Watch from the start: an edit between steps ends the run Stale.
            self._watcher.watch(
                context.targets,
                include_mesh_siblings=any(rule.check_id == "C" for rule in rules),
            )
        except Exception as exc:
            self._fail(exc)
            return
        self.progress.setRange(0, 0)
        QtCore.QTimer.singleShot(0, self._step)

    def cancel(self) -> None:
        if self._running and self._control is not None:
            self._control.cancel()

    def clear(self) -> None:
        if self._running:
            return
        self._watcher.clear_report_callbacks()
        self.model.clear_report()
        self.report = None
        self._stepper = None
        self._results = {}
        self._reset_fix_state()
        self._after_fix = None
        for row in self.check_rows.values():
            row.set_result(None)
        self.details.clear()
        self._show_state(None)
        self._status("", "other")

    def _reset_fix_state(self) -> None:
        """Forget the last run's base report, O measurements and groups view."""
        self._base_report = None
        self._measure_cache.clear()
        self._intent_notes = {}
        self._reset_groups("")

    def _step(self) -> None:
        # Runs from a QTimer: catch everything so the UI never stays Running.
        if not self._running or not is_alive(self):
            return
        try:
            deadline = time.perf_counter() + STEP_SECONDS
            while True:
                event = self._stepper.advance()
                if event is not None and event.total_work_units:
                    self.progress.setRange(0, event.total_work_units)
                    self.progress.setValue(event.completed_work_units)
                if self._stepper.done or time.perf_counter() >= deadline:
                    break
            if self._stepper.done:
                self._finish()        # showing the report can fail too
                return
        except Exception as exc:
            self._fail(exc)
            return
        QtCore.QTimer.singleShot(0, self._step)

    def _finish(self) -> None:
        self._running = False
        self._set_running_ui(False)
        base = self._stepper.report
        if base is None:
            self._fail(RuntimeError("The validation run ended without a report."))
            return
        self._base_report = base
        shown = base
        intent_problem: str | None = None
        notes: list[str] = []
        if base.state != ReportState.STALE and any(result.check_id == "O" for result in base.results):
            # Decisions are an explicit post-step on the finished report; the
            # stepper keeps the annotated one so mark_stale() keeps its rows.
            try:
                scene_path = base.context.scene_path
                has_sidecar = bool(scene_path) and os.path.exists(sidecar_path(scene_path))
                session = intent_session.get_session(create=has_sidecar)
                if session is not None:
                    result = apply_intent(base, session.store, handles=self._watcher.handles,
                                          cache=self._measure_cache)
                    shown = result.report
                    self._intent_notes = self._group_notes(result.notes)
                    notes = session.store.take_notes()
            except Exception as exc:
                shown = base
                self._intent_notes = {}
                intent_problem = (f"Overlap decisions could not be checked ({type(exc).__name__}: "
                                  f"{exc}); none were applied.")
            self._stepper.report = shown
        self._apply_report(shown)
        if shown.state == ReportState.STALE:
            self._watcher.clear_report_callbacks()
        self._finish_status(shown, intent_problem, notes)

    def _finish_status(self, report: ValidationReport, intent_problem: str | None,
                       notes: list[str]) -> None:
        """The after-fix verdict, then decision problems and notes, after a run."""
        parts: list[str] = []
        level = "other"
        if report.errors:            # _show_state already said so; keep it first
            parts.append(self.status_label.text())
            level = "error"
        if self._after_fix is not None:
            text, fix_level = self._after_fix_text(report)
            parts.append(text)
            level = fix_level if level == "other" else level
        if intent_problem:
            parts.append(intent_problem)
            level = "warning" if level in ("other", "pass") else level
        if notes:
            parts.extend(notes)
            level = "warning" if level in ("other", "pass") else level
        if parts:
            self._status(" ".join(part for part in parts if part), level)

    def _after_fix_text(self, report: ValidationReport) -> tuple[str, str]:
        """The fix's own message plus " D is now Pass." (or why not); clears it."""
        check_id, short_name, text, level = self._after_fix
        self._after_fix = None
        if report.state == ReportState.STALE:
            return (f"{text} The re-run ended stale; run the checks again.",
                    "error" if level == "error" else "warning")
        result = next((result for result in report.results if result.check_id == check_id), None)
        if result is None:
            return text, level
        if short_name is not None and any(
            dict(issue.details).get("short_name") == short_name for issue in result.issues
        ):
            # A failed fix keeps its error colour; only a success is demoted.
            return f"{text} {check_id} still reports '{short_name}'.", "error" if level == "error" else "warning"
        return f"{text} {check_id} is now {result.verdict.value}.", level

    def _fail(self, exc: BaseException) -> None:
        self._running = False
        self._set_running_ui(False)
        self._watcher.clear_report_callbacks()
        self.report = None
        self._reset_fix_state()
        self.state_label.setText("Error")
        self.state_label.setStyleSheet(f"color: {LEVEL_COLORS['error']}; font-weight: bold;")
        self.partial_label.setVisible(False)
        prefix = ""
        if self._after_fix is not None:
            prefix = self._after_fix[2] + " "
            self._after_fix = None
        self._status(f"{prefix}The run failed: {type(exc).__name__}: {exc}", "error")
        self.details.setPlainText("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        self._update_actions()

    def _apply_report(self, report: ValidationReport) -> None:
        self.report = report
        self._report_serial += 1
        self._results = {result.check_id: result for result in report.results}
        for check_id, row in self.check_rows.items():
            row.set_result(self._results.get(check_id))
        self.model.set_report(report)
        self.tree.expandAll()
        for column in (0, 2, 3, 4):
            self.tree.resizeColumnToContents(column)
        self._show_state(report)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self._update_actions()

    def _set_running_ui(self, running: bool) -> None:
        for widget in (self.run_all_button, self.run_checked_button, self.clear_button, self.scope):
            widget.setEnabled(not running)
        for row in self.check_rows.values():
            row.run_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self._update_actions()

    # ------------------------------------------------------------- staleness
    def _on_scene_edit(self) -> None:
        """A watched node, Undo/Redo, rename or scene change: mark Stale only."""
        if not is_alive(self):
            return
        if self._running and self._control is not None:
            self._control.mark_stale()
        elif self._stepper is not None and self._stepper.report is not None:
            if self._stepper.report.state != ReportState.STALE:
                self._stepper.mark_stale()
                self.report = self._stepper.report
                self._show_state(self.report)
        # Measurements and the groups view describe the scene before the edit.
        self._measure_cache.clear()
        self._reset_groups("The report is stale; run the checks again." if self.report is not None else "")
        if not self._running:
            # Stop watching a stale report. Defer: we may be inside a callback.
            # A bound method, so Qt drops the timer if the window dies first.
            self._stale_stepper = self._stepper
            QtCore.QTimer.singleShot(0, self._stop_watching)
        self._update_actions()

    def _stop_watching(self) -> None:
        # A run started since the edit owns the callbacks now; leave them.
        if not self._running and self._stale_stepper is self._stepper:
            self._watcher.clear_report_callbacks()
        self._stale_stepper = None

    def _verify_scene(self) -> None:
        """Re-resolve watched paths before a report-backed action."""
        if not self._running and not self.is_stale and self._watcher.watching_report:
            if not self._watcher.verify():
                self._watcher.mark_stale()

    def event(self, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.Type.WindowActivate:
            self._verify_scene()
            self._update_intent_label()       # a save in Maya changes the decisions summary
        return super().event(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        # A hidden window can be reused later by show_window(). Treat that
        # unobserved gap as a stale report when it is shown again.
        if not self._closed and (self._running or self.report is not None):
            self._hidden_with_report = True
        super().hideEvent(event)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        if self._hidden_with_report and not self._closed:
            self._hidden_with_report = False
            if self._watcher.watching_report:
                self._watcher.mark_stale()
            else:
                self._on_scene_edit()
        super().showEvent(event)

    @property
    def is_stale(self) -> bool:
        return self.report is not None and self.report.state == ReportState.STALE

    def _show_state(self, report: ValidationReport | None) -> None:
        if report is None:
            self.state_label.setText("Not run")
            self.state_label.setStyleSheet("font-weight: bold;")
            self.partial_label.setVisible(False)
            self.cancel_button.setEnabled(False)
            self._update_actions()
            return
        text = report.state.value
        if report.state == ReportState.STALE:
            text = "Stale - the scene changed; run again"
        self.state_label.setText(text)
        self.state_label.setStyleSheet(f"color: {STATE_COLORS[report.state]}; font-weight: bold;")
        self.partial_label.setVisible(bool(report.partial))
        if report.errors:
            self._status(f"{len(report.errors)} check error(s); see the rows marked Error or Incomplete.", "error")
        self._update_actions()

    # -------------------------------------------------------- details/select
    def _clicked(self, index: QtCore.QModelIndex) -> None:
        if index.isValid() and index.column() == ACTION:
            self.select_index(index)

    def _record(self, index: QtCore.QModelIndex):
        if not index.isValid():
            return None, None, None
        source = self.proxy.mapToSource(index)
        first = source.siblingAtColumn(0)
        return (self.model.data(first, KIND_ROLE), self.model.data(first, ISSUE_ROLE),
                self.model.data(first, RESULT_ROLE))

    def _show_details(self, index: QtCore.QModelIndex) -> None:
        kind, issue, result = self._record(index)
        if kind is None:
            self.details.clear()
            return
        parts = []
        if kind == "issue" and issue is not None:
            parts.append(f"<b>{html.escape(issue_severity_text(issue))}</b> &nbsp; {html.escape(issue.message)}")
            parts.append(f"Object: {html.escape(issue.target_path)}")
            parts.append(f"UUID: {html.escape(issue.target_uuid)}")
            if issue.reference_identity:
                parts.append(f"Reference: {html.escape(issue.reference_identity)}")
            if issue.measured_value is not None:
                parts.append(f"Measured value: {issue.measured_value:.6g}")
            if issue.threshold is not None:
                parts.append(f"Threshold: {issue.threshold:.6g}")
            if issue.component_type is not None:
                shown = ", ".join(str(i) for i in issue.component_indices[:200])
                more = len(issue.component_indices) - 200
                parts.append(
                    f"{issue.component_type.value.capitalize()} components "
                    f"({len(issue.component_indices)}): {shown}{f' ... and {more} more' if more > 0 else ''}"
                )
            for key, value in issue.details:
                parts.append(f"{html.escape(key)}: {html.escape(value)}")
            if issue.check_id == "O":
                for note in self._intent_notes.get(issue.target_path, ()):
                    parts.append(f"<span style='color:{LEVEL_COLORS['warning']}'>{html.escape(note)}</span>")
        elif result is not None:
            label = result.metadata.label if result.metadata else result.check_id
            parts.append(f"<b>{html.escape(result.check_id)} {html.escape(label)}</b>: {result.verdict.value}")
            parts.append(f"Execution: {result.execution.value}; targets {result.processed_targets}/"
                         f"{result.total_targets} processed")
            for error in result.errors:
                parts.append(f"<span style='color:{LEVEL_COLORS['error']}'>{html.escape(error)}</span>")
        if result is not None:
            settings = self.check_settings(result.check_id)
            if settings:
                parts.append("Thresholds and settings: " + html.escape(", ".join(
                    f"{key} = {value:.6g}" if isinstance(value, float) else f"{key} = {value}"
                    for key, value in settings.items()
                )))
        if result is not None and result.metadata is not None:
            parts.append(f"<i>Hint: {html.escape(result.metadata.hint)}</i>")
        self.details.setHtml("<br>".join(parts))

    def check_settings(self, check_id: str) -> dict[str, object]:
        """The check's defaults with the report's overrides: its thresholds.

        T, U, O, E and A publish ``DEFAULT_SETTINGS`` in their modules; D, N,
        L, C and G have no thresholds. E and A share O's distance overrides.
        """
        rule = next((rule for rule in self._rules if rule.check_id == check_id), None)
        module = sys.modules.get(type(rule).__module__) if rule is not None else None
        values = dict(getattr(module, "DEFAULT_SETTINGS", None) or {})
        if self.report is not None:
            settings_id = "O" if check_id in ("E", "A") else check_id
            values.update(self.report.context.settings.get(settings_id, {}))
        return values

    def _current_changed(self, current: QtCore.QModelIndex, _previous: QtCore.QModelIndex) -> None:
        self._show_details(current)
        self._update_actions()
        if self.detail_tabs.currentIndex() == GROUPS_TAB and not self._defer_groups:
            self._load_groups()

    def _select_current(self, *_args) -> None:
        self.select_index(self.tree.currentIndex())

    def select_index(self, index: QtCore.QModelIndex) -> None:
        """Select the row's verified targets (a check row selects all its issues)."""
        self._verify_scene()
        kind, issue, result = self._record(index)
        if kind == "issue" and issue is not None:
            issues = [issue]
        elif kind == "check" and result is not None:
            issues = list(result.issues)
        else:
            return
        if not issues:
            return
        self._select(issues)

    def _select(self, issues) -> None:
        try:
            items, refused = select_issues(
                issues, stale=self.is_stale or self.report is None, handles=self._watcher.handles,
            )
        except SelectRefused as exc:
            self._status(str(exc), "warning")
            return
        if refused and not items:
            self._status("Nothing selected: " + refused[0], "warning")
        elif refused:
            self._status(f"Selected {len(items)} item(s); {len(refused)} target(s) refused: {refused[0]}", "warning")
        else:
            self._status(f"Selected {len(items)} item(s).", "other")

    def _status(self, text: str, level: str) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {LEVEL_COLORS.get(level, LEVEL_COLORS['other'])};")

    # ---------------------------------------------------------- action row
    def _current_issue(self):
        """(issue, result) of the current issue row, or (None, None)."""
        kind, issue, result = self._record(self.tree.currentIndex())
        if kind == "issue" and issue is not None:
            return issue, result
        return None, result

    def _report_refusal(self) -> str | None:
        """Why no fix or mark may run on the current report now, or None."""
        if self._running:
            return "Wait for the run to finish."
        if self.report is None or self._base_report is None:
            return "Run the checks first."
        if self.is_stale:
            return "The report is stale; run the checks again."
        return None

    def _update_actions(self) -> None:
        """Enable the action row for the current row; no Maya calls."""
        issue, result = self._current_issue()
        report_reason = self._report_refusal()

        self.select_button.setEnabled(not self._running)

        if issue is None or issue.check_id != "D":
            rename_reason = "Select a Duplicate names (D) issue row to rename."
        elif result is None or result.metadata is None or not result.metadata.can_fix:
            rename_reason = "This check has no rename fix."
        else:
            rename_reason = report_reason
        self.rename_button.setEnabled(rename_reason is None)
        self.rename_button.setToolTip(rename_reason or _TIPS["rename"])

        o_row = issue is not None and issue.check_id == "O"
        if not o_row:
            mark_reason = "Select an Overlapping vertices (O) issue row."
        elif report_reason is None and self.report.stop_reason is not None:
            mark_reason = "The run did not finish; run the checks again."
        else:
            mark_reason = report_reason
        for button, tip in ((self.intentional_button, _TIPS["intentional"]),
                            (self.error_button, _TIPS["error"]),
                            (self.clear_mark_button, _TIPS["clear"]),
                            (self.new_identity_button, _TIPS["identity"])):
            button.setEnabled(mark_reason is None)
            button.setToolTip(mark_reason or tip)

        if not o_row and self.detail_tabs.currentIndex() == GROUPS_TAB:
            self.detail_tabs.setCurrentIndex(DETAILS_TAB)
        self.detail_tabs.setTabEnabled(GROUPS_TAB, o_row)
        self.detail_tabs.setTabToolTip(
            GROUPS_TAB, "Every overlap group of this mesh" if o_row
            else "Select an Overlapping vertices (O) issue row",
        )
        if o_row:
            count = self._selected_group_count(issue)
            self.acts_on_label.setText(
                f"Acts on: {_plural(count, 'selected group')}" if count
                else "Acts on: every group in this row"
            )
        else:
            self.acts_on_label.setText("")
        self.acts_on_label.setVisible(o_row)
        self._update_intent_label()

    def _update_intent_label(self) -> None:
        try:
            session = intent_session.get_session()
            text = session.store.summary() if session is not None else ""
        except Exception:                 # the timer must never raise every second
            return
        if text != self.intent_label.text() or self.intent_label.isVisible() != bool(text):
            self.intent_label.setText(text)
            self.intent_label.setVisible(bool(text))

    @staticmethod
    def _group_notes(notes) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for target_path, text in notes:
            grouped.setdefault(target_path, []).append(text)
        return grouped

    def run_dialog(self, dialog: QtWidgets.QDialog) -> bool:
        """The only place a modal dialog runs; headless tests patch this method."""
        return exec_dialog(dialog) == int(QtWidgets.QDialog.DialogCode.Accepted)

    # ---------------------------------------------------------- rename
    def rename_current(self, *_args) -> None:
        """Preview and rename the current D issue's clash (one undo chunk), then re-run."""
        self._verify_scene()
        issue, result = self._current_issue()
        if issue is None or issue.check_id != "D":
            self._status("Select a Duplicate names (D) issue row to rename.", "warning")
            return
        if result is None or result.metadata is None or not result.metadata.can_fix:
            self._status("This check has no rename fix.", "warning")
            return
        refusal = self._report_refusal()
        if refusal is not None:
            self._status(refusal, "warning")
            return
        # Copy before any edit: the deferred _stop_watching clears the live dict.
        handles = dict(self._watcher.handles)
        context = self.report.context
        try:
            plan = plan_rename(issue, context, handles=handles)
        except RenameRefused as exc:
            self._status(str(exc), "warning")
            return
        except Exception as exc:
            self._status(f"The rename could not be planned: {type(exc).__name__}: {exc}", "error")
            return
        # A partial of the module-level planner: it never captures the window.
        replan = functools.partial(plan_rename, issue, context, handles=handles, snapshot=plan.snapshot)
        dialog = RenameDialog(plan, replan, self)
        try:
            if not self.run_dialog(dialog):
                self._status("Rename cancelled; nothing changed.", "other")
                return
            self._verify_scene()
            if self.is_stale or self._running:
                self._status("The scene changed while the dialog was open; run the checks again.", "warning")
                return
            chosen_plan, chosen = dialog.plan(), dialog.choice()
            try:
                outcome = apply_rename(chosen_plan, chosen)
            except RenameRefused as exc:
                self._status(str(exc), "warning")
                return
            except Exception as exc:
                self._unexpected_fix_error("rename", exc)
                return
        finally:
            dialog.deleteLater()

        if outcome.ok:
            members = [member for member in chosen_plan.members if member.index in chosen]
            names = ", ".join(
                f"{member.canonical_path} \u2192 {member.ops[0].target[1:]}" for member in members
            )
            shapes = sum(len(member.ops) - 1 for member in members)
            extra = f" (+{_plural(shapes, 'shape')})" if shapes else ""
            if outcome.undoable:
                text, level = f"Renamed {names}{extra}. Ctrl+Z restores them.", "pass"
            else:
                text, level = (f"Renamed {names}{extra}. Maya did not record it for undo; "
                               "rename back by hand if needed."), "warning"
        elif outcome.rolled_back and outcome.restored:
            text, level = f"{outcome.error.rstrip('.')}; the rename was undone.", "warning"
        elif outcome.restored is None:
            self._status(f"The rename failed and nothing changed: {outcome.error}", "error")
            return
        else:
            text = outcome.error
            if not text.rstrip().endswith("use Undo."):
                text += " Not every name was restored; use Undo."
            level = "error"
        self._refresh_after_fix("D", chosen_plan.short_name, text, level)

    def _unexpected_fix_error(self, what: str, exc: BaseException) -> None:
        # The fix modules refuse before their chunk and catch errors inside it,
        # so this is a bug; any edit it made has already marked the report Stale.
        tail = " Run the checks again." if self.is_stale else ""
        self._status(f"The {what} failed: {type(exc).__name__}: {exc}.{tail}", "error")

    def _refresh_after_fix(self, check_id: str, short_name: str | None, text: str, level: str) -> None:
        """Re-run the last check set on the last run's scope; _finish appends the new verdict.

        The scope comes from the run the fix was made from, never from the
        Scope combo or Maya's selection now: Select changes the selection, and
        a re-run over other meshes would hide their issues behind a Pass.
        """
        self._after_fix = (check_id, short_name, text, level)
        rerun_scope = None
        if self._last_scope is not None:
            mode, dag_paths = self._last_scope
            rerun_scope = (mode, [dag.fullPathName() for dag in dag_paths if dag.isValid()])
        self.run_checks(self._last_check_ids or [rule.check_id for rule in self._rules],
                        rerun_scope=rerun_scope)
        if self._running:
            self._status(f"{text} Re-running checks\u2026", level)

    # ---------------------------------------------------- overlap decisions
    def _mark_intentional_clicked(self, *_args) -> None:
        self.mark_current("intentional")

    def _mark_error_clicked(self, *_args) -> None:
        self.mark_current("error")

    def mark_current(self, intent: str) -> None:
        """Mark the current O row's groups (or the selected groups) Intentional or Error."""
        self._classify(intent)

    def clear_current_mark(self, *_args) -> None:
        """Remove the marks of the current O row's groups (or the selected groups)."""
        self._classify(None)

    def _o_row_refusal(self, issue) -> str | None:
        if issue is None or issue.check_id != "O":
            return "Select an Overlapping vertices (O) issue row."
        refusal = self._report_refusal()
        if refusal is None and self.report.stop_reason is not None:
            refusal = "The run did not finish; run the checks again."
        return refusal

    def _classify(self, intent: str | None) -> None:
        self._verify_scene()
        issue, _result = self._current_issue()
        refusal = self._o_row_refusal(issue)
        if refusal is not None:
            self._status(refusal, "warning")
            return
        groups = self._selected_groups(issue)
        session = intent_session.get_session(create=True)
        try:
            outcome = classify(self._base_report, issue, session.store, intent, groups=groups,
                               handles=self._watcher.handles, cache=self._measure_cache)
        except ClassifyRefused as exc:
            notes = session.store.take_notes()
            self._status(" ".join([str(exc), *notes]), "warning")
            self._update_actions()
            return
        except Exception as exc:
            self._status(f"The decision could not be recorded: {type(exc).__name__}: {exc}", "error")
            return
        tab = self.detail_tabs.currentIndex()
        # The groups tab is reloaded once below, with the same groups selected;
        # the row changes on the way must not load it (each load re-measures).
        self._defer_groups = True
        try:
            problem = self._reannotate(session.store)
            self._reselect(outcome.target_path, intent)
        finally:
            self._defer_groups = False
        if tab == GROUPS_TAB and self.detail_tabs.isTabEnabled(GROUPS_TAB):
            # Blocked: currentChanged would load the tab once more without keep.
            blocked = self.detail_tabs.blockSignals(True)
            try:
                self.detail_tabs.setCurrentIndex(GROUPS_TAB)
            finally:
                self.detail_tabs.blockSignals(blocked)
            self._load_groups(keep=groups)          # the same groups stay selected
        notes = session.store.take_notes()
        parts = [outcome.message, *([problem] if problem else []), *notes]
        self._status(" ".join(parts), "warning" if problem or notes else "other")
        self._update_actions()

    def _reannotate(self, store) -> str | None:
        """Apply the decisions to the last run again, in place; no re-run, no Stale."""
        problem = None
        try:
            result = apply_intent(self._base_report, store, handles=self._watcher.handles,
                                  cache=self._measure_cache)
            shown = result.report
            self._intent_notes = self._group_notes(result.notes)
        except Exception as exc:
            shown = self._base_report
            self._intent_notes = {}
            problem = (f"Overlap decisions could not be checked ({type(exc).__name__}: {exc}); "
                       "none were applied.")
        self._stepper.report = shown
        self._apply_report(shown)
        return problem

    def _reselect(self, target_path: str, intent: str | None) -> None:
        """Make the first O row of ``target_path`` current, preferring ``intent``'s class."""
        wanted = intent or "unclassified"
        candidates = []
        for row in range(self.proxy.rowCount()):
            parent = self.proxy.index(row, 0)
            result = self.proxy.data(parent, RESULT_ROLE)
            if result is None or result.check_id != "O":
                continue
            for child in range(self.proxy.rowCount(parent)):
                index = self.proxy.index(child, 0, parent)
                issue = self.proxy.data(index, ISSUE_ROLE)
                if issue is not None and issue.target_path == target_path:
                    candidates.append((dict(issue.details).get("intent", "unclassified") != wanted, child, index))
        if not candidates:
            return
        self.tree.setCurrentIndex(min(candidates, key=lambda item: (item[0], item[1]))[2])

    def new_identity_current(self, *_args) -> None:
        """Give the current O row's imported copy a new UUID (confirmed, one undo), then re-run."""
        self._verify_scene()
        issue, _result = self._current_issue()
        refusal = self._o_row_refusal(issue)
        if refusal is not None:
            self._status(refusal, "warning")
            return
        handles = dict(self._watcher.handles)
        try:
            plan = plan_new_identity(issue, handles=handles)
        except IdentityRefused as exc:
            self._status(str(exc), "warning")
            return
        except Exception as exc:
            self._status(f"New Identity could not be planned: {type(exc).__name__}: {exc}", "error")
            return
        dialog = ConfirmDialog(
            "New identity",
            "Give this copy a new Maya UUID? Names and geometry do not change; one Undo restores it.",
            "New Identity", self,
        )
        try:
            if not self.run_dialog(dialog):
                self._status("New Identity cancelled; nothing changed.", "other")
                return
        finally:
            dialog.deleteLater()
        self._verify_scene()
        if self.is_stale or self._running:
            self._status("The scene changed while the dialog was open; run the checks again.", "warning")
            return
        session = intent_session.get_session(create=True)
        try:
            outcome = apply_new_identity(plan, session.store)
        except IdentityRefused as exc:
            self._status(str(exc), "warning")
            return
        except Exception as exc:
            # A UUID rename fires no callback; never leave a fresh report on doubt.
            self._watcher.mark_stale()
            self._unexpected_fix_error("New Identity", exc)
            return
        leaf = plan.shape_path.rsplit("|", 1)[-1]
        if outcome.ok:
            others = len(plan.holder_paths) - 1
            kept = "1 other copy keeps" if others == 1 else f"{others} other copies keep"
            text = (f"Gave {leaf} a new identity (UUID {outcome.new_uuid}); {kept} the old one. "
                    "Ctrl+Z restores it.")
            level = "pass"
        elif outcome.rolled_back and outcome.restored:
            text, level = f"{outcome.error.rstrip('.')}; the new identity was undone.", "warning"
        elif outcome.restored is None:
            self._status(f"New Identity failed and nothing changed: {outcome.error}", "error")
            return
        elif outcome.error.startswith(f"{plan.shape_path} now has UUID"):
            # Applied and verified; only the undo step is missing.
            text, level = outcome.error, "warning"
        else:
            text, level = outcome.error, "error"
        # A UUID rename fires no callback: mark the report stale ourselves.
        self._watcher.mark_stale()
        self._refresh_after_fix("O", None, text, level)

    # ------------------------------------------------------- groups tab
    def _tab_changed(self, index: int) -> None:
        if index == GROUPS_TAB:
            self._load_groups()
        self._update_actions()

    def _reset_groups(self, note: str) -> None:
        self.groups_model.set_rows(())
        self.groups_note.setText(note)
        self._groups_target = None
        self._groups_key = None

    def _load_groups(self, keep=None) -> None:
        """Fill the Overlap groups tab for the current O row (only while it is shown)."""
        issue, _result = self._current_issue()
        if issue is None or issue.check_id != "O":
            self._reset_groups("")
            return
        key = (self._report_serial, issue.target_path)
        if keep is None and key == self._groups_key:
            return
        refusal = self._report_refusal()
        if refusal is not None:
            self._reset_groups(refusal)
            return
        session = intent_session.get_session()
        store = session.store if session is not None else IntentStore(persist=False)
        try:
            view = describe_groups(self._base_report, issue, store, handles=self._watcher.handles,
                                   cache=self._measure_cache)
        except Exception as exc:
            view = GroupsView(issue.target_path, (), f"The groups could not be read: {type(exc).__name__}: {exc}")
        self.groups_model.set_rows(view.rows)
        self.groups_view.resizeColumnsToContents()
        self._groups_target = issue.target_path
        self._groups_key = key
        leaf = issue.target_path.rsplit("|", 1)[-1]
        summary = (f"{_plural(len(view.rows), 'overlap group')} on {leaf}. Select groups to act on "
                   "only those; double-click a group to select its vertices.") if view.rows else ""
        self.groups_note.setText(" ".join(part for part in (view.note, summary) if part))
        if keep:
            self._select_group_rows(self.groups_model.rows_of(keep))
        self._update_actions()

    def _select_group_rows(self, rows) -> None:
        selection = QtCore.QItemSelection()
        last_column = self.groups_model.columnCount() - 1
        start = previous = None
        for row in list(rows) + [None]:
            if row is not None and previous is not None and row == previous + 1:
                previous = row
                continue
            if start is not None:
                selection.select(self.groups_model.index(start, 0),
                                 self.groups_model.index(previous, last_column))
            start = previous = row
        self.groups_view.selectionModel().select(
            selection, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect,
        )

    def _groups_apply_to(self, issue) -> bool:
        """True when the tab is shown and was loaded for this row's mesh."""
        return (issue is not None and self.detail_tabs.currentIndex() == GROUPS_TAB
                and self._groups_target == issue.target_path)

    def _selected_group_rows(self) -> list[int]:
        """Selected rows from the selection ranges: selectedRows() costs Python
        model calls per row, and ranges^2 for scattered ctrl-clicks."""
        rows: set[int] = set()
        for selected in self.groups_view.selectionModel().selection():
            rows.update(range(selected.top(), selected.bottom() + 1))
        return sorted(rows)

    def _selected_groups(self, issue) -> list[tuple[int, ...]] | None:
        """The groups selected in the tab for this row's mesh, or None for the whole row."""
        if not self._groups_apply_to(issue):
            return None
        return [self.groups_model.group(row) for row in self._selected_group_rows()] or None

    def _selected_group_count(self, issue) -> int:
        """How many groups are selected (cheap for 10^6 rows)."""
        if not self._groups_apply_to(issue):
            return 0
        return len(self._selected_group_rows())

    def _groups_selection_changed(self, *_args) -> None:
        self._update_actions()

    def _raw_o_issue(self, target_path: str | None):
        if self._base_report is None or target_path is None:
            return None
        for result in self._base_report.results:
            if result.check_id == "O":
                for issue in result.issues:
                    if issue.target_path == target_path:
                        return issue
        return None

    def _group_double_clicked(self, index: QtCore.QModelIndex) -> None:
        """Select exactly one group's vertices (the verified, filtered Select chunk)."""
        if not index.isValid():
            return
        self._verify_scene()
        raw = self._raw_o_issue(self._groups_target)
        if raw is None:
            self._status("Run the checks again to select this group.", "warning")
            return
        group = self.groups_model.group(index.row())
        self._select([replace(raw, component_indices=group)])

    # ----------------------------------------------------------------- close
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        global _WINDOW
        self._closed = True
        self._label_timer.stop()
        if self._running and self._control is not None:
            self._control.cancel()
        self._running = False
        self._watcher.close()
        self._save_settings()
        # Drop the run and the report now; the widget is only deleted later.
        # The overlap-decision session is process-wide and is left alone.
        self._stepper = self._control = self._stale_stepper = None
        self.report = None
        self._results = {}
        self._reset_fix_state()
        self._after_fix = None
        self.model.clear_report()
        if _WINDOW is self:
            _WINDOW = None
        super().closeEvent(event)


def show_window() -> ValidatorWindow:
    """Open the window, or bring the open one (even hidden or minimized) to the front."""

    global _WINDOW
    window = _WINDOW
    if is_alive(window) and not window._closed:
        window.setWindowState(window.windowState() & ~QtCore.Qt.WindowState.WindowMinimized)
        window.show()
        window.raise_()
        window.activateWindow()
        return window
    # A window left from an earlier module load, shown or hidden: close it.
    # Closed ones linger until the event loop deletes them; skip those.
    for widget in QtWidgets.QApplication.topLevelWidgets():
        if (widget.objectName() == OBJECT_NAME and is_alive(widget)
                and not getattr(widget, "_closed", False)):
            widget.close()
    window = ValidatorWindow(maya_main_window())
    _WINDOW = window
    window.show()
    return window


__all__ = ["OBJECT_NAME", "ValidatorWindow", "show_window"]
