"""Writers that save scraped listings to CSV, JSON Lines or JSON."""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

log = logging.getLogger(__name__)

CHECKPOINT_INTERVAL = 60.0  # seconds between saves of a CSV/JSON file during a run
SAVE_ATTEMPTS = 3  # the final save is retried: on Windows Excel can hold the file for a long time
REPLACE_ATTEMPTS = 6  # each swap is retried within ~1.5 s: an antivirus or indexer holds new files briefly


def record_key(record: dict):
    """What identifies a listing across runs: its id (ints and CSV strings alike), else its URL."""
    listing_id = record.get("id")
    if isinstance(listing_id, str) and listing_id.isascii() and listing_id.isdigit():
        listing_id = int(listing_id)
    return listing_id or record.get("url") or None


def tmp_path(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def progress_path(path: Path) -> Path:
    """Where the search pages already done are kept, for --resume."""
    return path.with_name(path.name + ".progress.json")


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
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: [WinError 5] while another program has the file open
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.05 * 2 ** attempt)


class Writer:
    """Also tracks how far each search got, so --resume can continue from the next page.

    The progress is stored only together with the listings it covers, never ahead of them.
    """

    def __init__(self, path: Path, existing: Iterable[dict] = (), progress: dict | None = None):
        self.path = path
        self.count = 0  # records written by this run
        self.saved = False  # set once close() has stored everything
        existing = list(existing)
        self.resumed = len(existing)
        self.saved_keys = {key for key in map(record_key, existing) if key is not None}
        self._progress = progress or {"next_page": {}, "done": []}
        continuations = self._progress.setdefault("continue", {})
        # versions before the price chain finished price-sorted searches at the page limit without
        # recording whether more was left: walk those once more (saved listings are skipped)
        self._progress["done"] = [search for search in self._progress["done"]
                                  if search in continuations or not _sorted_by_price(search)]

    def write(self, record: dict) -> None:
        self._write(record)
        self.count += 1
        self._after_write()

    def next_page(self, search: str) -> int | None:
        """The page to continue ``search`` from, if an earlier run got part of the way."""
        return self._progress["next_page"].get(search)

    def search_done(self, search: str) -> bool:
        return search in self._progress["done"]

    def page_done(self, search: str, page: int) -> None:
        """Every listing of this search page has been written."""
        self._progress["next_page"][search] = page + 1
        self._progress_changed()

    def finish_search(self, search: str, continue_with: str | None = None) -> None:
        """The search's results ended; a price-sorted one cut off by the page limit goes on as ``continue_with``."""
        self._progress["next_page"].pop(search, None)
        if search not in self._progress["done"]:
            self._progress["done"].append(search)
        self._progress["continue"][search] = continue_with  # None: the results really ended
        self._progress_changed()

    def continuation(self, search: str) -> str | None:
        return self._progress["continue"].get(search)

    def _write(self, record: dict) -> None:
        raise NotImplementedError

    def _after_write(self) -> None:
        pass

    def _progress_changed(self) -> None:
        pass

    def _save_progress(self) -> None:
        """Best effort: an older progress file only makes --resume repeat a few pages."""
        if not (self._progress["next_page"] or self._progress["done"]):
            return
        self._progress["records"] = self.resumed + self.count  # lets --resume check the file still matches
        try:
            replace_file(progress_path(self.path), "utf-8",
                         lambda file: json.dump(self._progress, file, ensure_ascii=False, indent=2))
        except OSError as exc:
            log.warning("Не удалось сохранить %s (%s): --resume может начать поиск на несколько страниц раньше",
                        progress_path(self.path).name, exc)

    def close(self) -> None:
        self.saved = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class JsonLinesWriter(Writer):
    """One JSON object per line, flushed immediately so nothing is lost on a crash."""

    def __init__(self, path: Path, existing: Iterable[dict] = (), progress: dict | None = None):
        existing = list(existing)
        super().__init__(path, existing, progress)
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

    def _progress_changed(self) -> None:
        os.fsync(self._file.fileno())  # the listings reach the disk before the progress that covers them
        self._save_progress()

    def close(self) -> None:
        self._file.close()
        super().close()


class BufferedWriter(Writer):
    """Keeps every record and rewrites the whole file: on close and every CHECKPOINT_INTERVAL.

    Each save goes through a temporary file, so a run killed at any moment (a closed
    console window on Windows, a power cut) leaves the previous complete save behind.
    """

    encoding = "utf-8"

    def __init__(self, path: Path, existing: Iterable[dict] = (), progress: dict | None = None):
        existing = list(existing)
        super().__init__(path, existing, progress)
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
        self._save_progress()  # only after the listings it covers are on disk
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
    progress = _load_progress(path) if resume else None
    if not resume:
        progress_path(path).unlink(missing_ok=True)  # it described the file being replaced
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
    if progress and len(existing) < progress.get("records", 0):
        log.warning("В %s меньше объявлений (%d), чем было сохранено (%d): файл заменили или удалили? "
                    "Поиски пройдут заново, уже сохранённые объявления скачиваться не будут",
                    path.name, len(existing), progress["records"])
        progress = None
    return writer_class(path, existing, progress)


def _load_progress(path: Path) -> dict | None:
    file = progress_path(path)
    if not file.is_file():
        return None
    try:
        with open(file, encoding="utf-8") as stream:
            progress = json.load(stream)
        if not (isinstance(progress.get("next_page"), dict) and isinstance(progress.get("done"), list)
                and isinstance(progress.get("continue", {}), dict) and isinstance(progress.get("records", 0), int)):
            raise ValueError("неожиданный формат")
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("Не удалось прочитать %s (%s): поиски начнутся с первой страницы", file.name, exc)
        return None
    return progress


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


def _sorted_by_price(search: str) -> bool:
    return ("sort_by", "price-asc") in parse_qsl(urlsplit(search).query)


def _json_line(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False) + "\n"
