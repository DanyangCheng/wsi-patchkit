"""Optional browser-based whole-slide image viewer."""

from .app import create_app
from .crops import CropJobQueue
from .overlays import (
    BinaryRasterSource,
    IndexedOverlay,
    OverlayClass,
    OverlayFragment,
    OverlayGeometry,
    OverlayRegistry,
    OverlayRenderer,
    TiffRasterSource,
    load_indexed_overlay_manifest,
)
from .registry import SlideRegistry, SlideSource
from .workers import TileWorkerPool

__all__ = [
    "CropJobQueue",
    "BinaryRasterSource",
    "IndexedOverlay",
    "OverlayClass",
    "OverlayFragment",
    "OverlayGeometry",
    "OverlayRegistry",
    "OverlayRenderer",
    "SlideRegistry",
    "SlideSource",
    "TiffRasterSource",
    "TileWorkerPool",
    "create_app",
    "load_indexed_overlay_manifest",
]
