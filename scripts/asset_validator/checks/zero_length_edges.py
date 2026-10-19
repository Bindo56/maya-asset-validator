"""Report collapsed and short mesh edges from one object-space point snapshot.

The O check excludes vertex pairs joined by an edge; E owns those pairs. Both
checks use O's L = max(min_distance_cm, bbox_fraction * bbox diagonal), so an
artist cannot leave a gap between them by changing only one threshold.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import cast

from asset_validator.core import TargetIncomplete
from asset_validator.data_models import (
    CheckMetadata,
    ComponentType,
    Issue,
    Severity,
    TargetRef,
    ValidationContext,
)

from .overlapping_vertices import DEFAULT_SETTINGS as _O_DEFAULT_SETTINGS
from .overlapping_vertices import read_settings as _read_o_settings


# Published for the UI's threshold display. Overrides belong to O, not E.
DEFAULT_SETTINGS = dict(_O_DEFAULT_SETTINGS)


@dataclass(frozen=True)
class _Prepared:
    owners: frozenset[TargetRef]
    min_distance: float
    bbox_fraction: float


class ZeroLengthEdgesRule:
    """Error on exact zero length; warn on nonzero edges no longer than L."""

    check_id = "E"
    metadata = CheckMetadata(
        label="Zero-length and short edges",
        hint=(
            "Repair collapsed edges and inspect short edges before export. "
            "Their object-space limit L uses the overlapping-vertices (O) "
            "min_distance_cm and bbox_fraction settings."
        ),
        can_fix=False,
    )

    def prepare(self, context: ValidationContext) -> _Prepared:
        if self.check_id in context.settings:
            raise ValueError("E shares O's distance settings; configure settings['O']")
        min_distance, bbox_fraction = _read_o_settings(context)

        from maya.api import OpenMaya as om

        # Instances share one mesh node. Nested references and imported copies
        # can share UUIDs, so use each live DAG node's canonical path instead.
        owners: dict[str, TargetRef] = {}
        for target in context.targets:
            try:
                selection = om.MSelectionList()
                selection.add(target.shape_path)
                node = selection.getDagPath(0).node()
                key = om.MDagPath.getAPathTo(node).fullPathName()
            except RuntimeError:
                key = "unresolved|" + target.shape_path
            owners.setdefault(key, target)
        return _Prepared(frozenset(owners.values()), min_distance, bbox_fraction)

    def run_target(
        self,
        context: ValidationContext,
        target: TargetRef,
        prepared: object,
    ) -> tuple[Issue, ...]:
        del context
        state = cast(_Prepared, prepared)
        if target not in state.owners:
            return ()

        from maya import cmds
        from maya.api import OpenMaya as om

        # Let a missing DAG path fail this target while the engine continues
        # with later meshes. An uninitialized mesh has no edge topology and
        # cannot always construct MFnMesh.
        selection = om.MSelectionList()
        selection.add(target.shape_path)
        dag_path = selection.getDagPath(0)
        try:
            if cmds.polyEvaluate(target.shape_path, edge=True) == 0:
                return ()
            fn = om.MFnMesh(dag_path)

            # MPointArray elements must not outlive a temporary API array.
            # Keep the array alive while copying every coordinate to Python.
            points = fn.getPoints(om.MSpace.kObject)
            positions = tuple((point.x, point.y, point.z) for point in points)
            del points
            if not positions or any(
                not all(math.isfinite(value) for value in position)
                for position in positions
            ):
                raise ValueError("Missing or non-finite vertex position")

            xs, ys, zs = zip(*positions)
            diagonal = math.sqrt(
                (max(xs) - min(xs)) ** 2
                + (max(ys) - min(ys)) ** 2
                + (max(zs) - min(zs)) ** 2
            )
            tolerance = max(state.min_distance, state.bbox_fraction * diagonal)

            zero_edges: list[int] = []
            short_edges: list[tuple[int, float]] = []
            for edge_id in range(fn.numEdges):
                vertex_a, vertex_b = fn.getEdgeVertices(edge_id)
                first, second = positions[vertex_a], positions[vertex_b]
                dx = first[0] - second[0]
                dy = first[1] - second[1]
                dz = first[2] - second[2]
                distance = math.sqrt(dx * dx + dy * dy + dz * dz)
                if distance == 0.0:
                    zero_edges.append(edge_id)
                elif distance <= tolerance:
                    short_edges.append((edge_id, distance))
        except TargetIncomplete:
            raise
        except Exception as exc:
            raise TargetIncomplete(
                f"Could not measure edges on {target.shape_path!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        issues = []
        if zero_edges:
            count = len(zero_edges)
            issues.append(Issue(
                check_id=self.check_id,
                severity=Severity.ERROR,
                message=(f"{count} {'edge has' if count == 1 else 'edges have'} "
                         "zero endpoint distance."),
                target_path=target.shape_path,
                target_uuid=target.shape_uuid,
                reference_identity=target.reference_identity,
                component_type=ComponentType.EDGE,
                component_indices=tuple(zero_edges),
                measured_value=0.0,
                threshold=0.0,
                details=(("issue", "zero_length"), ("edge_count", str(count))),
            ))
        if short_edges:
            count = len(short_edges)
            lengths = [distance for _, distance in short_edges]
            issues.append(Issue(
                check_id=self.check_id,
                severity=Severity.WARNING,
                message=(
                    f"{count} nonzero {'edge is' if count == 1 else 'edges are'} "
                    f"at most {tolerance:.6g} cm long in object space."
                ),
                target_path=target.shape_path,
                target_uuid=target.shape_uuid,
                reference_identity=target.reference_identity,
                component_type=ComponentType.EDGE,
                component_indices=tuple(edge for edge, _ in short_edges),
                measured_value=max(lengths),
                threshold=tolerance,
                details=(("issue", "short_edge"),
                         ("edge_count", str(count)),
                         ("shortest_length_cm", f"{min(lengths):.9g}"),
                         ("longest_length_cm", f"{max(lengths):.9g}")),
            ))
        return tuple(issues)


__all__ = ["DEFAULT_SETTINGS", "ZeroLengthEdgesRule"]
