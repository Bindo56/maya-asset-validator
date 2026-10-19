"""Scene safety for the window: staleness callbacks and verified Select.

``SceneWatcher`` only reports that the scene changed; it never edits the
scene or the report. Window-lifetime callbacks watch scene new/open.
Per-report callbacks watch:

- node-dirty on the scoped meshes and every ancestor;
- Undo/Redo, except undoing or redoing our own Select;
- renames, UUID changes, additions, removals and DAG changes (reparent, new
  instance) of transforms and meshes, the nodes in the duplicate-name index;
  namespace renames; history nodes such as deleteComponent are ignored;
- reference create, load, unload and remove.
- time and playback events. Static meshes remain fresh when only the frame
  changes; explicitly time-dependent targets become stale. A keyed transform
  need not send node-dirty on a frame change in Maya 2025.

A callback that cannot tell what changed marks the report stale. ``verify``
re-resolves watched paths on activation and before Select as an extra guard.

``resolve_issue`` re-resolves an Issue's full instance path and verifies the
DAG node behind it (same path, same UUID, same reference, and the same Maya
object as when the run started) before selecting. UUID plus reference
identity alone cannot tell nested or imported copies apart.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import weakref

import maya.api.OpenMaya as om
import maya.cmds as cmds

from asset_validator.data_models import ComponentType, Issue, TargetRef
from asset_validator.maya_adapter import reference_identity

SELECT_CHUNK = "AssetValidatorSelect"     # undo chunk name of our own Select

_LIVE_CALLBACKS: set[int] = set()


def live_callback_count() -> int:
    """Callbacks registered by every SceneWatcher and not yet removed."""

    return len(_LIVE_CALLBACKS)


def _add(callback_id: int, bucket: list[int]) -> None:
    bucket.append(callback_id)
    _LIVE_CALLBACKS.add(callback_id)


def _remove(bucket: list[int]) -> None:
    while bucket:
        callback_id = bucket.pop()
        _LIVE_CALLBACKS.discard(callback_id)
        try:
            om.MMessage.removeCallback(callback_id)
        except RuntimeError:
            pass          # already gone with its node


def _chain(transform_path: str) -> list[str]:
    parts = transform_path.split("|")[1:]
    return ["|" + "|".join(parts[:end]) for end in range(len(parts), 0, -1)]


def _node(path: str) -> om.MObject | None:
    try:
        selection = om.MSelectionList()
        selection.add(path)
        return selection.getDependNode(0)
    except RuntimeError:
        return None


def _is_indexed(node: om.MObject) -> bool:
    return node.hasFn(om.MFn.kTransform) or node.hasFn(om.MFn.kMesh)


_TIME_TYPES = frozenset({
    "time", "expression", "cacheFile", "AlembicNode", "diskCache", "motionPath",
    "particle", "nParticle", "fluidShape", "hairSystem", "nCloth", "nucleus",
})
_MAX_UPSTREAM_NODES = 4096


def _has_time_dependency(paths: Iterable[str]) -> bool:
    """Check the watched nodes' upstream DG once when the report starts.

    This follows explicit time, keyframe, expression and cache inputs.
    Deformers and plugin dependencies may evaluate by frame without a time
    plug, so they are conservatively considered dynamic. An unreadable or
    exceptionally large graph is dynamic for the same reason.
    """

    pending = list(paths)
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if len(seen) > _MAX_UPSTREAM_NODES:
            return True
        try:
            kind = cmds.nodeType(name)
            api_type = cmds.nodeType(name, apiType=True)
            inherited = cmds.nodeType(name, inherited=True)
            if (kind.startswith("animCurve") or kind in _TIME_TYPES
                    or "geometryFilter" in inherited or api_type.startswith("kPlugin")):
                return True
            pending.extend(cmds.listConnections(
                name, source=True, destination=False, skipConversionNodes=True,
            ) or ())
        except Exception:
            return True
    return False


class SceneWatcher:
    """Calls ``on_stale`` when a watched part of the scene changes."""

    def __init__(self, on_stale: Callable[[], None]):
        # Maya keeps the callbacks, and they keep this watcher alive; hold the
        # window's method weakly so a closed window can still be freed.
        try:
            self._on_stale_ref = weakref.WeakMethod(on_stale)
        except TypeError:
            self._on_stale_ref = lambda: on_stale
        self._armed = False
        self._scene_ids: list[int] = []
        self._report_ids: list[int] = []
        self._handles: dict[str, om.MObjectHandle] = {}
        self._time_sensitive = False
        for message in (om.MSceneMessage.kBeforeNew, om.MSceneMessage.kBeforeOpen):
            _add(om.MSceneMessage.addCallback(message, self._scene_changed), self._scene_ids)

    @property
    def watching_report(self) -> bool:
        return bool(self._report_ids)

    @property
    def callback_count(self) -> int:
        return len(self._scene_ids) + len(self._report_ids)

    @property
    def handles(self) -> Mapping[str, om.MObjectHandle]:
        """The watched nodes as they were when the run started, by full path."""

        return self._handles

    def watch(
        self, targets: Iterable[TargetRef], *, include_mesh_siblings: bool = False,
    ) -> None:
        """Watch scoped nodes and, for C, every mesh sibling it examines."""

        self.clear_report_callbacks()
        paths: set[str] = set()
        for target in targets:
            paths.add(target.shape_path)
            paths.update(_chain(target.transform_path))
            if include_mesh_siblings:
                # C uses every mesh child to decide the live-shape count,
                # including children outside a shape-only selection. Watch
                # intermediate siblings too: toggling intermediateObject can
                # change C's verdict without renaming or changing the DAG.
                paths.update(cmds.listRelatives(
                    target.transform_path, shapes=True, type="mesh",
                    fullPath=True,
                ) or ())
        for path in sorted(paths):
            node = _node(path)
            if node is None:
                continue
            self._handles[path] = om.MObjectHandle(node)
            _add(om.MNodeMessage.addNodeDirtyCallback(node, self._changed), self._report_ids)
        self._time_sensitive = _has_time_dependency(self._handles)
        _add(om.MEventMessage.addEventCallback("Undo", self._undone), self._report_ids)
        _add(om.MEventMessage.addEventCallback("Redo", self._redone), self._report_ids)
        _add(om.MNodeMessage.addNameChangedCallback(om.MObject.kNullObj, self._node_event),
             self._report_ids)
        _add(om.MNodeMessage.addUuidChangedCallback(om.MObject.kNullObj, self._node_event),
             self._report_ids)
        _add(om.MSceneMessage.addNamespaceRenamedCallback(self._changed), self._report_ids)
        _add(om.MDGMessage.addNodeAddedCallback(self._node_event, "dagNode"), self._report_ids)
        _add(om.MDGMessage.addNodeRemovedCallback(self._node_event, "dagNode"), self._report_ids)
        _add(om.MDagMessage.addAllDagChangesCallback(self._dag_changed), self._report_ids)
        for message in (
            om.MSceneMessage.kAfterCreateReference, om.MSceneMessage.kAfterLoadReference,
            om.MSceneMessage.kAfterUnloadReference, om.MSceneMessage.kAfterRemoveReference,
        ):
            _add(om.MSceneMessage.addCallback(message, self._changed), self._report_ids)
        _add(om.MEventMessage.addEventCallback("timeChanged", self._time_changed), self._report_ids)
        _add(om.MConditionMessage.addConditionCallback("playingBack", self._playing_changed),
             self._report_ids)
        self._armed = True

    def verify(self) -> bool:
        """True while every watched path still names the node it named at the start."""

        for path, handle in self._handles.items():
            node = _node(path)
            if node is None or not handle.isValid() or node != handle.object():
                return False
        return True

    def clear_report_callbacks(self) -> None:
        self._armed = False
        _remove(self._report_ids)
        self._handles.clear()
        self._time_sensitive = False

    def close(self) -> None:
        self.clear_report_callbacks()
        _remove(self._scene_ids)

    # Callbacks only mark the report stale. Maya prints and swallows a
    # callback's exception, so a filter that fails marks the report stale.
    # The first one disarms the rest: an import can rename thousands of
    # nodes before the window gets to remove them.
    def mark_stale(self) -> None:
        if not self._armed:
            return
        self._armed = False
        on_stale = self._on_stale_ref()
        if on_stale is not None:
            on_stale()

    def _changed(self, *_args) -> None:
        self.mark_stale()

    def _node_event(self, node, *_args) -> None:
        try:
            relevant = _is_indexed(node)
        except Exception:
            relevant = True
        if relevant:
            self.mark_stale()

    def _dag_changed(self, _message, child, *_args) -> None:
        try:
            relevant = _is_indexed(child.node())
        except Exception:
            relevant = True
        if relevant:
            self.mark_stale()

    def _time_changed(self, *_args) -> None:
        if self._time_sensitive:
            self.mark_stale()

    def _playing_changed(self, is_playing, *_args) -> None:
        if is_playing and self._time_sensitive:
            self.mark_stale()

    def _undone(self, *_args) -> None:
        # After an undo, the chunk just undone is the one Redo would replay.
        if self._own_select("redoName"):
            return
        self.mark_stale()

    def _redone(self, *_args) -> None:
        if self._own_select("undoName"):
            return
        self.mark_stale()

    @staticmethod
    def _own_select(flag: str) -> bool:
        try:
            return cmds.undoInfo(query=True, **{flag: True}) == SELECT_CHUNK
        except Exception:
            return False

    def _scene_changed(self, *_args) -> None:
        # The watched nodes are about to go away: stale, and stop watching them.
        self.mark_stale()
        self.clear_report_callbacks()


class SelectRefused(Exception):
    """The Issue's target cannot be verified, so nothing was selected."""


_COMPONENT = {
    ComponentType.VERTEX: ("vtx", "vertex"),
    ComponentType.EDGE: ("e", "edge"),
    ComponentType.FACE: ("f", "face"),
    ComponentType.UV: ("map", "uvcoord"),
}


def _ranges(indices: tuple[int, ...]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for index in indices:
        if spans and index == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], index)
        else:
            spans.append((index, index))
    return spans


def resolve_issue(issue: Issue, handles: Mapping[str, om.MObjectHandle] | None = None) -> list[str]:
    """Selection strings for an Issue, or SelectRefused if its target changed.

    With ``handles`` (``SceneWatcher.handles``), the path must also still name
    the same Maya object it named when the run started.
    """

    path = issue.target_path
    if not path.startswith("|"):
        raise SelectRefused(f"{path!r} is not a full DAG path")
    if cmds.ls(path, long=True) != [path]:
        raise SelectRefused(f"{path} no longer exists, was renamed, or is ambiguous")
    if cmds.ls(path, uuid=True) != [issue.target_uuid]:
        raise SelectRefused(f"{path} now names a different node than the one validated")
    selection = om.MSelectionList()
    selection.add(path)
    if selection.getDagPath(0).fullPathName() != path:
        raise SelectRefused(f"{path} resolves to another instance path")
    if handles is not None:
        handle = handles.get(path)
        if handle is None:
            raise SelectRefused(f"{path} was not recorded when the checks ran")
        if not handle.isValid() or handle.object() != selection.getDependNode(0):
            raise SelectRefused(f"{path} now names a different node than the one validated")
    if issue.reference_identity is not None and reference_identity(path) != issue.reference_identity:
        raise SelectRefused(f"{path} now belongs to a different reference")
    if issue.component_type is None or not issue.component_indices:
        return [path]
    suffix, count_flag = _COMPONENT[issue.component_type]
    available = cmds.polyEvaluate(path, **{count_flag: True})
    if not isinstance(available, int) or issue.component_indices[-1] >= available:
        raise SelectRefused(f"{path} has fewer {count_flag}s than when it was validated")
    return [
        f"{path}.{suffix}[{first}]" if first == last else f"{path}.{suffix}[{first}:{last}]"
        for first, last in _ranges(issue.component_indices)
    ]


def select_issues(
    issues: Iterable[Issue], *, stale: bool,
    handles: Mapping[str, om.MObjectHandle] | None = None,
) -> tuple[list[str], list[str]]:
    """Select every verified target; return (selected items, refusal reasons).

    A stale report is refused outright: its components may no longer match.
    Nothing is selected unless at least one target verifies. The selection is
    one named undo chunk, so undoing it does not make the report stale.
    """

    if stale:
        raise SelectRefused("The report is stale; run the checks again before selecting.")
    items: list[str] = []
    refused: list[str] = []
    for issue in issues:
        try:
            items.extend(resolve_issue(issue, handles))
        except SelectRefused as exc:
            refused.append(str(exc))
    if items:
        cmds.undoInfo(openChunk=True, chunkName=SELECT_CHUNK)
        try:
            cmds.select(items, replace=True)
        finally:
            cmds.undoInfo(closeChunk=True)
    return items, refused


__all__ = [
    "SELECT_CHUNK", "SceneWatcher", "SelectRefused", "live_callback_count", "resolve_issue",
    "select_issues",
]
