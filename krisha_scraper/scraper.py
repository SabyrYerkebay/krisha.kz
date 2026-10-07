"""Crawling: walk search result pages and optionally open every listing."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from .client import KrishaClient, RobotsDisallowed
from .parsers import listing_id_from_url, parse_listing_page, parse_search_page
from .storage import Writer

log = logging.getLogger(__name__)

PAGE_SIZE = 20  # regular cards per search page; paid "hot" cards come on top


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
    on_page_done: Callable[[int], None] | None = None,
    on_finished: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Listing cards from consecutive result pages, without duplicates.

    Stops after the last page in the paginator or after ``max_pages`` pages.
    A page with only cards already seen does not end the search when the
    paginator shows more pages: in the default newest-first order, listings
    bumped by their sellers push the rest down while the search is walked.
    Without a paginator such a page is the end (krisha.kz repeats the last
    page for out-of-range numbers). ``on_page_done(page)`` is called once
    every card of that page has been taken, and ``on_finished()`` when the
    results end (not when ``max_pages`` cuts the walk short).
    """
    seen = set()
    page = start_page
    while max_pages is None or page < start_page + max_pages:
        url = page_url(search_url, page)
        result = parse_search_page(client.get(url), url)
        if page == start_page and result.total is not None:
            log.info("Найдено объявлений: %d", result.total)
            if result.last_page and result.total > result.last_page * PAGE_SIZE:
                # krisha.kz stops paging at 1000 pages, so the rest of a big search is unreachable
                log.warning(
                    "Сайт показывает только %d страниц (около %d объявлений) из %d. "
                    "Чтобы получить все, разбейте поиск фильтрами: цена, район, комнаты",
                    result.last_page, result.last_page * PAGE_SIZE, result.total,
                )
        if result.last_page is not None and page > result.last_page:
            log.info("Страница %d за пределами выдачи: всего страниц %d", page, result.last_page)
            break
        new_cards = []
        for card in result.cards:  # paid "hot" cards can repeat a listing, even on the same page
            if _key(card) not in seen:
                seen.add(_key(card))
                new_cards.append(card)
        of_pages = f" из {result.last_page}" if result.last_page else ""
        repeated = len(result.cards) - len(new_cards)
        log.info("Страница %d%s: %d объявлений%s", page, of_pages, len(new_cards),
                 f" (ещё {repeated} уже были на прошлых страницах)" if repeated else "")
        if not result.cards or (not new_cards and result.last_page is None):
            break
        yield from new_cards
        if on_page_done:
            on_page_done(page)
        if result.last_page is not None and page >= result.last_page:
            break
        page += 1
    else:
        return  # stopped by max_pages: the search itself is not finished
    if on_finished:
        on_finished()


def scrape(
    client: KrishaClient,
    urls: Iterable[str],
    writer: Writer,
    max_pages: int | None = None,
    start_page: int = 1,
    details: bool = False,
    limit: int | None = None,
    skip: Iterable = (),
) -> int:
    """Scrape search pages (or single listing URLs) into ``writer``; returns the record count.

    A listing found by several of the URLs is saved once. Listing URLs are handled
    before search URLs so their full record is the one kept. Listings whose id (or
    URL) is in ``skip``, e.g. saved by an earlier run, are not fetched again, and a
    search the writer's progress has started continues from its next page.
    """
    seen = set(skip)
    count = 0
    for url in sorted(urls, key=lambda u: listing_id_from_url(u) is None):
        listing_id = listing_id_from_url(url)
        search = None if listing_id else page_url(url, 1)
        if listing_id:
            cards: Iterable[dict] = [{"id": listing_id, "url": url}]
        elif writer.search_done(search):
            log.info("Поиск %s уже пройден", url)
            continue
        else:
            first_page = writer.next_page(search) or start_page
            if first_page != start_page:
                log.info("Продолжаю поиск %s со страницы %d", url, first_page)
            cards = iter_search(client, url, max_pages=max_pages, start_page=first_page,
                                on_page_done=lambda page, search=search: writer.page_done(search, page),
                                on_finished=lambda search=search: writer.finish_search(search))
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
