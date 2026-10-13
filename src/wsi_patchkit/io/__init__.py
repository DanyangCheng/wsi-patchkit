"""WSI reader backends."""

from .auto import AutoSlideReader
from .base import SlideReader
from .image import ImageReader
from .kfbslide import KfbSlideReader
from .openslide import OpenSlideReader
from .tiff import TiffReader

__all__ = [
    "AutoSlideReader",
    "ImageReader",
    "KfbSlideReader",
    "OpenSlideReader",
    "SlideReader",
    "TiffReader",
]
