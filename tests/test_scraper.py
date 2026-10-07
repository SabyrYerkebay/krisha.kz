import csv
import json
import logging
from pathlib import Path

import pytest
import requests

from krisha_scraper import storage
from krisha_scraper.client import RobotsDisallowed, SiteBlocked
from krisha_scraper.scraper import iter_search, page_url, scrape
from krisha_scraper.storage import open_writer

SEARCH_URL = "https://krisha.kz/prodazha/kvartiry/almaty/"


def search_html(ids, last_page=None, total=None):
    subtitle = f'<div class="a-search-subtitle">Найдено <span>{total}</span> объявлений</div>' if total else ""
    cards = subtitle + "".join(
        f'<div class="a-card" data-id="{i}"><a class="a-card__title" href="/a/show/{i}">'
        f'1-комнатная квартира · 40 м² · 2/5 этаж</a><div class="a-card__price">{i} 〒</div></div>'
        for i in ids
    )
    paginator = ""
    if last_page:
        paginator = "".join(f'<a class="paginator__btn" data-page="{n}">{n}</a>' for n in range(1, last_page + 1))
        paginator = f'<nav class="paginator">{paginator}</nav>'
    return f"<html><body>{cards}{paginator}</body></html>"


def listing_html(listing_id):
    return (f'<html><body><div class="offer__description"><div class="text">Описание {listing_id}</div></div>'
            f'<script id="jsdata">window.data = {{"advert": {{"id": {listing_id}, '
            f'"map": {{"lat": 43.2, "lon": 76.9}}}}}};</script></body></html>')


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        page = self.pages.get(url)
        if isinstance(page, Exception):
            raise page
        if page is None:
            raise requests.HTTPError(f"404 for {url}")
        return page


@pytest.mark.parametrize("url, page, expected", [
    (SEARCH_URL, 1, SEARCH_URL),
    (SEARCH_URL, 3, SEARCH_URL + "?page=3"),
    (SEARCH_URL + "?das[live.rooms]=2&page=5", 2, SEARCH_URL + "?das%5Blive.rooms%5D=2&page=2"),
    (SEARCH_URL + "?page=5", 1, SEARCH_URL),
])
def test_page_url(url, page, expected):
    assert page_url(url, page) == expected


def test_iter_search_stops_at_last_page():
    client = FakeClient({
        page_url(SEARCH_URL, 1): search_html([1, 2], last_page=2),
        page_url(SEARCH_URL, 2): search_html([3, 2], last_page=2),
    })
    assert [card["id"] for card in iter_search(client, SEARCH_URL)] == [1, 2, 3]
    assert len(client.requested) == 2


def test_iter_search_stops_when_page_repeats():
    # No paginator: krisha.kz shows the last page again for out-of-range page numbers.
    client = FakeClient({
        page_url(SEARCH_URL, 1): search_html([1, 2]),
        page_url(SEARCH_URL, 2): search_html([3]),
        page_url(SEARCH_URL, 3): search_html([3]),
    })
    assert [card["id"] for card in iter_search(client, SEARCH_URL)] == [1, 2, 3]
    assert len(client.requested) == 3


def test_iter_search_continues_past_a_page_of_already_seen_listings():
    # newest-first order shifts while the search is walked: page 3 repeats page 2
    client = FakeClient({
        page_url(SEARCH_URL, 1): search_html([1, 2], last_page=4),
        page_url(SEARCH_URL, 2): search_html([3, 4], last_page=4),
        page_url(SEARCH_URL, 3): search_html([3, 4], last_page=4),
        page_url(SEARCH_URL, 4): search_html([5], last_page=4),
    })
    assert [card["id"] for card in iter_search(client, SEARCH_URL)] == [1, 2, 3, 4, 5]


def test_iter_search_stops_on_a_page_without_cards():
    client = FakeClient({
        page_url(SEARCH_URL, 1): search_html([1, 2], last_page=1000),
        page_url(SEARCH_URL, 2): search_html([], last_page=1000),
    })
    assert [card["id"] for card in iter_search(client, SEARCH_URL)] == [1, 2]
    assert len(client.requested) == 2


def test_iter_search_respects_max_pages_and_start_page():
    client = FakeClient({page_url(SEARCH_URL, n): search_html([n * 10], last_page=50) for n in range(1, 51)})
    cards = list(iter_search(client, SEARCH_URL, max_pages=2, start_page=4))
    assert [card["id"] for card in cards] == [40, 50]


def test_iter_search_drops_duplicates_on_same_page():
    # paid "hot" cards can show the same listing twice on one page
    client = FakeClient({page_url(SEARCH_URL, 1): search_html([1, 2, 1], last_page=1)})
    assert [card["id"] for card in iter_search(client, SEARCH_URL)] == [1, 2]


def test_iter_search_start_page_past_last_page():
    # krisha.kz serves the last page for out-of-range numbers; its paginator tells us so
    client = FakeClient({page_url(SEARCH_URL, 10): search_html([51, 52], last_page=5)})
    assert list(iter_search(client, SEARCH_URL, start_page=10)) == []


def test_scrape_dedups_across_urls(tmp_path):
    rooms_url = SEARCH_URL + "?das[live.rooms]=2"
    client = FakeClient({
        page_url(SEARCH_URL, 1): search_html([1, 2], last_page=1),
        # the second search starts with listings already saved, then has a new one
        page_url(rooms_url, 1): search_html([2, 1], last_page=2),
        page_url(rooms_url, 2): search_html([5], last_page=2),
        "https://krisha.kz/a/show/2": listing_html(2),
    })
    out = tmp_path / "out.jsonl"
    with open_writer(out) as writer:
        count = scrape(client, [SEARCH_URL, rooms_url, "https://krisha.kz/a/show/2"], writer)

    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert (count, [r["id"] for r in records]) == (3, [2, 1, 5])
    assert records[0]["lat"] == 43.2  # the explicit listing URL keeps its full record


def test_scrape_with_details_and_limit(tmp_path):
    client = FakeClient({
        page_url(SEARCH_URL, 1): search_html([1, 2, 3], last_page=1),
        "https://krisha.kz/a/show/1": listing_html(1),
        "https://krisha.kz/a/show/2": RobotsDisallowed("robots.txt запрещает"),
    })
    out = tmp_path / "out.jsonl"
    with open_writer(out) as writer:
        count = scrape(client, [SEARCH_URL], writer, details=True, limit=2)

    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert count == 2
    assert records[0]["lat"] == 43.2
    assert records[0]["description"] == "Описание 1"
    assert records[0]["price"] == 1  # card data is kept when the listing page has none
    assert "lat" not in records[1]  # listing page failed: the card alone is saved
    assert "https://krisha.kz/a/show/3" not in client.requested


def test_site_blocked_during_details_stops_the_run(tmp_path):
    client = FakeClient({
        page_url(SEARCH_URL, 1): search_html([1, 2, 3], last_page=1),
        "https://krisha.kz/a/show/1": listing_html(1),
        "https://krisha.kz/a/show/2": SiteBlocked("HTTP 468"),
    })
    out = tmp_path / "out.jsonl"
    with pytest.raises(SiteBlocked):
        with open_writer(out) as writer:
            scrape(client, [SEARCH_URL], writer, details=True)

    assert [json.loads(line)["id"] for line in out.read_text(encoding="utf-8").splitlines()] == [1]
    assert "https://krisha.kz/a/show/3" not in client.requested  # no point hammering a blocking site


@pytest.mark.parametrize("total, last_page, warned", [(42_287, 1000, True), (8_406, 421, False)])
def test_warns_when_search_is_larger_than_the_site_pages_through(total, last_page, warned, caplog):
    client = FakeClient({page_url(SEARCH_URL, 1): search_html([1], last_page=last_page, total=total)})
    with caplog.at_level(logging.WARNING):
        list(iter_search(client, SEARCH_URL, max_pages=1))
    assert ("разбейте поиск" in caplog.text) is warned


def test_scrape_single_listing_url(tmp_path):
    url = "https://krisha.kz/a/show/5"
    client = FakeClient({url: listing_html(5)})
    out = tmp_path / "out.json"
    with open_writer(out) as writer:
        scrape(client, [url], writer)
    assert json.loads(out.read_text(encoding="utf-8"))[0]["id"] == 5


def test_csv_flattens_params_and_photos(tmp_path):
    out = tmp_path / "out.csv"
    with open_writer(out) as writer:
        writer.write({"id": 1, "photos": ["a.jpg", "b.jpg"], "params": {"Тип дома": "кирпичный"}})
        writer.write({"id": 2, "photos": [], "params": {"Санузел": "раздельный"}})

    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM so Excel detects UTF-8
    with open(out, encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert list(rows[0]) == ["id", "photos", "Тип дома", "Санузел"]
    assert rows[0]["photos"] == "a.jpg | b.jpg"
    assert rows[1]["Санузел"] == "раздельный"
    assert rows[1]["Тип дома"] == ""


def test_open_writer_rejects_unknown_format(tmp_path):
    with pytest.raises(ValueError):
        open_writer(tmp_path / "out.xlsx")


@pytest.mark.parametrize("name", ["out.csv", "out.json", "out.jsonl"])
def test_open_writer_fails_on_bad_path_before_scraping(tmp_path, name):
    (tmp_path / name).mkdir()
    with pytest.raises(OSError):
        open_writer(tmp_path / name)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.mark.parametrize("name", ["out.csv", "out.json"])
def test_buffered_writers_checkpoint_to_disk(tmp_path, name):
    out = tmp_path / name
    writer = open_writer(out)
    writer._clock = clock = FakeClock()
    writer._saved_at = 0.0

    writer.write({"id": 1, "params": {"Тип дома": "кирпичный"}})
    assert out.read_bytes() == b""  # nothing written before the first interval
    clock.now = storage.CHECKPOINT_INTERVAL + 1
    writer.write({"id": 2})
    clock.now += 1
    writer.write({"id": 3})  # buffered until the next checkpoint

    # what a killed process would leave behind: the first two records, as a valid file
    raw = out.read_bytes()
    if name.endswith(".csv"):
        assert raw.count(b"\xef\xbb\xbf") == 1 and raw.startswith(b"\xef\xbb\xbf")
        with open(out, encoding="utf-8-sig", newline="") as file:
            assert [row["id"] for row in csv.DictReader(file)] == ["1", "2"]
    else:
        assert [r["id"] for r in json.loads(raw.decode("utf-8"))] == [1, 2]

    writer.close()
    raw = out.read_bytes()
    if name.endswith(".csv"):
        assert raw.count(b"\xef\xbb\xbf") == 1
        with open(out, encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        assert [row["id"] for row in rows] == ["1", "2", "3"]
        assert rows[0]["Тип дома"] == "кирпичный"
    else:
        assert [r["id"] for r in json.loads(raw.decode("utf-8"))] == [1, 2, 3]


@pytest.mark.parametrize("name", ["out.csv", "out.json"])
def test_interrupted_save_keeps_the_previous_file(tmp_path, name, monkeypatch):
    out = tmp_path / name
    writer = open_writer(out)
    writer.write({"id": 1})
    writer._save()
    before = out.read_bytes()

    def dump_then_fail(file):
        file.write("half a file")
        raise KeyboardInterrupt

    monkeypatch.setattr(writer, "_dump", dump_then_fail)
    with pytest.raises(KeyboardInterrupt):
        writer._save()
    assert out.read_bytes() == before


def test_checkpoint_failure_does_not_stop_the_run(tmp_path, monkeypatch, caplog):
    out = tmp_path / "out.csv"
    writer = open_writer(out)
    writer._clock = clock = FakeClock()
    writer._saved_at = 0.0
    clock.now = storage.CHECKPOINT_INTERVAL + 1

    def locked(*args):
        raise PermissionError("файл открыт в другой программе")

    monkeypatch.setattr(storage.os, "replace", locked)
    monkeypatch.setattr(storage.time, "sleep", lambda seconds: None)
    with caplog.at_level(logging.WARNING):
        writer.write({"id": 1})  # the checkpoint fails, the record is kept
    assert "попробую через минуту" in caplog.text
    assert writer.count == 1


def test_final_save_failure_points_to_the_tmp_file(tmp_path, monkeypatch):
    out = tmp_path / "out.csv"
    writer = open_writer(out)
    writer.write({"id": 1})

    def locked(*args):
        raise PermissionError("файл открыт в другой программе")

    monkeypatch.setattr(storage.os, "replace", locked)
    monkeypatch.setattr(storage.time, "sleep", lambda seconds: None)
    with pytest.raises(OSError, match="out.csv.tmp"):
        writer.close()
    assert not writer.saved
    with open(tmp_path / "out.csv.tmp", encoding="utf-8-sig", newline="") as file:
        assert [row["id"] for row in csv.DictReader(file)] == ["1"]


@pytest.mark.parametrize("name", ["out.csv", "out.json", "out.jsonl"])
def test_existing_file_is_moved_aside_without_resume(tmp_path, name):
    out = tmp_path / name
    with open_writer(out) as writer:
        writer.write({"id": 1})
    with open_writer(out) as writer:
        writer.write({"id": 2})

    prev = tmp_path / name.replace("out.", "out.prev.")
    assert prev.exists()
    assert prev.read_bytes() != out.read_bytes()


@pytest.mark.parametrize("name", ["out.csv", "out.json", "out.jsonl"])
def test_resume_keeps_saved_listings(tmp_path, name):
    out = tmp_path / name
    with open_writer(out) as writer:
        writer.write({"id": 1, "url": "https://krisha.kz/a/show/1", "params": {"Этаж": "5 из 9"}})
        writer.write({"id": 2, "url": "https://krisha.kz/a/show/2"})
    if name.endswith(".jsonl"):  # a run killed in the middle of a line
        with open(out, "a", encoding="utf-8") as file:
            file.write('{"id": 3, "url": "https://kri')

    writer = open_writer(out, resume=True)
    assert (writer.resumed, writer.saved_keys) == (2, {1, 2})
    with writer:
        writer.write({"id": 4, "url": "https://krisha.kz/a/show/4"})

    reloaded = type(writer).load(out)
    assert [storage.record_key(r) for r in reloaded] == [1, 2, 4]
    assert not (tmp_path / name.replace("out.", "out.prev.")).exists()


def test_empty_file_does_not_replace_the_backup(tmp_path):
    out = tmp_path / "out.csv"
    with open_writer(out) as writer:
        writer.write({"id": 1})
    with open_writer(out):  # a run that failed before saving anything
        pass
    with open_writer(out):
        pass

    with open(tmp_path / "out.prev.csv", encoding="utf-8-sig", newline="") as file:
        assert [row["id"] for row in csv.DictReader(file)] == ["1"]


def test_resume_with_unreadable_file(tmp_path):
    out = tmp_path / "out.json"
    out.write_text('[{"id": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="--resume"):
        open_writer(out, resume=True)


def test_scrape_skips_saved_listings(tmp_path):
    client = FakeClient({
        page_url(SEARCH_URL, 1): search_html([1, 2, 3], last_page=1),
        "https://krisha.kz/a/show/3": listing_html(3),
    })
    out = tmp_path / "out.jsonl"
    with open_writer(out) as writer:
        assert scrape(client, [SEARCH_URL], writer, details=True, skip={1, 2}) == 1
    assert client.requested == [page_url(SEARCH_URL, 1), "https://krisha.kz/a/show/3"]


def lock_main_file(monkeypatch, locked_path):
    """Make os.replace onto ``locked_path`` fail, as on Windows when Excel has the file open."""
    real_replace = storage.os.replace

    def replace(src, dst):
        if Path(dst) == locked_path:
            raise PermissionError(13, "Permission denied")
        real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", replace)
    monkeypatch.setattr(storage.time, "sleep", lambda seconds: None)


def test_final_save_failure_keeps_a_copy_the_next_run_will_not_overwrite(tmp_path, monkeypatch):
    out = tmp_path / "out.csv"
    writer = open_writer(out)
    writer.write({"id": 1})
    writer.write({"id": 2})
    lock_main_file(monkeypatch, out)

    with pytest.raises(OSError, match=r"out\.unsaved-\d{8}-\d{6}\.csv") as exc:
        writer.close()
    rescued = next(tmp_path.glob("out.unsaved-*.csv"))
    assert rescued.name in str(exc.value)
    assert [row["id"] for row in storage.CsvWriter.load(rescued)] == ["1", "2"]

    monkeypatch.undo()
    with open_writer(out) as writer:  # the next run
        writer.write({"id": 3})
    assert [row["id"] for row in storage.CsvWriter.load(rescued)] == ["1", "2"]


@pytest.mark.parametrize("resume", [False, True])
def test_leftover_tmp_is_kept_and_used_by_resume(tmp_path, resume):
    out = tmp_path / "out.jsonl"
    with open_writer(out) as writer:
        writer.write({"id": 1})
    # a run killed while out.jsonl was locked left its newest data in the .tmp
    (tmp_path / "out.jsonl.tmp").write_text('{"id": 1}\n{"id": 2}\n{"id": 3}\n', encoding="utf-8")

    writer = open_writer(out, resume=resume)
    writer.close()

    rescued = list(tmp_path.glob("out.unsaved-*.jsonl"))
    assert len(rescued) == 1 and not (tmp_path / "out.jsonl.tmp").exists()
    if resume:
        assert writer.saved_keys == {1, 2, 3}
        assert [r["id"] for r in storage.JsonLinesWriter.load(out)] == [1, 2, 3]


class BlockOnPage(FakeClient):
    """Serves a 3-page search; raises SiteBlocked on ``blocked`` pages."""

    def __init__(self, blocked=()):
        pages = {page_url(SEARCH_URL, n): search_html([n * 10, n * 10 + 1], last_page=3) for n in (1, 2, 3)}
        pages.update({page_url(SEARCH_URL, n): SiteBlocked("HTTP 468") for n in blocked})
        super().__init__(pages)


@pytest.mark.parametrize("name", ["out.csv", "out.jsonl", "out.json"])
def test_resume_continues_a_search_from_the_next_page(tmp_path, name):
    out = tmp_path / name
    with pytest.raises(SiteBlocked):
        with open_writer(out) as writer:
            scrape(BlockOnPage(blocked=[3]), [SEARCH_URL], writer)

    client = BlockOnPage()
    with open_writer(out, resume=True) as writer:
        assert scrape(client, [SEARCH_URL], writer, skip=writer.saved_keys) == 2

    # page 2, the last one done, is checked again in case listings moved up meanwhile
    assert page_url(SEARCH_URL, 1) not in client.requested
    assert client.requested[-1] == page_url(SEARCH_URL, 3)
    assert [storage.record_key(r) for r in type(writer).load(out)] == [10, 11, 20, 21, 30, 31]


def test_finished_search_is_skipped_on_resume(tmp_path):
    out = tmp_path / "out.jsonl"
    with open_writer(out) as writer:
        scrape(BlockOnPage(), [SEARCH_URL], writer)

    client = BlockOnPage()
    with open_writer(out, resume=True) as writer:
        assert scrape(client, [SEARCH_URL], writer, skip=writer.saved_keys) == 0
    assert client.requested == []


def test_progress_is_never_ahead_of_the_saved_listings(tmp_path):
    out = tmp_path / "out.csv"
    writer = open_writer(out)
    writer._clock = clock = FakeClock()
    writer._saved_at = 0.0
    scrape(BlockOnPage(), [SEARCH_URL], writer, max_pages=2)

    # pages 1-2 are done, but nothing is on disk yet: a killed run must not skip them
    assert not storage.progress_path(out).exists()
    clock.now = storage.CHECKPOINT_INTERVAL + 1
    writer.write({"id": 99})  # triggers a checkpoint
    progress = json.loads(storage.progress_path(out).read_text(encoding="utf-8"))
    assert progress["next_page"] == {SEARCH_URL: 3}


def test_new_run_without_resume_starts_searches_over(tmp_path):
    out = tmp_path / "out.jsonl"
    with pytest.raises(SiteBlocked):
        with open_writer(out) as writer:
            scrape(BlockOnPage(blocked=[2]), [SEARCH_URL], writer)
    assert storage.progress_path(out).exists()

    client = BlockOnPage()
    with open_writer(out) as writer:
        scrape(client, [SEARCH_URL], writer)
    assert client.requested[0] == page_url(SEARCH_URL, 1)


def test_unreadable_progress_starts_from_the_first_page(tmp_path, caplog):
    out = tmp_path / "out.jsonl"
    with open_writer(out) as writer:
        writer.write({"id": 10})
    storage.progress_path(out).write_text("{broken", encoding="utf-8")

    client = BlockOnPage()
    with caplog.at_level(logging.WARNING):
        with open_writer(out, resume=True) as writer:
            scrape(client, [SEARCH_URL], writer, skip=writer.saved_keys)
    assert "первой страницы" in caplog.text
    assert client.requested[0] == page_url(SEARCH_URL, 1)


def test_pages_limit_leaves_the_search_open_for_resume(tmp_path):
    out = tmp_path / "out.jsonl"
    with open_writer(out) as writer:
        scrape(BlockOnPage(), [SEARCH_URL], writer, max_pages=1)

    client = BlockOnPage()
    with open_writer(out, resume=True) as writer:
        assert scrape(client, [SEARCH_URL], writer, skip=writer.saved_keys) == 4
    assert client.requested[-1] == page_url(SEARCH_URL, 3)


def test_briefly_locked_file_is_replaced_after_a_retry(tmp_path, monkeypatch):
    # Windows: an antivirus or the search indexer holds a just-written file for a moment
    real_replace, failures, sleeps = storage.os.replace, [PermissionError(13, "WinError 5")] * 2, []

    def replace(src, dst):
        if failures:
            raise failures.pop()
        real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", replace)
    monkeypatch.setattr(storage.time, "sleep", sleeps.append)
    out = tmp_path / "out.json"
    storage.replace_file(out, "utf-8", lambda file: file.write("[]"))
    assert out.read_text(encoding="utf-8") == "[]"
    assert sleeps == [0.05, 0.1]


def test_progress_that_cannot_be_saved_does_not_stop_the_run(tmp_path, monkeypatch, caplog):
    out = tmp_path / "out.jsonl"
    real_replace = storage.os.replace

    def replace(src, dst):
        if Path(dst) == storage.progress_path(out):
            raise PermissionError(13, "WinError 5")
        real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", replace)
    monkeypatch.setattr(storage.time, "sleep", lambda seconds: None)
    with caplog.at_level(logging.WARNING):
        with open_writer(out) as writer:
            assert scrape(BlockOnPage(), [SEARCH_URL], writer) == 6
    assert "несколько страниц раньше" in caplog.text
    assert [r["id"] for r in storage.JsonLinesWriter.load(out)] == [10, 11, 20, 21, 30, 31]


class ShiftingSite:
    """A price-sorted search of 20 listings per page whose listings can be sold between runs."""

    def __init__(self, ids, blocked_page=None):
        self.ids = list(ids)
        self.blocked_page = blocked_page
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        page = int(url.rsplit("page=", 1)[1]) if "page=" in url else 1
        if page == self.blocked_page:
            raise SiteBlocked("HTTP 468")
        last_page = max(1, -(-len(self.ids) // 20))
        page = min(page, last_page)  # out-of-range numbers show the last page
        return search_html(self.ids[(page - 1) * 20:page * 20], last_page=last_page)


@pytest.mark.parametrize("sold", [5, 25, 70])
def test_resume_finds_listings_that_moved_to_pages_already_done(tmp_path, sold):
    out = tmp_path / "out.jsonl"
    site = ShiftingSite(range(1000, 1200), blocked_page=6)
    with pytest.raises(SiteBlocked):
        with open_writer(out) as writer:
            scrape(site, [SEARCH_URL], writer)

    # while the run is stopped, the cheapest listings (already saved) are sold
    site.ids, site.blocked_page = site.ids[sold:], None
    with open_writer(out, resume=True) as writer:
        scrape(site, [SEARCH_URL], writer, skip=writer.saved_keys)

    saved = {r["id"] for r in storage.JsonLinesWriter.load(out)}
    assert set(site.ids) <= saved


def test_progress_is_ignored_when_the_data_file_no_longer_matches(tmp_path, caplog):
    out = tmp_path / "out.csv"
    with open_writer(out) as writer:
        scrape(BlockOnPage(), [SEARCH_URL], writer)
    out.unlink()  # e.g. the user archived the CSV to start over, keeping --resume in the command

    client = BlockOnPage()
    with caplog.at_level(logging.WARNING):
        with open_writer(out, resume=True) as writer:
            assert scrape(client, [SEARCH_URL], writer, skip=writer.saved_keys) == 6
    assert "меньше объявлений" in caplog.text


def test_page_without_cards_mid_search_does_not_finish_it(tmp_path, caplog):
    out = tmp_path / "out.jsonl"
    stub = BlockOnPage()
    stub.pages[page_url(SEARCH_URL, 2)] = "<html><body>Ведутся технические работы</body></html>"
    with caplog.at_level(logging.WARNING):
        with open_writer(out) as writer:
            assert scrape(stub, [SEARCH_URL], writer) == 2
    assert "заглушка" in caplog.text

    with open_writer(out, resume=True) as writer:
        assert scrape(BlockOnPage(), [SEARCH_URL], writer, skip=writer.saved_keys) == 4
