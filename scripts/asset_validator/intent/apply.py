"""Apply stored overlap decisions to a finished report, and record new ones.

Validation stays read-only: ``validate()`` never calls this module. The window
(or a batch caller) runs ``apply_intent`` as an explicit post-step, and when
nothing applies it gets back the very same report object.

A decision is applied only to the verified issue node, and only when that node
is the key's single holder, no other reference node shares a level of its
chain, and the geometry, settings, rule version and group set all still match.
A changed hash drops the decision, except on an animated or deformed mesh,
whose points follow the frame and pose: there it is kept unapplied until they
match again. Each O issue with decisions is split into up to three
issues: groups marked Error (Failed), groups not classified yet (Needs
Review) and groups accepted as Intentional (a warning shown as "Accepted").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import os
import re
from typing import Literal

import maya.api.OpenMaya as om
import maya.cmds as cmds

from asset_validator.checks import overlapping_vertices
from asset_validator.checks.overlapping_vertices import (
    RULE_VERSION,
    Measurement,
    groups_sha256,
    measure,
    overlap_groups,
    read_settings,
)
from asset_validator.core import report_state, validate
from asset_validator.data_models import (
    Issue,
    ReportState,
    Severity,
    ValidationContext,
    ValidationReport,
    derive_state,
)
from asset_validator.intent.identity import (
    absolute_dag_path,
    holders,
    identity_key,
    is_maya_ascii,
    reference_files,
    reference_tops,
    reference_twins,
)
from asset_validator.intent.store import Intent, IntentStore, MeshDecisions, sidecar_path
from asset_validator.ui.scene import SelectRefused, resolve_issue


# Rows of the groups detail entry; component_indices always lists every vertex.
MAX_LISTED_GROUPS = 1000


@dataclass(frozen=True)
class IntentResult:
    report: ValidationReport
    notes: tuple[tuple[str, str], ...]          # (target_path, text)


def cached_measure(node: om.MObject, settings: tuple[float, float],
                   cache: dict | None) -> Measurement | None:
    """O's measurement plus fingerprint, cached while the report stays fresh.

    The key holds the canonical path and UUID, so a renamed or re-identified
    mesh is measured again; the window clears the cache on any scene edit.
    """

    dag_path = om.MDagPath.getAPathTo(node)
    path = dag_path.fullPathName()
    key = (path, om.MFnDependencyNode(node).uuid().asString(), settings[0], settings[1])
    if cache is not None and key in cache:
        return cache[key]
    measured = measure(dag_path, path, settings[0], settings[1], fingerprint=True)
    if cache is not None:
        cache[key] = measured
    return measured


def _resolve_node(issue: Issue, handles: Mapping[str, om.MObjectHandle] | None) -> om.MObject:
    """The verified node behind an issue (as Select verifies it), or SelectRefused."""

    resolve_issue(issue, handles)
    if handles is not None:
        return handles[issue.target_path].object()
    selection = om.MSelectionList()
    selection.add(issue.target_path)
    return selection.getDependNode(0)


def _involved(close: Sequence[tuple[int, int, float]]) -> tuple[int, ...]:
    return tuple(sorted({vertex for i, j, _ in close for vertex in (i, j)}))


def _matches_issue(measured: Measurement | None, issue: Issue) -> bool:
    if measured is None:
        return False
    try:
        pair_count = int(dict(issue.details)["pair_count"])
    except (KeyError, ValueError):
        return False
    return (_involved(measured.close) == issue.component_indices
            and len(measured.close) == pair_count)


def _plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else (many or word + 's')}"


_EDITED = "mesh edited since the decision"
_MOVED = f"{_EDITED}. Only the vertex positions changed"
# Exact hash, no tolerance: when only the positions differ, say what else
# changes them. A .ma stores .vt in the working unit with float precision,
# so reopening one saved in m, mm, in or ft moves many points by one ulp.
_REPOSITIONED = (
    f"{_MOVED}, perhaps slightly: reopening a .ma saved in a working unit other "
    "than cm re-rounds them. Save the scene as .mb, or mark the groups again"
)
_POSE_NOTE = ("Overlap decisions not applied: the deformed mesh changed pose or frame; "
              "decisions apply again when it matches.")
_ORIG_NAME = re.compile(r"Orig\d*$")
# Upstream nodes that make a mesh's points follow the frame. Core types only:
# ``ls -type`` also matches derived types (animCurveTL, skinCluster).
_ANIMATED_TYPES = ("geometryFilter", "animCurve", "time", "cacheFile")
# Plugin types are matched by name: ``ls -type`` with a type that is not
# loaded matches nothing, and inside a type list it empties the whole query.
_ANIMATED_TYPE_NAMES = frozenset({"AlembicNode"})


def _is_animated(path: str) -> bool:
    """Whether the mesh's points follow the frame and pose rather than edits.

    True when its unpruned history (which runs through driving shapes) holds
    a deformer, an animation curve, the time node or a cache, when a visible
    shape drives it (``inMesh`` from another mesh), or when an intermediate
    "...Orig" sibling shape is left by a deformer. A different hash there is
    not an edit: the decision is kept unapplied instead of dropped.

    Plain modeling history still counts as an edit: polyMergeVert or
    polyMoveVertex leave an intermediate "polySurfaceShape1" and polyUnite
    leaves intermediate source shapes, and none of those is visible.
    """

    history = cmds.listHistory(path) or []
    for kind in _ANIMATED_TYPES:
        if cmds.ls(history, type=kind):
            return True
    typed = cmds.ls(history, showType=True) or []
    if _ANIMATED_TYPE_NAMES.intersection(typed[1::2]):
        return True
    own = set(cmds.ls(path, uuid=True) or ())
    for shape in cmds.ls(history, type="shape", long=True) or ():
        if (not cmds.getAttr(shape + ".intermediateObject")
                and own.isdisjoint(cmds.ls(shape, uuid=True) or ())):
            return True
    parents = cmds.listRelatives(path, parent=True, fullPath=True) or []
    for parent in parents[:1]:
        for sibling in cmds.listRelatives(parent, shapes=True, fullPath=True) or ():
            if (sibling != path and _ORIG_NAME.search(sibling.rsplit("|", 1)[-1])
                    and cmds.getAttr(sibling + ".intermediateObject")):
                return True
    return False


def _reopen_rerounds(scene_path: str) -> bool:
    """Whether reopening the bound scene re-rounds positions: Maya ASCII, not in cm."""

    try:
        return (bool(scene_path) and is_maya_ascii(scene_path)
                and cmds.currentUnit(query=True, linear=True) != "cm")
    except Exception:                         # unreadable file: no explanation offered
        return False


def _hash_mismatch_reason(measured: Measurement, entry: MeshDecisions, scene_path: str) -> str:
    """Why the geometry hash differs, as far as the stored gates can tell."""

    groups = overlap_groups(measured.close)
    same_shape = (measured.vertex_count == entry.vertex_count
                  and measured.face_count == entry.face_count
                  and len(groups) == entry.group_count
                  and groups_sha256(groups) == entry.groups_sha256)
    if not same_shape:
        return _EDITED
    return _REPOSITIONED if _reopen_rerounds(scene_path) else _MOVED


def _class_issue(
    issue: Issue,
    intent: Literal["error", "unclassified", "intentional"],
    groups: Sequence[tuple[int, ...]],
    close: Sequence[tuple[int, int, float]],
    tolerance: float,
    total_groups: int,
    source: str,
) -> Issue:
    """One O issue for the groups of one class, with O's details recomputed."""

    members = {vertex for group in groups for vertex in group}
    # A pair belongs to the group holding its first vertex (both share one).
    pairs = [pair for pair in close if pair[0] in members]
    by_distance = sorted(pairs, key=lambda pair: (pair[2], pair[0], pair[1]))
    listed_limit = overlapping_vertices.MAX_LISTED_PAIRS
    listed = " ".join(f"{i}-{j}" for i, j, _ in by_distance[:listed_limit])
    details = [
        ("pairs", listed),
        ("pair_count", str(len(pairs))),
        ("tolerance_cm", f"{tolerance:.6g}"),
        ("smallest_distance_cm", f"{by_distance[0][2]:.6g}"),
        ("largest_distance_cm", f"{by_distance[-1][2]:.6g}"),
    ]
    if len(pairs) > listed_limit:
        details.append(("pairs_not_listed", str(len(pairs) - listed_limit)))
    details.append(("group_count", str(len(groups))))
    details.append(("groups", " ".join(",".join(map(str, group))
                                       for group in groups[:MAX_LISTED_GROUPS])))
    details.append(("intent", intent))
    if intent != "unclassified":
        details.append(("intent_source", source))

    count, pair_count = len(groups), len(pairs)
    if intent == "error":
        severity, needs_review = Severity.ERROR, False
        message = (f"{count} overlap group(s) ({pair_count} pair(s)) marked Error by the "
                   "artist; merge or separate them.")
    elif intent == "unclassified":
        severity, needs_review = Severity.WARNING, True
        message = (
            f"{pair_count} unwelded vertex {'pair is' if pair_count == 1 else 'pairs are'} "
            f"closer than {tolerance:.6g} cm with no edge between them."
            f" {count} of {total_groups} overlap groups are not classified yet."
        )
    else:
        severity, needs_review = Severity.WARNING, False
        message = f"{count} overlap group(s) ({pair_count} pair(s)) accepted as Intentional."
    return replace(
        issue,
        severity=severity,
        needs_review=needs_review,
        message=message,
        component_indices=tuple(sorted(members)),
        measured_value=by_distance[-1][2],
        details=tuple(details),
    )


def _source_name(store: IntentStore) -> str:
    if not store.bound_path:
        return "unsaved scene"
    return os.path.basename(sidecar_path(store.bound_path))


def _annotate(
    issue: Issue,
    store: IntentStore,
    settings: tuple[float, float],
    handles: Mapping[str, om.MObjectHandle] | None,
    cache: dict | None,
    notes: list[tuple[str, str]],
    listing: dict[str, dict[str, list[str]]] | None = None,
) -> list[Issue] | None:
    """Replacement issues for one O issue, or None to keep it unchanged.

    ``listing`` keeps one reference listing for a whole ``apply_intent`` call.
    """

    path = issue.target_path

    def note(text: str) -> None:
        notes.append((path, text))

    try:
        node = _resolve_node(issue, handles)
    except SelectRefused as exc:
        note(f"Overlap decisions not checked: {exc}")
        return None
    except Exception as exc:
        note(f"Overlap decisions not checked: {type(exc).__name__}: {exc}")
        return None
    # A failed query keeps the issue and the decision: never applied, never moved.
    try:
        key = identity_key(node)
    except Exception as exc:
        note(f"Overlap decisions not checked: {exc}")
        return None
    entry = store.lookup(key)
    if entry is None:
        return None
    try:
        found = holders(key)
        if key.ref_chain and listing is not None and "tops" not in listing:
            listing["tops"] = reference_tops()
        twins = reference_twins(key, None if listing is None else listing.get("tops"))
    except Exception as exc:
        note(f"Overlap decisions not checked: {exc}")
        return None
    if twins:
        # The store drops these after each import or reference change; this
        # also covers a callback that never ran. One holder is not enough:
        # the key names whichever twin's copy is loaded.
        reason = (f"ambiguous identity: {twins} reference nodes share one level of its "
                  "reference chain")
        store.drop(key, reason)
        note(f"Overlap decisions dropped: {reason}.")
        return None
    if len(found) != 1 or found[0] != node:
        store.drop(key, "ambiguous identity")
        if len(found) > 1:
            note(f"Overlap decisions dropped: ambiguous identity ({len(found)} meshes "
                 "share this UUID and reference chain).")
        else:
            note("Overlap decisions dropped: ambiguous identity (the key no longer "
                 "names exactly this mesh).")
        return None
    if settings != entry.o_settings or entry.rule_version != RULE_VERSION:
        note("Overlap decisions were made with other O settings; not applied.")
        return None
    try:
        measured = cached_measure(node, settings, cache)
    except Exception as exc:
        note(f"Overlap decisions not checked: {type(exc).__name__}: {exc}")
        return None
    if measured is None:
        note("Overlap decisions not checked: the mesh has no vertices.")
        return None
    if measured.geometry_sha256 != entry.geometry_sha256:
        try:
            animated = _is_animated(om.MDagPath.getAPathTo(node).fullPathName())
        except Exception as exc:
            note(f"Overlap decisions not checked: {type(exc).__name__}: {exc}")
            return None
        if animated:
            note(_POSE_NOTE)
            return None
        reason = _hash_mismatch_reason(measured, entry, store.bound_path)
        store.drop(key, reason)
        note(f"Overlap decisions dropped: {reason}.")
        return None
    if measured.tolerance != entry.tolerance_cm:
        note(f"Overlap decisions not applied: the tolerance is now {measured.tolerance:.6g} cm, "
             f"not {entry.tolerance_cm:.6g} cm.")
        return None
    if not _matches_issue(measured, issue):
        note("Overlap decisions not applied: the report does not match the live mesh.")
        return None

    groups = overlap_groups(measured.close)
    if len(groups) != entry.group_count or groups_sha256(groups) != entry.groups_sha256:
        store.drop(key, "overlap groups changed")
        note("Overlap decisions dropped: overlap groups changed.")
        return None
    known = set(groups)
    if any(group.vertices not in known for group in entry.groups):
        store.drop(key, "sidecar entry does not match the mesh")
        note("Overlap decisions dropped: sidecar entry does not match the mesh.")
        return None

    decisions = entry.decision_map(groups)
    classes: dict[str, list[tuple[int, ...]]] = {"error": [], "unclassified": [], "intentional": []}
    for group in groups:
        classes[decisions[group] or "unclassified"].append(group)
    if not classes["error"] and not classes["intentional"]:
        return None
    source = _source_name(store)
    return [
        _class_issue(issue, intent, members, measured.close, measured.tolerance,
                     len(groups), source)
        for intent, members in classes.items() if members
    ]


def apply_intent(
    report: ValidationReport,
    store: IntentStore,
    *,
    handles: Mapping[str, om.MObjectHandle] | None = None,
    cache: dict | None = None,
) -> IntentResult:
    """Split O issues by the stored decisions; the same report object if none apply."""

    if (not store.entries or report.stop_reason is not None
            or report.state == ReportState.STALE):
        return IntentResult(report, ())
    positions = [index for index, result in enumerate(report.results) if result.check_id == "O"]
    if not positions:
        return IntentResult(report, ())
    try:
        settings = read_settings(report.context)
    except Exception:
        return IntentResult(report, ())

    notes: list[tuple[str, str]] = []
    listing: dict[str, dict[str, list[str]]] = {}
    results = list(report.results)
    changed = False
    for position in positions:
        result = results[position]
        issues: list[Issue] = []
        result_changed = False
        for issue in result.issues:
            replacement = _annotate(issue, store, settings, handles, cache, notes, listing)
            if replacement is None:
                issues.append(issue)
            else:
                issues.extend(replacement)
                result_changed = True
        if result_changed:
            changed = True
            results[position] = replace(
                result, issues=tuple(issues),
                verdict=derive_state(issues, execution=result.execution),
            )
    if not changed:
        return IntentResult(report, tuple(notes))
    annotated = replace(
        report,
        results=tuple(results),
        state=report_state(results, partial=report.partial, stop_reason=None),
    )
    return IntentResult(annotated, tuple(notes))


# ----------------------------------------------------------- classification


class ClassifyRefused(Exception):
    """Nothing was recorded; the message says why."""


@dataclass(frozen=True)
class ClassifyOutcome:
    target_path: str
    changed_groups: int
    intent: Intent | None
    message: str


@dataclass(frozen=True)
class GroupRow:
    vertices: tuple[int, ...]
    largest_distance: float
    decision: Literal["Error", "Intentional", "Not classified"]


@dataclass(frozen=True)
class GroupsView:
    target_path: str
    rows: tuple[GroupRow, ...]
    note: str


def _raw_issue(base_report: ValidationReport, row_issue: Issue) -> Issue:
    """The unannotated O issue of the row's mesh in the last run."""

    found = [
        issue for result in base_report.results if result.check_id == "O"
        for issue in result.issues if issue.target_path == row_issue.target_path
    ]
    if len(found) != 1:
        raise ClassifyRefused("This row has no overlap issue in the last run.")
    return found[0]


def _ambiguity(key, count: int) -> str:
    if key.ref_chain:
        return "These referenced copies cannot be told apart, so no decision can be stored for them."
    others = count - 1
    return (f"Maya cannot tell this mesh from {others} other {'copy' if others == 1 else 'copies'} "
            "(same UUID and reference chain); use New Identity… on this copy first.")


def classify(
    base_report: ValidationReport,
    row_issue: Issue,
    store: IntentStore,
    intent: Intent | None,
    *,
    groups: Sequence[Sequence[int]] | None = None,
    handles: Mapping[str, om.MObjectHandle] | None = None,
    cache: dict | None = None,
) -> ClassifyOutcome:
    """Mark the row's groups (or ``groups``) Intentional or Error; None clears.

    Changes only the store and Maya's modified flag (so Maya asks to save); no
    node changes and nothing enters the undo queue. ``base_report`` is the
    raw, unannotated report of the last run.
    """

    if (base_report.stop_reason is not None or base_report.state == ReportState.STALE):
        raise ClassifyRefused("The report is stale or incomplete; run the checks again.")
    if intent is not None and intent not in ("intentional", "error"):
        raise ValueError(f"Unknown overlap intent {intent!r}")
    if row_issue.check_id != "O":
        raise ClassifyRefused("Only Overlapping vertices (O) rows can be marked.")
    raw = _raw_issue(base_report, row_issue)
    try:
        settings = read_settings(base_report.context)
    except Exception as exc:
        raise ClassifyRefused(f"The O settings of the last run are invalid: {exc}") from exc
    try:
        node = _resolve_node(raw, handles)
    except SelectRefused as exc:
        raise ClassifyRefused(str(exc)) from exc
    try:
        key = identity_key(node)
        found = holders(key)
        twins = reference_twins(key)
    except Exception as exc:
        raise ClassifyRefused(f"The mesh's identity could not be read: {exc}") from exc
    if len(found) > 1 or twins:
        # Two reference nodes sharing a chain level make even one holder
        # ambiguous: unloading one copy and loading the other moves the key.
        if store.lookup(key) is not None:
            store.drop(key, (f"ambiguous identity: {len(found)} meshes share it" if len(found) > 1
                             else f"ambiguous identity: {twins} reference nodes share one "
                                  "level of its reference chain"))
        raise ClassifyRefused(_ambiguity(key, len(found)))
    if len(found) != 1 or found[0] != node:
        raise ClassifyRefused("The mesh's identity could not be verified; run the checks again.")

    try:
        measured = cached_measure(node, settings, cache)
    except Exception as exc:
        raise ClassifyRefused(f"The mesh could not be measured: {type(exc).__name__}: {exc}") from exc
    if not _matches_issue(measured, raw):
        raise ClassifyRefused("The mesh no longer matches the report; run the checks again.")
    assert measured is not None
    all_groups = overlap_groups(measured.close)
    if groups is not None:
        chosen = [tuple(group) for group in groups]
        known = set(all_groups)
        if any(group not in known for group in chosen):
            raise ClassifyRefused("A selected group is not an overlap group of this mesh; "
                                  "run the checks again.")
    else:
        # A split row's component_indices are exactly its class's groups.
        row_vertices = set(row_issue.component_indices)
        chosen = [group for group in all_groups if row_vertices.issuperset(group)]
    if not chosen:
        raise ClassifyRefused("No overlap group in this row.")

    path = om.MDagPath.getAPathTo(node).fullPathName()
    changed = store.record(
        key,
        measurement=measured,
        settings=settings,
        all_groups=all_groups,
        chosen=chosen,
        intent=intent,
        witness=absolute_dag_path(node),
        reference_files=reference_files(path),
    )
    if changed:
        cmds.file(modified=True)        # not an undo entry; Maya now asks to save
    leaf = raw.target_path.rsplit("|", 1)[-1]
    groups_text = _plural(len(chosen), "group")
    if intent is None:
        message = ("No decision to clear." if not changed else
                   f"Cleared the marks on {groups_text} of {leaf} "
                   "(save the scene to keep the change).")
    else:
        label = "Intentional" if intent == "intentional" else "Error"
        message = (f"Marked {groups_text} {label} on {leaf} (save the scene to keep them)."
                   if changed else f"The {groups_text} on {leaf} were already marked {label}.")
    return ClassifyOutcome(raw.target_path, changed, intent, message)


def describe_groups(
    base_report: ValidationReport,
    row_issue: Issue,
    store: IntentStore,
    *,
    handles: Mapping[str, om.MObjectHandle] | None = None,
    cache: dict | None = None,
) -> GroupsView:
    """Every overlap group of the row's mesh with its applicable decision; read-only."""

    path = row_issue.target_path
    try:
        raw = _raw_issue(base_report, row_issue)
        settings = read_settings(base_report.context)
        node = _resolve_node(raw, handles)
        measured = cached_measure(node, settings, cache)
    except (ClassifyRefused, SelectRefused) as exc:
        return GroupsView(path, (), str(exc))
    except Exception as exc:
        return GroupsView(path, (), f"The groups could not be read: {type(exc).__name__}: {exc}")
    if not _matches_issue(measured, raw):
        return GroupsView(path, (), "The mesh no longer matches the report; run the checks again.")
    assert measured is not None
    groups = overlap_groups(measured.close)
    largest: dict[int, float] = {}
    owner = {vertex: index for index, group in enumerate(groups) for vertex in group}
    for i, _, distance in measured.close:
        index = owner[i]
        largest[index] = max(largest.get(index, 0.0), distance)

    decisions: dict[tuple[int, ...], Intent | None] = {}
    note = ""
    try:
        key = identity_key(node)
        found = holders(key)
    except Exception as exc:
        key, found, note = None, (), f"Identity not checked: {exc}"
    entry: MeshDecisions | None = store.lookup(key) if key is not None else None
    if key is not None and (len(found) != 1 or found[0] != node):
        note = f"Ambiguous identity: {_ambiguity(key, len(found))}"
    elif entry is not None:
        if settings != entry.o_settings or entry.rule_version != RULE_VERSION:
            note = "Stored decisions were made with other O settings; they are not applied."
        elif (measured.geometry_sha256 != entry.geometry_sha256
              or measured.tolerance != entry.tolerance_cm
              or len(groups) != entry.group_count
              or groups_sha256(groups) != entry.groups_sha256):
            note = "Stored decisions no longer match this mesh; they are dropped at the next run."
            try:
                if (measured.geometry_sha256 != entry.geometry_sha256
                        and _is_animated(om.MDagPath.getAPathTo(node).fullPathName())):
                    note = ("Stored decisions do not match this pose or frame; they apply "
                            "again when it matches.")
            except Exception:
                pass
        else:
            decisions = entry.decision_map(groups)
    labels = {"error": "Error", "intentional": "Intentional", None: "Not classified"}
    rows = tuple(
        GroupRow(group, largest.get(index, 0.0), labels[decisions.get(group)])
        for index, group in enumerate(groups)
    )
    return GroupsView(path, rows, note)


# ---------------------------------------------------------------- batch


def validate_with_intent(
    context: ValidationContext | None = None,
    *,
    scene_path: str | None = None,
    **validate_kwargs,
) -> IntentResult:
    """``validate()`` plus the scene's stored decisions; never writes a file.

    The store is in-memory only, so its fail-closed drops are discarded. It
    binds late (it did not see the open), so every witness path is checked.
    """

    report = validate(context, **validate_kwargs)
    store = IntentStore(persist=False)
    store.bind(scene_path if scene_path is not None
               else (cmds.file(query=True, sceneName=True) or ""))
    return apply_intent(report, store)


__all__ = [
    "ClassifyOutcome", "ClassifyRefused", "GroupRow", "GroupsView", "IntentResult",
    "MAX_LISTED_GROUPS", "apply_intent", "cached_measure", "classify", "describe_groups",
    "validate_with_intent",
]
