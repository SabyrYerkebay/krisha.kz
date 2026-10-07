import csv
import os
import signal

import pytest

from krisha_scraper import cli
from krisha_scraper.scraper import page_url

SEARCH_URL = "https://krisha.kz/prodazha/kvartiry/almaty/"


def search_html(ids, last_page):
    cards = "".join(f'<div class="a-card" data-id="{i}"><a class="a-card__title" href="/a/show/{i}">'
                    f'1-комнатная квартира · 40 м²</a></div>' for i in ids)
    pages = "".join(f'<a class="paginator__btn" data-page="{n}">{n}</a>' for n in range(1, last_page + 1))
    return f'<html><body>{cards}<nav class="paginator">{pages}</nav></body></html>'


class FakeClient:
    """Serves 3 search pages; sends the process SIGTERM when page 2 is requested."""

    def __init__(self, **kwargs):
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        if url == page_url(SEARCH_URL, 2):
            os.kill(os.getpid(), signal.SIGTERM)
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
