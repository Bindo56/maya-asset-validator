"""Find ambiguous DAG leaf names in a static-mesh validation scope.

The index covers the scene so a selected mesh can be reported when its name
clashes with a mesh outside the selection. Maya calls stay in ``prepare``
and ``name_index``; ``run_target`` reads the prepared result without changing
the scene.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Mapping, cast

from asset_validator.data_models import (
    CheckMetadata,
    Issue,
    Severity,
    TargetRef,
    ValidationContext,
)


def _node_identity(path: str) -> str:
    """Return one canonical path per DAG node, including instanced nodes.

    UUID and innermost reference-node UUID are not enough: nested references
    and two imported copies can preserve both values on distinct Maya nodes.
    """

    import maya.api.OpenMaya as om

    selection = om.MSelectionList()
    selection.add(path)
    node = selection.getDagPath(0).node()
    return om.MDagPath.getAPathTo(node).fullPathName()


def name_index() -> dict[str, dict[str, set[str]]]:
    """Scene-wide: short name (namespace kept) -> canonical node path -> instance paths.

    All instance paths are collected, but a shared DAG node counts once. Keep
    namespaces: ``a:mesh`` and ``b:mesh`` are different Maya names. The rule
    and the rename fix read the same index, so a rename plan sees
    exactly the clash the report showed.
    """

    from maya import cmds

    groups: dict[str, dict[str, set[str]]] = {}
    for path in sorted(set(cmds.ls(type=("transform", "mesh"), long=True, allPaths=True) or ())):
        groups.setdefault(path.rsplit("|", 1)[-1], {}).setdefault(_node_identity(path), set()).add(path)
    return groups


class DuplicateNamesRule:
    """Report one Error for each ambiguous short name touching the scope."""

    check_id = "D"
    metadata = CheckMetadata(
        label="Duplicate names",
        hint=(
            "Give each conflicting transform or mesh shape a distinct DAG name. "
            "In the window, Rename\u2026 proposes free _NN names, renames shapes derived "
            "from the transform name with it, and one Undo reverts it."
        ),
        can_fix=True,
    )

    def prepare(self, context: ValidationContext) -> dict[TargetRef, tuple[Issue, ...]]:
        from asset_validator.maya_adapter import reference_identity

        groups = name_index()

        issues_by_target: dict[TargetRef, list[Issue]] = defaultdict(list)
        for short_name, members in groups.items():
            if len(members) < 2:
                continue

            conflicting_paths = tuple(sorted(path for paths in members.values() for path in paths))
            path_set = set(conflicting_paths)

            # The first scoped owner emits the group. Otherwise every scoped
            # mesh would duplicate the same issue in the final report.
            for target in context.targets:
                if target.transform_path in path_set:
                    owner_path, owner_uuid = target.transform_path, target.transform_uuid
                elif target.shape_path in path_set:
                    owner_path, owner_uuid = target.shape_path, target.shape_uuid
                else:
                    continue

                issues_by_target[target].append(
                    Issue(
                        check_id=self.check_id,
                        severity=Severity.ERROR,
                        message=(
                            f"Duplicate DAG name {short_name!r} is used by "
                            f"{len(members)} nodes."
                        ),
                        target_path=owner_path,
                        target_uuid=owner_uuid,
                        related_paths=conflicting_paths,
                        details=(("short_name", short_name),),
                        # The owner's own reference: a referenced shape can sit
                        # under a local transform.
                        reference_identity=reference_identity(owner_path),
                    )
                )
                break

        return {target: tuple(issues) for target, issues in issues_by_target.items()}

    def run_target(
        self,
        context: ValidationContext,
        target: TargetRef,
        prepared: object,
    ) -> tuple[Issue, ...]:
        del context
        issues_by_target = cast(Mapping[TargetRef, tuple[Issue, ...]], prepared)
        return issues_by_target.get(target, ())


__all__ = ["DuplicateNamesRule", "name_index"]
