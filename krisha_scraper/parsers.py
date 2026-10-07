"""Parsers for krisha.kz search result pages and listing pages.

Search results are ``div.a-card`` blocks. A listing page keeps its data in
``.offer__*`` blocks plus a ``<script id="jsdata">window.data = {...}</script>``
JSON payload (``var data = ...`` on older pages). The payload is the only
source of coordinates, the full address and the creation date.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

BASE_URL = "https://krisha.kz"

_ROOMS_RE = re.compile(r"(\d+)\s*-\s*комнат", re.IGNORECASE)
_AREA_RE = re.compile(r"(\d+(?:[   ]\d{3})*(?:[.,]\d+)?)\s*м²")
_FLOOR_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*этаж", re.IGNORECASE)
_SINGLE_FLOOR_RE = re.compile(r"(\d+)\s*этаж\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d[\d\s]*")
_LISTING_ID_RE = re.compile(r"/a/show/(\d+)")
_JSDATA_RE = re.compile(r"\bdata\s*=\s*\{")


@dataclass
class SearchPage:
    cards: list[dict] = field(default_factory=list)
    total: int | None = None
    last_page: int | None = None


def clean_text(value) -> str | None:
    """Collapse whitespace (including non-breaking spaces); empty or non-string -> None."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def parse_int(text: str | None) -> int | None:
    """First number in the text: '25 000 000 ₸' -> 25000000."""
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
        result["area_m2"] = float(re.sub(r"\s", "", match.group(1)).replace(",", "."))
    if match := _FLOOR_RE.search(title):
        result["floor"], result["floors_total"] = int(match.group(1)), int(match.group(2))
    elif match := _SINGLE_FLOOR_RE.search(title):
        result["floor"] = int(match.group(1))
    return result


def listing_id_from_url(url: str | None) -> int | None:
    match = _LISTING_ID_RE.search(url or "")
    return int(match.group(1)) if match else None


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
        # The card shows when the listing was last bumped, not when it was created.
        "updated": stats[1] if len(stats) > 1 else None,
        "description_preview": _node_text(card.select_one(".a-card__text-preview")),
        "photo": _image_src(card.select_one(".a-card__image img")),
    }


def parse_listing_page(html: str, url: str | None = None) -> dict:
    soup = BeautifulSoup(html, "lxml")
    for map_link in soup.select(".btm-map"):  # "показать на карте" inside the location value
        map_link.decompose()

    data = _extract_jsdata(soup)
    advert = data.get("advert") if isinstance(data.get("advert"), dict) else {}
    listing_id = _to_int(advert.get("id")) or listing_id_from_url(url)
    summary = _advert_summary(data, listing_id)
    map_data = advert.get("map") if isinstance(advert.get("map"), dict) else {}

    title = _node_text(soup.select_one(".offer__advert-title h1")) or clean_text(advert.get("title"))
    price_text = _node_text(soup.select_one(".offer__price"))
    # .offer__location holds only "city, district"; the street is in the JSON payload.
    location = _node_text(soup.select_one(".offer__location span"))
    address = clean_text(summary.get("fullAddress")) or (
        ", ".join(part for part in (location, clean_text(advert.get("addressTitle"))) if part) or None
    )
    # .text also wraps the translate widget, so prefer the inner .js-description.
    description = (soup.select_one(".offer__description .js-description")
                   or soup.select_one(".offer__description .text"))
    return {
        "id": listing_id,
        "url": url,
        "title": title,
        **parse_title(title),
        "price": parse_int(price_text) if price_text else _to_int(advert.get("price")),
        "price_text": price_text,
        "price_m2": _to_int(summary.get("priceM2")),
        "address": address,
        "city": clean_text(summary.get("city")),
        "lat": _to_float(map_data.get("lat")),
        "lon": _to_float(map_data.get("lon")),
        "created_at": clean_text(summary.get("createdAt")),
        "updated_at": clean_text(summary.get("addedAt")),
        "seller_type": clean_text(advert.get("userType")),
        "status": clean_text(advert.get("status")),
        "description": _multiline_text(description),
        "photos": _parse_photos(soup, advert),
        "params": _parse_params(soup),
    }


def _advert_summary(data: dict, listing_id: int | None) -> dict:
    """The entry for this listing in the payload's ``adverts`` list."""
    adverts = data.get("adverts")
    if not isinstance(adverts, list):
        return {}
    entries = [entry for entry in adverts if isinstance(entry, dict)]
    if listing_id is None:
        return entries[0] if entries else {}
    return next((entry for entry in entries if _to_int(entry.get("id")) == listing_id), {})


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
    raw_photos = advert.get("photos")
    for photo in raw_photos if isinstance(raw_photos, list) else []:
        src = photo.get("src") if isinstance(photo, dict) else photo
        if isinstance(src, str) and src:
            photos.append(src)
    if not photos:
        photos = [node.get("data-photo-url") for node in soup.select(".gallery__small-item[data-photo-url]")]
    if not photos:
        photos = [_image_src(img) for img in soup.select(".gallery__main img")]
    return list(dict.fromkeys(src for src in photos if src))


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
    match = _JSDATA_RE.search(text)
    start = match.end() - 1 if match else text.find("{")
    if start == -1:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _node_text(node: Tag | None) -> str | None:
    return clean_text(node.get_text(" ")) if node is not None else None


def _multiline_text(node: Tag | None) -> str | None:
    """Text with line breaks kept: whitespace collapsed per line, at most one blank line in a row."""
    if node is None:
        return None
    lines = [" ".join(line.split()) for line in node.get_text("\n").splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text or None


def _image_src(node: Tag | None) -> str | None:
    """Image URL, skipping lazy-load stubs and the site's "no photo" placeholders."""
    if node is None:
        return None
    for attr in ("src", "data-src"):
        src = node.get(attr)
        if src and not src.startswith("data:") and "/static/" not in src:
            return src
    return None


def _to_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        return int(value.strip())
    return None


def _to_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
