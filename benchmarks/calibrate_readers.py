"""Bounded native-grid calibration using local vendor samples, with JSON evidence.

No network or full-slide decode. Identity hashes stream through a small buffer.
Native neighborhoods are checked against independent crops of anchored blocks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from wsi_patchkit import (
    KfbSlideReader,
    OpenSlideReader,
    PhysicalPatchRequest,
    TiffReader,
    read_physical_patch_result,
)
from wsi_patchkit.geometry import native_to_level_zero

READERS = {"tiff": TiffReader, "openslide": OpenSlideReader, "kfb": KfbSlideReader}


def calibrate(backend, path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    report = {
        "backend": backend,
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
        "checks": [],
        "levels": [],
    }
    with READERS[backend]() as reader:
        metadata = reader.metadata(path)
        if metadata.mpp is None:
            raise ValueError("sample must supply calibrated physical MPP metadata")
        report["mpp"] = metadata.mpp
        report["vendor"] = metadata.vendor
        for level in metadata.levels:
            report["levels"].append(
                {
                    "level": level.level,
                    "dimensions": level.dimensions,
                    "downsample": level.downsample,
                    "mpp": level.mpp,
                }
            )
            for anchor in (
                (0, 0),
                (level.dimensions[0] // 3, level.dimensions[1] // 3),
            ):
                width, height = (
                    min(64, level.dimensions[a] - anchor[a]) for a in (0, 1)
                )
                location = tuple(
                    native_to_level_zero(anchor[a], level.downsample[a]) for a in (0, 1)
                )
                block = reader.read_region(path, location, level.level, (width, height))
                for offset in ((1, 1), (7, 11), (29, 17)):
                    if offset[0] + 8 > width or offset[1] + 8 > height:
                        continue
                    origin = tuple(anchor[a] + offset[a] for a in (0, 1))
                    selected_location = tuple(
                        native_to_level_zero(origin[a], level.downsample[a])
                        for a in (0, 1)
                    )
                    actual = reader.read_region(
                        path, selected_location, level.level, (8, 8)
                    )
                    expected = block[
                        offset[1] : offset[1] + 8, offset[0] : offset[0] + 8
                    ]
                    physical = read_physical_patch_result(
                        reader,
                        PhysicalPatchRequest(
                            path,
                            origin_um=tuple(origin[a] * level.mpp[a] for a in (0, 1)),
                            target_mpp=level.mpp,
                            output_size=(8, 8),
                            level=level.level,
                            interpolation="nearest",
                        ),
                        metadata=metadata,
                    )
                    report["checks"].append(
                        {
                            "level": level.level,
                            "native_origin": origin,
                            "native_crop_equal": bool(np.array_equal(actual, expected)),
                            "physical_equal": bool(
                                np.array_equal(physical.image, expected)
                            ),
                            "valid_centers": int(physical.valid_mask.sum()),
                            "varied_pixels": bool(np.any(expected != expected[0, 0])),
                        }
                    )
    report["passed"] = all(
        c["native_crop_equal"] and c["physical_equal"] and c["valid_centers"] == 64
        for c in report["checks"]
    ) and bool(report["checks"])
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="append", required=True, help="backend:path")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reports = []
    for sample in args.sample:
        backend, path = sample.split(":", 1)
        try:
            result = calibrate(backend, Path(path))
        except Exception as error:
            result = {
                "backend": backend,
                "path": path,
                "passed": False,
                "error": f"{type(error).__name__}: {error}",
            }
        reports.append(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(reports, indent=2) + "\n")
        print(backend, path, "passed:", result["passed"], flush=True)
    raise SystemExit(0 if all(r["passed"] for r in reports) else 1)
