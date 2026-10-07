"""Parsers for krisha.kz search result pages and listing pages.

Search results are ``div.a-card`` blocks. A listing page keeps its data in
``.offer__*`` blocks plus a ``<script id="jsdata">window.data = {...}</script>``
JSON payload, which is the most reliable source for coordinates and photos.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

BASE_URL = "https://krisha.kz"

_ROOMS_RE = re.compile(r"(\d+)\s*-\s*комнат", re.IGNORECASE)
_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*м²")
_FLOOR_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*этаж", re.IGNORECASE)
_SINGLE_FLOOR_RE = re.compile(r"(\d+)\s*этаж\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d[\d\s]*")
_LISTING_ID_RE = re.compile(r"/a/show/(\d+)")


@dataclass
class SearchPage:
    cards: list[dict] = field(default_factory=list)
    total: int | None = None
    last_page: int | None = None


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace (including non-breaking spaces); empty -> None."""
    if value is None:
        return None
    text = " ".join(value.split())
    return text or None


def parse_int(text: str | None) -> int | None:
    """First number in the text: '25 000 000 〒' -> 25000000."""
    if not text:
        return None
    match = _NUMBER_RE.search(text)
    return int(re.sub(r"\s", "", match.group())) if match else None


def parse_title(title: str | None) -> dict:
    """Rooms, area and floor from a title like '2-комнатная квартира · 56 м² · 5/9 этаж'."""
    result = {"rooms": None, "area_m2": None, "floor": None, "floors_total": None}
    if not title:
        return result
    if match := _ROOMS_RE.search(title):
        result["rooms"] = int(match.group(1))
    if match := _AREA_RE.search(title):
        result["area_m2"] = float(match.group(1).replace(",", "."))
    if match := _FLOOR_RE.search(title):
        result["floor"], result["floors_total"] = int(match.group(1)), int(match.group(2))
    elif match := _SINGLE_FLOOR_RE.search(title):
        result["floor"] = int(match.group(1))
    return result


def parse_search_page(html: str, page_url: str = BASE_URL) -> SearchPage:
    soup = BeautifulSoup(html, "lxml")
    cards = [parse_card(card, page_url) for card in soup.select("div.a-card[data-id]")]
    total = parse_int(_node_text(soup.select_one(".a-search-subtitle")))
    return SearchPage(cards=cards, total=total, last_page=_parse_last_page(soup))


def parse_card(card: Tag, page_url: str = BASE_URL) -> dict:
    link = card.select_one("a.a-card__title") or card.select_one('a[href*="/a/show/"]')
    href = link.get("href") if link is not None else None
    listing_id = _to_int(card.get("data-id")) or listing_id_from_url(href)
    if href:
        url = urljoin(page_url, href)
    else:
        url = f"{BASE_URL}/a/show/{listing_id}" if listing_id else None
    title = _node_text(link)
    price_text = _node_text(card.select_one(".a-card__price"))
    stats = [_node_text(item) for item in card.select(".a-card__stats-item")]
    return {
        "id": listing_id,
        "url": url,
        "title": title,
        **parse_title(title),
        "price": parse_int(price_text),
        "price_text": price_text,
        "address": _node_text(card.select_one(".a-card__subtitle")),
        "city": stats[0] if stats else None,
        "published": stats[1] if len(stats) > 1 else None,
        "description_preview": _node_text(card.select_one(".a-card__text-preview")),
        "photo": _image_src(card.select_one(".a-card__image img") or card.select_one("img")),
    }


def parse_listing_page(html: str, url: str | None = None) -> dict:
    soup = BeautifulSoup(html, "lxml")
    advert = _extract_jsdata(soup).get("advert")
    if not isinstance(advert, dict):
        advert = {}
    map_data = advert.get("map") if isinstance(advert.get("map"), dict) else {}

    title = _node_text(soup.select_one(".offer__advert-title h1")) or clean_text(advert.get("title"))
    price_text = _node_text(soup.select_one(".offer__price"))
    address = _node_text(soup.select_one(".offer__location span")) or clean_text(advert.get("addressTitle"))
    description = soup.select_one(".offer__description .text, .offer__description .js-description")
    return {
        "id": _to_int(advert.get("id")) or listing_id_from_url(url),
        "url": url,
        "title": title,
        **parse_title(title),
        "price": parse_int(price_text) if price_text else _to_int(advert.get("price")),
        "price_text": price_text,
        "address": address,
        "lat": _to_float(map_data.get("lat")),
        "lon": _to_float(map_data.get("lon")),
        "description": description.get_text("\n", strip=True) if description is not None else None,
        "photos": _parse_photos(soup, advert),
        "params": _parse_params(soup),
    }


def _parse_params(soup: BeautifulSoup) -> dict:
    """Listing parameters keyed by their label on the site ('Тип дома', 'Этаж', ...)."""
    params = {}
    for item in soup.select(".offer__info-item"):
        label = _node_text(item.select_one(".offer__info-title"))
        value = _node_text(item.select_one(".offer__advert-short-info"))
        if label and value:
            params[label] = value
    for row in soup.select(".offer__parameters dl"):
        label, value = _node_text(row.find("dt")), _node_text(row.find("dd"))
        if label and value:
            params.setdefault(label, value)
    return params


def _parse_photos(soup: BeautifulSoup, advert: dict) -> list[str]:
    photos = []
    for photo in advert.get("photos") or []:
        src = photo.get("src") if isinstance(photo, dict) else photo
        if isinstance(src, str) and src:
            photos.append(src)
    if not photos:
        for node in soup.select(".gallery__small-item[data-photo-url], .gallery__main img"):
            src = node.get("data-photo-url") or _image_src(node)
            if src:
                photos.append(src)
    return list(dict.fromkeys(photos))


def _parse_last_page(soup: BeautifulSoup) -> int | None:
    pages = []
    for button in soup.select(".paginator a, .paginator__btn"):
        number = _to_int(button.get("data-page")) or _to_int(_node_text(button))
        if number:
            pages.append(number)
    return max(pages) if pages else None


def _extract_jsdata(soup: BeautifulSoup) -> dict:
    script = soup.find("script", id="jsdata")
    if script is None:
        return {}
    text = script.string or script.get_text()
    start = text.find("{")
    if start == -1:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _node_text(node: Tag | None) -> str | None:
    return clean_text(node.get_text(" ")) if node is not None else None


def _image_src(node: Tag | None) -> str | None:
    if node is None:
        return None
    for attr in ("src", "data-src"):
        src = node.get(attr)
        if src and not src.startswith("data:"):
            return src
    return None


def listing_id_from_url(url: str | None) -> int | None:
    match = _LISTING_ID_RE.search(url or "")
    return int(match.group(1)) if match else None


def _to_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _to_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
