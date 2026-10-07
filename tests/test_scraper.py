import csv
import json

import pytest
import requests

from krisha_scraper.client import RobotsDisallowed
from krisha_scraper.scraper import iter_search, page_url, scrape
from krisha_scraper.storage import open_writer

SEARCH_URL = "https://krisha.kz/prodazha/kvartiry/almaty/"


def search_html(ids, last_page=None):
    cards = "".join(
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


def test_iter_search_respects_max_pages_and_start_page():
    client = FakeClient({page_url(SEARCH_URL, n): search_html([n * 10], last_page=50) for n in range(1, 51)})
    cards = list(iter_search(client, SEARCH_URL, max_pages=2, start_page=4))
    assert [card["id"] for card in cards] == [40, 50]


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
