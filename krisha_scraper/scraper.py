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
        if result.last_page is not None and page > result.last_page:
            log.info("Страница %d за пределами выдачи: всего страниц %d", page, result.last_page)
            break
        new_cards = []
        for card in result.cards:  # paid "hot" cards can repeat a listing, even on the same page
            if _key(card) not in seen:
                seen.add(_key(card))
                new_cards.append(card)
        of_pages = f" из {result.last_page}" if result.last_page else ""
        log.info("Страница %d%s: %d объявлений", page, of_pages, len(new_cards))
        if not new_cards:
            break
        yield from new_cards
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
    """Scrape search pages (or single listing URLs) into ``writer``; returns the record count.

    A listing found by several of the URLs is saved once.
    """
    seen = set()
    count = 0
    for url in urls:
        listing_id = listing_id_from_url(url)
        if listing_id:
            cards: Iterable[dict] = [{"id": listing_id, "url": url}]
        else:
            cards = iter_search(client, url, max_pages=max_pages, start_page=start_page)
        for card in cards:
            if _key(card) in seen:
                continue
            seen.add(_key(card))
            if listing_id:
                record = parse_listing_page(client.get(url), url)
            elif details:
                record = with_details(client, card)
            else:
                record = card
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
