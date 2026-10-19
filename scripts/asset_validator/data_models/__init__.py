"""Pure-Python records shared by Maya collection, checks, and presentation.

The records hold values captured during one validation run. In particular,
``TargetRef`` stores Maya identities and paths as strings rather than live API
objects, so a report can be inspected after the scene changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping


SettingValue = str | int | float | bool | None


class ScopeMode(str, Enum):
    SELECTION = "Selection"
    HIERARCHY = "Hierarchy"
    SCENE = "Scene"


class Severity(str, Enum):
    ERROR = "Error"
    WARNING = "Warning"


class ComponentType(str, Enum):
    VERTEX = "vertex"
    EDGE = "edge"
    FACE = "face"
    UV = "uv"


class ExecutionStatus(str, Enum):
    COMPLETE = "Complete"
    ERROR = "Error"
    INCOMPLETE = "Incomplete"


class ReportState(str, Enum):
    STALE = "Stale"
    ERROR = "Error"
    INCOMPLETE = "Incomplete"
    FAILED = "Failed"
    NEEDS_REVIEW = "Needs Review"
    PASS_WITH_WARNINGS = "Pass with warnings"
    PASS = "Pass"
    NO_TARGETS = "NoTargets"


class RunStopReason(str, Enum):
    CANCELLED = "Cancelled"
    STALE = "Stale"


@dataclass(frozen=True)
class CheckMetadata:
    label: str
    hint: str
    can_fix: bool


@dataclass(frozen=True)
class TargetRef:
    shape_path: str
    shape_uuid: str
    transform_path: str
    transform_uuid: str
    reference_identity: str | None = None


@dataclass(frozen=True)
class Issue:
    check_id: str
    severity: Severity
    message: str
    target_path: str
    target_uuid: str
    related_paths: tuple[str, ...] = ()
    needs_review: bool = False
    details: tuple[tuple[str, str], ...] = ()
    reference_identity: str | None = None
    component_type: ComponentType | None = None
    component_indices: tuple[int, ...] = ()
    measured_value: float | None = None
    threshold: float | None = None

    def __post_init__(self) -> None:
        indices = tuple(sorted(set(self.component_indices)))
        if indices and self.component_type is None:
            raise ValueError("component_indices require a component_type")
        object.__setattr__(self, "component_indices", indices)


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    execution: ExecutionStatus
    verdict: ReportState
    issues: tuple[Issue, ...]
    errors: tuple[str, ...] = ()
    processed_targets: int = 0
    total_targets: int = 0
    metadata: CheckMetadata | None = None
    attempted_targets: int = 0
    error_tracebacks: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationContext:
    mode: ScopeMode
    targets: tuple[TargetRef, ...]
    selection: tuple[str, ...] = ()
    scene_path: str | None = None
    settings: Mapping[str, Mapping[str, SettingValue]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Keep settings stable for the lifetime of one validation run."""

        snapshot = {}
        for check_id, values in self.settings.items():
            if not isinstance(check_id, str) or not isinstance(values, Mapping):
                raise TypeError("settings must map check IDs to setting mappings")
            scalar_values = {}
            for key, value in values.items():
                if not isinstance(key, str) or not (
                    value is None or isinstance(value, (str, int, float, bool))
                ):
                    raise TypeError(
                        f"{check_id}: settings need string keys and scalar values"
                    )
                scalar_values[key] = value
            snapshot[check_id] = MappingProxyType(scalar_values)
        object.__setattr__(self, "settings", MappingProxyType(snapshot))


@dataclass(frozen=True)
class ValidationReport:
    context: ValidationContext
    results: tuple[CheckResult, ...]
    state: ReportState
    partial: bool = False
    errors: tuple[str, ...] = ()
    stop_reason: RunStopReason | None = None
    error_tracebacks: tuple[str, ...] = ()


def derive_state(
    issues: Iterable[Issue],
    execution: ExecutionStatus = ExecutionStatus.COMPLETE,
    partial: bool = False,
    no_targets: bool = False,
    stale: bool = False,
) -> ReportState:
    """Apply the report verdict precedence to a rule or whole run.

    ``partial`` describes a run that omitted required checks. An execution
    failure outranks it. A stale report outranks every other state.
    """

    if stale:
        return ReportState.STALE
    if no_targets:
        return ReportState.NO_TARGETS
    if execution == ExecutionStatus.ERROR:
        return ReportState.ERROR
    if execution == ExecutionStatus.INCOMPLETE or partial:
        return ReportState.INCOMPLETE

    findings = tuple(issues)
    if any(issue.severity == Severity.ERROR for issue in findings):
        return ReportState.FAILED
    if any(issue.needs_review for issue in findings):
        return ReportState.NEEDS_REVIEW
    if any(issue.severity == Severity.WARNING for issue in findings):
        return ReportState.PASS_WITH_WARNINGS
    return ReportState.PASS


__all__ = [
    "ScopeMode",
    "SettingValue",
    "Severity",
    "ComponentType",
    "ExecutionStatus",
    "ReportState",
    "RunStopReason",
    "CheckMetadata",
    "TargetRef",
    "Issue",
    "CheckResult",
    "ValidationContext",
    "ValidationReport",
    "derive_state",
]
