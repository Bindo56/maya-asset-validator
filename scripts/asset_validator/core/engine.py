"""One validation pipeline for headless runs and the QTimer-driven UI."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
import traceback
from typing import Any, Literal, Protocol

from asset_validator.data_models import (
    CheckResult,
    CheckMetadata,
    ExecutionStatus,
    Issue,
    ReportState,
    RunStopReason,
    SettingValue,
    ScopeMode,
    TargetRef,
    ValidationContext,
    ValidationReport,
    derive_state,
)
from asset_validator.maya_adapter import resolve_scope


REQUIRED_CHECK_IDS = frozenset(("D", "T", "O", "L", "N", "U"))


class ValidationRule(Protocol):
    check_id: str
    metadata: CheckMetadata

    def prepare(self, context: ValidationContext) -> Any: ...

    def run_target(
        self, context: ValidationContext, target: TargetRef, prepared: Any
    ) -> Iterable[Issue]: ...


class ValidationIncomplete(Exception):
    """A rule stopped early (for example, at a time budget)."""


class TargetIncomplete(ValidationIncomplete):
    """One target could not be finished; the rule continues with the next.

    Raise this from ``run_target`` when a single mesh cannot be read. The rule
    ends Incomplete, but later targets are still checked. A plain
    ``ValidationIncomplete`` still stops the rule after the current target.
    """


class ValidationControl:
    """A UI callback can request an exit between generator steps.

    Scene edits outrank a user cancellation: findings from an edited scene may
    still be inspected, but they must never be presented as a current report.
    """

    def __init__(self) -> None:
        self._cancelled = False
        self._stale = False

    def cancel(self) -> None:
        self._cancelled = True

    def mark_stale(self) -> None:
        self._stale = True

    @property
    def stop_reason(self) -> RunStopReason | None:
        if self._stale:
            return RunStopReason.STALE
        if self._cancelled:
            return RunStopReason.CANCELLED
        return None


@dataclass(frozen=True)
class ValidationEvent:
    kind: Literal["started", "target", "check", "finished"]
    check_id: str | None = None
    target_path: str | None = None
    issue_count: int = 0
    result: CheckResult | None = None
    report: ValidationReport | None = None
    total_targets: int = 0
    total_checks: int = 0
    total_work_units: int = 0
    completed_work_units: int = 0


def _default_rules() -> tuple[ValidationRule, ...]:
    # Explicit order makes a run reproducible as further checks are added.
    from asset_validator.checks import (
        DuplicateNamesRule, FreezeTransformsRule, NonManifoldRule, LaminaRule,
        OverlappingVerticesRule, UVAreaRule, NamingConventionRule,
        ZeroLengthEdgesRule, ZeroAreaFacesRule, NgonsRule,
    )

    return (
        DuplicateNamesRule(), FreezeTransformsRule(), NonManifoldRule(),
        LaminaRule(), UVAreaRule(), OverlappingVerticesRule(),
        NamingConventionRule(), ZeroLengthEdgesRule(), ZeroAreaFacesRule(),
        NgonsRule(),
    )


def default_rules() -> tuple[ValidationRule, ...]:
    """Fresh instances of all installed checks, in run order (for the UI)."""

    return _default_rules()


def _rules(rules: Sequence[ValidationRule] | None) -> tuple[ValidationRule, ...]:
    chosen = tuple(_default_rules() if rules is None else rules)
    ids = [rule.check_id for rule in chosen]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate check IDs in one run: {ids!r}")
    for rule in chosen:
        if not isinstance(getattr(rule, "metadata", None), CheckMetadata):
            raise TypeError(f"{rule.check_id}: rule must provide CheckMetadata")
    return chosen


def _error_text(error: Exception, *, check_id: str, target_path: str | None) -> str:
    location = f" on {target_path}" if target_path else " during preparation"
    return f"{check_id}{location}: {type(error).__name__}: {error}"


def report_state(
    results: Sequence[CheckResult],
    *,
    partial: bool,
    stop_reason: RunStopReason | None,
) -> ReportState:
    """The whole-run verdict from finished check results.

    Shared with the intent step, which re-derives the verdict after
    re-classifying O issues and must fold results exactly like a run does.
    """

    all_issues = tuple(issue for result in results for issue in result.issues)
    if any(result.execution == ExecutionStatus.ERROR for result in results):
        execution = ExecutionStatus.ERROR
    elif any(result.execution == ExecutionStatus.INCOMPLETE for result in results):
        execution = ExecutionStatus.INCOMPLETE
    else:
        execution = ExecutionStatus.COMPLETE
    if stop_reason == RunStopReason.STALE:
        return ReportState.STALE
    if stop_reason == RunStopReason.CANCELLED:
        return ReportState.INCOMPLETE
    return derive_state(all_issues, execution=execution, partial=partial)


def iter_validation(
    context: ValidationContext | None = None,
    *,
    scope: ScopeMode | str = ScopeMode.SELECTION,
    rules: Sequence[ValidationRule] | None = None,
    partial: bool = False,
    settings: Mapping[str, Mapping[str, SettingValue]] | None = None,
    control: ValidationControl | None = None,
) -> Iterator[ValidationEvent]:
    """Yield bounded steps; the final event carries the completed report.

    A caller in Maya's GUI can advance this iterator once per QTimer tick.
    ``validate()`` drains the exact same iterator without a Qt event loop.
    """
    chosen = _rules(rules)
    partial = partial or not REQUIRED_CHECK_IDS.issubset(
        rule.check_id for rule in chosen
    )
    context = context if context is not None else resolve_scope(scope)
    if settings is not None:
        context = replace(context, settings=settings)
    control = control if control is not None else ValidationControl()
    total_targets = len(context.targets)
    total_checks = len(chosen)
    total_work_units = total_targets * total_checks
    completed_work_units = 0
    yield ValidationEvent(
        kind="started", total_targets=total_targets,
        total_checks=total_checks, total_work_units=total_work_units,
    )

    # A cancellation after the started event must not prepare even one rule.
    if control.stop_reason is not None:
        reason = control.stop_reason
        report = ValidationReport(
            context=context, results=(),
            state=ReportState.STALE if reason == RunStopReason.STALE else ReportState.INCOMPLETE,
            partial=partial, stop_reason=reason,
        )
        yield ValidationEvent(
            kind="finished", report=report,
            total_targets=total_targets, total_checks=total_checks,
            total_work_units=total_work_units,
        )
        return

    if not context.targets:
        report = ValidationReport(
            context=context, results=(), state=ReportState.NO_TARGETS,
            partial=partial,
        )
        yield ValidationEvent(
            kind="finished", report=report,
            total_targets=total_targets, total_checks=total_checks,
            total_work_units=total_work_units,
        )
        return

    if not chosen:
        report = ValidationReport(
            context=context, results=(), state=ReportState.INCOMPLETE,
            partial=True, errors=("No validation checks were selected.",),
        )
        yield ValidationEvent(
            kind="finished", report=report,
            total_targets=total_targets, total_checks=total_checks,
            total_work_units=total_work_units,
        )
        return

    results: list[CheckResult] = []
    for rule in chosen:
        if control.stop_reason is not None:
            break
        issues: list[Issue] = []
        errors: list[str] = []
        error_tracebacks: list[str] = []
        execution = ExecutionStatus.COMPLETE
        processed = 0
        attempted = 0
        try:
            prepared = rule.prepare(context)
        except ValidationIncomplete as exc:
            execution = ExecutionStatus.INCOMPLETE
            errors.append(_error_text(exc, check_id=rule.check_id, target_path=None))
            error_tracebacks.append(traceback.format_exc())
        except Exception as exc:
            execution = ExecutionStatus.ERROR
            errors.append(_error_text(exc, check_id=rule.check_id, target_path=None))
            error_tracebacks.append(traceback.format_exc())
        else:
            for target in context.targets:
                if control.stop_reason is not None:
                    break
                attempted += 1
                stop_after_target = False
                try:
                    found = rule.run_target(context, target, prepared)
                    if found is not None:
                        for issue in found:
                            if not isinstance(issue, Issue):
                                raise TypeError(
                                    f"{rule.check_id} yielded {type(issue).__name__}, not Issue"
                                )
                            issues.append(issue)
                except TargetIncomplete as exc:
                    # Only this target is unfinished; keep checking the rest.
                    if execution != ExecutionStatus.ERROR:
                        execution = ExecutionStatus.INCOMPLETE
                    errors.append(
                        _error_text(exc, check_id=rule.check_id,
                                    target_path=target.shape_path)
                    )
                    error_tracebacks.append(traceback.format_exc())
                except ValidationIncomplete as exc:
                    if execution != ExecutionStatus.ERROR:
                        execution = ExecutionStatus.INCOMPLETE
                    errors.append(
                        _error_text(exc, check_id=rule.check_id,
                                    target_path=target.shape_path)
                    )
                    error_tracebacks.append(traceback.format_exc())
                    stop_after_target = True
                except Exception as exc:
                    execution = ExecutionStatus.ERROR
                    errors.append(
                        _error_text(exc, check_id=rule.check_id,
                                    target_path=target.shape_path)
                    )
                    error_tracebacks.append(traceback.format_exc())
                else:
                    # A generator that yielded a finding and then raised did
                    # not finish this target, even though its findings remain.
                    processed += 1
                completed_work_units += 1
                yield ValidationEvent(
                    kind="target", check_id=rule.check_id,
                    target_path=target.shape_path, issue_count=len(issues),
                    total_targets=total_targets, total_checks=total_checks,
                    total_work_units=total_work_units,
                    completed_work_units=completed_work_units,
                )
                if stop_after_target:
                    break

        if control.stop_reason is not None and execution == ExecutionStatus.COMPLETE:
            execution = ExecutionStatus.INCOMPLETE
        result = CheckResult(
            check_id=rule.check_id,
            execution=execution,
            verdict=derive_state(issues, execution=execution),
            issues=tuple(issues),
            errors=tuple(errors),
            processed_targets=processed,
            total_targets=total_targets,
            metadata=rule.metadata,
            attempted_targets=attempted,
            error_tracebacks=tuple(error_tracebacks),
        )
        results.append(result)
        yield ValidationEvent(
            kind="check", check_id=rule.check_id,
            issue_count=len(issues), result=result,
            total_targets=total_targets, total_checks=total_checks,
            total_work_units=total_work_units,
            completed_work_units=completed_work_units,
        )

    reason = control.stop_reason
    state = report_state(results, partial=partial, stop_reason=reason)
    report = ValidationReport(
        context=context,
        results=tuple(results),
        state=state,
        partial=partial,
        errors=tuple(error for result in results for error in result.errors),
        stop_reason=reason,
        error_tracebacks=tuple(
            diagnostic for result in results
            for diagnostic in result.error_tracebacks
        ),
    )
    yield ValidationEvent(
        kind="finished", report=report,
        total_targets=total_targets, total_checks=total_checks,
        total_work_units=total_work_units,
        completed_work_units=completed_work_units,
    )


class ValidationStepper:
    """Advance one event at a time; suitable for a QTimer callback."""

    def __init__(
        self, iterator: Iterator[ValidationEvent],
        *, control: ValidationControl | None = None,
    ):
        self._iterator = iterator
        self._control = control
        self.report: ValidationReport | None = None
        self.done = False

    def cancel(self) -> None:
        """Request an Incomplete exit before the next generator step."""
        if self._control is None:
            raise RuntimeError("Pass the same ValidationControl to iterator and stepper")
        if not self.done:
            self._control.cancel()

    def mark_stale(self) -> None:
        """Invalidate an active or completed report after a watched edit."""
        if self._control is not None:
            self._control.mark_stale()
        if self.report is not None:
            self.report = replace(
                self.report, state=ReportState.STALE,
                stop_reason=RunStopReason.STALE,
            )
            return
        if self._control is None:
            raise RuntimeError("Pass the same ValidationControl to iterator and stepper")

    def advance(self) -> ValidationEvent | None:
        if self.done:
            return None
        try:
            event = next(self._iterator)
        except StopIteration:
            self.done = True
            return None
        if event.kind == "finished":
            self.report = event.report
            self.done = True
        return event


def validate(
    context: ValidationContext | None = None,
    *,
    scope: ScopeMode | str = ScopeMode.SELECTION,
    rules: Sequence[ValidationRule] | None = None,
    partial: bool = False,
    settings: Mapping[str, Mapping[str, SettingValue]] | None = None,
    control: ValidationControl | None = None,
) -> ValidationReport:
    """Run synchronously in Maya or mayapy by draining ``iter_validation``."""
    driver = ValidationStepper(
        iter_validation(
            context, scope=scope, rules=rules, partial=partial,
            settings=settings, control=control,
        ),
        control=control,
    )
    while not driver.done:
        driver.advance()
    assert driver.report is not None
    return driver.report
