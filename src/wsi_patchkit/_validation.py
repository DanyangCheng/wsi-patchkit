"""Shared strict numeric checks for sampling APIs."""

from __future__ import annotations

import math
from numbers import Integral, Real


def integer(value: object, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} must be an integer, excluding bool, got {value!r}")
    result = int(value)
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value!r}")
    return result


def real(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite real number, excluding bool")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(result) or (positive and result <= 0):
        suffix = " and positive" if positive else ""
        raise ValueError(f"{name} must be finite{suffix}, got {value!r}")
    return result


def pair(value: object, name: str) -> tuple[object, object]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must contain exactly two values")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{name} must contain exactly two values") from error
    if len(items) != 2:
        raise ValueError(f"{name} must contain exactly two values")
    return items  # type: ignore[return-value]


def physical_pair(value: object, name: str, *, scalar: bool = False):
    items = (value, value) if scalar and isinstance(value, Real) else pair(value, name)
    return tuple(
        real(item, f"{name}[{axis}]", positive=scalar)
        for axis, item in enumerate(items)
    )


def option(value: object, name: str, choices: tuple[str, ...]) -> None:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{name} must be one of {choices!r}, got {value!r}")


def derived_finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} derived range is not finite (overflow)")
    return value


def finite_product(left: int | float, right: float, name: str) -> float:
    try:
        return derived_finite(left * right, name)
    except OverflowError as error:
        raise ValueError(f"{name} derived range overflow") from error
