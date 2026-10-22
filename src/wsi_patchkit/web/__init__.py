"""Optional browser-based whole-slide image viewer."""

from .app import create_app
from .crops import CropJobQueue
from .detections import DetectionOverlay, load_detection_overlay_manifest
from .exporters import CropBundle, ExporterRegistry
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
    load_overlay_manifest,
)
from .registry import SlideRegistry, SlideSource
from .workers import TileWorkerPool

__all__ = [
    "DetectionOverlay",
    "load_detection_overlay_manifest",
    "load_overlay_manifest",
    "CropJobQueue",
    "CropBundle",
    "ExporterRegistry",
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
