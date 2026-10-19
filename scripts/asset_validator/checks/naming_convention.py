"""Check the static-mesh transform and live-shape naming convention.

Only transforms that own a scoped, non-intermediate mesh are checked. A
selected shape still has all of its live mesh siblings considered, because
the required suffix depends on their count. Grouping transforms and Orig
shapes are outside this rule. Findings are warnings and never rename nodes.
"""

from __future__ import annotations

import re
from typing import cast

from asset_validator.core import TargetIncomplete
from asset_validator.data_models import (
    CheckMetadata,
    Issue,
    Severity,
    TargetRef,
    ValidationContext,
)


_TRANSFORM_PATTERN = re.compile(
    r"SM_[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)?_[0-9]{2,}", re.ASCII
)
_PATTERN_TEXT = "SM_AssetName[_Descriptor]_NN (ASCII; at least two variant digits)"


def _leaf(path: str) -> str:
    """Remove the DAG path and namespace for convention comparisons only."""

    return path.rsplit("|", 1)[-1].rsplit(":", 1)[-1]


class NamingConventionRule:
    """Report naming warnings once per scoped mesh transform node."""

    check_id = "C"
    metadata = CheckMetadata(
        label="Naming convention",
        hint=(
            "Name static-mesh transforms SM_AssetName[_Descriptor]_NN with ASCII "
            "letters/digits and at least two variant digits. Name one live mesh "
            "shape <transformName>Shape; name multiple live shapes "
            "<transformName>_Shape01, _Shape02, and so on."
        ),
        can_fix=False,
    )

    def prepare(self, context: ValidationContext) -> frozenset[TargetRef]:
        supplied = context.settings.get(self.check_id, {})
        if supplied:
            raise ValueError(f"Unknown C settings: {sorted(supplied)!r}")

        from maya.api import OpenMaya as om

        # Scope has one target per live shape path. A transform with several
        # shapes, or an instanced transform (one node, several DAG paths),
        # emits its convention findings only for its first target. Key on the
        # node's canonical path, as T does: reference copies can share UUIDs.
        owners: dict[str, TargetRef] = {}
        for target in context.targets:
            try:
                selection = om.MSelectionList()
                selection.add(target.transform_path)
                node = selection.getDagPath(0).node()
                key = om.MDagPath.getAPathTo(node).fullPathName()
            except RuntimeError:
                # A deleted transform still fails its own target in run_target.
                key = "unresolved|" + target.transform_path
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

        from asset_validator.maya_adapter import reference_identity

        transform = target.transform_path
        live_shapes = tuple(sorted(set(cmds.listRelatives(
            transform, shapes=True, type="mesh", fullPath=True,
            noIntermediate=True,
        ) or ())))
        if not live_shapes:
            raise TargetIncomplete(f"No live mesh shapes remain under {transform}")

        transform_name = _leaf(transform)
        owner_reference = reference_identity(transform)
        issues = []
        if _TRANSFORM_PATTERN.fullmatch(transform_name) is None:
            issues.append(Issue(
                check_id=self.check_id,
                severity=Severity.WARNING,
                message=(
                    f"Mesh transform {transform_name!r} does not match "
                    f"{_PATTERN_TEXT}."
                ),
                target_path=transform,
                target_uuid=target.transform_uuid,
                reference_identity=owner_reference,
                details=(("reason", "transform_name"),
                         ("actual_name", transform_name),
                         ("expected_pattern", _PATTERN_TEXT)),
            ))

        if len(live_shapes) == 1:
            expected = (transform_name + "Shape",)
        else:
            expected = tuple(
                f"{transform_name}_Shape{number:02d}"
                for number in range(1, len(live_shapes) + 1)
            )
        actual = tuple(sorted(_leaf(shape) for shape in live_shapes))
        if actual != tuple(sorted(expected)):
            issues.append(Issue(
                check_id=self.check_id,
                severity=Severity.WARNING,
                message=(
                    f"Live mesh shapes under {transform_name!r} must be named "
                    f"{', '.join(expected)}."
                ),
                target_path=transform,
                target_uuid=target.transform_uuid,
                reference_identity=owner_reference,
                related_paths=live_shapes,
                details=(("reason", "shape_names"),
                         ("actual_names", ", ".join(actual)),
                         ("expected_names", ", ".join(expected))),
            ))
        return tuple(issues)


__all__ = ["NamingConventionRule"]
