"""Maya scene access and API snapshots."""

from .scope import _reference_identity as reference_identity
from .scope import resolve_scope

__all__ = ["reference_identity", "resolve_scope"]
