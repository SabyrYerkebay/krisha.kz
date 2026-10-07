"""Writers that save scraped listings to CSV, JSON Lines or JSON."""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path

log = logging.getLogger(__name__)

CHECKPOINT_INTERVAL = 60.0  # seconds between saves of a CSV/JSON file during a run
SAVE_ATTEMPTS = 3  # the final save is retried: on Windows Excel or an antivirus can hold the file


def record_key(record: dict):
    """What identifies a listing across runs: its id (ints and CSV strings alike), else its URL."""
    listing_id = record.get("id")
    if isinstance(listing_id, str) and listing_id.isascii() and listing_id.isdigit():
        listing_id = int(listing_id)
    return listing_id or record.get("url") or None


def tmp_path(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def unsaved_path(path: Path) -> Path:
    """A fresh name for data that could not be saved to ``path``; no later run reuses it."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.stem}.unsaved-{stamp}{path.suffix}")
    number = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}.unsaved-{stamp}-{number}{path.suffix}")
        number += 1
    return candidate


def replace_file(path: Path, encoding: str, dump: Callable) -> None:
    """Write via a temporary file and swap it in, so the file on disk is always complete.

    If the swap fails, the complete data stays in ``<name>.tmp``.
    """
    tmp = tmp_path(path)
    with open(tmp, "w", encoding=encoding, newline="") as file:
        dump(file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


class Writer:
    def __init__(self, path: Path, existing: Iterable[dict] = ()):
        self.path = path
        self.count = 0  # records written by this run
        self.saved = False  # set once close() has stored everything
        existing = list(existing)
        self.resumed = len(existing)
        self.saved_keys = {key for key in map(record_key, existing) if key is not None}

    def write(self, record: dict) -> None:
        self._write(record)
        self.count += 1
        self._after_write()

    def _write(self, record: dict) -> None:
        raise NotImplementedError

    def _after_write(self) -> None:
        pass

    def close(self) -> None:
        self.saved = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class JsonLinesWriter(Writer):
    """One JSON object per line, flushed immediately so nothing is lost on a crash."""

    def __init__(self, path: Path, existing: Iterable[dict] = ()):
        existing = list(existing)
        super().__init__(path, existing)
        if existing:  # rewrite without a line a killed run may have cut short, then append
            replace_file(path, "utf-8", lambda file: file.writelines(_json_line(r) for r in existing))
        self._file = open(path, "a" if existing else "w", encoding="utf-8")

    @staticmethod
    def load(path: Path) -> list[dict]:
        records = []
        with open(path, encoding="utf-8") as file:
            for line in file:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
        return records

    def _write(self, record: dict) -> None:
        self._file.write(_json_line(record))
        self._file.flush()

    def close(self) -> None:
        self._file.close()
        super().close()


class BufferedWriter(Writer):
    """Keeps every record and rewrites the whole file: on close and every CHECKPOINT_INTERVAL.

    Each save goes through a temporary file, so a run killed at any moment (a closed
    console window on Windows, a power cut) leaves the previous complete save behind.
    """

    encoding = "utf-8"

    def __init__(self, path: Path, existing: Iterable[dict] = ()):
        existing = list(existing)
        super().__init__(path, existing)
        with open(path, "a", encoding=self.encoding):  # fail on a bad path before scraping
            pass
        self._records: list[dict] = [self._prepare(record) for record in existing]
        self._clock = time.monotonic
        self._saved_at = self._clock()

    def _write(self, record: dict) -> None:
        self._records.append(self._prepare(record))

    def _after_write(self) -> None:
        if self._clock() - self._saved_at < CHECKPOINT_INTERVAL:
            return
        try:
            self._save()
        except OSError as exc:  # e.g. the CSV is open in Excel; the next checkpoint tries again
            log.warning("Не удалось сохранить %s (%s), попробую через минуту; свежие данные пока в %s",
                        self.path, exc, tmp_path(self.path).name)
            self._saved_at = self._clock()

    def close(self) -> None:
        for attempt in range(1, SAVE_ATTEMPTS + 1):
            try:
                self._save()
                break
            except PermissionError as exc:
                if attempt == SAVE_ATTEMPTS:
                    raise OSError(f"Не удалось записать {self.path} (файл открыт в Excel?): {exc}. "
                                  f"{self._rescue()}") from exc
                time.sleep(1)
        super().close()

    def _rescue(self) -> str:
        """Move the complete copy left in .tmp to a name the next run will not overwrite."""
        tmp = tmp_path(self.path)
        try:
            rescue = unsaved_path(self.path)
            os.replace(tmp, rescue)
        except OSError:
            return f"Все данные сохранены в {tmp.name} рядом с ним — переименуйте его, пока не запустили снова"
        return f"Все данные сохранены в {rescue.name} рядом с ним"

    def _save(self) -> None:
        replace_file(self.path, self.encoding, self._dump)
        self._saved_at = self._clock()

    def _prepare(self, record: dict) -> dict:
        return record

    def _dump(self, file) -> None:
        raise NotImplementedError


class JsonWriter(BufferedWriter):
    @staticmethod
    def load(path: Path) -> list[dict]:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, list):
            raise ValueError("ожидался список объявлений")
        return [record for record in data if isinstance(record, dict)]

    def _dump(self, file) -> None:
        json.dump(self._records, file, ensure_ascii=False, indent=2)


class CsvWriter(BufferedWriter):
    """The header includes every listing parameter seen, so rows are kept until the file is written.

    Written with a BOM (utf-8-sig) so Excel shows Cyrillic correctly.
    """

    encoding = "utf-8-sig"

    @staticmethod
    def load(path: Path) -> list[dict]:
        with open(path, encoding="utf-8-sig", newline="") as file:
            return list(csv.DictReader(file))

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


def open_writer(path: str | Path, resume: bool = False) -> Writer:
    """A writer for the file's format.

    With ``resume`` the listings already in the file are kept (and reported in
    ``saved_keys`` so they are not fetched again). Without it an existing file is
    moved aside to ``<name>.prev<ext>`` rather than overwritten.
    """
    path = Path(path)
    writers = {".csv": CsvWriter, ".jsonl": JsonLinesWriter, ".ndjson": JsonLinesWriter, ".json": JsonWriter}
    writer_class = writers.get(path.suffix.lower())
    if writer_class is None:
        raise ValueError(f"Неизвестный формат файла {path.name!r}: используйте .csv, .jsonl или .json")
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    leftover = _keep_leftover_tmp(path)
    if path.is_file() and path.stat().st_size > 0:
        try:
            saved = writer_class.load(path)
        except (ValueError, csv.Error) as exc:
            if resume:
                raise ValueError(f"Не удалось прочитать {path} для --resume: {exc}") from exc
            saved = None  # unreadable, but still worth keeping
        if resume:
            existing = saved
        elif saved != []:  # a file without listings must not push a real one out of .prev
            backup = path.with_name(f"{path.stem}.prev{path.suffix}")
            os.replace(path, backup)
            log.warning("Файл %s уже был: старая версия перенесена в %s", path, backup.name)
    if resume and leftover is not None:
        try:
            unsaved = writer_class.load(leftover)
        except (ValueError, csv.Error):
            unsaved = []
        if len(unsaved) > len(existing):
            log.info("Продолжаю по %s: в нём больше объявлений, чем в %s", leftover.name, path.name)
            existing = unsaved
    return writer_class(path, existing)


def _keep_leftover_tmp(path: Path) -> Path | None:
    """A .tmp left by a run killed while the file was locked may hold its newest data: keep it."""
    tmp = tmp_path(path)
    if not (tmp.is_file() and tmp.stat().st_size > 0):
        return None
    leftover = unsaved_path(path)
    os.replace(tmp, leftover)
    log.warning("Найден %s от прошлого запуска (там могут быть данные новее основного файла): "
                "переименован в %s", tmp.name, leftover.name)
    return leftover


def _json_line(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False) + "\n"
