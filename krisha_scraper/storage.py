"""Writers that save scraped listings to CSV, JSON Lines or JSON."""

from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path

CHECKPOINT_INTERVAL = 60.0  # seconds between rewrites of a CSV/JSON file during a run


class Writer:
    def __init__(self, path: Path):
        self.path = path
        self.count = 0

    def write(self, record: dict) -> None:
        self._write(record)
        self.count += 1

    def _write(self, record: dict) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class JsonLinesWriter(Writer):
    """One JSON object per line, flushed immediately so nothing is lost on a crash."""

    def __init__(self, path: Path):
        super().__init__(path)
        self._file = open(path, "w", encoding="utf-8")

    def _write(self, record: dict) -> None:
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


class BufferedWriter(Writer):
    """Keeps every record and rewrites the whole file: on close and every CHECKPOINT_INTERVAL.

    The checkpoints mean a run killed without a chance to save (a closed console
    window on Windows, a power cut) loses at most the last interval.
    """

    encoding = "utf-8"

    def __init__(self, path: Path):
        super().__init__(path)
        self._file = open(path, "w", encoding=self.encoding, newline="")  # fail on a bad path before scraping
        self._records: list[dict] = []
        self._clock = time.monotonic
        self._saved_at = self._clock()

    def _write(self, record: dict) -> None:
        self._records.append(self._prepare(record))
        if self._clock() - self._saved_at >= CHECKPOINT_INTERVAL:
            self._save()

    def close(self) -> None:
        with self._file:
            self._save()

    def _save(self) -> None:
        self._file.seek(0)
        self._file.truncate()
        self._dump(self._file)
        self._file.flush()
        os.fsync(self._file.fileno())
        self._saved_at = self._clock()

    def _prepare(self, record: dict) -> dict:
        return record

    def _dump(self, file) -> None:
        raise NotImplementedError


class JsonWriter(BufferedWriter):
    def _dump(self, file) -> None:
        json.dump(self._records, file, ensure_ascii=False, indent=2)


class CsvWriter(BufferedWriter):
    """The header includes every listing parameter seen, so rows are kept until the file is written.

    Written with a BOM (utf-8-sig) so Excel shows Cyrillic correctly.
    """

    encoding = "utf-8-sig"

    def _prepare(self, record: dict) -> dict:
        return flatten(record)

    def _dump(self, file) -> None:
        columns = list(dict.fromkeys(key for row in self._records for key in row))
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(self._records)


def flatten(record: dict) -> dict:
    """Nested dicts (listing params) become columns, lists are joined with ' | '."""
    row = {}
    for key, value in record.items():
        if isinstance(value, dict):
            row.update(value)
        elif isinstance(value, list):
            row[key] = " | ".join(map(str, value))
        else:
            row[key] = value
    return row


def open_writer(path: str | Path) -> Writer:
    path = Path(path)
    writers = {".csv": CsvWriter, ".jsonl": JsonLinesWriter, ".ndjson": JsonLinesWriter, ".json": JsonWriter}
    writer_class = writers.get(path.suffix.lower())
    if writer_class is None:
        raise ValueError(f"Неизвестный формат файла {path.name!r}: используйте .csv, .jsonl или .json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return writer_class(path)
