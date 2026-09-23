"""Explicit, path-safe registration of slides exposed by the web viewer."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..types import MPP, as_mpp

_SLIDE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def valid_public_id(value: str, *, name: str = "public ID") -> str:
    """Validate an identifier that is safe to expose as one URL segment."""
    if not isinstance(value, str) or not _SLIDE_ID.fullmatch(value):
        raise ValueError(
            f"{name} must use 1-128 letters, numbers, dots, dashes, "
            "or underscores"
        )
    return value


@dataclass(frozen=True, slots=True)
class SlideSource:
    """One server-side WSI and an optional physical-resolution override."""

    path: str | Path
    source_mpp: MPP | float | None = None

    def __post_init__(self) -> None:
        resolved = Path(self.path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        object.__setattr__(self, "path", resolved)
        if self.source_mpp is not None:
            object.__setattr__(
                self,
                "source_mpp",
                as_mpp(self.source_mpp, name="source_mpp"),
            )


class SlideRegistry(Mapping[str, SlideSource]):
    """Map public identifiers to private server-side WSI paths."""

    def __init__(
        self,
        slides: Mapping[str, SlideSource | str | Path],
        *,
        allow_empty: bool = False,
    ) -> None:
        if not slides and not allow_empty:
            raise ValueError("at least one slide must be registered")
        sources: dict[str, SlideSource] = {}
        for slide_id, source in slides.items():
            valid_public_id(slide_id, name="slide IDs")
            sources[slide_id] = (
                source if isinstance(source, SlideSource) else SlideSource(source)
            )
        self._sources = sources

    def replace(self, slides: Mapping[str, SlideSource | str | Path]) -> None:
        """Publish a complete new snapshot without mutating active iterators."""
        self._sources = SlideRegistry(slides, allow_empty=True)._sources

    def __getitem__(self, slide_id: str) -> SlideSource:
        return self._sources[slide_id]

    def __iter__(self) -> Iterator[str]:
        return iter(self._sources)

    def __len__(self) -> int:
        return len(self._sources)
