"""New Identity: give one imported copy its own Maya UUID.

``importReference`` copies share every UUID and every attribute, so no
read-only identity can tell them apart and no overlap decision can be stored
for them. After the artist confirms, this renames the UUID of ONE copy's
shape (``rename -uuid``) inside one named undo chunk. Names and geometry do
not change, and one Undo restores the old UUID.

``rename -uuid`` is undoable and redoable, persists
through .ma/.mb reopen, refuses referenced and locked nodes, and neither
dirties the scene nor fires a name, dirty or DAG callback, so the caller sets
the modified flag here and the window marks its report stale itself.
Rejected alternative: dynamic-attribute stamps (duplicate and import copy
them, they add reference edits, and their undo takes two steps).

Every refusal happens before the chunk opens, including undo turned off and a
finite undo queue of length 0; a failed postcondition is rolled back with at
most one guarded ``cmds.undo()``, only when our chunk is the last one. If the
chunk is not the last undo step afterwards, the outcome says so (``ok`` is
False, ``restored`` False) instead of promising Ctrl+Z. No Qt here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import uuid as uuid_module

import maya.api.OpenMaya as om
import maya.cmds as cmds

from asset_validator.data_models import Issue
from asset_validator.intent.identity import IdentityError, IdentityKey, holders, identity_key
from asset_validator.intent.store import IntentStore
from asset_validator.ui.scene import SelectRefused, resolve_issue


IDENTITY_CHUNK = "AssetValidatorIdentity"


class IdentityRefused(Exception):
    """Nothing changed and no undo entry was made; the message says why."""


@dataclass(frozen=True, eq=False)
class IdentityPlan:
    shape_path: str
    handle: om.MObjectHandle
    old_key: IdentityKey
    holder_paths: tuple[str, ...]          # every copy sharing the old key
    new_uuid: str


@dataclass(frozen=True)
class IdentityOutcome:
    ok: bool
    old_uuid: str
    new_uuid: str
    error: str | None
    rolled_back: bool
    restored: bool | None                  # None when nothing had changed


def _refusal(node: om.MObject, path: str) -> str | None:
    """Maya's own wording for why this node's UUID cannot be renamed."""

    fn = om.MFnDependencyNode(node)
    if fn.isFromReferencedFile or fn.isShared or fn.isDefaultNode:
        return "Cannot rename a read only node."
    if (cmds.lockNode(path, query=True, lock=True) or [False])[0]:
        return "Cannot rename a locked node."
    if (cmds.lockNode(path, query=True, lockName=True) or [False])[0]:
        return "Cannot rename a node with a locked name."
    return None


def _undo_refusal() -> str | None:
    """Why the undo queue could not hold the change as one step, or None.

    A finite queue of length 0 is "on" but keeps nothing: the rename applies
    and Undo then says there are no more commands to undo.
    """

    if not cmds.undoInfo(query=True, state=True):
        return "Undo is turned off; the new identity could not be undone."
    if not cmds.undoInfo(query=True, infinity=True) and cmds.undoInfo(query=True, length=True) < 1:
        return ("The undo queue holds no steps (its length is 0); the new identity "
                "could not be undone.")
    return None


def _not_undoable(old_uuid: str) -> str:
    """Why our chunk is not the last undo step, without guessing.

    ``undoInfo -q -chunkName`` names an open chunk; an unnamed one reads as
    "" like no chunk at all, so only a named chunk is ever claimed.
    """

    open_chunk = cmds.undoInfo(query=True, chunkName=True) or ""
    if open_chunk:
        return (f"it went into the undo chunk {open_chunk!r}, which is still open; "
                f"use Undo after it closes to restore the old UUID {old_uuid}.")
    return (f"Maya did not record it for undo as its own step, so Ctrl+Z may not "
            f"restore the old UUID {old_uuid}.")


def _free_uuid() -> str:
    while True:
        candidate = str(uuid_module.uuid4()).upper()
        if not cmds.ls(candidate):
            return candidate


def plan_new_identity(
    issue: Issue, *, handles: Mapping[str, om.MObjectHandle] | None = None,
) -> IdentityPlan:
    """Read-only: verify the issue's mesh is a duplicated local copy, or refuse."""

    try:
        resolve_issue(issue, handles)
    except SelectRefused as exc:
        raise IdentityRefused(str(exc)) from exc
    if handles is not None:
        node = handles[issue.target_path].object()
    else:
        selection = om.MSelectionList()
        selection.add(issue.target_path)
        node = selection.getDependNode(0)
    try:
        key = identity_key(node)
        found = holders(key)
    except IdentityError as exc:
        raise IdentityRefused(f"The mesh's identity could not be read: {exc}") from exc
    if key.ref_chain:
        raise IdentityRefused(
            "Referenced copies cannot be given a new identity; re-reference instead of importing."
        )
    if len(found) < 2:
        raise IdentityRefused("This mesh already has its own identity.")
    if not any(other == node for other in found):
        raise IdentityRefused("The mesh's identity could not be verified; run the checks again.")
    undo_refusal = _undo_refusal()
    if undo_refusal is not None:
        raise IdentityRefused(undo_refusal)
    path = om.MDagPath.getAPathTo(node).fullPathName()
    refusal = _refusal(node, path)
    if refusal is not None:
        raise IdentityRefused(refusal)
    return IdentityPlan(
        shape_path=issue.target_path,
        handle=om.MObjectHandle(node),
        old_key=key,
        holder_paths=tuple(om.MDagPath.getAPathTo(other).fullPathName() for other in found),
        new_uuid=_free_uuid(),
    )


def apply_new_identity(plan: IdentityPlan, store: IntentStore | None) -> IdentityOutcome:
    """Rename this one shape's UUID in the ``AssetValidatorIdentity`` chunk."""

    old_uuid = plan.old_key.shape_uuid
    if not plan.handle.isValid():
        raise IdentityRefused(f"{plan.shape_path} changed since the preview.")
    node = plan.handle.object()
    fn = om.MFnDependencyNode(node)
    if fn.uuid().asString() != old_uuid:
        raise IdentityRefused(f"{plan.shape_path} changed since the preview.")
    undo_refusal = _undo_refusal()
    if undo_refusal is not None:
        raise IdentityRefused(undo_refusal)
    path_now = om.MDagPath.getAPathTo(node).fullPathName()
    refusal = _refusal(node, path_now)
    if refusal is not None:
        raise IdentityRefused(refusal)
    new_uuid = plan.new_uuid if not cmds.ls(plan.new_uuid) else _free_uuid()

    error: str | None = None
    cmds.undoInfo(openChunk=True, chunkName=IDENTITY_CHUNK)
    try:
        cmds.rename(path_now, new_uuid, uuid=True)       # this shape only
    except RuntimeError as exc:
        error = str(exc).strip()
    finally:
        cmds.undoInfo(closeChunk=True)

    if error is None:
        try:
            now = fn.uuid().asString()
            found = holders(IdentityKey(new_uuid, ()))
            if now != new_uuid:
                error = f"Maya gave {plan.shape_path} UUID {now} instead of {new_uuid}."
            elif len(found) != 1 or found[0] != node:
                error = f"UUID {new_uuid} is not held by exactly this mesh."
        except Exception as exc:
            error = f"The new identity could not be verified: {type(exc).__name__}: {exc}"

    rolled_back = False
    restored: bool | None = None
    changed = fn.uuid().asString() != old_uuid
    recorded = cmds.undoInfo(query=True, undoName=True) == IDENTITY_CHUNK
    if error is not None:
        if changed and recorded:
            try:
                cmds.undo()
            except RuntimeError:
                pass
            rolled_back = True
            restored = fn.uuid().asString() == old_uuid
        elif changed:
            restored = False
            error += " It could not be undone automatically: " + _not_undoable(old_uuid)
    elif changed and not recorded:
        # Applied and verified, but never promise Ctrl+Z without our own step.
        restored = False
        error = f"{plan.shape_path} now has UUID {new_uuid}, but " + _not_undoable(old_uuid)
    if error is None or restored is False:
        cmds.file(modified=True)       # rename -uuid does not dirty the scene
    # The copy keeps its new UUID whenever the change was not undone.
    if (store is not None and fn.uuid().asString() != old_uuid
            and store.lookup(plan.old_key) is not None):
        store.drop(plan.old_key, "the copy was given a new identity")
    return IdentityOutcome(error is None, old_uuid, new_uuid, error, rolled_back, restored)


__all__ = [
    "IDENTITY_CHUNK", "IdentityOutcome", "IdentityPlan", "IdentityRefused",
    "apply_new_identity", "plan_new_identity",
]
