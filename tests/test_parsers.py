from pathlib import Path

import pytest

from krisha_scraper.parsers import parse_int, parse_listing_page, parse_search_page, parse_title

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://krisha.kz/prodazha/kvartiry/almaty/"
LISTING_URL = "https://krisha.kz/a/show/1000001"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("title, expected", [
    ("2-комнатная квартира · 56 м² · 5/9 этаж", (2, 56.0, 5, 9)),
    ("1-комнатная квартира, 38,5 м², 12 этаж", (1, 38.5, 12, None)),
    ("4-комнатный дом · 150 м² · 2 этажа · 6 сот.", (4, 150.0, None, None)),
    ("Офис · 120 м²", (None, 120.0, None, None)),
])
def test_parse_title(title, expected):
    result = parse_title(title)
    assert (result["rooms"], result["area_m2"], result["floor"], result["floors_total"]) == expected


@pytest.mark.parametrize("text, expected", [
    ("25 000 000 〒", 25_000_000),
    ("от 18 900 000 〒", 18_900_000),
    ("200 000 〒 / за 2 дня", 200_000),
    ("договорная", None),
    (None, None),
])
def test_parse_int(text, expected):
    assert parse_int(text) == expected


def test_parse_search_page():
    page = parse_search_page(read_fixture("search_page.html"), SEARCH_URL)

    assert page.total == 12_345
    assert page.last_page == 618
    assert [card["id"] for card in page.cards] == [1000001, 1000002]

    first, second = page.cards
    assert first == {
        "id": 1000001,
        "url": "https://krisha.kz/a/show/1000001",
        "title": "2-комнатная квартира · 56 м² · 5/9 этаж",
        "rooms": 2,
        "area_m2": 56.0,
        "floor": 5,
        "floors_total": 9,
        "price": 25_000_000,
        "price_text": "25 000 000 〒",
        "address": "Алмалинский р-н, Абая 10 — Байтурсынова",
        "city": "Алматы",
        "published": "5 окт.",
        "description_preview": "Квартира в хорошем состоянии, рядом школа и парк.",
        "photo": "https://alakt-photos-kr.kcdn.kz/webp/1000001-280x175.webp",
    }
    assert second["price"] == 18_900_000
    assert second["description_preview"] is None
    assert second["photo"] == "https://alakt-photos-kr.kcdn.kz/webp/1000002-280x175.webp"


def test_parse_search_page_without_results():
    page = parse_search_page("<html><body><p>Ничего не найдено</p></body></html>")
    assert page.cards == []
    assert page.total is None
    assert page.last_page is None


def test_parse_listing_page():
    listing = parse_listing_page(read_fixture("listing_page.html"), LISTING_URL)

    assert listing["id"] == 1000001
    assert listing["url"] == LISTING_URL
    assert listing["title"].startswith("2-комнатная квартира")
    assert (listing["rooms"], listing["area_m2"], listing["floor"], listing["floors_total"]) == (2, 56.0, 5, 9)
    assert listing["price"] == 25_000_000
    assert listing["address"] == "Алматы, Алмалинский р-н, Абая 10"
    assert (listing["lat"], listing["lon"]) == (43.238949, 76.889709)
    assert listing["description"] == "Продаётся светлая квартира.\nРядом школа и парк."
    assert listing["photos"] == [
        "https://alakt-photos-kr.kcdn.kz/webp/1-full.webp",
        "https://alakt-photos-kr.kcdn.kz/webp/2-full.webp",
    ]
    assert listing["params"] == {
        "Город": "Алматы, Алмалинский р-н",
        "Тип дома": "кирпичный",
        "Год постройки": "1985",
        "Состояние": "хорошее",
        "Санузел": "раздельный",
        "Балкон": "балкон",
    }


def test_parse_listing_page_falls_back_to_jsdata():
    html = """<html><body><script id="jsdata">window.data = {"advert": {"id": 7,
        "title": "3-комнатная квартира · 80 м² · 2/5 этаж", "price": 41000000,
        "addressTitle": "Сарайшык 5"}};</script></body></html>"""
    listing = parse_listing_page(html, "https://krisha.kz/a/show/7")

    assert listing["id"] == 7
    assert listing["price"] == 41_000_000
    assert listing["rooms"] == 3
    assert listing["address"] == "Сарайшык 5"
    assert listing["lat"] is None
    assert listing["photos"] == []
    assert listing["params"] == {}


def test_parse_listing_page_with_broken_jsdata():
    html = '<html><body><script id="jsdata">window.data = {broken</script></body></html>'
    listing = parse_listing_page(html, "https://krisha.kz/a/show/9")
    assert listing["id"] == 9
    assert listing["title"] is None
