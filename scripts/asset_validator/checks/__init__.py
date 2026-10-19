"""Read-only validation rules."""

from .duplicate_names import DuplicateNamesRule
from .freeze_transforms import FreezeTransformsRule
from .non_manifold import NonManifoldRule
from .lamina import LaminaRule
from .uv_area import UVAreaRule
from .overlapping_vertices import OverlappingVerticesRule
from .naming_convention import NamingConventionRule
from .zero_length_edges import ZeroLengthEdgesRule
from .zero_area_faces import ZeroAreaFacesRule
from .ngons import NgonsRule

__all__ = [
    "DuplicateNamesRule", "FreezeTransformsRule", "NonManifoldRule",
    "LaminaRule", "UVAreaRule", "OverlappingVerticesRule",
    "NamingConventionRule", "ZeroLengthEdgesRule", "ZeroAreaFacesRule",
    "NgonsRule",
]
