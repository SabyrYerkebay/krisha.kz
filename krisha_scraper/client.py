"""HTTP client for krisha.kz: throttling, retries and robots.txt."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from .robots import RobotsRules

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
# 468 is krisha.kz's own "too many requests"; it clears up after a minute or so.
THROTTLE_STATUSES = {429, 468}
RETRY_STATUSES = THROTTLE_STATUSES | {500, 502, 503, 504}
MAX_RETRY_AFTER = 120


class RobotsDisallowed(Exception):
    """robots.txt of the site forbids fetching the URL."""


class SiteBlocked(Exception):
    """The site keeps rate-limiting us (HTTP 429/468) after every retry."""


class KrishaClient:
    def __init__(
        self,
        delay: float = 1.5,
        timeout: float = 30.0,
        retries: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
        session: requests.Session | None = None,
    ):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "ru-RU,ru;q=0.9",
        })
        self._robots: dict[str, RobotsRules] = {}
        self._last_request = 0.0
        self._sleep = time.sleep
        self._clock = time.monotonic

    def get(self, url: str) -> str:
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt запрещает загрузку {url}")
        response = self._fetch(url)
        self._check_blocked(response, url)
        response.raise_for_status()
        return response.text

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin)
        return self._robots[origin].can_fetch(url)

    def _load_robots(self, origin: str) -> RobotsRules:
        robots_url = f"{origin}/robots.txt"
        response = self._fetch(robots_url)  # a network error means the site is down anyway
        self._check_blocked(response, robots_url)
        if response.status_code >= 500:
            log.warning("robots.txt ответил HTTP %s, загрузка страниц запрещена", response.status_code)
            return RobotsRules(disallow_all=True)
        if response.status_code >= 400:
            return RobotsRules()
        return RobotsRules.parse(response.text, self.user_agent)

    def _fetch(self, url: str) -> requests.Response:
        attempt = 0
        while True:
            self._throttle()
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.retries:
                    raise
                wait, reason = self._backoff(attempt), str(exc)
            else:
                if response.status_code not in RETRY_STATUSES or attempt >= self.retries:
                    if "charset" not in response.headers.get("Content-Type", "").lower():
                        response.encoding = "utf-8"  # requests would fall back to ISO-8859-1
                    return response
                if response.status_code in THROTTLE_STATUSES:  # a short Retry-After would not clear a block
                    wait = max(self._retry_after(response) or 0.0, self._backoff(attempt, throttled=True))
                else:
                    wait = self._retry_after(response) or self._backoff(attempt)
                reason = f"HTTP {response.status_code}"
            attempt += 1
            log.warning("%s: %s, повтор %d/%d через %.0f с", url, reason, attempt, self.retries, wait)
            self._sleep(wait)

    def _throttle(self) -> None:
        if self.delay > 0:
            wait = self._last_request + self.delay * random.uniform(0.8, 1.2) - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    @staticmethod
    def _check_blocked(response: requests.Response, url: str) -> None:
        if response.status_code in THROTTLE_STATUSES:
            raise SiteBlocked(
                f"krisha.kz ограничил доступ (HTTP {response.status_code}) для {url}. "
                "Подождите и запустите ту же команду с --resume (лучше и с большим --delay): "
                "уже сохранённые объявления не будут скачиваться заново"
            )

    @staticmethod
    def _backoff(attempt: int, throttled: bool = False) -> float:
        """2, 4, 8 s for server errors; 30, 60, 120 s when the site asks us to slow down."""
        if throttled:
            return min(120.0, 30.0 * 2 ** attempt)
        return min(60.0, 2.0 ** (attempt + 1))

    @staticmethod
    def _retry_after(response: requests.Response) -> float | None:
        """Retry-After in seconds; the header holds either seconds or an HTTP date."""
        value = response.headers.get("Retry-After", "").strip()
        if value.isascii() and value.isdigit():
            return min(float(value), MAX_RETRY_AFTER)
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
        return min(seconds, MAX_RETRY_AFTER) if seconds > 0 else None
