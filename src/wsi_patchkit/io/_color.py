"""Pixel-exact RGB conversion without large uint32 temporary arrays."""

import numpy as np
from numpy.typing import NDArray
from PIL import Image


def image_to_rgb(image: Image.Image) -> NDArray[np.uint8]:
    """Composite straight RGBA over white using Pillow's integer arithmetic."""
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    return np.array(image, dtype=np.uint8)
