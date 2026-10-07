"""Command-line interface: ``python -m krisha_scraper URL ...``."""

from __future__ import annotations

import argparse
import logging
import math
import signal
from contextlib import contextmanager
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


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("должно быть целое число не меньше 0")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if not (math.isfinite(number) and number > 0):
        raise argparse.ArgumentTypeError("должно быть число больше 0")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if not (math.isfinite(number) and number >= 0):
        raise argparse.ArgumentTypeError("должно быть число не меньше 0")
    return number


def _krisha_url(value: str) -> str:
    if "://" not in value:
        value = "https://" + value.lstrip("/")
    parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or (host != "krisha.kz" and not host.endswith(".krisha.kz")):
        raise argparse.ArgumentTypeError(f"ожидается ссылка на krisha.kz, получено {value!r}")
    return value


@contextmanager
def _stop_on_termination():
    """Turn SIGTERM/SIGHUP into KeyboardInterrupt so buffered CSV/JSON output still gets written."""
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    names = [name for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)]
    previous = {name: signal.signal(getattr(signal, name), interrupt) for name in names}
    try:
        yield
    finally:
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)


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
    parser.add_argument("--delay", type=_non_negative_float, default=1.5,
                        help="пауза между запросами, секунд (по умолчанию %(default)s)")
    parser.add_argument("--timeout", type=_positive_float, default=30.0,
                        help="таймаут запроса, секунд (по умолчанию %(default)s)")
    parser.add_argument("--retries", type=_non_negative_int, default=3,
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
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    client = KrishaClient(delay=args.delay, timeout=args.timeout, retries=args.retries,
                          user_agent=args.user_agent)
    status = 0
    try:
        with _stop_on_termination(), writer:
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
