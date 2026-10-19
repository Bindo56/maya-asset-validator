"""Which mesh a stored overlap decision belongs to.

The key is the shape UUID plus the UUIDs of every reference node from the top
reference down to the innermost one:

- UUID plus the innermost reference fails for nested copies: ``n1:inBRN`` and
  ``n2:inBRN`` share one UUID, so ``TargetRef.reference_identity`` is never
  used here;
- UUID plus the top reference fails when one reference holds a file twice;
- the full chain separates every copy and survives .ma/.mb reopen, Save As,
  rename, reparent, a namespace change and unload/reload.

Imported copies (``importReference``) share every UUID and have an empty
chain, so the key cannot tell them apart. That is why a decision applies only
when ``holders(key)`` is exactly one node and that node is the verified issue
node; anything else is ambiguous and fails closed.

Importing both parents of a nested reference leaves two top-level reference
nodes with one UUID. An unloaded twin hides its mesh from ``holders`` but not
its reference node, so ``reference_twins`` counts reference nodes, loaded or
not, and a key whose chain is shared that way is ambiguous too.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
import mmap
import os
import re
from typing import Literal

import maya.api.OpenMaya as om
import maya.cmds as cmds


# A chain deeper than this is a cycle or a corrupt scene, never a real nest.
_MAX_REFERENCE_DEPTH = 256
_UID_LINE = re.compile(
    rb'rename -uid "([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})";'
)
_MA_SCAN_CACHE: dict[tuple[str, int, int], frozenset[str]] = {}
_MA_SCAN_CACHE_LIMIT = 16
_ASCII_HEADER = b"//Maya ASCII"


class IdentityError(Exception):
    """A reference chain could not be read; the decision is neither applied nor dropped."""


@dataclass(frozen=True, order=True)
class IdentityKey:
    shape_uuid: str
    ref_chain: tuple[str, ...]       # reference-node UUIDs, top -> innermost; () if local


def _one_uuid(node: str) -> str:
    found = cmds.ls(node, uuid=True) or []
    if len(found) != 1:
        raise IdentityError(f"Could not resolve one UUID for {node!r}: {found!r}")
    return found[0]


def reference_chain(path: str) -> tuple[str, ...]:
    """Reference-node UUIDs from the top reference to the innermost; () for local nodes."""

    try:
        if not cmds.referenceQuery(path, isNodeReferenced=True):
            return ()
    except RuntimeError:
        return ()
    chain: list[str] = []
    try:
        reference = cmds.referenceQuery(path, referenceNode=True)
        while reference:
            if len(chain) >= _MAX_REFERENCE_DEPTH:
                raise IdentityError(f"Reference chain of {path!r} does not end")
            chain.append(_one_uuid(reference))
            reference = cmds.referenceQuery(reference, referenceNode=True, parent=True)
    except RuntimeError as exc:
        raise IdentityError(f"Could not read the reference chain of {path!r}: {exc}") from exc
    return tuple(reversed(chain))


def identity_key(node: om.MObject) -> IdentityKey:
    """The persistent key of one mesh node (instances share it)."""

    path = om.MDagPath.getAPathTo(node).fullPathName()
    return IdentityKey(om.MFnDependencyNode(node).uuid().asString(), reference_chain(path))


def _node(path: str) -> om.MObject:
    selection = om.MSelectionList()
    selection.add(path)
    return selection.getDependNode(0)


def holders(key: IdentityKey) -> tuple[om.MObject, ...]:
    """Every node in the scene that carries this exact key.

    ``ls(uuid)`` lists one path per node, so instances count once. A decision
    applies only when this is exactly one node and that node is the issue's.
    """

    found: list[om.MObject] = []
    for path in cmds.ls(key.shape_uuid, long=True) or ():
        if reference_chain(path) != key.ref_chain:
            continue
        try:
            node = _node(path)
        except RuntimeError as exc:
            raise IdentityError(f"Could not resolve {path!r}: {exc}") from exc
        if not any(node == other for other in found):
            found.append(node)
    return tuple(found)


_FAILED = object()


def _query(reference: str, **flags):
    try:
        return cmds.referenceQuery(reference, **flags)
    except RuntimeError:
        return _FAILED


def _uuid_or_none(node: str) -> str | None:
    found = cmds.ls(node, uuid=True) or []
    return found[0] if len(found) == 1 else None


def reference_tops() -> dict[str, list[str]]:
    """Top-level file reference nodes by UUID, loaded or not.

    One listing serves every key of a bind or verification pass.
    """

    tops: dict[str, list[str]] = {}
    for reference in cmds.ls(type="reference") or ():
        if _query(reference, filename=True) is _FAILED:
            continue                      # sharedReferenceNode and other non-file nodes
        if _query(reference, referenceNode=True, parent=True) is not None:
            continue                      # nested, or unreadable
        uuid = _uuid_or_none(reference)
        if uuid is not None:
            tops.setdefault(uuid, []).append(reference)
    return tops


ChainState = Literal["loaded", "unloaded", "missing", "shared"]


def chain_state(
    key: IdentityKey, tops: dict[str, list[str]] | None = None,
) -> tuple[ChainState, int]:
    """Walk the key's reference chain by position, top first.

    The walk is positional because sibling copies of one nested file share
    their inner reference-node UUID. A level held by more than one reference
    node, loaded or not, is "shared" (with that count); the first unloaded
    level is "unloaded"; a level that is not found is "missing".
    """

    if not key.ref_chain:
        return "loaded", 0
    if tops is None:
        tops = reference_tops()
    current = ""
    for depth, wanted in enumerate(key.ref_chain):
        if depth:
            children = _query(current, child=True, referenceNode=True)
            if children is _FAILED:
                return "missing", 0
            matches = [child for child in children or () if _uuid_or_none(child) == wanted]
        else:
            matches = list(tops.get(wanted, ()))
        if len(matches) > 1:
            return "shared", len(matches)
        if not matches:
            return "missing", 0
        current = matches[0]
        loaded = _query(current, isLoaded=True)
        if loaded is _FAILED:
            return "missing", 0
        if not loaded:
            return "unloaded", 0
    return "loaded", 0


def reference_twins(key: IdentityKey, tops: dict[str, list[str]] | None = None) -> int:
    """Reference nodes sharing one level of the key's chain; 0 when none do.

    importReference turns the imported file's child references into top-level
    nodes that keep their UUIDs, so two copies can end with one chain. Such a
    key names whichever copy is loaded, so it is ambiguous even with one holder.
    """

    state, count = chain_state(key, tops)
    return count if state == "shared" else 0


def absence_reason(key: IdentityKey) -> Literal["unloaded"] | None:
    """"unloaded" when the key's mesh is hidden behind an unloaded reference.

    Every level must be found exactly once (``chain_state``); otherwise the
    mesh is simply missing (None).
    """

    return "unloaded" if chain_state(key)[0] == "unloaded" else None


def absolute_dag_path(node: om.MObject) -> str:
    """Full path built from absolute names, e.g. ``|:r1:SM_B_01|:r1:SM_B_01Shape``.

    Unlike ``fullPathName`` it does not depend on relativeNames mode, so a
    witness written in one session compares equal in another.
    """

    dag_path = om.MDagPath.getAPathTo(node)
    parts: list[str] = []
    function_set = om.MFnDependencyNode()
    while dag_path.length() > 0:
        function_set.setObject(dag_path.node())
        parts.append(function_set.absoluteName())
        dag_path.pop()
    return "|" + "|".join(reversed(parts))


def reference_files(path: str) -> tuple[str, ...]:
    """Referenced file names along the chain, top first; for display only."""

    try:
        if not cmds.referenceQuery(path, isNodeReferenced=True):
            return ()
        names: list[str] = []
        reference = cmds.referenceQuery(path, referenceNode=True)
        while reference and len(names) < _MAX_REFERENCE_DEPTH:
            filename = cmds.referenceQuery(
                reference, filename=True, unresolvedName=True, withoutCopyNumber=True,
            )
            names.append(os.path.basename(filename))
            reference = cmds.referenceQuery(reference, referenceNode=True, parent=True)
    except RuntimeError:
        return ()
    return tuple(reversed(names))


def is_maya_ascii(path: str) -> bool:
    """Whether the scene file is Maya ASCII, judged by its header, not its name.

    An ASCII scene saved under another extension needs the duplicate scan as
    much as a ``.ma``. A file that cannot be read raises OSError.
    """

    with open(path, "rb") as handle:
        return handle.read(len(_ASCII_HEADER)) == _ASCII_HEADER


def main_file_uuids(key: IdentityKey) -> tuple[str, ...]:
    """The key's UUIDs that the main scene file itself assigns.

    A local mesh's shape UUID is written there. A referenced mesh's comes from
    its own file, and so do its nested reference nodes, so only the top-level
    reference node (``ref_chain[0]``) is the main file's. A nested level's UUID
    may legitimately appear in the main file on imported copies of a parent
    asset; ``chain_state`` walks those levels by position and reports a shared
    child itself.
    """

    return key.ref_chain[:1] if key.ref_chain else (key.shape_uuid,)


def ma_duplicate_uuids(path: str, wanted: Iterable[str]) -> frozenset[str]:
    """The wanted UUIDs that a .ma file assigns ("rename -uid") more than once.

    Opening such a file gives the later copy a fresh random UUID, so the scene
    in memory looks clean while a stored key may now name the other copy.
    Results are cached per file size and mtime.
    """

    wanted = frozenset(wanted)
    stat = os.stat(path)
    cache_key = (os.path.normcase(os.path.abspath(path)), stat.st_size, stat.st_mtime_ns)
    duplicated = _MA_SCAN_CACHE.get(cache_key)
    if duplicated is None:
        counts: Counter[bytes] = Counter()
        with open(path, "rb") as handle:
            if os.fstat(handle.fileno()).st_size:
                with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                    counts.update(match.group(1) for match in _UID_LINE.finditer(data))
        duplicated = frozenset(
            value.decode("ascii") for value, count in counts.items() if count > 1
        )
        if len(_MA_SCAN_CACHE) >= _MA_SCAN_CACHE_LIMIT:
            _MA_SCAN_CACHE.clear()
        _MA_SCAN_CACHE[cache_key] = duplicated
    return duplicated & wanted


__all__ = [
    "ChainState", "IdentityError", "IdentityKey", "absence_reason", "absolute_dag_path",
    "chain_state", "holders", "identity_key", "is_maya_ascii", "ma_duplicate_uuids",
    "main_file_uuids", "reference_chain", "reference_files", "reference_tops",
    "reference_twins",
]
