"""Find zero-area faces and small or poorly shaped Maya triangles.

Maya's own triangulation supplies face-corner offsets into the *mesh-wide*
vertex-connectivity array. Areas use absolute 3D triangle cross products from
one copied object-space point snapshot. A whole zero-area face is an Error;
partial degeneracy, low height/quality, or a small nonzero face is a Warning.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from itertools import chain
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


DEFAULT_SETTINGS = dict(_O_DEFAULT_SETTINGS)
QUALITY_LIMIT = 0.0001
_QUALITY_NUMERATOR = 4.0 * math.sqrt(3.0)
_REASONS = (
    "zero_face", "zero_triangle", "low_height", "low_quality", "small_area",
)


@dataclass(frozen=True)
class _Prepared:
    owners: frozenset[TargetRef]
    min_distance: float
    bbox_fraction: float


@dataclass(frozen=True)
class _Finding:
    face: int
    measured: float
    threshold: float
    area: float
    height: float
    quality: float


class ZeroAreaFacesRule:
    """Report exact zero-area faces and warning-level triangle defects."""

    check_id = "A"
    metadata = CheckMetadata(
        label="Zero-area and sliver faces",
        hint=(
            "Repair collapsed faces and inspect the listed triangulated faces. "
            "Warnings identify an internal zero-area triangle, height at most "
            "L, quality at most 0.0001, or nonzero face area at most L squared. "
            "L shares overlapping-vertices (O) distance settings."
        ),
        can_fix=False,
    )

    def prepare(self, context: ValidationContext) -> _Prepared:
        if self.check_id in context.settings:
            raise ValueError("A shares O's distance settings; configure settings['O']")
        min_distance, bbox_fraction = _read_o_settings(context)

        from maya.api import OpenMaya as om

        # One pass per DAG node, not per instance path. Distinct nested or
        # imported copies can share UUIDs, so UUIDs cannot be the owner key.
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

        try:
            selection = om.MSelectionList()
            selection.add(target.shape_path)
            dag_path = selection.getDagPath(0)
            # Maya cannot construct MFnMesh for an uninitialized mesh node.
            if cmds.polyEvaluate(target.shape_path, face=True) == 0:
                return ()
            fn = om.MFnMesh(dag_path)
            # Copy every coordinate while the API array is alive, into one
            # flat list per axis for the triangle loop below.
            points = fn.getPoints(om.MSpace.kObject)
            positions = [(point.x, point.y, point.z) for point in points]
            del points
            if not positions:
                raise ValueError("Missing or non-finite vertex position")
            xs, ys, zs = (list(axis) for axis in zip(*positions))
            del positions
            if not all(map(math.isfinite, chain(xs, ys, zs))):
                raise ValueError("Missing or non-finite vertex position")

            diagonal = math.sqrt(
                (max(xs) - min(xs)) ** 2
                + (max(ys) - min(ys)) ** 2
                + (max(zs) - min(zs)) ** 2
            )
            limit = max(state.min_distance, state.bbox_fraction * diagonal)
            small_area = limit * limit

            face_counts_array, connects_array = fn.getVertices()
            triangle_counts_array, offsets_array = fn.getTriangleOffsets()
            face_counts = list(face_counts_array)
            connects = list(connects_array)
            triangle_counts = list(triangle_counts_array)
            offsets = list(offsets_array)
            if (
                len(face_counts) != fn.numPolygons
                or len(triangle_counts) != len(face_counts)
                or sum(face_counts) != len(connects)
                or 3 * sum(triangle_counts) != len(offsets)
            ):
                raise ValueError("Inconsistent polygon or triangle array lengths")
            if connects and (min(connects) < 0 or max(connects) >= len(xs)):
                raise ValueError("Face vertex outside mesh")

            # One plain loop, no per-triangle tuples or generators.
            sqrt = math.sqrt
            numerator = _QUALITY_NUMERATOR
            findings: dict[str, list[_Finding]] = defaultdict(list)
            offset_index = corner_start = 0
            for face, triangles in enumerate(triangle_counts):
                if triangles <= 0:
                    raise ValueError(f"Face {face} has no Maya triangles")
                corner_end = corner_start + face_counts[face]
                face_area = 0.0
                zero_triangle = False
                min_height = min_quality = math.inf
                for _ in range(triangles):
                    # Offsets index the mesh-wide face-vertex list; they must
                    # stay inside this face's own corners.
                    first = offsets[offset_index]
                    second = offsets[offset_index + 1]
                    third = offsets[offset_index + 2]
                    offset_index += 3
                    if not (corner_start <= first < corner_end
                            and corner_start <= second < corner_end
                            and corner_start <= third < corner_end):
                        raise ValueError(f"Triangle offset leaves face {face}")
                    first, second, third = connects[first], connects[second], connects[third]
                    ax, ay, az = xs[first], ys[first], zs[first]
                    bx, by, bz = xs[second], ys[second], zs[second]
                    cx, cy, cz = xs[third], ys[third], zs[third]
                    abx, aby, abz = bx - ax, by - ay, bz - az
                    acx, acy, acz = cx - ax, cy - ay, cz - az
                    bcx, bcy, bcz = cx - bx, cy - by, cz - bz
                    nx = aby * acz - abz * acy
                    ny = abz * acx - abx * acz
                    nz = abx * acy - aby * acx
                    area = 0.5 * sqrt(nx * nx + ny * ny + nz * nz)
                    ab2 = abx * abx + aby * aby + abz * abz
                    bc2 = bcx * bcx + bcy * bcy + bcz * bcz
                    ac2 = acx * acx + acy * acy + acz * acz
                    total2 = ab2 + bc2 + ac2
                    longest2 = ab2 if ab2 >= bc2 else bc2
                    if ac2 > longest2:
                        longest2 = ac2
                    longest = sqrt(longest2)
                    height = 2.0 * area / longest if longest else 0.0
                    quality = numerator * area / total2 if total2 else 0.0
                    face_area += area
                    if area == 0.0:
                        zero_triangle = True
                    if height < min_height:
                        min_height = height
                    if quality < min_quality:
                        min_quality = quality
                corner_start = corner_end

                if not math.isfinite(face_area):
                    raise ValueError(f"Non-finite area on face {face}")
                if face_area == 0.0:
                    reason, measured, threshold = "zero_face", 0.0, 0.0
                elif zero_triangle:
                    reason, measured, threshold = "zero_triangle", 0.0, 0.0
                elif min_height <= limit:
                    reason, measured, threshold = "low_height", min_height, limit
                elif min_quality <= QUALITY_LIMIT:
                    reason, measured, threshold = "low_quality", min_quality, QUALITY_LIMIT
                elif face_area <= small_area:
                    reason, measured, threshold = "small_area", face_area, small_area
                else:
                    continue
                findings[reason].append(_Finding(
                    face, measured, threshold, face_area, min_height, min_quality,
                ))
        except TargetIncomplete:
            raise
        except Exception as exc:
            raise TargetIncomplete(
                f"Could not assess face areas on {target.shape_path!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        descriptions = {
            "zero_face": "zero total triangulated area",
            "zero_triangle": "a zero-area triangle inside a nonzero face",
            "low_height": "a triangle altitude at most L",
            "low_quality": "a triangle quality at most 0.0001",
            "small_area": "nonzero total area at most L squared",
        }
        issues = []
        for reason in _REASONS:
            found = findings.get(reason, ())
            if not found:
                continue
            count = len(found)
            severity = Severity.ERROR if reason == "zero_face" else Severity.WARNING
            issues.append(Issue(
                check_id=self.check_id,
                severity=severity,
                message=(
                    f"{count} {'face has' if count == 1 else 'faces have'} "
                    f"{descriptions[reason]}."
                ),
                target_path=target.shape_path,
                target_uuid=target.shape_uuid,
                reference_identity=target.reference_identity,
                component_type=ComponentType.FACE,
                component_indices=tuple(item.face for item in found),
                measured_value=min(item.measured for item in found),
                threshold=found[0].threshold,
                details=(("reason", reason),
                         ("face_count", str(count)),
                         ("small_area_cm2", f"{small_area:.9g}"),
                         ("min_face_area_cm2", f"{min(item.area for item in found):.9g}"),
                         ("min_triangle_height_cm", f"{min(item.height for item in found):.9g}"),
                         ("min_triangle_quality", f"{min(item.quality for item in found):.9g}")),
            ))
        return tuple(issues)


__all__ = ["DEFAULT_SETTINGS", "QUALITY_LIMIT", "ZeroAreaFacesRule"]
