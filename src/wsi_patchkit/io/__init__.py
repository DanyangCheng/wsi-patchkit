"""WSI reader backends."""

from .auto import AutoSlideReader
from .base import SlideReader
from .kfbslide import KfbSlideReader
from .openslide import OpenSlideReader
from .tiff import TiffReader

__all__ = [
    "AutoSlideReader",
    "KfbSlideReader",
    "OpenSlideReader",
    "SlideReader",
    "TiffReader",
]
