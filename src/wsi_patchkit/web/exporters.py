"""Label exporters operate on saved crops, without reading the source WSI."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from PIL import Image


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


def _voc_color(index: int) -> tuple[int, int, int]:
    color = [0, 0, 0]
    for bit in range(8):
        for channel in range(3):
            color[channel] |= ((index >> channel) & 1) << (7 - bit)
        index >>= 3
    return tuple(color)


def export_segmentation_masks(bundles: list[CropBundle], archive: ZipFile) -> None:
    """Write one CVAT dataset per overlay, with a shared batch label map."""
    groups = {}
    for bundle in bundles:
        if not bundle.labels.is_dir():
            continue
        metadata = bundle.metadata
        for overlay in metadata["overlays"]:
            groups.setdefault(overlay["id"], []).append((bundle, overlay))
    if not groups:
        raise ValueError("所选裁剪没有类别掩码，请选择已保存类别掩码的裁剪")
    datasets = []
    for number, (overlay_id, entries) in enumerate(sorted(groups.items()), 1):
        if len(groups) == 1:
            _write_segmentation_dataset(entries, archive, overlay_id)
        else:
            # A nested ZIP is independently importable; layer IDs never form paths.
            with (
                archive.open(f"segmentation-mask-{number}.zip", "w") as output,
                ZipFile(output, "w") as nested,
            ):
                _write_segmentation_dataset(entries, nested, overlay_id)
            datasets.append(
                {"archive": f"segmentation-mask-{number}.zip", "overlay_id": overlay_id}
            )
    if datasets:
        archive.writestr(
            "segmentation-datasets.json",
            json.dumps(datasets, ensure_ascii=False, indent=2),
        )


def _write_segmentation_dataset(entries, archive: ZipFile, overlay_id: str) -> None:
    classes = {}
    for _, overlay in entries:
        for item in overlay["classes"]:
            class_id, name = item["id"], item["name"]
            if class_id in classes and classes[class_id] != name:
                raise ValueError("同一图层的类别 ID 对应不同名称，请分别导出")
            classes[class_id] = name
    labels = ["background:0,0,0::"]
    mapping = []
    used_names = {"background"}
    for index, (class_id, name) in enumerate(sorted(classes.items()), 1):
        if (
            not name.strip()
            or name != name.strip()
            or name.startswith("#")
            or any(c in name for c in ":\r\n")
        ):
            raise ValueError("类别名称不能为空或包含冒号、换行，请修改后导出")
        exported_name = name
        if name.casefold() in used_names:
            exported_name = f"{name} (class {class_id})"
        while exported_name.casefold() in used_names:
            exported_name += "_"
        used_names.add(exported_name.casefold())
        color = _voc_color(index)
        labels.append(f"{exported_name}:{','.join(map(str, color))}::")
        mapping.append(
            {
                "source_id": class_id,
                "source_name": name,
                "label": exported_name,
                "color": color,
            }
        )
    archive.writestr("labelmap.txt", "\n".join(labels) + "\n")
    names = []
    for bundle, overlay in entries:
        stem = bundle.image.stem
        if stem in names or stem.startswith("#") or "\n" in stem or "\r" in stem:
            raise ValueError("裁剪文件名重复或无法写入 CVAT 图片列表，请重命名后导出")
        names.append(stem)
        files = set(bundle.files())
        paths = [bundle.labels / overlay[key] for key in ("mask", "coverage")]
        if any(path not in files for path in paths):
            raise ValueError("invalid crop bundle")
        with Image.open(paths[0]) as image:
            indices = np.array(image)
        with Image.open(paths[1]) as image:
            coverage = np.array(image) != 0
        with Image.open(bundle.image) as image:
            expected_shape = (image.height, image.width)
        if indices.shape != expected_shape or coverage.shape != expected_shape:
            raise ValueError("裁剪掩码尺寸与图片不一致")
        rgb = np.zeros((*indices.shape, 3), dtype=np.uint8)
        known = np.zeros(indices.shape, dtype=bool)
        for item in mapping:
            selected = coverage & (indices == item["source_id"])
            rgb[selected] = item["color"]
            known |= selected
        if np.any(coverage & ~known):
            raise ValueError("裁剪掩码包含类别表未声明的 ID")
        buffer = BytesIO()
        Image.fromarray(rgb).save(buffer, format="PNG")
        archive.writestr(f"SegmentationClass/{stem}.png", buffer.getvalue())
    archive.writestr("ImageSets/Segmentation/default.txt", "\n".join(names) + "\n")
    archive.writestr(
        "class-map.json",
        json.dumps(
            {
                "overlay_id": overlay_id,
                "classes": mapping,
                "uncovered_color": [0, 0, 0],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )


def export_segmentation_mask(bundle: CropBundle, archive: ZipFile) -> None:
    export_segmentation_masks([bundle], archive)


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
        self.register(
            "segmentation-mask",
            export_segmentation_mask,
            name="Segmentation Mask 1.1",
        )

    def export_many(self, format_id: str, bundles: list[CropBundle], archive: ZipFile):
        exporter = self.get(format_id)
        if exporter is export_segmentation_mask:
            export_segmentation_masks(bundles, archive)
        else:
            for bundle in bundles:
                exporter(bundle, archive)

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
