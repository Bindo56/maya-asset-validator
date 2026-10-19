"""Report non-manifold edges and vertices on each scoped mesh shape.

Maya's ``polyInfo`` catches inconsistent face winding as well as edges with
more than two faces. It also detects vertex-only fans, which an edge-only
topology walk misses. ``polyInfo`` can return partial *transform* paths and
compressed component ranges even when given a full shape path, so its path is
never used as an identity or a selection target.
"""

from __future__ import annotations

from collections.abc import Iterable
import re
from typing import cast

from asset_validator.data_models import (
    CheckMetadata,
    ComponentType,
    Issue,
    Severity,
    TargetRef,
    ValidationContext,
)


_RAW_COMPONENT = re.compile(r"\.(?P<kind>e|vtx)\[(?P<first>\d+)(?::(?P<last>\d+))?\]")
_COMPONENT_START = re.compile(r"\.(?:e|vtx|f)\[")
_FLAT_COMPONENT = re.compile(r"\.(?P<kind>e|vtx)\[(?P<index>\d+)\]$")


def _component_indices(cmds: object, shape_path: str, flag: str, kind: str) -> tuple[int, ...]:
    """Expand ``polyInfo`` ranges, retaining only indices for ``shape_path``.

    The names in both ``polyInfo`` and ``cmds.ls`` output may refer to the
    transform rather than the queried shape. An unexpected nonempty result
    is an execution error, never a clean result.
    """

    raw = cmds.polyInfo(shape_path, **{flag: True})
    if not raw:
        return ()
    if isinstance(raw, str):
        raw = (raw,)

    rebuilt: list[str] = []
    expected: set[int] = set()
    for line in raw:
        if not isinstance(line, str):
            raise ValueError(f"Unexpected {flag} output for {shape_path!r}: {line!r}")
        matches = tuple(_RAW_COMPONENT.finditer(line))
        if not matches or len(matches) != len(_COMPONENT_START.findall(line)):
            raise ValueError(f"Unexpected {flag} output for {shape_path!r}: {line!r}")
        for match in matches:
            if match.group("kind") != kind:
                raise ValueError(f"Unexpected {flag} output for {shape_path!r}: {line!r}")
            first = int(match.group("first"))
            last = int(match.group("last") or match.group("first"))
            if last < first:
                raise ValueError(f"Reversed {flag} range for {shape_path!r}: {line!r}")
            suffix = f".{kind}[{first}:{last}]" if last != first else f".{kind}[{first}]"
            rebuilt.append(shape_path + suffix)
            expected.update(range(first, last + 1))

    flattened = cmds.ls(rebuilt, flatten=True) or ()
    found: set[int] = set()
    for component in flattened:
        if not isinstance(component, str):
            raise ValueError(f"Unexpected flattened {flag} component: {component!r}")
        match = _FLAT_COMPONENT.search(component)
        if match is None or match.group("kind") != kind:
            raise ValueError(f"Unexpected flattened {flag} component: {component!r}")
        found.add(int(match.group("index")))
    if found != expected:
        raise ValueError(
            f"Could not expand all {flag} components for {shape_path!r}: "
            f"expected {sorted(expected)!r}, found {sorted(found)!r}"
        )
    return tuple(sorted(found))


class NonManifoldRule:
    """Emit one Error per nonempty edge or vertex component kind."""

    check_id = "N"
    metadata = CheckMetadata(
        label="Non-manifold geometry",
        hint=(
            "Look for edges shared by too many faces, disconnected face fans, "
            "or faces wound against their neighbours. Check normals; use Conform "
            "or Reverse for incorrect winding, then repair remaining topology."
        ),
        can_fix=False,
    )

    def prepare(self, context: ValidationContext) -> frozenset[TargetRef]:
        settings = context.settings.get(self.check_id, {})
        if settings:
            raise ValueError(f"N does not accept settings: {sorted(settings)!r}")
        # Instances share one MObject despite having multiple DAG paths. Two
        # distinct meshes can share both UUID and inner reference-node UUID,
        # so derive the key from the live DAG object instead of those values.
        from maya.api import OpenMaya as om

        owners: dict[str, TargetRef] = {}
        for target in context.targets:
            selection = om.MSelectionList()
            selection.add(target.shape_path)
            obj = selection.getDagPath(0).node()
            key = om.MDagPath.getAPathTo(obj).fullPathName()
            owners.setdefault(key, target)
        return frozenset(owners.values())

    def run_target(
        self,
        context: ValidationContext,
        target: TargetRef,
        prepared: object,
    ) -> Iterable[Issue]:
        del context
        if target not in cast(frozenset[TargetRef], prepared):
            return
        from maya import cmds

        for flag, kind, component_type, singular, plural in (
            ("nonManifoldEdges", "e", ComponentType.EDGE, "edge", "edges"),
            ("nonManifoldVertices", "vtx", ComponentType.VERTEX, "vertex", "vertices"),
        ):
            indices = _component_indices(cmds, target.shape_path, flag, kind)
            if indices:
                label = singular if len(indices) == 1 else plural
                yield Issue(
                    check_id=self.check_id,
                    severity=Severity.ERROR,
                    message=f"Mesh has {len(indices)} non-manifold {label}.",
                    target_path=target.shape_path,
                    target_uuid=target.shape_uuid,
                    reference_identity=target.reference_identity,
                    component_type=component_type,
                    component_indices=indices,
                )


__all__ = ["NonManifoldRule"]
