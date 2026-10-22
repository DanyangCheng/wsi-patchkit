"""Label exporters operate on saved crops, without reading the source WSI."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile


@dataclass(frozen=True)
class CropBundle:
    image: Path
    labels: Path

    def files(self):
        """Enumerate managed files without following symbolic links."""
        if self.labels.is_symlink():
            raise ValueError("invalid crop bundle")
        if self.labels.is_dir():
            for path in sorted(self.labels.rglob("*")):
                if path.is_symlink():
                    raise ValueError("invalid crop bundle")
                if path.is_file():
                    yield path

    @property
    def metadata(self) -> dict:
        # Validate the directory before reading metadata supplied to adapters.
        list(self.files())
        return json.loads((self.labels / "metadata.json").read_text(encoding="utf-8"))


def export_indexed_png(bundle: CropBundle, archive: ZipFile) -> None:
    """Copy the canonical PNG masks, coverage, and metadata without conversion."""
    for path in bundle.files():
        archive.write(
            path,
            arcname=f"{bundle.labels.name}/{path.relative_to(bundle.labels).as_posix()}",
        )


def export_none(bundle: CropBundle, archive: ZipFile) -> None:
    """Leave the archive with its images and optional coordinate manifest."""


class ExporterRegistry:
    """Register trusted server-side adapters: callable(CropBundle, ZipFile).

    The archive already contains the image. Adapters should place their outputs
    under ``bundle.labels.name`` to avoid collisions in multi-crop downloads.
    """

    def __init__(self):
        self._exporters: dict[
            str, tuple[str, Callable[[CropBundle, ZipFile], None]]
        ] = {}
        self.register("indexed-png", export_indexed_png, name="类别索引 PNG")
        self.register("none", export_none, name="无")

    def register(self, format_id: str, exporter, *, name: str) -> None:
        if not isinstance(format_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,63}", format_id
        ):
            raise ValueError("invalid exporter ID")
        if format_id in self._exporters:
            raise ValueError("exporter is already registered")
        if not callable(exporter) or not isinstance(name, str) or not name.strip():
            raise ValueError("exporter requires a callable and a display name")
        self._exporters[format_id] = (name, exporter)

    def get(self, format_id: str):
        if not isinstance(format_id, str) or format_id not in self._exporters:
            raise ValueError("unknown label format")
        return self._exporters[format_id][1]

    def public(self):
        return [{"id": key, "name": value[0]} for key, value in self._exporters.items()]
