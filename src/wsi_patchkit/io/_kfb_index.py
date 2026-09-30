"""Persistent, bounded-memory KFB index compatible with kfbslide 0.3.x.

Only this adapter depends on the backend's private index interface. SQLite
serializes builders and repairs; readers have independent connections/caches.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_SCHEMA_VERSION = 1
_COLUMNS = "id,scale,x,y,width,height,size,offset"


class _Window:
    """Indexable repair window with original IDs and a bounded iteration end."""

    def __init__(self, values: dict[int, Any], end: int) -> None:
        self.values = values
        self.end = end

    def __len__(self) -> int:
        return self.end

    def __getitem__(self, index: int) -> Any:
        return self.values[index]

    def __setitem__(self, index: int, value: Any) -> None:
        self.values[index] = value


class _Entries:
    def __init__(self, owner: SQLiteTileIndex) -> None:
        self.owner = owner

    def __len__(self) -> int:
        return self.owner.tile_count

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.owner._entry(index)


class _Offsets(_Entries):
    def __getitem__(self, index: int) -> int:
        return self.owner._entry(index)["offset"]


class _Lookup:
    def __init__(self, owner: SQLiteTileIndex) -> None:
        self.owner = owner

    def get(self, key: tuple[float, int, int]) -> int | None:
        owner = self.owner
        if owner._region_bounds is not None:
            scale, x0, y0, x1, y1 = owner._region_bounds
            if key[0] == scale and x0 <= key[1] < x1 and y0 <= key[2] < y1:
                return owner._region_lookup.get(key)
        row = owner._connection.execute(
            f"SELECT {_COLUMNS} FROM tiles WHERE scale=? AND x=? AND y=? "
            "ORDER BY id DESC LIMIT 1",
            key,
        ).fetchone()
        if row is None:
            return None
        owner._remember(row)
        return row[0]


class SQLiteTileIndex:
    """Stream an index once, then fetch only a region's contributing records."""

    def __init__(self, filename: str, info: Any, cache_dir: str | Path) -> None:
        self.filename = filename
        self.tile_size = int(info.header.tile_size)
        self.tile_count = int(info.header.tile_count)
        self._records: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self._region_lookup: dict[tuple[float, int, int], int] = {}
        self._region_bounds: tuple[float, int, int, int, int] | None = None
        self._closed = False
        stat = Path(filename).stat()
        self._signature = (stat.st_size, stat.st_mtime_ns)
        identity = json.dumps(
            [
                _SCHEMA_VERSION,
                str(Path(filename).resolve()),
                *self._signature,
                info.header.tile_index_start,
                self.tile_count,
                info.tile_data_offset,
                info.header.scan_scale,
                info.header.width,
                info.header.height,
            ],
            separators=(",", ":"),
        )
        directory = Path(cache_dir).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        self.database_path = directory / (
            hashlib.sha256(identity.encode()).hexdigest() + ".sqlite3"
        )
        self._connection = sqlite3.connect(
            self.database_path,
            timeout=60,
            check_same_thread=False,
        )
        try:
            # This is a per-connection page-cache budget, not a process RSS cap.
            self._connection.execute("PRAGMA cache_size=-2048")
            self._connection.execute("PRAGMA busy_timeout=60000")
            # Concurrent first opens can race while changing journal mode;
            # SQLite does not always invoke busy_timeout for that transition.
            deadline = time.monotonic() + 60
            while True:
                try:
                    self._connection.execute("PRAGMA journal_mode=WAL").fetchone()
                    break
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error) or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, "
                "value TEXT NOT NULL)"
            )
            complete = self._connection.execute(
                "SELECT value FROM metadata WHERE key='identity'"
            ).fetchone()
            self.built = complete != (identity,)
            if self.built:
                self._build(info, identity)
            self._connection.commit()
            scales = [
                row[0]
                for row in self._connection.execute(
                    "SELECT DISTINCT scale FROM tiles WHERE scale>=1 "
                    "ORDER BY scale DESC"
                )
            ]
            self.level_scales = dict(enumerate(scales))
            self.scale_to_level = {scale: level for level, scale in enumerate(scales)}
            self.level_count = len(scales)
            self._level_downsamples = [info.header.scan_scale / s for s in scales]
            self._level_dimensions = [
                (int(info.header.width / ds), int(info.header.height / ds))
                for ds in self._level_downsamples
            ]
            self._data_version = self._version()
        except BaseException:
            self._connection.rollback()
            self._connection.close()
            self._closed = True
            raise
        self.entries = _Entries(self)
        self.offsets = _Offsets(self)
        self.lookup = _Lookup(self)

    def _build(self, info: Any, identity: str) -> None:
        start = int(info.header.tile_index_start)
        if (
            self.tile_count < 1
            or start < 0
            or start + self.tile_count * 64 > (self._signature[0])
        ):
            raise ValueError("invalid KFB index bounds")
        self._connection.execute("DROP TABLE IF EXISTS tiles")
        self._connection.execute(
            "CREATE TABLE tiles (id INTEGER PRIMARY KEY, scale REAL NOT NULL, "
            "x INTEGER NOT NULL, y INTEGER NOT NULL, width INTEGER NOT NULL, "
            "height INTEGER NOT NULL, size INTEGER NOT NULL, offset INTEGER NOT NULL)"
        )
        offset = int(info.tile_data_offset)
        with open(self.filename, "rb") as handle:
            handle.seek(start)
            for first in range(0, self.tile_count, 4096):
                count = min(4096, self.tile_count - first)
                block = handle.read(count * 64)
                if len(block) != count * 64:
                    raise ValueError("truncated KFB index")
                rows = []
                for local in range(count):
                    position = local * 64
                    x, y, width, height = struct.unpack_from(
                        "<IIII", block, position + 4
                    )
                    scale = struct.unpack_from("<f", block, position + 20)[0]
                    size = struct.unpack_from("<I", block, position + 32)[0]
                    if (
                        not math.isfinite(scale)
                        or scale <= 0
                        or width < 1
                        or height < 1
                    ):
                        raise ValueError("invalid KFB index entry")
                    rows.append(
                        (first + local, scale, x, y, width, height, size, offset)
                    )
                    offset += size
                self._connection.executemany(
                    "INSERT INTO tiles VALUES (?,?,?,?,?,?,?,?)",
                    rows,
                )
        stat = Path(self.filename).stat()
        if (stat.st_size, stat.st_mtime_ns) != self._signature:
            raise ValueError("KFB changed while its index was being built")
        self._connection.execute("CREATE INDEX tile_region ON tiles(scale,y,x)")
        self._connection.execute("DELETE FROM metadata")
        self._connection.execute(
            "INSERT INTO metadata VALUES ('identity',?)",
            (identity,),
        )

    def _version(self) -> int:
        return self._connection.execute("PRAGMA data_version").fetchone()[0]

    def _remember(self, row: tuple) -> dict[str, Any]:
        record = dict(zip(_COLUMNS.split(","), row, strict=True))
        self._records[row[0]] = record
        self._records.move_to_end(row[0])
        while len(self._records) > 512:
            self._records.popitem(last=False)
        return record

    def _entry(self, index: int) -> dict[str, Any]:
        if not 0 <= index < self.tile_count:
            raise IndexError(index)
        cached = self._records.get(index)
        if cached is not None:
            self._records.move_to_end(index)
            return cached
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM tiles WHERE id=?",
            (index,),
        ).fetchone()
        if row is None:
            raise ValueError("incomplete KFB index cache")
        return self._remember(row)

    def prefetch(self, scale: float, bounds: tuple[int, int, int, int]) -> None:
        version = self._version()
        if version != self._data_version:
            self._records.clear()
            self._data_version = version
        x0, y0, x1, y1 = bounds
        self._region_lookup.clear()
        # Bound both lookup and record caches even for a large direct crop.
        # Larger regions use indexed point lookups during decoding instead.
        tile_count = max(0, (x1 - x0) // self.tile_size) * max(
            0, (y1 - y0) // self.tile_size
        )
        self._region_bounds = None
        if tile_count > 512:
            return
        for row in self._connection.execute(
            f"SELECT {_COLUMNS} FROM tiles WHERE scale=? AND y>=? AND y<? "
            "AND x>=? AND x<? ORDER BY id",
            (scale, y0, y1, x0, x1),
        ):
            self._remember(row)
            self._region_lookup[(row[1], row[2], row[3])] = row[0]
        self._region_bounds = (scale, *bounds)

    @contextmanager
    def repair_window(self, index: int):
        """Let the backend repair its current tile and neighbors atomically.

        The backend compensates a tile's size change in its next neighbor, so
        offsets after that neighbor are unchanged. Limit its cumulative-offset
        loop to this window instead of fetching every following record.
        Persist partial repairs even when decoding ultimately raises, matching
        the backend's behavior for an unrecoverable JPEG.
        """
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            rows = list(
                self._connection.execute(
                    f"SELECT {_COLUMNS} FROM tiles WHERE id BETWEEN ? AND ?",
                    (max(0, index - 1), min(self.tile_count - 1, index + 1)),
                )
            )
            entries = {
                row[0]: dict(zip(_COLUMNS.split(","), row, strict=True)) for row in rows
            }
            offsets = {row[0]: row[-1] for row in rows}
            end = min(self.tile_count, index + 2)
            window = SimpleNamespace(
                entries=_Window(entries, end),
                offsets=_Window(offsets, end),
            )
        except BaseException:
            self._connection.rollback()
            raise
        try:
            yield window
        finally:
            try:
                self._connection.executemany(
                    "UPDATE tiles SET size=?, offset=? WHERE id=?",
                    [(entries[i]["size"], offsets[i], i) for i in entries],
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            finally:
                self._records.clear()

    def level_dimensions(self) -> tuple[tuple[int, int], ...]:
        return tuple(self._level_dimensions)

    def level_downsamples(self) -> tuple[float, ...]:
        return tuple(self._level_downsamples)

    def get_best_level_for_downsample(self, downsample: float) -> int:
        return max(
            (i for i, ds in enumerate(self._level_downsamples) if ds <= downsample),
            default=0,
        )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._records.clear()
            self._region_lookup.clear()
            self._connection.close()
