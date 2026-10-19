"""Report faces with more than four corners without changing Maya selection.

Maya's face vertex counts include hole loops, so holed faces get a distinct
message rather than being presented as ordinary five-sided polygons.
"""

from __future__ import annotations

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


class NgonsRule:
    """Warn on scoped mesh faces with over four boundary or hole corners."""

    check_id = "G"
    metadata = CheckMetadata(
        label="N-gons and holed faces",
        hint=(
            "Inspect faces with more than four corners before export. "
            "A face with a hole is called out separately because Maya counts "
            "hole vertices among its corners. Resolve topology manually."
        ),
        can_fix=False,
    )

    def prepare(self, context: ValidationContext) -> frozenset[TargetRef]:
        if self.check_id in context.settings:
            raise ValueError("G does not accept settings")

        from maya.api import OpenMaya as om

        # An instance has multiple DAG paths for one mesh node. Nested and
        # imported reference copies can share UUIDs, so UUID is not a key.
        owners: dict[str, TargetRef] = {}
        for target in context.targets:
            try:
                selection = om.MSelectionList()
                selection.add(target.shape_path)
                node = selection.getDagPath(0).node()
                key = om.MDagPath.getAPathTo(node).fullPathName()
            except RuntimeError:
                # A deleted target must be reported by run_target as
                # Incomplete while later meshes continue to be checked.
                key = "unresolved|" + target.shape_path
            owners.setdefault(key, target)
        return frozenset(owners.values())

    def run_target(
        self,
        context: ValidationContext,
        target: TargetRef,
        prepared: object,
    ) -> tuple[Issue, ...]:
        del context
        if target not in cast(frozenset[TargetRef], prepared):
            return ()

        from maya import cmds
        from maya.api import OpenMaya as om

        try:
            # MFnMesh(dagPath) rejects an uninitialized mesh node, which can
            # still be in scope. It has no polygon topology to inspect.
            if cmds.polyEvaluate(target.shape_path, face=True) == 0:
                return ()
            selection = om.MSelectionList()
            selection.add(target.shape_path)
            fn = om.MFnMesh(selection.getDagPath(0))
            counts_array, _ = fn.getVertices()
            counts = tuple(int(count) for count in counts_array)
            if len(counts) != fn.numPolygons:
                raise ValueError("Face count does not match polygon topology")
            # getHoles is available in Maya 2025. Multiple loops may belong
            # to one face. Subtract their vertices to describe the exterior
            # without calling a holed quad an eight-sided polygon.
            hole_loops: dict[int, int] = {}
            hole_vertices: dict[int, int] = {}
            for face_id, vertices in fn.getHoles():
                if face_id < 0 or face_id >= len(counts):
                    raise ValueError(f"Hole references unknown face {face_id}")
                hole_loops[face_id] = hole_loops.get(face_id, 0) + 1
                hole_vertices[face_id] = hole_vertices.get(face_id, 0) + len(vertices)
            if any(counts[face] <= vertices for face, vertices in hole_vertices.items()):
                raise ValueError("Hole vertices leave no exterior boundary")
            plain = tuple(
                face_id for face_id, corners in enumerate(counts)
                if corners > 4 and face_id not in hole_loops
            )
            holed = tuple(sorted(hole_loops))
        except TargetIncomplete:
            raise
        except Exception as exc:
            raise TargetIncomplete(
                f"Could not inspect face topology on {target.shape_path!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        issues: list[Issue] = []
        if plain:
            issues.append(Issue(
                check_id=self.check_id,
                severity=Severity.WARNING,
                message=f"{len(plain)} {'face has' if len(plain) == 1 else 'faces have'} more than four corners.",
                target_path=target.shape_path,
                target_uuid=target.shape_uuid,
                reference_identity=target.reference_identity,
                component_type=ComponentType.FACE,
                component_indices=plain,
                measured_value=float(max(counts[face_id] for face_id in plain)),
                threshold=4.0,
                details=(("issue", "ngon"), ("face_count", str(len(plain)))),
            ))
        if holed:
            issues.append(Issue(
                check_id=self.check_id,
                severity=Severity.WARNING,
                message=(
                    f"{len(holed)} {'face has' if len(holed) == 1 else 'faces have'} "
                    "hole loops; Maya counts their vertices among the face corners."
                ),
                target_path=target.shape_path,
                target_uuid=target.shape_uuid,
                reference_identity=target.reference_identity,
                component_type=ComponentType.FACE,
                component_indices=holed,
                measured_value=float(max(counts[face_id] for face_id in holed)),
                details=(("issue", "holed_face"),
                         ("face_count", str(len(holed))),
                         ("hole_loops", str(sum(hole_loops[face_id] for face_id in holed))),
                         ("outer_corners_by_face", " ".join(
                             f"{face_id}:{counts[face_id] - hole_vertices[face_id]}"
                             for face_id in holed
                         )),
                         ("total_corners_by_face", " ".join(
                             f"{face_id}:{counts[face_id]}" for face_id in holed
                         ))),
            ))
        return tuple(issues)


__all__ = ["NgonsRule"]
