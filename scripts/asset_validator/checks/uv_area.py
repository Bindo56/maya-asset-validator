"""Validate coverage and per-face area in one explicitly named UV set.

Maya's triangle offsets index the mesh-wide face-corner array, whereas
``getAssignedUVs`` omits the UV IDs for wholly unmapped faces. Both the
polygon-corner and assigned-UV start offsets are needed to map triangles.

UVs are stored as float32, so a face collapsed onto a slanted line keeps a
signed area of about 1e-9 instead of 0.0, with a random sign. Area tests use a
tolerance of ``collapse_tolerance_ulps`` float32 steps of UV width: a face is
collapsed when its absolute area is at most that width times the face's UV
extent, and triangles inside that band are ignored by the fold test.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import accumulate, chain, repeat
import math
from operator import add, itemgetter, mul, sub
import struct
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


DEFAULT_SETTINGS = {
    "uv_set": "map1",
    "collapse_tolerance_ulps": 4.0,   # float32 steps of UV width; 0 = exact zero only
}


def _ulp32(value: float) -> float:
    """Spacing of float32 values at ``abs(value)``."""

    stored = struct.unpack("<f", struct.pack("<f", abs(value)))[0]
    bits = struct.unpack("<I", struct.pack("<f", stored))[0]
    return struct.unpack("<f", struct.pack("<I", bits + 1))[0] - stored


def _width_tolerance(steps: float, magnitude: float) -> float:
    """UV width below which a face counts as collapsed.

    UV layouts live around the 0-1 tile, so the spacing at 1.0 is the floor;
    UDIM tiles further out get their own, coarser spacing.
    """

    return steps * _ulp32(max(1.0, magnitude))


def read_settings(context: ValidationContext) -> tuple[str, float]:
    supplied = context.settings.get("U", {})
    unknown = sorted(set(supplied) - set(DEFAULT_SETTINGS))
    if unknown:
        raise ValueError(f"U does not accept settings: {unknown!r}")
    uv_set = supplied.get("uv_set", DEFAULT_SETTINGS["uv_set"])
    if not isinstance(uv_set, str) or not uv_set.strip():
        raise ValueError("U uv_set must be a nonempty UV-set name")
    supplied_steps = supplied.get("collapse_tolerance_ulps", DEFAULT_SETTINGS["collapse_tolerance_ulps"])
    if isinstance(supplied_steps, bool) or not isinstance(supplied_steps, (int, float)):
        raise TypeError(f"U collapse_tolerance_ulps must be a number, not {supplied_steps!r}")
    try:
        steps = float(supplied_steps)
    except OverflowError:
        steps = math.inf
    if not math.isfinite(steps) or steps < 0:
        raise ValueError(
            f"U collapse_tolerance_ulps must be finite and >= 0, not {supplied_steps!r}"
        )
    return uv_set, steps


@dataclass(frozen=True)
class _Prepared:
    owners: frozenset[TargetRef]
    uv_set: str
    steps: float


def _faces_text(count: int) -> str:
    return f"{count} face has" if count == 1 else f"{count} faces have"


def _per_instance_member(om, dag_path, family: str, uv_sets) -> str | None:
    """The member of per-instance UV-set ``family`` that this instance uses.

    Maya lists per-instance sets as "family(N)", where N is the member's
    creation index, not the instance number: a set created through instance 1
    first is "family(0)". The instance list of each member decides.
    """

    if not any(name.startswith(family + "(") for name in uv_sets):
        return None
    try:
        fn = om.MFnMesh(dag_path)
        members = fn.getUVSetsInFamily(family)
        instance = dag_path.instanceNumber()
        for member in members:
            if instance in list(fn.getAssociatedUVSetInstances(member)):
                return member
    except RuntimeError:
        return None
    return None


class UVAreaRule:
    """Report a missing UV set, unmapped or collapsed faces, and UV fold-overs."""

    check_id = "U"
    metadata = CheckMetadata(
        label="UV coverage and area",
        hint=(
            "Assign the requested UV set to every face and give each face UV "
            "area. A flipped-triangle warning means crossing UVs, or a concave "
            "UV outline that Maya's triangulation folds; move the UVs or the "
            "face's triangulation so no UV triangle is mirrored."
        ),
        can_fix=False,
    )

    def prepare(self, context: ValidationContext) -> _Prepared:
        uv_set, steps = read_settings(context)

        from maya.api import OpenMaya as om

        # Instance paths share one DAG node. Nested and imported copies can
        # share UUIDs, so neither a UUID nor the inner reference node is a key.
        # A path that no longer resolves keeps its own owner entry, so only
        # that target fails in run_target and the others are still checked.
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
        return _Prepared(frozenset(owners.values()), uv_set, steps)

    def run_target(
        self,
        context: ValidationContext,
        target: TargetRef,
        prepared: object,
    ) -> Iterator[Issue]:
        del context
        state = cast(_Prepared, prepared)
        if target not in state.owners:
            return

        from maya import cmds
        from maya.api import OpenMaya as om

        # A deleted or renamed target raises here: an Error for this target only.
        selection = om.MSelectionList()
        selection.add(target.shape_path)
        dag_path = selection.getDagPath(0)

        try:
            uv_sets = cmds.polyUVSet(target.shape_path, query=True, allUVSets=True) or ()
        except Exception as exc:
            raise TargetIncomplete(
                f"Could not list UV sets on {target.shape_path!r}"
            ) from exc
        if isinstance(uv_sets, str):
            uv_sets = (uv_sets,)
        uv_set = state.uv_set
        if uv_set not in uv_sets:
            uv_set = _per_instance_member(om, dag_path, uv_set, uv_sets)
            if uv_set is None:
                yield Issue(
                    check_id=self.check_id,
                    severity=Severity.ERROR,
                    message=f"Mesh is missing UV set {state.uv_set!r}.",
                    target_path=target.shape_path,
                    target_uuid=target.shape_uuid,
                    reference_identity=target.reference_identity,
                    details=(
                        ("issue", "missing_set"),
                        ("uv_set", state.uv_set),
                        # repr() keeps leading or trailing spaces in set names visible.
                        ("available_uv_sets", ", ".join(map(repr, uv_sets)) or "(none)"),
                    ),
                )
                return

        try:
            # An uninitialized mesh node cannot construct MFnMesh. It has no
            # faces to assess, but the missing-set check above still applies.
            if cmds.polyEvaluate(target.shape_path, face=True) == 0:
                return
            unmapped, collapsed, folded = self._classify(om, dag_path, uv_set, state.steps)
        except Exception as exc:
            raise TargetIncomplete(
                f"Could not finish UV evaluation on {target.shape_path!r} "
                f"in set {uv_set!r}: {type(exc).__name__}: {exc}"
            ) from exc

        def face_issue(faces, severity, message, details, **values):
            return Issue(
                check_id=self.check_id,
                severity=severity,
                message=message,
                target_path=target.shape_path,
                target_uuid=target.shape_uuid,
                reference_identity=target.reference_identity,
                component_type=ComponentType.FACE,
                component_indices=tuple(faces),
                details=(("uv_set", uv_set),) + details,
                **values,
            )

        if unmapped:
            yield face_issue(
                unmapped, Severity.ERROR,
                f"{_faces_text(len(unmapped))} no UVs in set {uv_set!r}.",
                (("issue", "unmapped"),),
            )
        if collapsed:
            area, tolerance = max((area, tol) for _, area, tol in collapsed)
            yield face_issue(
                [face for face, _, _ in collapsed], Severity.ERROR,
                f"{_faces_text(len(collapsed))} zero UV area in set {uv_set!r}.",
                (
                    ("issue", "collapsed"),
                    ("collapse_tolerance_ulps", f"{state.steps:g}"),
                    ("largest_area", f"{area:.6g}"),
                ),
                measured_value=area, threshold=tolerance,
            )
        if folded:
            yield face_issue(
                [face for face, _, _ in folded], Severity.WARNING,
                f"{_faces_text(len(folded))} UV triangles flipped under Maya's "
                f"triangulation in set {uv_set!r} (crossing UVs or a concave "
                "UV outline).",
                (
                    ("issue", "flipped_triangles"),
                    ("largest_flipped_area", f"{max(flip for _, flip, _ in folded):.6g}"),
                ),
                measured_value=max(fraction for _, _, fraction in folded),
            )

    @staticmethod
    def _classify(om, dag_path, uv_set: str, steps: float):
        """Return (unmapped faces, [(face, area, tol)], [(face, flipped, fraction)])."""

        fn = om.MFnMesh(dag_path)
        counts_array, ids_array = fn.getAssignedUVs(uv_set)
        counts, ids = list(counts_array), list(ids_array)
        polygon_counts = list(fn.getVertices()[0])
        triangle_counts_array, triangle_offsets_array = fn.getTriangleOffsets()
        triangle_counts = list(triangle_counts_array)
        triangle_offsets = list(triangle_offsets_array)
        u_array, v_array = fn.getUVs(uv_set)
        u_values, v_values = list(u_array), list(v_array)

        corner_total = sum(polygon_counts)
        if not (
            len(counts) == len(polygon_counts) == len(triangle_counts)
            and sum(counts) == len(ids)
            and len(u_values) == len(v_values)
            and 3 * sum(triangle_counts) == len(triangle_offsets)
        ):
            raise ValueError("Inconsistent UV, polygon, or triangle array lengths")
        if ids and (min(ids) < 0 or max(ids) >= len(u_values)):
            raise ValueError("UV id outside the UV array")
        if triangle_offsets and (min(triangle_offsets) < 0 or max(triangle_offsets) >= corner_total):
            raise ValueError("Triangle offset outside the face-corner array")

        face_count = len(polygon_counts)
        # UV id per face corner. Corners of unmapped faces point at an extra
        # (0, 0) entry so the lookups below need no branch.
        blank = len(u_values)
        if counts == polygon_counts:
            corner_uv = ids
        else:
            corner_uv = []
            uv_start = 0
            for face, (corner_count, assigned) in enumerate(zip(polygon_counts, counts)):
                if assigned == 0:
                    corner_uv.extend([blank] * corner_count)
                    continue
                if assigned != corner_count:
                    raise ValueError(
                        f"Face {face} has {assigned} UV assignments for {corner_count} corners"
                    )
                corner_uv.extend(ids[uv_start:uv_start + assigned])
                uv_start += assigned
        corner_u = list(map((u_values + [0.0]).__getitem__, corner_uv))
        corner_v = list(map((v_values + [0.0]).__getitem__, corner_uv))

        # Each triangle's three offsets must lie inside its own face's corners.
        corner_face = list(chain.from_iterable(map(repeat, range(face_count), polygon_counts)))
        triangle_face = list(chain.from_iterable(map(repeat, range(face_count), triangle_counts)))
        first, second, third = (triangle_offsets[k::3] for k in range(3))
        for corners in (first, second, third):
            if list(map(corner_face.__getitem__, corners)) != triangle_face:
                raise ValueError("A triangle offset points outside its face")

        # Signed UV area of every triangle, computed with C-level map().
        ua, ub, uc = (list(map(corner_u.__getitem__, corners)) for corners in (first, second, third))
        va, vb, vc = (list(map(corner_v.__getitem__, corners)) for corners in (first, second, third))
        du_b, du_c = list(map(sub, ub, ua)), list(map(sub, uc, ua))
        dv_b, dv_c = list(map(sub, vb, va)), list(map(sub, vc, va))
        areas = list(map(mul, map(sub, map(mul, du_b, dv_c), map(mul, dv_b, du_c)), repeat(0.5)))
        if not math.isfinite(sum(areas)):
            raise ValueError("Non-finite UV coordinates or triangle area")

        unmapped = [face for face, assigned in enumerate(counts) if assigned == 0]
        triangle_start = list(accumulate(triangle_counts, initial=0))
        corner_start = list(accumulate(polygon_counts, initial=0))

        # Candidate faces, found per triangle with C-level map():
        #   tolerance(face) = steps * ulp32(face magnitude) * face extent
        #                  <= steps * U * sum over its triangles of e(t)
        # where e(t) = |du_b|+|du_c|+|dv_b|+|dv_c| is at least the triangle's
        # bounding-box width plus height (so its diagonal), its connected
        # triangles cover the face's bounding box, and U >= ulp32(face
        # magnitude). If every triangle had |area| > steps * U * e(t), the
        # face's total would exceed its tolerance. So a collapsed face has at
        # least one triangle with |area| <= steps * U * e(t). A flipped face
        # has a negative and a positive triangle. Only UVs that mapped faces
        # use enter U, so unused (even non-finite) UVs cannot hide a face.
        extents = map(add, map(add, map(abs, du_b), map(abs, du_c)),
                      map(add, map(abs, dv_b), map(abs, dv_c)))
        used_magnitude = max(max(map(abs, corner_u), default=0.0), max(map(abs, corner_v), default=0.0))
        if used_magnitude <= 16.0:
            # Every used UV is within 16 of the origin (UDIM tiles included):
            # one float32 spacing bounds every face.
            limits = map(mul, extents, repeat(steps * _ulp32(max(1.0, used_magnitude)) * (1.0 + 1e-9)))
        else:
            # An outlying UV must not loosen every face's limit. Each face uses
            # the sum of its triangles' spacings, 2 ** (frexp exponent - 24)
            # for x >= 1, which is at least the spacing of its largest corner.
            magnitude = map(max, map(abs, ua), map(abs, ub), map(abs, uc),
                            map(abs, va), map(abs, vb), map(abs, vc), repeat(1.0))
            ulps = list(map(math.ldexp, repeat(1.0),
                            map(sub, map(itemgetter(1), map(math.frexp, magnitude)), repeat(24))))
            face_ulp = [sum(ulps[start:end]) for start, end in zip(triangle_start, triangle_start[1:])]
            limits = map(mul, map(mul, extents, map(face_ulp.__getitem__, triangle_face)),
                         repeat(steps * (1.0 + 1e-9)))
        thin = {face for face, area, limit in zip(triangle_face, map(abs, areas), limits) if area <= limit}
        negative = {face for face, area in zip(triangle_face, areas) if area < 0.0}
        positive = {face for face, area in zip(triangle_face, areas) if area > 0.0}
        no_triangles = {face for face, count in enumerate(triangle_counts) if count == 0}
        candidates = sorted((thin | (negative & positive) | no_triangles) - set(unmapped))

        collapsed: list[tuple[int, float, float]] = []
        folded: list[tuple[int, float, float]] = []
        for face in candidates:
            face_areas = areas[triangle_start[face]:triangle_start[face + 1]]
            total = sum(map(abs, face_areas))
            us = corner_u[corner_start[face]:corner_start[face + 1]]
            vs = corner_v[corner_start[face]:corner_start[face + 1]]
            extent = math.hypot(max(us) - min(us), max(vs) - min(vs))
            face_magnitude = max(abs(min(us)), abs(max(us)), abs(min(vs)), abs(max(vs)))
            tolerance = _width_tolerance(steps, face_magnitude) * extent
            if total <= tolerance:
                collapsed.append((face, total, tolerance))
                continue
            positive = sum(area for area in face_areas if area > tolerance)
            negative = -sum(area for area in face_areas if area < -tolerance)
            if positive > 0.0 and negative > 0.0:
                flipped = min(positive, negative)
                folded.append((face, flipped, flipped / total))
        return unmapped, collapsed, folded


__all__ = ["DEFAULT_SETTINGS", "UVAreaRule", "read_settings"]
