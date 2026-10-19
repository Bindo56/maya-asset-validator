"""The artist's overlap decisions (Intentional or Error), per mesh.

Decisions are keyed by shape UUID plus the full reference chain, gated by a
geometry hash, the O settings and the overlap group set, and kept in a
``<scene file>.validator-intent.json`` sidecar written after real saves.
Validation never applies them: ``apply_intent`` is an explicit post-step that
returns the same report object when nothing applies. No module here imports Qt.

``session`` is imported on demand (``from asset_validator.intent import
session``) so that reloading it never leaves a stale alias here.
"""

from .apply import (
    ClassifyOutcome,
    ClassifyRefused,
    GroupRow,
    GroupsView,
    IntentResult,
    apply_intent,
    cached_measure,
    classify,
    describe_groups,
    validate_with_intent,
)
from .identity import IdentityError, IdentityKey, holders, identity_key
from .store import GroupDecision, IntentStore, MeshDecisions, sidecar_path

__all__ = [
    "ClassifyOutcome",
    "ClassifyRefused",
    "GroupDecision",
    "GroupRow",
    "GroupsView",
    "IdentityError",
    "IdentityKey",
    "IntentResult",
    "IntentStore",
    "MeshDecisions",
    "apply_intent",
    "cached_measure",
    "classify",
    "describe_groups",
    "holders",
    "identity_key",
    "sidecar_path",
    "validate_with_intent",
]
