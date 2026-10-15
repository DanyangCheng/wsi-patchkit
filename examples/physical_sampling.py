"""Runnable sync/serial/parallel physical sampling and legacy conversion demo."""

from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

import numpy as np
from PIL import Image

from wsi_patchkit import (
    ImageReader,
    PatchRequest,
    PhysicalPatchRequest,
    PhysicalPatchStream,
    read_physical_patch_result,
)


def run(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "gradient.png"
    pixels = np.arange(16 * 12 * 3, dtype=np.uint8).reshape(12, 16, 3)
    Image.fromarray(pixels).save(path)
    requests = [
        PhysicalPatchRequest(
            path,
            origin_um=(i * 0.5 - 1, -0.25),
            output_size=(6, 5),
            target_mpp=(0.5, 0.75),
            source_mpp=(0.25, 0.5),
            interpolation=mode,
            padding_mode=padding,
        )
        for i, (mode, padding) in enumerate(
            (
                ("nearest", "constant"),
                ("bilinear", "reflect"),
                ("bicubic", "constant"),
                ("area", "reflect"),
            )
        )
    ]
    with ImageReader() as reader:
        direct = [read_physical_patch_result(reader, request) for request in requests]
        serial = list(PhysicalPatchStream(reader, requests))
    with closing(
        iter(
            PhysicalPatchStream(
                None, requests, reader_factory=ImageReader, num_workers=2, max_pending=2
            )
        )
    ) as results:
        parallel = list(results)
    for expected, first, second in zip(direct, serial, parallel, strict=True):
        for actual in (first, second):
            np.testing.assert_array_equal(actual.image, expected.image)
            np.testing.assert_array_equal(actual.valid_mask, expected.valid_mask)
            assert (
                actual.grid == expected.grid
                and actual.source_level == expected.source_level
            )
    old = PatchRequest(path, -2, 3, 6, 5, (0.5, 0.75), source_mpp=(0.25, 0.5))
    assert PhysicalPatchRequest.from_legacy(old).origin_um == (-1, 2.25)
    print("sync / serial / parallel: 4 results equal; legacy origin conversion passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    run(parser.parse_args().output_dir)
