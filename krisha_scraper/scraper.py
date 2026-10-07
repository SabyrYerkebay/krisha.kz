"""Crawling: walk search result pages and optionally open every listing."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from .client import KrishaClient, RobotsDisallowed
from .parsers import listing_id_from_url, parse_listing_page, parse_search_page
from .storage import Writer

log = logging.getLogger(__name__)


def page_url(search_url: str, page: int) -> str:
    """The search URL for a given result page; filters in the query are kept."""
    parts = urlsplit(search_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
    if page > 1:
        query.append(("page", str(page)))
    return urlunsplit(parts._replace(query=urlencode(query)))


def iter_search(
    client: KrishaClient,
    search_url: str,
    max_pages: int | None = None,
    start_page: int = 1,
) -> Iterator[dict]:
    """Listing cards from consecutive result pages, without duplicates.

    Stops after the last page in the paginator, on a page with no new cards
    (krisha.kz repeats the last page for out-of-range numbers) or after
    ``max_pages`` pages.
    """
    seen = set()
    page = start_page
    while max_pages is None or page < start_page + max_pages:
        url = page_url(search_url, page)
        result = parse_search_page(client.get(url), url)
        if page == start_page and result.total is not None:
            log.info("Найдено объявлений: %d", result.total)
        new_cards = [card for card in result.cards if _key(card) not in seen]
        of_pages = f" из {result.last_page}" if result.last_page else ""
        log.info("Страница %d%s: %d объявлений", page, of_pages, len(new_cards))
        if not new_cards:
            break
        for card in new_cards:
            seen.add(_key(card))
            yield card
        if result.last_page is not None and page >= result.last_page:
            break
        page += 1


def scrape(
    client: KrishaClient,
    urls: Iterable[str],
    writer: Writer,
    max_pages: int | None = None,
    start_page: int = 1,
    details: bool = False,
    limit: int | None = None,
) -> int:
    """Scrape search pages (or single listing URLs) into ``writer``; returns the record count."""
    count = 0
    for url in urls:
        if listing_id_from_url(url):
            records: Iterable[dict] = [parse_listing_page(client.get(url), url)]
        else:
            records = iter_search(client, url, max_pages=max_pages, start_page=start_page)
            if details:
                records = (with_details(client, card) for card in records)
        for record in records:
            writer.write(record)
            count += 1
            if limit and count >= limit:
                return count
    return count


def with_details(client: KrishaClient, card: dict) -> dict:
    """The card merged with data from its listing page; the card alone if that fails."""
    if not card.get("url"):
        return card
    try:
        detail = parse_listing_page(client.get(card["url"]), card["url"])
    except (RobotsDisallowed, requests.RequestException) as exc:
        log.warning("Не удалось открыть %s: %s", card["url"], exc)
        return card
    merged = dict(card)
    merged.update({key: value for key, value in detail.items() if value not in (None, "", [], {})})
    return merged


def _key(card: dict):
    return card.get("id") or card.get("url")
