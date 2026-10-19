"""Report both faces of each lamina pair on scoped mesh shapes.

``polyInfo(laminaFaces=True)`` returns only one face per welded pair in Maya
2025. The polygon API predicate identifies every participating face, which is
what the Select action needs.
"""

from __future__ import annotations

from typing import cast

from asset_validator.data_models import (
    CheckMetadata,
    ComponentType,
    Issue,
    Severity,
    TargetRef,
    ValidationContext,
)


class LaminaRule:
    """Emit one Error containing every lamina face on a scoped mesh."""

    check_id = "L"
    metadata = CheckMetadata(
        label="Lamina faces",
        hint=(
            "Inspect both faces of each welded coincident pair and remove only "
            "the unwanted face. Recheck topology afterward; a same-winding "
            "pair may also be non-manifold."
        ),
        can_fix=False,
    )

    def prepare(self, context: ValidationContext) -> frozenset[TargetRef]:
        settings = context.settings.get(self.check_id, {})
        if settings:
            raise ValueError(f"L does not accept settings: {sorted(settings)!r}")

        from maya.api import OpenMaya as om

        # One MObject can have several instance paths. Conversely, distinct
        # nested or imported copies can share UUIDs and reference identities.
        owners: dict[str, TargetRef] = {}
        for target in context.targets:
            selection = om.MSelectionList()
            selection.add(target.shape_path)
            node = selection.getDagPath(0).node()
            key = om.MDagPath.getAPathTo(node).fullPathName()
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

        from maya.api import OpenMaya as om
        from maya import cmds

        # An uninitialized mesh node can be in scope but has no mesh data:
        # even MFnMesh(dagPath) rejects it. Maya's face count is 0 for both
        # that node and a valid, empty mesh data object.
        if cmds.polyEvaluate(target.shape_path, face=True) == 0:
            return ()

        selection = om.MSelectionList()
        selection.add(target.shape_path)
        shape_path = selection.getDagPath(0)
        polygons = om.MItMeshPolygon(shape_path)
        faces: list[int] = []
        while not polygons.isDone():
            if polygons.isLamina():
                faces.append(polygons.index())
            polygons.next()

        if not faces:
            return ()
        issue = Issue(
            check_id=self.check_id,
            severity=Severity.ERROR,
            message=f"Mesh has {len(faces)} lamina faces.",
            target_path=target.shape_path,
            target_uuid=target.shape_uuid,
            reference_identity=target.reference_identity,
            component_type=ComponentType.FACE,
            component_indices=tuple(faces),
        )
        return (issue,)


__all__ = ["LaminaRule"]
