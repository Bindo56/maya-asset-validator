"""Duplicate-name rename: a read-only plan, then one undoable chunk.

Why it is built this way:

- ``plan_rename`` never edits the scene. It re-indexes with D's own
  ``name_index`` and refuses when the clash changed since the run, because
  some edits (renaming a namespace) fire no callback the window sees.
- Targets are absolute (``:ns:leaf``) in each node's own namespace. A
  relative target lands in the *current* namespace, and ``ns:X`` resolves
  under it too.
- Free names come from ``name_snapshot``: every node's absolute name plus
  every namespace. Maya silently suffixes a clash with a DG node, a root
  sibling or a namespace, and ``MSelectionList.add`` calls an ambiguous name
  free, so neither can be the free test.
- ``apply_rename`` runs every refusal before the chunk opens. Inside one
  named chunk it renames deepest first from paths
  re-derived through handles: a stored path goes stale once a parent is
  renamed. Transforms use ``ignoreShape`` and every derived shape, including
  the intermediate Orig shape, gets an explicit prefix swap; Maya's default
  shape rewrite mangles ``_ShapeNN`` names and ignores a locked shape.
- The postcondition checks what Maya actually did: a silent suffix, a leaf
  that already exists under another parent (never suffixed), the UUID and
  the exact returned string. A failure is rolled back with at most one
  ``cmds.undo()``, only when our chunk is the newest entry and no caller
  chunk is still open. An empty chunk is never recorded, so a blind undo
  would revert the artist's previous action or the previous fix.

No Qt here. The window calls ``plan_rename``, shows the plan, then calls
``apply_rename`` with the ticked member indices.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import re
from typing import Literal

import maya.api.OpenMaya as om
import maya.cmds as cmds

from asset_validator.checks.duplicate_names import name_index
from asset_validator.data_models import Issue, ValidationContext
from asset_validator.ui.scene import SelectRefused, resolve_issue

RENAME_CHUNK = "AssetValidatorRename"     # undo chunk name of our own rename
LEAF_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)      # always fullmatch
_TRAILING = re.compile(r"(?P<base>.*?)_(?P<digits>[0-9]+)")         # fullmatch on the leaf
MAX_NUMBER_TRIES = 10_000

RELATIVE_NAMES_REFUSAL = (
    "Relative namespace names are on; turn them off "
    "(namespace -relativeNames false) to rename."
)
_INVALID_NAME = "'{}' is not a valid Maya name (ASCII letters, digits and _, not starting with a digit)."


class RenameRefused(Exception):
    """Nothing was renamed: the request was refused before any undo entry."""


@dataclass(frozen=True, eq=False)
class RenameOp:
    handle: om.MObjectHandle
    role: Literal["transform", "shape", "intermediate"]
    original: str          # MFnDependencyNode.absoluteName() at plan time, e.g. ":a:mesh_01"
    target: str            # absolute, in the node's own namespace, e.g. ":a:mesh_02"
    uuid: str


@dataclass(frozen=True, eq=False)
class RenameMember:
    index: int                               # position in RenamePlan.members (sorted by canonical_path)
    canonical_path: str                      # om.MDagPath.getAPathTo(node).fullPathName()
    paths: tuple[str, ...]                   # instance paths from name_index()
    kind: Literal["transform", "mesh"]
    in_scope: bool
    promoted_from: str | None                # shape path when a shape member became its transform
    refusal: str | None                      # cannot be renamed (Maya's own wording)
    problem: str | None                      # invalid or taken edited name
    ops: tuple[RenameOp, ...]                # empty when refused
    kept: tuple[tuple[str, str], ...]        # (shape path, reason) left untouched


@dataclass(frozen=True, eq=False)
class RenamePlan:
    short_name: str
    touched_names: tuple[str, ...]           # D short names that lose a member (display)
    members: tuple[RenameMember, ...]
    default_choice: frozenset[int]
    warnings: tuple[str, ...]
    snapshot: frozenset[str] = field(repr=False)


@dataclass(frozen=True)
class RenameOutcome:
    ok: bool
    renamed: tuple[tuple[str, str], ...]     # (original, target) absolute names, in chunk order
    returned: tuple[str, ...]                # raw cmds.rename results
    error: str | None
    rolled_back: bool
    restored: bool | None                    # None when nothing had changed
    undoable: bool = True                    # Ctrl+Z (ours or the caller's chunk) can restore it


# --- small helpers ---------------------------------------------------------

def _ns(absolute: str) -> str:
    """``":a:mesh_01"`` -> ``":a:"``; a root name gives ``":"``."""

    return absolute[:absolute.rfind(":") + 1]


def _leaf(absolute: str) -> str:
    """The name without any namespace."""

    return absolute[absolute.rfind(":") + 1:]


def _node(path: str) -> om.MObject:
    selection = om.MSelectionList()
    selection.add(path)
    return selection.getDependNode(0)


def _path(node: om.MObject) -> str:
    return om.MDagPath.getAPathTo(node).fullPathName()


def _absolute(node: om.MObject) -> str:
    return om.MFnDependencyNode(node).absoluteName()


def _is_derived(shape_leaf: str, transform_leaf: str) -> bool:
    """A shape named after its transform; it must follow a transform rename."""

    return shape_leaf.startswith((transform_leaf + "Shape", transform_leaf + "_Shape"))


def _refusal(node: om.MObject) -> str | None:
    """Maya's own reason why ``cmds.rename`` would raise for this node."""

    fn = om.MFnDependencyNode(node)
    if fn.isFromReferencedFile or fn.isShared or fn.isDefaultNode:
        return "Cannot rename a read only node."
    path = _path(node)
    if cmds.lockNode(path, query=True, lock=True)[0]:
        return "Cannot rename a locked node."
    if cmds.lockNode(path, query=True, lockName=True)[0]:
        return "Cannot rename a node with a locked name."
    return None


def name_snapshot() -> frozenset[str]:
    """Every used absolute name: all DAG and DG nodes, plus every namespace.

    Independent of relativeNames mode. Maya silently suffixes a rename onto
    any of these, so a candidate is free only when it is absent here.
    """

    names: set[str] = set()
    fn = om.MFnDependencyNode()
    iterator = om.MItDependencyNodes()
    while not iterator.isDone():
        fn.setObject(iterator.thisNode())
        names.add(fn.absoluteName())
        iterator.next()
    names.update(cmds.namespaceInfo(
        ":", listOnlyNamespaces=True, recurse=True, absoluteName=True,
    ) or ())
    return frozenset(names)


# --- planning --------------------------------------------------------------

@dataclass(eq=False)
class _Draft:
    """A member while the plan is being built (mutable, private)."""

    canonical_path: str
    paths: tuple[str, ...]
    kind: Literal["transform", "mesh"]
    node: om.MObject
    original: str                            # absoluteName() at plan time
    scope_paths: set[str]
    promoted_from: str | None = None
    refusal: str | None = None
    problem: str | None = None
    derived: list[tuple[om.MObject, str, str]] = field(default_factory=list)   # (node, role, absolute)
    kept: list[tuple[str, str]] = field(default_factory=list)
    preferred_leaf: str | None = None        # "<parentLeaf>Shape" for a shape-only clash
    targets: list[tuple[om.MObject, str, str, str]] = field(default_factory=list)
    index: int = -1
    in_scope: bool = False

    def proposal(self, new_leaf: str) -> list[tuple[om.MObject, str, str, str]]:
        """(node, role, original, target) for every op if the member gets ``new_leaf``."""

        original = self.original
        if self.kind == "mesh":
            role = "intermediate" if om.MFnDagNode(self.node).isIntermediateObject else "shape"
            return [(self.node, role, original, _ns(original) + new_leaf)]
        old_leaf = _leaf(original)
        ops = [(self.node, "transform", original, _ns(original) + new_leaf)]
        for shape, role, shape_absolute in self.derived:
            suffix = _leaf(shape_absolute)[len(old_leaf):]
            ops.append((shape, role, shape_absolute, _ns(shape_absolute) + new_leaf + suffix))
        return ops


def _promotion(node: om.MObject, index: Mapping[str, Mapping[str, set[str]]]) -> om.MObject | None:
    """The parent transform a shape member is renamed through, if any.

    A clashing shape is redirected to its clashing parent only when the shape
    is named after that parent, so renaming the parent renames it.
    """

    dag = om.MFnDagNode(node)
    if dag.parentCount() != 1:
        return None
    parent = dag.parent(0)
    if not parent.hasFn(om.MFn.kTransform):
        return None
    if not _is_derived(_leaf(_absolute(node)), _leaf(_absolute(parent))):
        return None
    if len(index.get(_path(parent).rsplit("|", 1)[-1], {})) < 2:
        return None
    return parent


def _drafts(
    group: Mapping[str, set[str]],
    index: Mapping[str, Mapping[str, set[str]]],
) -> list[_Draft]:
    """One draft per canonical node, shapes promoted to their transforms."""

    by_path: dict[str, _Draft] = {}
    for canonical in sorted(group):
        node = _node(canonical)
        paths = tuple(sorted(group[canonical]))
        promoted_from = None
        scope_paths = set(paths)
        if node.hasFn(om.MFn.kTransform):
            kind: Literal["transform", "mesh"] = "transform"
        else:
            kind = "mesh"
            parent = _promotion(node, index)
            if parent is not None:
                promoted_from = canonical
                node, kind = parent, "transform"
                canonical = _path(parent)
                paths = tuple(sorted(index[canonical.rsplit("|", 1)[-1]][canonical]))
                scope_paths.update(paths)
        existing = by_path.get(canonical)
        if existing is not None:
            existing.scope_paths.update(scope_paths)
            continue
        by_path[canonical] = _Draft(
            canonical_path=canonical, paths=paths, kind=kind, node=node,
            original=_absolute(node), scope_paths=scope_paths, promoted_from=promoted_from,
        )
    drafts = [by_path[path] for path in sorted(by_path)]
    for number, draft in enumerate(drafts):
        draft.index = number
    return drafts


def _classify(draft: _Draft, index: Mapping[str, Mapping[str, set[str]]], warnings: list[str]) -> None:
    """Fill in the refusal, the derived shapes and the kept shapes of one draft."""

    if draft.kind == "mesh":
        draft.refusal = _refusal(draft.node)
        dag = om.MFnDagNode(draft.node)
        if draft.refusal is None and dag.parentCount() > 1:
            draft.refusal = f"{draft.canonical_path} is an instanced shape; rename it by hand."
        if draft.refusal is not None or dag.isIntermediateObject:
            return
        # A shape-only clash: prefer the C convention "<parentLeaf>Shape" when
        # the parent is unique and this is its only live mesh.
        parent_path = draft.canonical_path.rsplit("|", 1)[0]
        live = cmds.listRelatives(
            parent_path, shapes=True, type="mesh", noIntermediate=True, fullPath=True,
        ) or []
        if len(index.get(parent_path.rsplit("|", 1)[-1], {})) == 1 and len(live) == 1:
            draft.preferred_leaf = _leaf(_absolute(dag.parent(0))) + "Shape"
        return

    draft.refusal = _refusal(draft.node)
    old_leaf = _leaf(draft.original)
    custom_warnings: list[str] = []
    for shape_path in cmds.listRelatives(draft.canonical_path, shapes=True, fullPath=True) or ():
        shape = _node(shape_path)
        fn = om.MFnDependencyNode(shape)
        if fn.isFromReferencedFile:
            draft.kept.append((shape_path, "referenced"))
            continue
        shape_absolute = fn.absoluteName()
        if not _is_derived(_leaf(shape_absolute), old_leaf):
            draft.kept.append((shape_path, "custom name"))
            if not fn.hasUniqueName():
                custom_warnings.append(f"{shape_path} keeps its name and still clashes")
            continue
        dag = om.MFnDagNode(shape)
        role = "intermediate" if dag.isIntermediateObject else "shape"
        if dag.parentCount() > 1:
            reason: str | None = "it is an instanced shape; rename it by hand."
        else:
            reason = _refusal(shape)
        if reason is not None and draft.refusal is None:
            draft.refusal = f"{shape_path}: {reason}"
        draft.derived.append((shape, role, shape_absolute))
    if draft.refusal is None:
        warnings.extend(custom_warnings)


def _invalid_leaf(proposal: list[tuple[om.MObject, str, str, str]]) -> str | None:
    for *_, target in proposal:
        if not LEAF_PATTERN.fullmatch(_leaf(target)):
            return _leaf(target)
    return None


def _number(draft: _Draft, taken: set[str] | frozenset[str]) -> None:
    """Give a draft the first free name; sets ``targets``, ``problem`` or ``refusal``."""

    if draft.preferred_leaf is not None:
        proposal = draft.proposal(draft.preferred_leaf)
        if _invalid_leaf(proposal) is None and all(op[3] not in taken for op in proposal):
            draft.targets = proposal
            return
    leaf = _leaf(draft.original)
    match = _TRAILING.fullmatch(leaf)
    if match:
        base, start, width = match["base"], int(match["digits"]) + 1, max(2, len(match["digits"]))
    else:
        base, start, width = leaf, 1, 2
    first = draft.proposal(f"{base}_{start:0{width}d}")
    invalid = _invalid_leaf(first)
    if invalid is not None:
        # Only the digits change between candidates, so none can be valid; the
        # artist can still type a valid name for this row.
        draft.targets, draft.problem = first, _INVALID_NAME.format(invalid)
        return
    for number in range(start, start + MAX_NUMBER_TRIES):
        proposal = draft.proposal(f"{base}_{number:0{width}d}")
        if all(op[3] not in taken for op in proposal):
            draft.targets = proposal
            return
    draft.refusal = f"No free name within {MAX_NUMBER_TRIES} numbers"


def _edited(draft: _Draft, leaf: str, snapshot: frozenset[str], allocated: set[str]) -> None:
    proposal = draft.proposal(leaf)
    draft.targets = proposal
    invalid = leaf if not LEAF_PATTERN.fullmatch(leaf) else _invalid_leaf(proposal)
    if invalid is not None:
        draft.problem = _INVALID_NAME.format(invalid)
        return
    for *_, target in proposal:
        if target in snapshot:
            draft.problem = f"'{target[1:]}' is already used."
            return
        if target in allocated:
            draft.problem = f"'{target[1:]}' is used by another row."
            return
    allocated.update(op[3] for op in proposal)


_READ_ONLY = "Cannot rename a read only node."
_LISTED_REFUSALS = 5


def _nothing_renamable(short_name: str, drafts: list[_Draft]) -> str:
    """Name each node's own reason; only referenced nodes belong in source files."""

    listed = "; ".join(
        f"{draft.canonical_path}: {draft.refusal}" for draft in drafts[:_LISTED_REFUSALS]
    )
    if len(drafts) > _LISTED_REFUSALS:
        listed += f"; and {len(drafts) - _LISTED_REFUSALS} more"
    text = f"No node named {short_name!r} can be renamed: {listed}"
    if all(draft.refusal == _READ_ONLY for draft in drafts):
        text += " Rename referenced nodes in their source files."
    return text


def _default_choice(drafts: list[_Draft]) -> frozenset[int]:
    renamable = [draft for draft in drafts if draft.refusal is None]
    if any(draft.refusal is not None or not draft.in_scope for draft in drafts):
        # Keepers are the refused and out-of-scope members.
        return frozenset(draft.index for draft in renamable if draft.in_scope)
    return frozenset(draft.index for draft in renamable[1:])


def plan_rename(
    issue: Issue,
    context: ValidationContext,
    *,
    handles: Mapping[str, om.MObjectHandle] | None = None,
    names: Mapping[int, str] | None = None,
    snapshot: frozenset[str] | None = None,
) -> RenamePlan:
    """Describe how to rename the members of one D clash. Read-only.

    ``names`` maps member indices to edited namespace-free leaves. Pass the
    first plan's ``snapshot`` back when re-planning, so the preview stays
    comparable while the dialog is open.
    """

    if issue.check_id != "D":
        raise RenameRefused("Rename only works on Duplicate names (D) issues.")
    short_name = dict(issue.details).get("short_name")
    if not short_name:
        raise RenameRefused("This issue does not name its clash; run the checks again.")
    if cmds.namespace(query=True, relativeNames=True):
        raise RenameRefused(RELATIVE_NAMES_REFUSAL)
    try:
        resolve_issue(issue, handles)
    except SelectRefused as exc:
        raise RenameRefused(str(exc)) from None

    index = name_index()
    group = index.get(short_name, {})
    if {path for paths in group.values() for path in paths} != set(issue.related_paths):
        raise RenameRefused(
            f"The clash on {short_name!r} changed since the run; run the checks again."
        )

    scoped = {path for target in context.targets for path in (target.transform_path, target.shape_path)}
    warnings: list[str] = []
    drafts = _drafts(group, index)
    for draft in drafts:
        draft.in_scope = bool(draft.scope_paths & scoped)
        _classify(draft, index, warnings)
    if all(draft.refusal is not None for draft in drafts):
        raise RenameRefused(_nothing_renamable(short_name, drafts))

    # Edited names first, so a typed name wins over a proposal. Then the rows
    # renamed by default, then the keepers: whatever is ticked, the preview
    # stays the same and every row's names are distinct.
    snapshot = name_snapshot() if snapshot is None else snapshot
    allocated: set[str] = set()
    edits = dict(names or {})
    for draft in drafts:
        if draft.refusal is None and draft.index in edits:
            _edited(draft, edits[draft.index], snapshot, allocated)
    chosen_first = _default_choice(drafts)
    order = sorted(
        (draft for draft in drafts if draft.refusal is None and draft.index not in edits),
        key=lambda draft: (draft.index not in chosen_first, draft.index),
    )
    for draft in order:
        _number(draft, snapshot | allocated)
        allocated.update(op[3] for op in draft.targets if draft.refusal is None)
    if all(draft.refusal is not None for draft in drafts):
        raise RenameRefused(_nothing_renamable(short_name, drafts))

    members = []
    for draft in drafts:
        ops: tuple[RenameOp, ...] = ()
        if draft.refusal is None:
            ops = tuple(
                RenameOp(
                    handle=om.MObjectHandle(node), role=role, original=original,
                    target=target, uuid=om.MFnDependencyNode(node).uuid().asString(),
                )
                for node, role, original, target in draft.targets
            )
        members.append(RenameMember(
            index=draft.index, canonical_path=draft.canonical_path, paths=draft.paths,
            kind=draft.kind, in_scope=draft.in_scope, promoted_from=draft.promoted_from,
            refusal=draft.refusal, problem=draft.problem if draft.refusal is None else None,
            ops=ops, kept=tuple(draft.kept),
        ))

    default_choice = _default_choice(drafts)
    unrenamed = [member.canonical_path for member in members if member.index not in default_choice]
    if len(unrenamed) >= 2:
        warnings.append(
            f"{short_name!r} will still be used by {len(unrenamed)} nodes: {', '.join(unrenamed)}"
        )
    return RenamePlan(
        short_name=short_name,
        touched_names=tuple(sorted({op.original[1:] for member in members for op in member.ops})),
        members=tuple(members),
        default_choice=default_choice,
        warnings=tuple(warnings),
        snapshot=snapshot,
    )


# --- apply -----------------------------------------------------------------

def _current(op: RenameOp) -> str | None:
    """The op's node's absolute name now, or None when the node is gone."""

    if not op.handle.isValid():
        return None
    return _absolute(op.handle.object())


def _verify(op: RenameOp) -> None:
    """Refuse unless the op's node is exactly as the preview saw it."""

    name = op.original[1:]
    if not op.handle.isValid():
        raise RenameRefused(f"{name} changed since the preview.")
    node = op.handle.object()
    fn = om.MFnDependencyNode(node)
    if fn.absoluteName() != op.original or fn.uuid().asString() != op.uuid:
        raise RenameRefused(f"{name} changed since the preview.")
    reason = _refusal(node)
    if reason is None and op.role != "transform" and om.MFnDagNode(node).parentCount() != 1:
        reason = "It is now an instanced shape."
    if reason is not None:
        raise RenameRefused(f"{name} changed since the preview. {reason}")


def _postcondition(op: RenameOp, returned: str) -> str | None:
    """What went wrong with one rename, or None when Maya did exactly what was planned."""

    expected = op.target[1:]                 # the return keeps the namespace, no leading colon
    if not op.handle.isValid():
        return f"{op.original[1:]} disappeared during the rename."
    node = op.handle.object()
    fn = om.MFnDependencyNode(node)
    actual = fn.absoluteName()
    if actual != op.target:                  # Maya's silent suffix
        return f"Maya named {op.original[1:]} '{actual[1:]}' instead of '{expected}'."
    if not fn.hasUniqueName():               # a leaf held under another parent is never suffixed
        return f"'{expected}' is now used by more than one node."
    if fn.uuid().asString() != op.uuid:
        return f"{expected} changed its UUID during the rename."
    if len(om.MDagPath.getAllPathsTo(node)) == 1:
        matches = returned == expected
    else:                                    # an instance returns a partial path
        matches = returned.rsplit("|", 1)[-1] == expected
    if not matches:
        return f"Maya named {op.original[1:]} '{returned}' instead of '{expected}'."
    return None


def apply_rename(plan: RenamePlan, chosen: Iterable[int]) -> RenameOutcome:
    """Rename the chosen members in one ``AssetValidatorRename`` undo chunk.

    Raises RenameRefused before any undo entry exists. Never touches the
    report or the UI; the caller re-runs the checks.
    """

    chosen_set = frozenset(chosen)
    if not chosen_set:
        raise RenameRefused("Tick at least one node to rename.")
    by_index = {member.index: member for member in plan.members}
    if not chosen_set <= by_index.keys():
        raise RenameRefused("The choice names rows that are not in this plan.")
    for number in sorted(chosen_set):
        member = by_index[number]
        if member.refusal is not None:
            raise RenameRefused(f"{member.canonical_path}: {member.refusal}")
        if member.problem is not None:
            raise RenameRefused(f"{member.canonical_path}: {member.problem}")
        if not member.ops:
            raise RenameRefused(f"{member.canonical_path} has nothing to rename.")
    if cmds.namespace(query=True, relativeNames=True):
        raise RenameRefused(RELATIVE_NAMES_REFUSAL)
    refusal = undo_refusal("rename")
    if refusal is not None:
        raise RenameRefused(refusal)

    members = sorted(
        (by_index[number] for number in chosen_set),
        key=lambda member: (-member.canonical_path.count("|"), member.canonical_path),
    )
    ops = [op for member in members for op in member.ops]
    for op in ops:
        _verify(op)
    fresh = name_snapshot()
    for op in ops:
        if op.target in fresh:
            raise RenameRefused(
                f"The preview is out of date: '{op.target[1:]}' is now used; open Rename\u2026 again."
            )

    returned: list[str] = []
    error: str | None = None
    cmds.undoInfo(openChunk=True, chunkName=RENAME_CHUNK)
    try:
        for op in ops:
            source = om.MDagPath.getAPathTo(op.handle.object()).fullPathName()   # never a stored path
            if op.role == "transform":
                returned.append(cmds.rename(source, op.target, ignoreShape=True))
            else:
                returned.append(cmds.rename(source, op.target))
    except RuntimeError as exc:
        error = str(exc).strip()
    except Exception as exc:                 # still roll back; report what failed
        error = f"{type(exc).__name__}: {exc}"
    finally:
        cmds.undoInfo(closeChunk=True)
    # An open caller chunk keeps a name after ours closes (ours, when
    # the caller's had none), and undoName then still shows the entry before
    # it, possibly a previous rename. Undoing then would hit the wrong entry.
    caller_chunk_open = bool(cmds.undoInfo(query=True, chunkName=True))
    changed = any(_current(op) != op.original for op in ops)
    # Whether Maya recorded our chunk; without it neither the rollback nor
    # the artist's Ctrl+Z can restore the names.
    recorded = changed and not caller_chunk_open and (
        cmds.undoInfo(query=True, undoName=True) == RENAME_CHUNK
    )

    if error is None:
        for op, result in zip(ops, returned):
            error = _postcondition(op, result)
            if error is not None:
                break

    rolled_back = False
    restored: bool | None = None
    if error is not None and changed:
        if recorded:
            try:
                cmds.undo()
            except RuntimeError as exc:
                error += f" Undo failed: {str(exc).strip()}"
            rolled_back = True
            restored = all(_current(op) == op.original for op in ops)
        else:
            restored = False
            reason = ("another undo chunk is open" if caller_chunk_open
                      else "Maya did not record the rename for undo")
            error += f" It could not be undone automatically ({reason}); use Undo."
    return RenameOutcome(
        ok=error is None,
        renamed=tuple((op.original, op.target) for op in ops[:len(returned)]),
        returned=tuple(returned),
        error=error,
        rolled_back=rolled_back,
        restored=restored,
        undoable=recorded or caller_chunk_open,
    )


def undo_refusal(action: str) -> str | None:
    """Why Maya could not record ``action`` for undo, or None when it can.

    A finite undo queue of length 0 (Preferences > Undo) keeps the state on
    but records nothing, so the state query alone is not enough.
    """

    if not cmds.undoInfo(query=True, state=True):
        return f"Undo is turned off; the {action} could not be undone."
    if not cmds.undoInfo(query=True, infinity=True) and cmds.undoInfo(query=True, length=True) < 1:
        return (f"The undo queue holds no entries (Preferences > Undo > Queue size 0); "
                f"the {action} could not be undone.")
    return None


__all__ = [
    "LEAF_PATTERN", "MAX_NUMBER_TRIES", "RELATIVE_NAMES_REFUSAL", "RENAME_CHUNK",
    "RenameMember", "RenameOp", "RenameOutcome", "RenamePlan", "RenameRefused",
    "apply_rename", "name_snapshot", "plan_rename", "undo_refusal",
]
