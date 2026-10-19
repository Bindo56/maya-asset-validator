"""Find mesh transforms and ancestors whose local transform is not frozen.

Each node in a scoped mesh's parent chain is tested once, in its own local
space, against these channels: translate, rotate, scale, rotateAxis, shear,
jointOrient (joints), rotatePivotTranslate, scalePivotTranslate, and
``offsetParentMatrix``. Together they are every input to ``dagLocalMatrix``
that moves a mesh, so each one is compared with its own tolerance and the
report names the channel to fix. ``dagLocalMatrix`` itself is a backstop: it is
compared only when every channel sits exactly at rest (so pivots cannot amplify
a deviation the channel tolerances accepted), and never on joints, whose matrix
also carries the parent joint's scale through ``segmentScaleCompensate``.
World-space values are never compared, because a parent offset can cancel a
child offset and rotateAxis or shear can hide inside an identity world rotation.

Values come from the API in internal units (centimetres and degrees), so the
tolerances hold whatever linear or angular unit the scene uses. Maya calls stay
in ``prepare`` for the shared chain nodes and in ``run_target`` for the
per-transform origin-offset measurement; nothing is written to the scene.

The report names the non-identity node itself, joints included. The
origin-offset warning reads the world rotate pivot.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import math
from typing import Mapping, cast

from asset_validator.data_models import (
    CheckMetadata,
    Issue,
    Severity,
    TargetRef,
    ValidationContext,
)


DEFAULT_SETTINGS = {
    "translate_tolerance": 0.00001,   # cm: translate, pivot translates, matrix translation
    "rotate_tolerance": 0.000001,     # degrees: rotate, rotateAxis, jointOrient
    "scale_tolerance": 0.000001,      # scale and shear
    "matrix_tolerance": 0.000001,     # offsetParentMatrix / dagLocalMatrix basis
    "origin_offset": 100.0,           # cm; None turns the warning off
}

# (attribute, kind, rest value). kind picks the unit conversion and tolerance.
_CHANNELS = (
    ("translate", "distance", 0.0),
    ("rotate", "angle", 0.0),
    ("scale", "scalar", 1.0),
    ("rotateAxis", "angle", 0.0),
    ("shear", "shear", 0.0),
    ("jointOrient", "angle", 0.0),
    ("rotatePivotTranslate", "distance", 0.0),
    ("scalePivotTranslate", "distance", 0.0),
)
_UNITS = {"distance": "cm", "angle": "deg", "scalar": "", "shear": ""}


def read_settings(context: ValidationContext) -> dict[str, float | None]:
    """Merge ``settings["T"]`` over the defaults and reject bad keys or values."""

    supplied = context.settings.get("T", {})
    unknown = sorted(set(supplied) - set(DEFAULT_SETTINGS))
    if unknown:
        raise ValueError(f"Unknown T settings: {unknown!r}")
    merged = dict(DEFAULT_SETTINGS)
    for key, value in supplied.items():
        if value is None and key == "origin_offset":
            merged[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"T setting {key!r} must be a number, not {value!r}")
        try:
            number = float(value)
        except OverflowError:
            number = math.inf
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"T setting {key!r} must be finite and >= 0, not {value!r}")
        merged[key] = number
    return merged


def _dag_path(om: object, path: str):
    selection = om.MSelectionList()
    selection.add(path)
    return selection.getDagPath(0)


def _matrix(om: object, plug) -> object:
    return om.MFnMatrixData(plug.asMObject()).matrix()


def _matrix_offsets(matrix, translate_tol: float, basis_tol: float) -> list[str]:
    """Describe where a 4x4 matrix leaves identity, in internal units."""

    found = []
    basis = max(
        abs(matrix.getElement(row, col) - (1.0 if row == col else 0.0))
        for row in range(4) for col in range(4) if row < 3 or col == 3
    )
    if basis > basis_tol:
        found.append(f"basis off identity by {basis:.6g}")
    move = [matrix.getElement(3, col) for col in range(3)]
    if max(abs(value) for value in move) > translate_tol:
        found.append("translation ({:.6g}, {:.6g}, {:.6g}) cm".format(*move))
    return found


@dataclass
class _NodeResult:
    """Local-transform verdict for one DAG node, shared by every mesh under it."""

    key: str                      # the node's first DAG path: one node, one key
    path: str
    uuid: str
    reference_identity: str | None
    inherits_transform: bool
    offsets: tuple[tuple[str, str], ...]
    affected: list[str] = field(default_factory=list)


def _evaluate_node(om: object, path: str, settings: Mapping[str, float | None]) -> _NodeResult:
    from asset_validator.maya_adapter import reference_identity

    obj = _dag_path(om, path).node()
    node = om.MFnDependencyNode(obj)
    tolerance = {
        "distance": settings["translate_tolerance"],
        "angle": settings["rotate_tolerance"],
        "scalar": settings["scale_tolerance"],
        "shear": settings["scale_tolerance"],
    }

    offsets = []
    exactly_at_rest = True
    for attribute, kind, rest in _CHANNELS:
        if not node.hasAttribute(attribute):
            continue
        plug = node.findPlug(attribute, False)
        values = []
        for index in range(plug.numChildren()):
            child = plug.child(index)
            if kind == "distance":
                values.append(child.asMDistance().asCentimeters())
            elif kind == "angle":
                values.append(child.asMAngle().asDegrees())
            else:
                values.append(child.asDouble())
        deviation = max(abs(value - rest) for value in values)
        exactly_at_rest = exactly_at_rest and deviation == 0.0
        if deviation > tolerance[kind]:
            unit = _UNITS[kind]
            text = "(" + ", ".join(f"{value:.9g}" for value in values) + ")"
            offsets.append((attribute, f"{text} {unit}".rstrip()))

    found = _matrix_offsets(
        _matrix(om, node.findPlug("offsetParentMatrix", False)),
        settings["translate_tolerance"], settings["matrix_tolerance"],
    )
    if found:
        offsets.append(("offsetParentMatrix", "; ".join(found)))

    # Backstop for any input the channel list does not cover. Joints are left
    # out: their dagLocalMatrix includes the parent joint's inverse scale.
    if not offsets and exactly_at_rest and not obj.hasFn(om.MFn.kJoint):
        found = _matrix_offsets(
            _matrix(om, node.findPlug("dagLocalMatrix", False)),
            settings["translate_tolerance"], settings["matrix_tolerance"],
        )
        if found:
            offsets.append(("dagLocalMatrix", "; ".join(found)))

    return _NodeResult(
        key=om.MDagPath.getAPathTo(obj).fullPathName(),
        path=path,
        uuid=node.uuid().asString(),
        reference_identity=reference_identity(path),
        inherits_transform=node.findPlug("inheritsTransform", False).asBool(),
        offsets=tuple(offsets),
    )


def _chain(transform_path: str) -> list[str]:
    """The mesh transform first, then each ancestor up to the root."""

    parts = transform_path.split("|")[1:]
    return ["|" + "|".join(parts[:end]) for end in range(len(parts), 0, -1)]


@dataclass
class _Prepared:
    settings: Mapping[str, float | None]
    issues_by_target: dict[TargetRef, tuple[Issue, ...]]
    blocked_targets: frozenset[TargetRef]
    origin_owner: dict[str, TargetRef]          # transform path -> first target
    shapes_by_transform: dict[str, tuple[str, ...]]


class FreezeTransformsRule:
    """Report each unfrozen transform once, plus an origin-offset warning."""

    check_id = "T"
    metadata = CheckMetadata(
        label="Freeze transforms",
        hint=(
            "Freeze the reported node (Modify > Freeze Transformations) and clear "
            "offsetParentMatrix. For an origin-offset warning, move the geometry "
            "and pivot to the world origin before freezing."
        ),
        can_fix=False,
    )

    def prepare(self, context: ValidationContext) -> _Prepared:
        import maya.api.OpenMaya as om

        settings = read_settings(context)
        nodes: dict[str, _NodeResult] = {}          # node key -> verdict
        by_path: dict[str, _NodeResult] = {}
        chains: dict[TargetRef, list[_NodeResult]] = {}

        for target in context.targets:
            chain = []
            for path in _chain(target.transform_path):
                result = by_path.get(path)
                if result is None:
                    result = _evaluate_node(om, path, settings)
                    # Instanced transforms are one node with several paths:
                    # report it once. Two references of one file share a UUID
                    # but are separate nodes, so the key is a DAG path, not a UUID.
                    result = nodes.setdefault(result.key, result)
                    by_path[path] = result
                chain.append(result)
                # Ancestors above a node that ignores its parents never move it.
                if not result.inherits_transform:
                    break
            chains[target] = chain
            for result in chain:
                if target.transform_path not in result.affected:
                    result.affected.append(target.transform_path)

        issues_by_target: dict[TargetRef, list[Issue]] = {}
        owned: set[str] = set()
        for target in context.targets:
            for result in chains[target]:
                if not result.offsets or result.key in owned:
                    continue
                owned.add(result.key)
                names = ", ".join(name for name, _ in result.offsets)
                issues_by_target.setdefault(target, []).append(
                    Issue(
                        check_id=self.check_id,
                        severity=Severity.ERROR,
                        message=(
                            f"Transform {result.path.rsplit('|', 1)[-1]!r} is not "
                            f"frozen ({names}); it moves "
                            f"{len(result.affected)} mesh transform(s) in scope."
                        ),
                        target_path=result.path,
                        target_uuid=result.uuid,
                        related_paths=tuple(sorted(result.affected)),
                        details=result.offsets,
                        reference_identity=result.reference_identity,
                    )
                )

        shapes_by_transform: dict[str, list[str]] = defaultdict(list)
        origin_owner: dict[str, TargetRef] = {}
        for target in context.targets:
            shapes_by_transform[target.transform_path].append(target.shape_path)
            origin_owner.setdefault(target.transform_path, target)

        return _Prepared(
            settings=settings,
            issues_by_target={k: tuple(v) for k, v in issues_by_target.items()},
            blocked_targets=frozenset(
                target for target, chain in chains.items()
                if any(result.offsets for result in chain)
            ),
            origin_owner=origin_owner,
            shapes_by_transform={k: tuple(v) for k, v in shapes_by_transform.items()},
        )

    def run_target(
        self,
        context: ValidationContext,
        target: TargetRef,
        prepared: object,
    ) -> tuple[Issue, ...]:
        del context
        state = cast(_Prepared, prepared)
        issues = list(state.issues_by_target.get(target, ()))
        limit = state.settings["origin_offset"]
        # A frozen chain is the precondition: an unfrozen transform already
        # explains any offset, so only report what freezing left behind. The
        # pivot belongs to the transform, so one warning covers all its shapes.
        if (
            limit is None
            or target in state.blocked_targets
            or state.origin_owner.get(target.transform_path) != target
        ):
            return tuple(issues)

        import maya.api.OpenMaya as om

        from asset_validator.maya_adapter import reference_identity

        transform = om.MFnTransform(_dag_path(om, target.transform_path))
        pivot = transform.rotatePivot(om.MSpace.kWorld)
        pivot_distance = math.sqrt(pivot.x ** 2 + pivot.y ** 2 + pivot.z ** 2)

        shapes = state.shapes_by_transform[target.transform_path]
        box = None
        for shape in shapes:
            shape_path = _dag_path(om, shape)
            shape_box = om.MFnDagNode(shape_path).boundingBox
            shape_box.transformUsing(shape_path.inclusiveMatrix())
            if box is None:
                box = shape_box
            else:
                box.expand(shape_box)
        low, high = box.min, box.max
        # Distance from the origin to the nearest point of the world box.
        gaps = [max(low[axis], 0.0, -high[axis]) for axis in range(3)]
        geometry_distance = math.sqrt(sum(gap * gap for gap in gaps))

        measured = max(pivot_distance, geometry_distance)
        if measured > limit:
            issues.append(
                Issue(
                    check_id=self.check_id,
                    severity=Severity.WARNING,
                    message=(
                        f"Frozen mesh sits {measured:.6g} cm from the world origin "
                        f"(limit {limit:.6g} cm): freezing kept the offset in the "
                        "geometry or pivot."
                    ),
                    target_path=target.transform_path,
                    target_uuid=target.transform_uuid,
                    related_paths=tuple(sorted(shapes)),
                    details=(
                        ("issue", "origin_offset"),
                        ("rotate_pivot_distance_cm", f"{pivot_distance:.6g}"),
                        ("geometry_distance_cm", f"{geometry_distance:.6g}"),
                    ),
                    # The transform's own reference, not its shape's.
                    reference_identity=reference_identity(target.transform_path),
                    measured_value=measured,
                    threshold=limit,
                )
            )
        return tuple(issues)


__all__ = ["DEFAULT_SETTINGS", "FreezeTransformsRule", "read_settings"]
