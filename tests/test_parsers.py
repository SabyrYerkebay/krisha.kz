from pathlib import Path

import pytest

from krisha_scraper.parsers import parse_int, parse_listing_page, parse_search_page, parse_title

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://krisha.kz/prodazha/kvartiry/almaty/"
LISTING_URL = "https://krisha.kz/a/show/1000001"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def listing_without_jsdata() -> str:
    html = read_fixture("listing_page.html")
    return html[:html.index('<script id="jsdata">')] + "</body></html>"


@pytest.mark.parametrize("title, expected", [
    ("2-комнатная квартира · 56 м² · 5/9 этаж", (2, 56.0, 5, 9)),
    ("1-комнатная квартира, 42 м², 15/16 этаж, Акмешит", (1, 42.0, 15, 16)),
    ("1-комнатная квартира · 44.95 м²", (1, 44.95, None, None)),
    ("1-комнатная квартира, 38,5 м², 12 этаж", (1, 38.5, 12, None)),
    ("4-комнатный дом · 150 м² · 2 этажа · 6 сот.", (4, 150.0, None, None)),
    ("Помещение · 1 200 м²", (None, 1200.0, None, None)),
    ("Офис · 120 м²", (None, 120.0, None, None)),
])
def test_parse_title(title, expected):
    result = parse_title(title)
    assert (result["rooms"], result["area_m2"], result["floor"], result["floors_total"]) == expected


@pytest.mark.parametrize("text, expected", [
    ("25 000 000 ₸", 25_000_000),
    ("от 18 900 000 ₸", 18_900_000),
    ("320 000 〒 за месяц", 320_000),
    ("договорная", None),
    (None, None),
])
def test_parse_int(text, expected):
    assert parse_int(text) == expected


def test_parse_search_page():
    page = parse_search_page(read_fixture("search_page.html"), SEARCH_URL)

    assert page.total == 12_345
    assert page.last_page == 618
    # div.hot-a-wrap[data-id] is not a card; the repeated hot card is (iter_search drops it)
    assert [card["id"] for card in page.cards] == [1000001, 1000002, 1000001]

    first, second, _ = page.cards
    assert first == {
        "id": 1000001,
        "url": "https://krisha.kz/a/show/1000001",
        "title": "2-комнатная квартира · 56 м² · 5/9 этаж",
        "rooms": 2,
        "area_m2": 56.0,
        "floor": 5,
        "floors_total": 9,
        "price": 25_000_000,
        "price_text": "25 000 000 ₸",
        "address": "Алмалинский р-н, Абая 10 — Байтурсынова",
        "city": "Алматы",
        "updated": "5 окт.",
        "description_preview": "жил. комплекс Пример, кирпичный дом, 1985 г.п., состояние: хорошее",
        "photo": "https://krisha-photos.kcdn.online/webp/aa/aaa/1-400x300.jpg",
    }
    assert second["price"] == 18_900_000
    assert (second["area_m2"], second["floor"]) == (38.5, None)
    assert second["description_preview"] is None


def test_card_photo_ignores_other_images():
    html = """<div class="a-card" data-id="5"><a class="a-card__title" href="/a/show/5">Офис · 50 м²</a>
        <div class="a-card__paid-services"><img src="/static/frontend/images/tooltip-hot.svg"></div></div>"""
    assert parse_search_page(html).cards[0]["photo"] is None


def test_parse_search_page_without_results():
    page = parse_search_page("<html><body><p>Ничего не найдено</p></body></html>")
    assert page.cards == []
    assert page.total is None
    assert page.last_page is None


def test_parse_listing_page():
    listing = parse_listing_page(read_fixture("listing_page.html"), LISTING_URL)

    assert listing == {
        "id": 1000001,
        "url": LISTING_URL,
        "title": "2-комнатная квартира · 56 м² · 5/9 этаж, Абая 10",
        "rooms": 2,
        "area_m2": 56.0,
        "floor": 5,
        "floors_total": 9,
        "price": 25_000_000,
        "price_text": "25 000 000 ₸",
        "price_m2": 446_429,
        "address": "Алматы, Алмалинский р-н, Абая 10",
        "city": "Алматы",
        "lat": 43.238949,
        "lon": 76.889709,
        "created_at": "2026-09-20",
        "updated_at": "2026-10-05",
        "seller_type": "specialist",
        "status": "live",
        "description": "Продаётся светлая квартира.\n\nРядом школа и парк.",
        "photos": [
            "https://krisha-photos.kcdn.online/webp/aa/aaa/1-full.jpg",
            "https://krisha-photos.kcdn.online/webp/aa/aaa/2-full.jpg",
        ],
        "params": {
            "Город": "Алматы, Алмалинский р-н",
            "Тип дома": "кирпичный",
            "Год постройки": "1985",
            "Этаж": "5 из 9",
            "Санузел": "раздельный",
            "Балкон": "балкон",
        },
    }


def test_parse_listing_page_without_jsdata():
    listing = parse_listing_page(listing_without_jsdata(), LISTING_URL)

    assert listing["id"] == 1000001  # from the URL
    assert listing["price"] == 25_000_000
    assert listing["address"] == "Алматы, Алмалинский р-н"  # the street is only in the JSON payload
    assert listing["lat"] is None
    assert listing["created_at"] is None
    # gallery thumbnails, not the "empty photo" placeholder
    assert listing["photos"] == [
        "https://krisha-photos.kcdn.online/webp/aa/aaa/1-750x470.jpg",
        "https://krisha-photos.kcdn.online/webp/aa/aaa/2-750x470.jpg",
    ]


def test_photo_fallback_skips_placeholder():
    html = listing_without_jsdata().replace('class="gallery__small-item', 'class="removed')
    listing = parse_listing_page(html, LISTING_URL)
    assert listing["photos"] == ["https://krisha-photos.kcdn.online/webp/aa/aaa/1-750x470.jpg"]


def test_description_without_js_description():
    # 2024 layout: no .js-description, the text blocks sit directly in .text
    html = """<div class="offer__description"><div class="text">
        <div class="a-options-text a-text-white-spaces">Пластиковые окна, тихий двор.</div>
        <div class="a-text a-text-white-spaces">Дом в центре. \n \nДокументы в порядке.</div></div></div>"""
    assert parse_listing_page(html)["description"] == (
        "Пластиковые окна, тихий двор.\n\nДом в центре.\n\nДокументы в порядке."
    )


def test_older_var_data_payload_and_address_fallback():
    html = """<html><body>
        <div class="offer__location offer__advert-short-info"><span>Астана, Есильский р-н</span>
        <a class="btm-map" href="javascript:;">показать на карте</a></div>
        <script id="jsdata">
        var digitalData = {"page": {}};
        var data = {"advert": {"id": 7, "title": "3-комнатная квартира · 80 м² · 2/5 этаж",
            "price": 41000000, "addressTitle": "Сарайшык 5", "map": {"lat": "51.1", "lon": "71.4"}}};
        </script></body></html>"""
    listing = parse_listing_page(html, "https://krisha.kz/a/show/7")

    assert listing["id"] == 7
    assert listing["price"] == 41_000_000
    assert listing["rooms"] == 3
    assert listing["address"] == "Астана, Есильский р-н, Сарайшык 5"
    assert (listing["lat"], listing["lon"]) == (51.1, 71.4)
    assert listing["photos"] == []
    assert listing["params"] == {}


def test_summary_of_another_listing_is_ignored():
    html = """<script id="jsdata">window.data = {"advert": {"id": 7},
        "adverts": [{"id": 8, "fullAddress": "чужой адрес", "createdAt": "2020-01-01"}]};</script>"""
    listing = parse_listing_page(html)
    assert listing["address"] is None
    assert listing["created_at"] is None


@pytest.mark.parametrize("payload", [
    "{broken",
    "[1, 2]",
    '{"advert": [1]}',
    '{"advert": {"id": 9, "title": 123, "addressTitle": ["a"], "photos": 5, "map": "x"}, "adverts": "x"}',
    '{"advert": {"photos": "https://p/1.webp", "map": {"lat": true}}, "adverts": [null, 3]}',
])
def test_parse_listing_page_with_unexpected_jsdata(payload):
    html = f'<html><body><script id="jsdata">window.data = {payload};</script></body></html>'
    listing = parse_listing_page(html, "https://krisha.kz/a/show/9")
    assert listing["id"] == 9
    assert listing["title"] is None
    assert listing["photos"] == []
    assert listing["lat"] is None
