"""Overlap decisions of one scene and their sidecar file.

The sidecar sits next to the scene as ``<scene file>.validator-intent.json``
(``chair.ma`` and ``chair.mb`` never share one; Windows paths ignore case).
It is read when a scene is bound and written only after Maya really wrote the
scene file, so it always describes a saved scene.

Every decision carries the gates it was made under: the geometry hash, the O
settings and rule version, the tolerance and a digest of the whole group set.
A failed identity or geometry gate deletes the decision (fail closed); a
settings change only keeps it unapplied. A bind check that cannot run keeps
the decisions ``unverified`` instead (``load_error``): never applied, never
dropped for it, and written back unchanged at the next save beside any new
marks, with no scene fingerprint so that the next bind checks every witness.
A sidecar this store cannot read is never overwritten without a timestamped
backup. Nothing here changes the scene; only a failed write sets Maya's
modified flag, so quitting asks to save again.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import math
import os
import re
from typing import Any, Literal

import maya.cmds as cmds

from asset_validator.checks.overlapping_vertices import (
    RULE_VERSION,
    Measurement,
    groups_sha256,
)
from asset_validator.intent.identity import (
    IdentityError,
    IdentityKey,
    absence_reason,
    absolute_dag_path,
    holders,
    is_maya_ascii,
    ma_duplicate_uuids,
    main_file_uuids,
    reference_tops,
    reference_twins,
)


FORMAT = "asset_validator.overlap_intent"
VERSION = 1
SIDECAR_SUFFIX = ".validator-intent.json"

Intent = Literal["intentional", "error"]
GroupIntent = Literal["intentional", "error", "unclassified"]

_INTENTS = ("intentional", "error")
_GROUP_INTENTS = ("intentional", "error", "unclassified")
# Normalisation tie-break: the most frequent decision becomes the default;
# equal counts prefer "no default", then Intentional, then Error.
_TIE_ORDER: tuple[Intent | None, ...] = (None, "intentional", "error")
_UUID = re.compile(r"[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}")
_SHA = re.compile(r"[0-9a-f]{64}")

_DROP_IMPORT = (
    "the mesh was not in the scene when an import started; an imported copy "
    "must not inherit its decision"
)
_DROP_UNOBSERVED = (
    "the scene was saved without the validator and this mesh moved or was "
    "renamed; mark it again"
)
_DROP_LATE = (
    "the scene was changed before the validator started watching it, and this "
    "mesh is no longer where the decision was saved (moved, renamed or "
    "replaced); mark it again"
)


def sidecar_path(scene_path: str) -> str:
    """``chair.ma`` -> ``chair.ma.validator-intent.json`` (extension kept)."""

    return scene_path + SIDECAR_SUFFIX


def file_stat(path: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _move_aside(path: str, label: str) -> str:
    """Rename ``path`` to ``<path>.<label>-<UTC stamp>.json``; returns the new path.

    A rename also works on a read-only file (as a VCS leaves it) in a
    writable folder. An existing backup is never replaced.
    """

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    target = f"{path}.{label}-{stamp}.json"
    number = 1
    while os.path.lexists(target):
        number += 1
        target = f"{path}.{label}-{stamp}-{number}.json"
    os.replace(path, target)
    return target


def _tops_for(keys: Iterable[IdentityKey]) -> dict[str, list[str]]:
    """One reference listing for a pass, skipped when every key is local."""

    return reference_tops() if any(key.ref_chain for key in keys) else {}


def _shared_reason(count: int) -> str:
    return (f"ambiguous identity: {count} reference nodes share one level of its "
            "reference chain")


class _Unverified(Exception):
    """A bind check could not run; every decision stays unapplied, none is dropped."""


@dataclass(frozen=True)
class GroupDecision:
    vertices: tuple[int, ...]
    intent: GroupIntent


@dataclass(frozen=True)
class MeshDecisions:
    """One mesh's decisions in compact form: a default plus explicit overrides."""

    key: IdentityKey
    geometry_sha256: str
    vertex_count: int
    face_count: int
    o_settings: tuple[float, float]
    rule_version: int
    tolerance_cm: float
    group_count: int
    groups_sha256: str
    default: Intent | None
    groups: tuple[GroupDecision, ...]
    updated_utc: str
    witness_path: str
    reference_files: tuple[str, ...] = ()

    def decision_map(self, groups: Iterable[Sequence[int]]) -> dict[tuple[int, ...], Intent | None]:
        """Each group's decision; an explicit entry overrides the default."""

        explicit: dict[tuple[int, ...], Intent | None] = {
            group.vertices: None if group.intent == "unclassified" else group.intent
            for group in self.groups
        }
        return {tuple(group): explicit.get(tuple(group), self.default) for group in groups}

    def decided_count(self) -> int:
        """Groups that carry Intentional or Error."""

        explicit = sum(1 for group in self.groups if group.intent != "unclassified")
        implicit = self.group_count - len(self.groups) if self.default is not None else 0
        return explicit + implicit

    def gate(self) -> tuple:
        return (self.geometry_sha256, self.o_settings, self.tolerance_cm,
                self.rule_version, self.group_count, self.groups_sha256)


def normalise(
    decisions: Mapping[tuple[int, ...], Intent | None],
) -> tuple[Intent | None, tuple[GroupDecision, ...]]:
    """Compact form: the most frequent decision is the default, the rest overrides.

    Marking 20,000 groups Intentional therefore stores one word, not 20,000 rows.
    """

    counts = Counter(decisions.values())
    default = max(_TIE_ORDER, key=lambda value: (counts.get(value, 0), -_TIE_ORDER.index(value)))
    overrides = tuple(
        GroupDecision(group, "unclassified" if decision is None else decision)
        for group, decision in sorted(decisions.items())
        if decision != default
    )
    return default, overrides


# ---------------------------------------------------------------- parsing


class SidecarError(ValueError):
    """The sidecar cannot be trusted; nothing in it is applied."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SidecarError(message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value: Any, where: str, *, positive: bool) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool),
             f"{where} must be a number")
    try:
        number = float(value)
    except OverflowError:
        number = math.inf
    _require(math.isfinite(number), f"{where} must be finite")
    _require(number > 0 if positive else number >= 0,
             f"{where} must be {'greater than 0' if positive else 'at least 0'}")
    return number


def _reject_constant(name: str) -> None:
    raise SidecarError(f"{name} is not valid JSON")


def _parse_mesh(index: int, raw: Any) -> MeshDecisions:
    where = f"meshes[{index}]"
    _require(isinstance(raw, dict), f"{where} must be an object")

    shape_uuid = raw.get("shape_uuid")
    _require(isinstance(shape_uuid, str) and _UUID.fullmatch(shape_uuid) is not None,
             f"{where}.shape_uuid is not an upper-case UUID")
    chain = raw.get("ref_chain")
    _require(isinstance(chain, list) and all(
        isinstance(value, str) and _UUID.fullmatch(value) is not None for value in chain
    ), f"{where}.ref_chain must be a list of upper-case UUIDs")
    for name in ("geometry_sha256", "groups_sha256"):
        value = raw.get(name)
        _require(isinstance(value, str) and _SHA.fullmatch(value) is not None,
                 f"{where}.{name} is not a lower-case sha256")

    vertex_count = raw.get("vertex_count")
    _require(_is_int(vertex_count) and vertex_count >= 1, f"{where}.vertex_count must be >= 1")
    face_count = raw.get("face_count")
    _require(_is_int(face_count) and face_count >= 0, f"{where}.face_count must be >= 0")
    rule_version = raw.get("o_rule_version")
    _require(_is_int(rule_version), f"{where}.o_rule_version must be an integer")
    group_count = raw.get("group_count")
    _require(_is_int(group_count) and group_count >= 1, f"{where}.group_count must be >= 1")
    settings = raw.get("o_settings")
    _require(isinstance(settings, dict), f"{where}.o_settings must be an object")
    min_distance = _number(settings.get("min_distance_cm"), f"{where}.o_settings.min_distance_cm",
                           positive=True)
    bbox_fraction = _number(settings.get("bbox_fraction"), f"{where}.o_settings.bbox_fraction",
                            positive=False)
    tolerance = _number(raw.get("tolerance_cm"), f"{where}.tolerance_cm", positive=True)

    default = raw.get("default")
    _require(default is None or default in _INTENTS,
             f"{where}.default must be null, \"intentional\" or \"error\"")
    raw_groups = raw.get("groups")
    _require(isinstance(raw_groups, list), f"{where}.groups must be a list")
    _require(len(raw_groups) <= group_count, f"{where} lists more groups than group_count")
    groups: list[GroupDecision] = []
    used: set[int] = set()
    for number, group in enumerate(raw_groups):
        place = f"{where}.groups[{number}]"
        _require(isinstance(group, dict), f"{place} must be an object")
        vertices = group.get("vertices")
        _require(isinstance(vertices, list) and len(vertices) >= 2
                 and all(_is_int(vertex) and vertex >= 0 for vertex in vertices),
                 f"{place}.vertices must list at least two vertex indices >= 0")
        _require(all(a < b for a, b in zip(vertices, vertices[1:])),
                 f"{place}.vertices must be strictly increasing")
        _require(used.isdisjoint(vertices), f"{place} overlaps another group")
        used.update(vertices)
        intent = group.get("intent")
        _require(intent in _GROUP_INTENTS, f"{place}.intent {intent!r} is unknown")
        _require(not (intent == "unclassified" and default is None),
                 f"{place} is \"unclassified\" but the entry has no default")
        groups.append(GroupDecision(tuple(vertices), intent))
    decided = sum(1 for group in groups if group.intent != "unclassified")
    if default is not None:
        decided += group_count - len(groups)
    _require(decided > 0, f"{where} holds no decision")

    updated = raw.get("updated_utc")
    _require(isinstance(updated, str), f"{where}.updated_utc must be a string")
    witness = raw.get("witness_path")
    _require(isinstance(witness, str) and witness.startswith("|"),
             f"{where}.witness_path must be a full DAG path")
    files = raw.get("reference_files", [])
    _require(isinstance(files, list) and all(isinstance(name, str) for name in files),
             f"{where}.reference_files must be a list of strings")

    return MeshDecisions(
        key=IdentityKey(shape_uuid, tuple(chain)),
        geometry_sha256=raw["geometry_sha256"],
        vertex_count=vertex_count,
        face_count=face_count,
        o_settings=(min_distance, bbox_fraction),
        rule_version=rule_version,
        tolerance_cm=tolerance,
        group_count=group_count,
        groups_sha256=raw["groups_sha256"],
        default=default,
        groups=tuple(groups),
        updated_utc=updated,
        witness_path=witness,
        reference_files=tuple(files),
    )


@dataclass(frozen=True)
class _Document:
    scene_file: str
    fingerprint: tuple[int, int] | None
    meshes: tuple[MeshDecisions, ...]


def parse_sidecar(text: str) -> _Document:
    """Strictly parse a sidecar; any doubt raises SidecarError. Unknown keys are ignored."""

    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:
        if isinstance(exc, SidecarError):
            raise
        raise SidecarError(f"not valid JSON ({exc})") from exc
    _require(isinstance(data, dict), "the file must hold a JSON object")
    _require(data.get("format") == FORMAT, f"format is not {FORMAT!r}")
    version = data.get("version")
    _require(_is_int(version) and version == VERSION,
             f"version {version!r} is not supported (expected {VERSION})")
    scene_file = data.get("scene_file")
    _require(isinstance(scene_file, str) and bool(scene_file), "scene_file must be a file name")
    fingerprint = data.get("scene_fingerprint")
    if fingerprint is not None:
        _require(isinstance(fingerprint, dict)
                 and _is_int(fingerprint.get("size")) and _is_int(fingerprint.get("mtime_ns")),
                 "scene_fingerprint must hold integer size and mtime_ns")
        fingerprint = (fingerprint["size"], fingerprint["mtime_ns"])
    written = data.get("written_utc")
    _require(written is None or isinstance(written, str), "written_utc must be a string")
    meshes = data.get("meshes")
    _require(isinstance(meshes, list), "meshes must be a list")
    return _Document(scene_file, fingerprint,
                     tuple(_parse_mesh(index, raw) for index, raw in enumerate(meshes)))


def _mesh_json(entry: MeshDecisions) -> dict[str, Any]:
    return {
        "shape_uuid": entry.key.shape_uuid,
        "ref_chain": list(entry.key.ref_chain),
        "geometry_sha256": entry.geometry_sha256,
        "vertex_count": entry.vertex_count,
        "face_count": entry.face_count,
        "o_settings": {
            "min_distance_cm": entry.o_settings[0],
            "bbox_fraction": entry.o_settings[1],
        },
        "o_rule_version": entry.rule_version,
        "tolerance_cm": entry.tolerance_cm,
        "group_count": entry.group_count,
        "groups_sha256": entry.groups_sha256,
        "default": entry.default,
        "groups": [
            {"vertices": list(group.vertices), "intent": group.intent}
            for group in entry.groups
        ],
        "updated_utc": entry.updated_utc,
        "witness_path": entry.witness_path,
        "reference_files": list(entry.reference_files),
    }


# ------------------------------------------------------------------ store


class IntentStore:
    """In-memory decisions of the bound scene, plus their sidecar persistence.

    ``persist=False`` (batch use) never writes a file; its drops stay in memory.
    """

    def __init__(self, *, persist: bool = True) -> None:
        self.persist = persist
        self.bound_path = ""
        self.entries: dict[IdentityKey, MeshDecisions] = {}
        # Parsed decisions a bind check could not verify: never applied or
        # looked up, but written back unchanged; load_error says why.
        self.unverified: dict[IdentityKey, MeshDecisions] = {}
        self.dormant: set[IdentityKey] = set()
        self.notes: list[str] = []
        self.load_error: str | None = None
        self.dirty = False
        self.save_blocked = False
        # Sidecars bound with a load_error: backed up before any overwrite,
        # even after a Save As round trip has cleared load_error.
        self._untrusted: set[str] = set()
        # (file key, stat) of the sidecar this store last parsed or wrote, so
        # write() re-parses an existing file only when it changed on disk.
        self._known: tuple[str, tuple[int, int] | None] | None = None

    # -- binding ---------------------------------------------------------

    def _reset(self, scene_path: str) -> None:
        self.bound_path = scene_path
        self.entries = {}
        self.unverified = {}
        self.dormant = set()
        self.notes = []
        self.load_error = None
        self.dirty = False
        self.save_blocked = False
        self._untrusted = set()
        self._known = None

    def unbind(self) -> None:
        """Forget the decisions of the scene being left (before New or Open)."""

        self._reset("")

    def bind(self, scene_path: str, *, fresh_open: bool = False) -> None:
        """Load the scene's sidecar and drop every decision that cannot be trusted.

        ``fresh_open`` is True only at kAfterOpen of an unmodified scene, when
        the scene in memory is the file just read. Any other bind (a session
        or batch store created later) cannot know what changed in memory, so
        every decision with one holder must still sit at its witness path.
        Nothing is written here; drops reach the file at the next real save.
        """

        self._reset(scene_path)
        if not scene_path:
            return
        path = sidecar_path(scene_path)
        if not os.path.lexists(path):
            return
        try:
            stat = file_stat(path)
            with open(path, "rb") as handle:
                text = handle.read().decode("utf-8")
            document = parse_sidecar(text)
        except Exception as exc:          # any doubt: apply nothing, never overwrite unbacked
            self.load_error = str(exc) or type(exc).__name__
            self._untrusted.add(_file_key(path))
            return
        self._known = (_file_key(path), stat)
        try:
            self._verify_loaded(scene_path, document, fresh_open)
        except Exception as exc:
            # A check that cannot run neither applies nor drops: what the
            # checks that did run left stays unverified. Those decisions never
            # apply, and the next save writes them back unchanged beside any
            # new marks (no backup needed), so reopening checks them again.
            self.unverified, self.entries, self.dormant = dict(self.entries), {}, set()
            if self.unverified:
                self.load_error = (str(exc) if isinstance(exc, _Unverified) else
                                   f"a check raised {type(exc).__name__}: {exc}")

    def _verify_loaded(self, scene_path: str, document: _Document, fresh_open: bool) -> None:
        counts = Counter(entry.key for entry in document.meshes)
        for entry in document.meshes:
            if counts[entry.key] > 1:
                self.notes.append(
                    f"Overlap decisions for {entry.witness_path} were dropped: the "
                    "sidecar lists this mesh more than once."
                )
                self.dirty = True
            else:
                self.entries[entry.key] = entry
        if not self.entries:
            return

        # Maya re-randomizes a UUID that an ASCII file assigns twice, so the
        # scene in memory looks clean. The file type comes from the header,
        # and only UUIDs the main file itself assigns are counted.
        try:
            if is_maya_ascii(scene_path):
                wanted = {value for key in self.entries for value in main_file_uuids(key)}
                duplicated = ma_duplicate_uuids(scene_path, wanted)
            else:
                duplicated = frozenset()
        except Exception as exc:
            raise _Unverified(
                f"the scene file could not be scanned for duplicate UUIDs "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        for key in list(self.entries):
            clash = sorted(duplicated.intersection(main_file_uuids(key)))
            if clash:
                self.drop(key, f"the .ma file holds UUID {clash[0]} more than once; "
                               "Maya re-assigned one copy on open")

        tops = _tops_for(self.entries)
        single: dict[IdentityKey, Any] = {}
        for key in list(self.entries):
            twins = reference_twins(key, tops)
            if twins:
                self.drop(key, _shared_reason(twins))
                continue
            try:
                found = holders(key)
            except IdentityError:
                continue                          # re-checked when applied
            if len(found) == 1:
                single[key] = found[0]
            elif len(found) > 1:
                self.drop(key, f"ambiguous identity: {len(found)} meshes share it")
            else:
                absence = self._absence(key)
                if absence == "unloaded":
                    self.dormant.add(key)
                elif absence is None:
                    self.drop(key, "mesh not found")

        unobserved = (
            document.fingerprint is None
            or file_stat(scene_path) != document.fingerprint
            or os.path.normcase(os.path.basename(scene_path))
            != os.path.normcase(document.scene_file)
        )
        if unobserved or not fresh_open:
            # After an unobserved save, or edits made before this store was
            # bound, the single holder may be another copy with identical
            # geometry: a .ma reopen re-randomizes duplicated UUIDs by file
            # order, and a re-imported copy takes a deleted copy's UUID.
            # Its path is the only witness, for every file format.
            reason = _DROP_UNOBSERVED if unobserved else _DROP_LATE
            for key, node in single.items():
                entry = self.entries.get(key)
                if entry is not None and absolute_dag_path(node) != entry.witness_path:
                    self.drop(key, reason)

    def lookup(self, key: IdentityKey) -> MeshDecisions | None:
        return self.entries.get(key)

    # -- changes ---------------------------------------------------------

    def record(
        self,
        key: IdentityKey,
        *,
        measurement: Measurement,
        settings: tuple[float, float],
        all_groups: Sequence[Sequence[int]],
        chosen: Iterable[Sequence[int]],
        intent: Intent | None,
        witness: str,
        reference_files: Iterable[str] = (),
    ) -> int:
        """Set ``chosen`` groups to ``intent`` (None clears); return how many changed.

        An entry made under other geometry or settings is replaced, never merged.
        """

        if intent is not None and intent not in _INTENTS:
            raise ValueError(f"Unknown overlap intent {intent!r}")
        if measurement.geometry_sha256 is None or measurement.face_count is None:
            raise ValueError("record() needs a fingerprinted measurement")
        all_groups = tuple(tuple(group) for group in all_groups)
        known = set(all_groups)
        chosen = [tuple(group) for group in chosen]
        missing = [group for group in chosen if group not in known]
        if missing:
            raise ValueError(f"{missing[0]!r} is not an overlap group of this mesh")
        settings = (float(settings[0]), float(settings[1]))
        digest = groups_sha256(all_groups)
        gate = (measurement.geometry_sha256, settings, measurement.tolerance,
                RULE_VERSION, len(all_groups), digest)

        existing = self.entries.get(key)
        changed = 0
        if existing is not None and existing.gate() != gate:
            changed += existing.decided_count()
            self.notes.append(
                f"Earlier overlap decisions on {existing.witness_path} were replaced "
                "(different geometry or settings)."
            )
            existing = None
        decisions = (existing.decision_map(all_groups) if existing is not None
                     else dict.fromkeys(all_groups))
        for group in chosen:
            if decisions[group] != intent:
                decisions[group] = intent
                changed += 1
        self.dormant.discard(key)
        if not changed:
            return 0
        if key in self.unverified:
            # Never merged: the old entry's identity was not verified. The
            # file that holds it is backed up before the next write.
            replaced = self._forget_unverified(key)
            if self.bound_path:
                self._untrusted.add(_file_key(sidecar_path(self.bound_path)))
            self.notes.append(
                f"Unverified overlap decisions on {replaced.witness_path} were replaced by "
                "the new marks; the sidecar is backed up before the next save writes it."
            )

        default, overrides = normalise(decisions)
        if default is None and not overrides:
            self.entries.pop(key, None)
        else:
            self.entries[key] = MeshDecisions(
                key=key,
                geometry_sha256=measurement.geometry_sha256,
                vertex_count=measurement.vertex_count,
                face_count=measurement.face_count,
                o_settings=settings,
                rule_version=RULE_VERSION,
                tolerance_cm=measurement.tolerance,
                group_count=len(all_groups),
                groups_sha256=digest,
                default=default,
                groups=overrides,
                updated_utc=_utc_now(),
                witness_path=witness,
                reference_files=tuple(reference_files),
            )
        self.dirty = True
        return changed

    def drop(self, key: IdentityKey, reason: str) -> None:
        """Delete a decision permanently; the note keeps where it was."""

        entry = self.entries.pop(key, None)
        if entry is None and key in self.unverified:
            entry = self._forget_unverified(key)
        self.dormant.discard(key)
        if entry is None:
            return
        self.dirty = True
        self.notes.append(
            f"Overlap decisions for {entry.witness_path} ({entry.decided_count()} of "
            f"{entry.group_count} group(s)) were dropped: {reason}."
        )

    def _forget_unverified(self, key: IdentityKey) -> MeshDecisions:
        """Remove one unverified entry; the load error goes with the last one."""

        entry = self.unverified.pop(key)
        if not self.unverified:
            self.load_error = None
        return entry

    @staticmethod
    def _absence(key: IdentityKey) -> Literal["unloaded", "unknown"] | None:
        """``absence_reason`` that reports a failed query as "unknown"."""

        try:
            return absence_reason(key)
        except Exception:
            return "unknown"

    def verify_identities(self, event: str) -> None:
        """After an import or reference change: drop ambiguous and vanished keys.

        Unverified entries get the same live checks; they only never apply.
        """

        del event
        keys = [*self.entries, *self.unverified]
        tops = _tops_for(keys)
        for key in keys:
            twins = reference_twins(key, tops)
            if twins:
                self.drop(key, _shared_reason(twins))
                continue
            try:
                found = holders(key)
            except IdentityError:
                continue
            if len(found) == 1:
                self.dormant.discard(key)
            elif len(found) > 1:
                self.drop(key, f"ambiguous identity: {len(found)} meshes share it")
            else:
                absence = self._absence(key)
                if absence == "unloaded":
                    if key in self.entries:
                        self.dormant.add(key)
                elif absence is None:
                    self.drop(key, "mesh not found")
                # "unknown": kept; an absent mesh is never annotated anyway

    def drop_absent(self, event: str) -> None:
        """Before an import: a missing mesh's key could come back on an imported copy.

        Re-importing a deleted importReference copy recreates its UUID on a
        different mesh, so its decision must go before the import.
        A decision waiting behind an unloaded reference stays, for every
        ``event``: its chain still names that reference. If an importReference
        turns another copy's child reference into a top-level node with the
        same UUID, two reference nodes share a level of the chain, and
        ``verify_identities`` (kAfterImportReference) or apply's own twin
        check drops it then.
        """

        del event
        for key in [*self.entries, *self.unverified]:
            try:
                if holders(key):
                    continue
            except IdentityError:
                continue
            if self._absence(key) != "unloaded":
                self.drop(key, _DROP_IMPORT)        # "unknown" too: fail closed

    # -- saving ----------------------------------------------------------

    def before_save(self) -> None:
        """Re-verify identities and refresh each witness path before Maya writes."""

        self.save_blocked = False
        try:
            self.verify_identities("save")
            for key, entry in list(self.entries.items()):
                try:
                    found = holders(key)
                except IdentityError:
                    continue
                if len(found) == 1:
                    witness = absolute_dag_path(found[0])
                    if witness != entry.witness_path:
                        self.entries[key] = replace(entry, witness_path=witness)
        except Exception as exc:
            self.save_blocked = True
            self.notes.append(
                f"Overlap decisions could not be verified before the save "
                f"({type(exc).__name__}: {exc}); they were not written."
            )

    def after_save(self, scene_path: str, before_stat: tuple[int, int] | None) -> None:
        """Write the sidecar only when the scene file itself was written."""

        now = file_stat(scene_path)
        if now is None or now == before_stat:
            return
        if (os.path.normcase(scene_path) != os.path.normcase(self.bound_path)
                and not self.unverified):
            # Save As: the load error belongs to the scene left behind; its
            # file stays untrusted (_untrusted) if the artist saves back to it.
            # Unverified decisions belong to the scene in memory and move on.
            self.load_error = None
        self.bound_path = scene_path
        if not self.persist:
            return
        if self.save_blocked:
            self.notes.append("Overlap decisions were not written with this save.")
            return
        path = sidecar_path(scene_path)
        if (self.unverified and not self.entries and not self.dirty
                and self._known == (_file_key(path), file_stat(path))):
            # The file already holds exactly these decisions, and its old
            # fingerprint makes the next bind check every witness anyway.
            return
        if not self.entries and not self.unverified:
            if not os.path.lexists(path):
                return                            # never had decisions: no file
            if not self._replaceable(path):
                # Keep a file this version cannot read (for example a newer
                # one) until the artist makes new decisions; write() then
                # backs it up. No decision in memory says nothing either.
                self.dirty = False
                return
        self.write(scene_path, fingerprint=now)

    def _document(self, scene_path: str, fingerprint: tuple[int, int] | None) -> dict[str, Any]:
        meshes = {**self.unverified, **self.entries}
        if self.unverified:
            # Their bind checks never ran: no fingerprint makes the next bind
            # treat this save as unobserved and check every witness path.
            fingerprint = None
        return {
            "format": FORMAT,
            "version": VERSION,
            "scene_file": os.path.basename(scene_path),
            "scene_fingerprint": (
                None if fingerprint is None
                else {"size": fingerprint[0], "mtime_ns": fingerprint[1]}
            ),
            "written_utc": _utc_now(),
            "meshes": [_mesh_json(meshes[key]) for key in sorted(meshes)],
        }

    def _replaceable(self, path: str) -> bool:
        """Whether the sidecar at ``path`` may be overwritten without a backup.

        True for no file, for the file this store last parsed or wrote, or for
        one that parses now. A corrupt or newer file, or one bound with a
        load_error, is never overwritten unbacked, whatever load_error says now.
        """

        if not os.path.isfile(path):
            return True                           # nothing there (or a folder: write fails)
        name = _file_key(path)
        if name in self._untrusted:
            return False
        if self._known is not None and self._known == (name, file_stat(path)):
            return True
        try:
            with open(path, "rb") as handle:
                parse_sidecar(handle.read().decode("utf-8"))
        except (OSError, ValueError):             # SidecarError and bad UTF-8 are ValueErrors
            return False
        return True

    def write(self, scene_path: str, *, fingerprint: tuple[int, int] | None = None) -> bool:
        """Atomically write the sidecar; on failure keep everything in memory.

        A failed write must not leave an older sidecar to be trusted at the
        next open, so ``_write_failed`` moves it aside.
        """

        path = sidecar_path(scene_path)
        temporary = f"{path}.tmp-{os.getpid()}"
        if fingerprint is None:
            fingerprint = file_stat(scene_path)
        try:
            if not self._replaceable(path):
                _move_aside(path, "unreadable")
            text = json.dumps(self._document(scene_path, fingerprint), indent=1)
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text + "\n")
            os.replace(temporary, path)
        except OSError as exc:
            try:
                os.remove(temporary)
            except OSError:
                pass
            self._write_failed(path, exc)
            return False
        self.dirty = False
        if not self.unverified:
            self.load_error = None
        self._untrusted.discard(_file_key(path))
        self._known = (_file_key(path), file_stat(path))
        return True

    def _write_failed(self, path: str, exc: OSError) -> None:
        """Make the older sidecar untrusted, and say so now, not at the next action.

        The older file still holds the previous save's decisions, which the
        artist may have changed or cleared since; trusted at the next open it
        would bring them back. Moved aside, it is not read; Maya's modified
        flag makes quitting ask to save (and write) again.
        """

        self.dirty = True
        name = os.path.basename(path)
        text = (f"Overlap decisions could not be written to {name} ({exc}); they stay "
                "in memory until the scene is saved again.")
        if os.path.isfile(path):
            try:
                moved = _move_aside(path, "stale")
            except OSError as move_error:
                text += (f" The older {name} could not be moved aside either ({move_error}) "
                         "and may hold out-of-date decisions.")
            else:
                text += (f" The older file was moved aside to {os.path.basename(moved)}, "
                         "so it is not trusted.")
        self.notes.append(text)
        try:
            cmds.file(modified=True)
            cmds.warning(text)
        except Exception:                         # reporting must not hide the note
            pass

    # -- reporting -------------------------------------------------------

    def summary(self) -> str:
        """One status line for the window; empty when there is nothing to say."""

        name = os.path.basename(sidecar_path(self.bound_path)) if self.bound_path else ""
        if self.load_error is not None and not self.unverified:
            return (f"Overlap decisions: {name} could not be read ({self.load_error}); "
                    "none applied. Saving with new decisions keeps a backup.")
        text = ""
        if self.entries or self.dirty:
            count = len(self.entries)
            if not self.bound_path:
                text = f"Overlap decisions: {count} (unsaved scene; kept until you save)"
            elif self.dirty:
                text = f"Overlap decisions: {count}; save the scene to keep changes ({name})"
            else:
                text = f"Overlap decisions: {count} stored in {name}"
            if self.dormant:
                text += f"; {len(self.dormant)} waiting for an unloaded reference"
        if self.unverified:
            held = (f"{len(self.unverified)} in {name} could not be verified ({self.load_error}); "
                    "none applied until the scene is opened again, and saving keeps them")
            text = f"{text}; {held}" if text else f"Overlap decisions: {held}"
        return text

    def take_notes(self) -> list[str]:
        notes, self.notes = self.notes, []
        return notes


__all__ = [
    "FORMAT", "GroupDecision", "Intent", "IntentStore", "MeshDecisions", "SIDECAR_SUFFIX",
    "SidecarError", "VERSION", "file_stat", "normalise", "parse_sidecar", "sidecar_path",
]
