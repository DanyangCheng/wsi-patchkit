"""One virtual-canvas rounding rule for specifications and aligned readers."""

from __future__ import annotations

import math
from typing import Literal

from ._validation import derived_finite, finite_product, option

CanvasRounding = Literal["round", "ceil"]


def canvas_size(dimensions, source_mpp, target_mpp, rounding: CanvasRounding):
    option(rounding, "canvas_rounding", ("round", "ceil"))
    quantize = round if rounding == "round" else math.ceil
    return tuple(
        max(
            1,
            quantize(
                derived_finite(
                    finite_product(
                        dimensions[axis], source_mpp[axis], f"canvas_size[{axis}]"
                    )
                    / target_mpp[axis],
                    f"canvas_size[{axis}]",
                )
            ),
        )
        for axis in (0, 1)
    )
