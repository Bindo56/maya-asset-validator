"""Report unwelded vertex pairs closer than L that no edge joins.

L = max(min_distance_cm, bbox_fraction x the mesh's object-space bounding-box
diagonal), by default max(0.001 cm, 0.000001 x diagonal). Distances and L are
measured in object space: that avoids world-matrix noise and equals world space
once T passes. Pairs joined by an edge are skipped, because the edge-length
check owns them.

The search is a spatial hash. Each vertex gets integer cell coordinates of
size L; two vertices within L differ by at most one cell per axis, so they
share a cell in at least one of 8 grids of size 2L shifted by 0 or 1 cell per
axis. A mesh with no near pair costs 8 set-uniqueness checks at C speed; only
vertices that share a cell get the exact squared-distance test.

Positions are copied from one point array kept alive for the whole copy.

The ``intent`` package reuses the measurement: ``measure`` is the rule's own
point read and pair search, optionally with a geometry fingerprint from the
same read, and ``overlap_groups`` splits the pairs into the vertex groups an
artist marks.
"""

from __future__ import annotations

from array import array
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
import hashlib
from itertools import combinations, product, repeat
import math
from operator import add, mul, rshift
import struct
import sys
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
    "min_distance_cm": 0.001,     # floor of L
    "bbox_fraction": 0.000001,    # L grows with the object-space bbox diagonal
}
MAX_BBOX_FRACTION = 0.01          # L above 1% of the diagonal is no longer "overlap"
# A mesh with this many overlapping pairs is reported Incomplete, never Pass:
# listing them would cost more memory than the report is worth.
MAX_PAIRS = 5_000_000
# Distinct vertex positions that may share one 2L hash cell. More means a
# near-collapsed region whose pairwise test would be quadratic.
MAX_CELL_GROUP = 4096
# Pairs written into Issue.details; component_indices always lists every vertex.
MAX_LISTED_PAIRS = 1000
# Stored with every overlap decision. Bump it whenever pairing,
# edge filtering, grouping or hashing changes, so old decisions stop applying.
# 2: the geometry hash folds -0.0 into 0.0 (tag avfp2).
RULE_VERSION = 2


def read_settings(context: ValidationContext) -> tuple[float, float]:
    supplied = context.settings.get("O", {})
    unknown = sorted(set(supplied) - set(DEFAULT_SETTINGS))
    if unknown:
        raise ValueError(f"O does not accept settings: {unknown!r}")
    values = {}
    for key, default in DEFAULT_SETTINGS.items():
        value = supplied.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"O setting {key!r} must be a number, not {value!r}")
        try:
            number = float(value)
        except OverflowError:
            number = math.inf
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"O setting {key!r} must be finite and >= 0, not {value!r}")
        values[key] = number
    if values["min_distance_cm"] <= 0:
        raise ValueError("O setting 'min_distance_cm' must be greater than 0")
    if values["bbox_fraction"] > MAX_BBOX_FRACTION:
        raise ValueError(
            f"O setting 'bbox_fraction' must be at most {MAX_BBOX_FRACTION}, "
            f"not {supplied['bbox_fraction']!r}"
        )
    return values["min_distance_cm"], values["bbox_fraction"]


@dataclass(frozen=True)
class _Prepared:
    owners: frozenset[TargetRef]
    min_distance: float
    bbox_fraction: float


def _near_pairs(xs, ys, zs, tolerance: float) -> dict[tuple[int, int], float]:
    """(i, j) -> distance for distinct positions within ``tolerance``."""

    cell = tolerance * (1.0 + 1e-6)          # margin for rounding in x / cell
    inverse = 1.0 / cell
    halves = []
    for values in (xs, ys, zs):
        cells = list(map(math.floor, map(mul, values, repeat(inverse))))
        halves.append((
            list(map(rshift, cells, repeat(1))),
            list(map(rshift, map(add, cells, repeat(1)), repeat(1))),
        ))

    limit = tolerance * tolerance
    found: dict[tuple[int, int], float] = {}
    seen_groups: set[tuple[int, ...]] = set()
    count = len(xs)
    for shift_x, shift_y, shift_z in product((0, 1), repeat=3):
        keys = list(zip(halves[0][shift_x], halves[1][shift_y], halves[2][shift_z]))
        if len(set(keys)) == count:
            continue
        shared = {key for key, members in Counter(keys).items() if members > 1}
        groups: dict[tuple[int, int, int], list[int]] = defaultdict(list)
        for index, key in enumerate(keys):
            if key in shared:
                groups[key].append(index)
        for members in groups.values():
            # The same group often reappears in another shifted grid.
            signature = tuple(members)
            if signature in seen_groups:
                continue
            seen_groups.add(signature)
            if len(members) > MAX_CELL_GROUP:
                raise TargetIncomplete(
                    f"More than {MAX_CELL_GROUP} distinct vertex positions within "
                    f"{2 * tolerance:.6g} cm of each other; this region is too "
                    "dense or collapsed to list its overlaps"
                )
            for i, j in combinations(members, 2):
                if (i, j) in found:
                    continue
                dx, dy, dz = xs[i] - xs[j], ys[i] - ys[j], zs[i] - zs[j]
                squared = dx * dx + dy * dy + dz * dz
                if squared <= limit:
                    found[(i, j)] = math.sqrt(squared)
                    if len(found) > MAX_PAIRS:
                        raise TargetIncomplete(f"More than {MAX_PAIRS} overlapping vertex pairs")
    return found


def find_close_pairs(xs, ys, zs, tolerance: float) -> list[tuple[int, int, float]]:
    """Every (i, j, distance) with i < j and distance <= tolerance, sorted by (i, j).

    Coincident vertices are the common overlap, so exact duplicates are bucketed
    first; the spatial hash then runs on one representative per position, and
    its pairs are expanded back to every vertex at those positions.
    """

    count = len(xs)
    positions = list(zip(xs, ys, zs))
    if len(set(positions)) == count:
        return sorted((i, j, d) for (i, j), d in _near_pairs(xs, ys, zs, tolerance).items())

    # Representative = lowest index at a position (-0.0 and 0.0 compare equal).
    representative: dict[tuple[float, float, float], int] = {}
    members: dict[int, list[int]] = defaultdict(list)
    for index, position in enumerate(positions):
        first = representative.setdefault(position, index)
        members[first].append(index)
    unique = list(representative.values())          # ascending index order

    pair_total = sum(len(group) * (len(group) - 1) // 2 for group in members.values())
    if pair_total > MAX_PAIRS:
        raise TargetIncomplete(f"More than {MAX_PAIRS} overlapping vertex pairs")
    pairs = [
        (i, j, 0.0)
        for group in members.values() if len(group) > 1
        for i, j in combinations(group, 2)
    ]

    near = _near_pairs(
        [xs[i] for i in unique], [ys[i] for i in unique], [zs[i] for i in unique], tolerance,
    )
    for (a, b), distance in near.items():
        group_a, group_b = members[unique[a]], members[unique[b]]
        pair_total += len(group_a) * len(group_b)
        if pair_total > MAX_PAIRS:
            raise TargetIncomplete(f"More than {MAX_PAIRS} overlapping vertex pairs")
        pairs.extend(
            (min(i, j), max(i, j), distance) for i in group_a for j in group_b
        )
    pairs.sort()
    return pairs


@dataclass(frozen=True)
class Measurement:
    """O's view of one mesh: the edge-filtered pairs and the tolerance used."""

    tolerance: float
    close: tuple[tuple[int, int, float], ...]   # edge-filtered, sorted by (i, j)
    vertex_count: int
    face_count: int | None = None
    geometry_sha256: str | None = None


def _little_endian(values: array) -> bytes:
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def geometry_sha256(xs, ys, zs, counts, connects) -> str:
    """Hash object-space points (float64) and face topology (int32).

    Renames, reparenting, transform moves, UV edits and reopening keep the hash;
    a vertex move, a merge or a reversed face changes it. Never ``str()`` an API
    array here: that takes about 21 s for a million ints.

    Signed zero is folded first (``v + 0.0`` turns -0.0 into 0.0 and leaves
    every other value as it is): an OBJ import keeps -0.0, and the .ma writer
    saves it as "0", so a reopen would otherwise read as an edit.
    """

    digest = hashlib.sha256(b"avfp2")
    digest.update(struct.pack("<Q", len(xs)))
    for values in (xs, ys, zs):
        digest.update(_little_endian(array("d", map(add, values, repeat(0.0)))))
    counts = list(counts)
    digest.update(struct.pack("<Q", len(counts)))
    for values in (counts, connects):
        digest.update(_little_endian(array("i", list(values))))
    return digest.hexdigest()


def overlap_groups(close: Iterable[Sequence]) -> tuple[tuple[int, ...], ...]:
    """Connected components over the (i, j) pairs: the units an artist classifies.

    Groups are transitive: a chain a-b-c spaced just under L is one group even
    though a and c are farther apart than L.
    """

    parent: dict[int, int] = {}

    def find(vertex: int) -> int:
        parent.setdefault(vertex, vertex)
        while parent[vertex] != vertex:
            parent[vertex] = parent[parent[vertex]]
            vertex = parent[vertex]
        return vertex

    for pair in close:
        first, second = find(pair[0]), find(pair[1])
        if first != second:
            parent[max(first, second)] = min(first, second)
    members: dict[int, list[int]] = defaultdict(list)
    for vertex in parent:
        members[find(vertex)].append(vertex)
    return tuple(sorted(tuple(sorted(group)) for group in members.values()))


def groups_sha256(groups: Iterable[Sequence[int]]) -> str:
    """Digest of a whole group set; any split, merge or renumbering changes it."""

    text = ";".join(",".join(map(str, group)) for group in groups)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def measure(
    dag_path,
    shape_path: str,
    min_distance: float,
    bbox_fraction: float,
    *,
    fingerprint: bool = False,
) -> Measurement | None:
    """Read one mesh once and find its unwelded close pairs; None if it has no vertices.

    ``run_target`` and the intent gate share this read, so a stored
    fingerprint and the pairs it vouches for always come from the same points.
    """

    from maya import cmds
    from maya.api import OpenMaya as om

    # An uninitialized mesh node cannot construct MFnMesh; it has no
    # vertices to compare.
    if cmds.polyEvaluate(shape_path, vertex=True) == 0:
        return None
    fn = om.MFnMesh(dag_path)
    # Keep the point array alive while copying into Python floats.
    points = fn.getPoints(om.MSpace.kObject)
    xs = [point.x for point in points]
    ys = [point.y for point in points]
    zs = [point.z for point in points]
    del points
    if not all(math.isfinite(sum(map(abs, values))) for values in (xs, ys, zs)):
        raise ValueError("Non-finite vertex position")
    diagonal = math.sqrt(
        (max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2 + (max(zs) - min(zs)) ** 2
    )
    tolerance = max(min_distance, bbox_fraction * diagonal)
    magnitude = max(abs(min(xs)), abs(max(xs)), abs(min(ys)), abs(max(ys)),
                    abs(min(zs)), abs(max(zs)))
    if not math.isfinite(1.0 / tolerance) or magnitude / tolerance > 2.0 ** 60:
        raise TargetIncomplete(
            f"O tolerance {tolerance!r} cm is out of range for vertex "
            f"coordinates up to {magnitude:.6g} cm"
        )
    close = find_close_pairs(xs, ys, zs, tolerance)
    if close:
        # Skip pairs joined by an edge; the edge-length check owns them.
        # Only the lower vertex of each pair needs its neighbours.
        vertices = om.MItMeshVertex(dag_path)
        neighbours: dict[int, set[int]] = {}
        for index in sorted({i for i, _, _ in close}):
            vertices.setIndex(index)
            neighbours[index] = set(vertices.getConnectedVertices())
        close = [(i, j, d) for i, j, d in close if j not in neighbours[i]]

    face_count = digest = None
    if fingerprint:
        # Both arrays stay referenced until the hash has copied them.
        counts, connects = fn.getVertices()
        face_count = len(counts)
        digest = geometry_sha256(xs, ys, zs, counts, connects)
        del counts, connects
    return Measurement(tolerance, tuple(close), len(xs), face_count, digest)


class OverlappingVerticesRule:
    """Warn, for review, about unwelded vertices closer than L."""

    check_id = "O"
    metadata = CheckMetadata(
        label="Overlapping vertices",
        hint=(
            "Review the listed pairs (closest first). Merge accidental "
            "duplicates with Edit Mesh > Merge, using a threshold just above "
            "the issue's largest distance (largest_distance_cm); leave intended "
            "splits, such as separate shells, as they are."
        ),
        can_fix=False,
    )

    def prepare(self, context: ValidationContext) -> _Prepared:
        min_distance, bbox_fraction = read_settings(context)

        from maya.api import OpenMaya as om

        # Instance paths share one DAG node; nested and imported copies can
        # share UUIDs. A path that no longer resolves keeps its own owner
        # entry, so only that target fails and the others are still checked.
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
    ) -> Iterator[Issue]:
        del context
        state = cast(_Prepared, prepared)
        if target not in state.owners:
            return

        from maya.api import OpenMaya as om

        # A deleted or renamed target raises here: an Error for this target only.
        selection = om.MSelectionList()
        selection.add(target.shape_path)
        dag_path = selection.getDagPath(0)

        try:
            measured = measure(dag_path, target.shape_path, state.min_distance, state.bbox_fraction)
        except TargetIncomplete:
            raise
        except Exception as exc:
            raise TargetIncomplete(
                f"Could not read vertex positions on {target.shape_path!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if measured is None:
            return
        close, tolerance = list(measured.close), measured.tolerance
        if not close:
            return
        # Closest pairs first (exact duplicates lead), so a capped list still
        # shows the likeliest accidental duplicates.
        by_distance = sorted(close, key=lambda pair: (pair[2], pair[0], pair[1]))
        listed = " ".join(f"{i}-{j}" for i, j, _ in by_distance[:MAX_LISTED_PAIRS])
        details = [
            ("pairs", listed),
            ("pair_count", str(len(close))),
            ("tolerance_cm", f"{tolerance:.6g}"),
            ("smallest_distance_cm", f"{by_distance[0][2]:.6g}"),
            ("largest_distance_cm", f"{by_distance[-1][2]:.6g}"),
        ]
        if len(close) > MAX_LISTED_PAIRS:
            details.append(("pairs_not_listed", str(len(close) - MAX_LISTED_PAIRS)))
        count = len(close)
        yield Issue(
            check_id=self.check_id,
            severity=Severity.WARNING,
            needs_review=True,
            message=(
                f"{count} unwelded vertex {'pair is' if count == 1 else 'pairs are'} "
                f"closer than {tolerance:.6g} cm with no edge between them."
            ),
            target_path=target.shape_path,
            target_uuid=target.shape_uuid,
            reference_identity=target.reference_identity,
            component_type=ComponentType.VERTEX,
            component_indices=tuple(sorted({v for i, j, _ in close for v in (i, j)})),
            measured_value=max(d for _, _, d in close),
            threshold=tolerance,
            details=tuple(details),
        )


__all__ = [
    "DEFAULT_SETTINGS", "Measurement", "OverlappingVerticesRule", "RULE_VERSION",
    "find_close_pairs", "geometry_sha256", "groups_sha256", "measure", "overlap_groups",
    "read_settings",
]
