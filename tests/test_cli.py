import csv
import signal

import pytest

from krisha_scraper import cli, storage
from krisha_scraper.client import SiteBlocked
from krisha_scraper.scraper import page_url

SEARCH_URL = "https://krisha.kz/prodazha/kvartiry/almaty/"


def search_html(ids, last_page):
    cards = "".join(f'<div class="a-card" data-id="{i}"><a class="a-card__title" href="/a/show/{i}">'
                    f'1-комнатная квартира · 40 м²</a></div>' for i in ids)
    pages = "".join(f'<a class="paginator__btn" data-page="{n}">{n}</a>' for n in range(1, last_page + 1))
    return f'<html><body>{cards}<nav class="paginator">{pages}</nav></body></html>'


class FakeClient:
    """Serves 3 search pages; sends the process ``SIGNAL`` when page 2 is requested."""

    SIGNAL = getattr(signal, "SIGTERM", None)

    def __init__(self, **kwargs):
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        if url == page_url(SEARCH_URL, 2):
            signal.raise_signal(self.SIGNAL)
        page = int(url.rsplit("page=", 1)[1]) if "page=" in url else 1
        return search_html([page * 10, page * 10 + 1], last_page=3)


@pytest.mark.parametrize("value, expected", [
    ("https://krisha.kz/prodazha/kvartiry/", "https://krisha.kz/prodazha/kvartiry/"),
    ("krisha.kz/prodazha/kvartiry/", "https://krisha.kz/prodazha/kvartiry/"),
    ("https://m.krisha.kz/a/show/1", "https://m.krisha.kz/a/show/1"),
])
def test_krisha_url_accepted(value, expected):
    assert cli._krisha_url(value) == expected


@pytest.mark.parametrize("value", ["https://example.com/", "https://notkrisha.kz/", "ftp://krisha.kz/x"])
def test_krisha_url_rejected(value):
    with pytest.raises(cli.argparse.ArgumentTypeError):
        cli._krisha_url(value)


@pytest.mark.parametrize("args", [
    ["--timeout", "0"], ["--timeout", "-1"], ["--delay", "-1"], ["--delay", "inf"],
    ["--retries", "-1"], ["--pages", "0"],
])
def test_invalid_numbers_rejected(args, tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main([SEARCH_URL, "-o", str(tmp_path / "out.csv"), *args])
    assert exc.value.code == 2


def test_bad_output_path_fails_before_scraping(tmp_path, monkeypatch):
    (tmp_path / "out.csv").mkdir()
    monkeypatch.setattr(cli, "KrishaClient", FakeClient)
    with pytest.raises(SystemExit) as exc:
        cli.main([SEARCH_URL, "-o", str(tmp_path / "out.csv")])
    assert exc.value.code == 2


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="no SIGTERM on this platform")
def test_sigterm_saves_collected_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "KrishaClient", FakeClient)
    handler_before = signal.getsignal(signal.SIGTERM)
    out = tmp_path / "out.csv"

    assert cli.main([SEARCH_URL, "--delay", "0", "-o", str(out)]) == 130

    with open(out, encoding="utf-8-sig", newline="") as file:
        assert [row["id"] for row in csv.DictReader(file)] == ["10", "11"]
    assert signal.getsignal(signal.SIGTERM) is handler_before


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="no SIGTERM on this platform")
def test_repeated_signal_does_not_cut_the_save_short(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "KrishaClient", FakeClient)
    original_close = storage.CsvWriter.close

    def close_with_second_signal(self):
        signal.raise_signal(signal.SIGTERM)
        original_close(self)

    monkeypatch.setattr(storage.CsvWriter, "close", close_with_second_signal)
    out = tmp_path / "out.csv"

    assert cli.main([SEARCH_URL, "--delay", "0", "-o", str(out)]) == 130

    with open(out, encoding="utf-8-sig", newline="") as file:
        assert [row["id"] for row in csv.DictReader(file)] == ["10", "11"]


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
def test_sighup_ignored_by_nohup_stays_ignored(tmp_path, monkeypatch):
    class HangupClient(FakeClient):
        SIGNAL = signal.SIGHUP

    monkeypatch.setattr(cli, "KrishaClient", HangupClient)
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        out = tmp_path / "out.csv"
        assert cli.main([SEARCH_URL, "--delay", "0", "-o", str(out)]) == 0
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)

    with open(out, encoding="utf-8-sig", newline="") as file:
        assert [row["id"] for row in csv.DictReader(file)] == ["10", "11", "20", "21", "30", "31"]


def test_site_blocked_saves_collected_rows(tmp_path, monkeypatch):
    class BlockingClient(FakeClient):
        def get(self, url):
            if url == page_url(SEARCH_URL, 2):
                raise SiteBlocked("krisha.kz ограничил доступ (HTTP 468)")
            return search_html([10, 11], last_page=3)

    monkeypatch.setattr(cli, "KrishaClient", BlockingClient)
    out = tmp_path / "out.csv"

    assert cli.main([SEARCH_URL, "--delay", "0", "-o", str(out)]) == 1

    with open(out, encoding="utf-8-sig", newline="") as file:
        assert [row["id"] for row in csv.DictReader(file)] == ["10", "11"]


def test_ctrl_c_during_the_final_save_is_ignored(tmp_path, monkeypatch):
    class OnePageClient(FakeClient):
        def get(self, url):
            return search_html([10, 11], last_page=1)

    monkeypatch.setattr(cli, "KrishaClient", OnePageClient)
    original_dump = storage.CsvWriter._dump

    def dump_with_ctrl_c(self, file):
        signal.raise_signal(signal.SIGINT)
        original_dump(self, file)

    monkeypatch.setattr(storage.CsvWriter, "_dump", dump_with_ctrl_c)
    handler_before = signal.getsignal(signal.SIGINT)
    out = tmp_path / "out.csv"

    assert cli.main([SEARCH_URL, "--delay", "0", "-o", str(out)]) == 0

    with open(out, encoding="utf-8-sig", newline="") as file:
        assert [row["id"] for row in csv.DictReader(file)] == ["10", "11"]
    assert signal.getsignal(signal.SIGINT) is handler_before


def test_resume_after_site_blocked(tmp_path, monkeypatch):
    class BlockingClient(FakeClient):
        def get(self, url):
            if url == page_url(SEARCH_URL, 2):
                raise SiteBlocked("krisha.kz ограничил доступ (HTTP 468)")
            return search_html([10, 11], last_page=3)

    class WorkingClient(FakeClient):
        def get(self, url):
            self.requested.append(url)
            page = int(url.rsplit("page=", 1)[1]) if "page=" in url else 1
            return search_html([page * 10, page * 10 + 1], last_page=3)

    out = tmp_path / "out.csv"
    monkeypatch.setattr(cli, "KrishaClient", BlockingClient)
    assert cli.main([SEARCH_URL, "--delay", "0", "-o", str(out)]) == 1

    clients = []
    monkeypatch.setattr(cli, "KrishaClient", lambda **kwargs: clients.append(WorkingClient()) or clients[-1])
    assert cli.main([SEARCH_URL, "--delay", "0", "--resume", "-o", str(out)]) == 0
    # page 1, the last one done, is checked again in case listings moved up meanwhile
    assert clients[0].requested == [page_url(SEARCH_URL, n) for n in (1, 2, 3)]

    with open(out, encoding="utf-8-sig", newline="") as file:
        assert [row["id"] for row in csv.DictReader(file)] == ["10", "11", "20", "21", "30", "31"]


@pytest.mark.parametrize("stop, status, message", [
    (SiteBlocked("krisha.kz ограничил доступ (HTTP 468)"), 1, "ограничил доступ"),
    (KeyboardInterrupt(), 130, "Остановлено пользователем"),
])
def test_failed_save_does_not_hide_why_the_run_stopped(tmp_path, monkeypatch, caplog, stop, status, message):
    class StoppingClient(FakeClient):
        def get(self, url):
            if url == page_url(SEARCH_URL, 2):
                raise stop
            return search_html([10, 11], last_page=3)

    def locked_close(self):
        raise OSError("Не удалось записать out.csv (файл открыт в Excel?)")

    monkeypatch.setattr(cli, "KrishaClient", StoppingClient)
    monkeypatch.setattr(storage.CsvWriter, "close", locked_close)

    assert cli.main([SEARCH_URL, "--delay", "0", "-o", str(tmp_path / "out.csv")]) == status
    assert message in caplog.text
    assert "Ошибка сохранения" in caplog.text
    assert "Сохранено объявлений" not in caplog.text


def test_limit_counts_listings_already_in_the_file(tmp_path, monkeypatch):
    class AllPagesClient(FakeClient):
        def get(self, url):
            page = int(url.rsplit("page=", 1)[1]) if "page=" in url else 1
            return search_html([page * 10, page * 10 + 1], last_page=3)

    monkeypatch.setattr(cli, "KrishaClient", AllPagesClient)
    out = tmp_path / "out.csv"
    assert cli.main([SEARCH_URL, "--delay", "0", "--limit", "2", "-o", str(out)]) == 0
    assert cli.main([SEARCH_URL, "--delay", "0", "--limit", "3", "--resume", "-o", str(out)]) == 0
    assert cli.main([SEARCH_URL, "--delay", "0", "--limit", "3", "--resume", "-o", str(out)]) == 0

    assert [row["id"] for row in storage.CsvWriter.load(out)] == ["10", "11", "20"]
