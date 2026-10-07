"""Writers that save scraped listings to CSV, JSON Lines or JSON."""

from __future__ import annotations

import csv
import json
from pathlib import Path


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


class JsonWriter(Writer):
    def __init__(self, path: Path):
        super().__init__(path)
        self._file = open(path, "w", encoding="utf-8")  # fail on a bad path before scraping
        self._records: list[dict] = []

    def _write(self, record: dict) -> None:
        self._records.append(record)

    def close(self) -> None:
        with self._file:
            json.dump(self._records, self._file, ensure_ascii=False, indent=2)


class CsvWriter(Writer):
    """Buffers rows so the header includes every listing parameter seen.

    Written with a BOM (utf-8-sig) so Excel shows Cyrillic correctly.
    """

    def __init__(self, path: Path):
        super().__init__(path)
        self._file = open(path, "w", encoding="utf-8-sig", newline="")  # fail on a bad path before scraping
        self._rows: list[dict] = []

    def _write(self, record: dict) -> None:
        self._rows.append(flatten(record))

    def close(self) -> None:
        columns = list(dict.fromkeys(key for row in self._rows for key in row))
        with self._file:
            writer = csv.DictWriter(self._file, fieldnames=columns)
            writer.writeheader()
            writer.writerows(self._rows)


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
