"""Command-line interface: ``python -m krisha_scraper URL ...``."""

from __future__ import annotations

import argparse
import logging
from urllib.parse import urlsplit

import requests

from .client import DEFAULT_USER_AGENT, KrishaClient, RobotsDisallowed
from .scraper import scrape
from .storage import open_writer

log = logging.getLogger("krisha_scraper")


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("должно быть целое число больше 0")
    return number


def _krisha_url(value: str) -> str:
    host = urlsplit(value).hostname or ""
    if host != "krisha.kz" and not host.endswith(".krisha.kz"):
        raise argparse.ArgumentTypeError(f"ожидается ссылка на krisha.kz, получено {value!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m krisha_scraper",
        description="Парсер объявлений krisha.kz.",
        epilog="Пример: python -m krisha_scraper https://krisha.kz/prodazha/kvartiry/almaty/ "
               "--pages 3 --details -o data/almaty.csv",
    )
    parser.add_argument("urls", nargs="+", metavar="URL", type=_krisha_url,
                        help="страница поиска krisha.kz (с любыми фильтрами) или объявление /a/show/…")
    parser.add_argument("-o", "--output", default="data/listings.csv",
                        help="файл результата: .csv, .jsonl или .json (по умолчанию %(default)s)")
    parser.add_argument("-p", "--pages", type=_positive_int,
                        help="сколько страниц поиска обойти (по умолчанию все)")
    parser.add_argument("--start-page", type=_positive_int, default=1,
                        help="с какой страницы начать (по умолчанию %(default)s)")
    parser.add_argument("-d", "--details", action="store_true",
                        help="открывать каждое объявление: параметры, описание, координаты, фото")
    parser.add_argument("--limit", type=_positive_int, help="остановиться после N объявлений")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="пауза между запросами, секунд (по умолчанию %(default)s)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="таймаут запроса, секунд (по умолчанию %(default)s)")
    parser.add_argument("--retries", type=int, default=3,
                        help="повторы при ошибках сети и HTTP 429/5xx (по умолчанию %(default)s)")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="заголовок User-Agent")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        writer = open_writer(args.output)
    except ValueError as exc:
        parser.error(str(exc))

    client = KrishaClient(delay=args.delay, timeout=args.timeout, retries=args.retries,
                          user_agent=args.user_agent)
    status = 0
    try:
        with writer:
            scrape(client, args.urls, writer, max_pages=args.pages, start_page=args.start_page,
                   details=args.details, limit=args.limit)
    except KeyboardInterrupt:
        log.warning("Остановлено пользователем")
        status = 130
    except (RobotsDisallowed, requests.RequestException) as exc:
        log.error("Ошибка: %s", exc)
        status = 1
    log.info("Сохранено объявлений: %d → %s", writer.count, args.output)
    return status
