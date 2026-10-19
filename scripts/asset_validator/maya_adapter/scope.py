"""Resolve Maya mesh scope without changing the artist's selection."""

from __future__ import annotations

from collections.abc import Sequence

import maya.cmds as cmds

from asset_validator.data_models import ScopeMode, TargetRef, ValidationContext


def _mode(value: ScopeMode | str) -> ScopeMode:
    if isinstance(value, ScopeMode):
        return value
    for mode in ScopeMode:
        if value.casefold() in (mode.value.casefold(), mode.name.casefold()):
            return mode
    raise ValueError(f"Unknown validation scope: {value!r}")


def _selected_objects(selection: Sequence[str] | None) -> tuple[str, ...]:
    if selection is None:
        paths = cmds.ls(selection=True, objectsOnly=True, long=True) or []
    elif not selection:
        paths = []
    else:
        paths = cmds.ls(list(selection), objectsOnly=True, long=True) or []
    return tuple(dict.fromkeys(paths))


def _mesh_paths(mode: ScopeMode, selected: tuple[str, ...]) -> tuple[str, ...]:
    if mode == ScopeMode.SCENE:
        # allPaths preserves each instance's path. The shorter documented call
        # without it exposes only the first path of an instanced shape.
        paths = cmds.ls(type="mesh", long=True, noIntermediate=True, allPaths=True) or []
        return tuple(sorted(set(paths)))

    found = set()
    for node in selected:
        if not cmds.objExists(node):
            continue
        if mode == ScopeMode.HIERARCHY:
            candidates = cmds.ls(
                node, dag=True, allPaths=True, type="mesh",
                long=True, noIntermediate=True,
            ) or []
            # Maya expands *every* path of an instanced descendant here, even
            # paths under an unselected parent. Keep this selected branch only.
            branch = node + "|"
            found.update(
                path for path in candidates
                if path == node or path.startswith(branch)
            )
        elif cmds.nodeType(node) == "mesh":
            if not cmds.getAttr(node + ".intermediateObject"):
                found.add(node)
        else:
            found.update(
                cmds.listRelatives(node, shapes=True, type="mesh",
                                   fullPath=True, noIntermediate=True) or []
            )
    return tuple(sorted(found))


def _uuid(path: str) -> str:
    uuids = cmds.ls(path, uuid=True) or []
    if len(uuids) != 1:
        raise RuntimeError(f"Could not resolve one UUID for {path!r}: {uuids!r}")
    return uuids[0]


def _reference_identity(path: str) -> str | None:
    try:
        is_referenced = cmds.referenceQuery(path, isNodeReferenced=True)
    except RuntimeError:
        return None
    if not is_referenced:
        return None
    reference_node = cmds.referenceQuery(path, referenceNode=True)
    return _uuid(reference_node)


def resolve_scope(
    mode: ScopeMode | str = ScopeMode.SELECTION,
    *,
    selection: Sequence[str] | None = None,
) -> ValidationContext:
    """Return live, non-intermediate mesh instances in the requested scope.

    Selection includes selected mesh shapes and direct mesh children of
    selected transforms. Hierarchy includes descendant meshes. An empty
    Selection or Hierarchy remains empty; it never expands to the scene.
    """
    resolved_mode = _mode(mode)
    selected = _selected_objects(selection) if resolved_mode != ScopeMode.SCENE else ()
    paths = _mesh_paths(resolved_mode, selected)
    targets = []
    for shape_path in paths:
        transform_path = shape_path.rsplit("|", 1)[0]
        targets.append(
            TargetRef(
                shape_path=shape_path,
                shape_uuid=_uuid(shape_path),
                transform_path=transform_path,
                transform_uuid=_uuid(transform_path),
                reference_identity=_reference_identity(shape_path),
            )
        )
    return ValidationContext(
        mode=resolved_mode,
        targets=tuple(targets),
        selection=selected,
        scene_path=cmds.file(query=True, sceneName=True) or None,
    )
