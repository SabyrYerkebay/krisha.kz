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
# 468 is krisha.kz's own "too many requests". It hits listing pages (/a/show/) first, after
# a few hundred requests in ten minutes, and can last half an hour or more.
THROTTLE_STATUSES = {429, 468}
RETRY_STATUSES = THROTTLE_STATUSES | {500, 502, 503, 504}
MAX_RETRY_AFTER = 120
BLOCK_WAITS = (30, 60, 120, 300, 600, 900, 1200, 1800)  # seconds; the last one repeats
DEFAULT_BLOCK_WAIT = 2 * 3600  # give up on a rate limit that lasts longer than this
MAX_DELAY = 20.0  # the pause between requests grows up to this after rate limits
SPEED_UP_AFTER = 300  # requests without a rate limit before the pause is shortened again
ERROR_WAITS = (5, 15, 30, 60, 120, 300)  # seconds between retries after network or server errors


class RobotsDisallowed(Exception):
    """robots.txt of the site forbids fetching the URL."""


class SiteBlocked(Exception):
    """The site kept rate-limiting us (HTTP 429/468) for longer than we wait."""


class KrishaClient:
    def __init__(
        self,
        delay: float = 1.5,
        timeout: float = 30.0,
        retries: int = 8,
        user_agent: str = DEFAULT_USER_AGENT,
        session: requests.Session | None = None,
        block_wait: float = DEFAULT_BLOCK_WAIT,
    ):
        self.delay = delay
        self.base_delay = delay
        self.block_wait = block_wait
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
        self._calm_requests = 0
        self._blocked_pace = 0.0  # the longest pause between requests that still got rate-limited
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
        """GET with retries; returns the last answer when the site is still rate-limiting us."""
        attempt = 0  # server and network errors
        blocks = 0  # rate-limit answers in a row
        waited = 0.0  # time spent waiting for a rate limit to end
        while True:
            self._throttle()
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.retries:
                    raise
                wait = self._backoff(attempt)
                attempt += 1
                log.warning("%s: %s, повтор %d/%d через %.0f с", url, exc, attempt, self.retries, wait)
            else:
                status = response.status_code
                if status in THROTTLE_STATUSES:
                    if blocks == 0:
                        self._slow_down()
                    # a short Retry-After would not clear a block
                    wait = max(self._retry_after(response) or 0.0, BLOCK_WAITS[min(blocks, len(BLOCK_WAITS) - 1)])
                    if waited + wait > self.block_wait:
                        return self._done(response)
                    blocks += 1
                    waited += wait
                    log.warning("%s: сайт ограничил доступ (HTTP %d), жду %s — до %s", url, status,
                                _duration(wait), time.strftime("%H:%M", time.localtime(time.time() + wait)))
                elif status in RETRY_STATUSES and attempt < self.retries:
                    wait = self._retry_after(response) or self._backoff(attempt)
                    attempt += 1
                    log.warning("%s: HTTP %d, повтор %d/%d через %.0f с", url, status, attempt, self.retries, wait)
                else:
                    self._calm_down()
                    return self._done(response)
            self._sleep(wait)

    @staticmethod
    def _done(response: requests.Response) -> requests.Response:
        if "charset" not in response.headers.get("Content-Type", "").lower():
            response.encoding = "utf-8"  # requests would fall back to ISO-8859-1
        return response

    def _slow_down(self) -> None:
        """A rate limit means we were too fast: double the pause between requests."""
        self._calm_requests = 0
        if self.base_delay <= 0:
            return
        self._blocked_pace = max(self._blocked_pace, self.delay)
        delay = min(MAX_DELAY, max(self.delay * 2, 3.0))
        if delay > self.delay:
            self.delay = delay
            log.warning("Пауза между запросами увеличена до %.0f с", delay)

    def _calm_down(self) -> None:
        """After a long stretch without rate limits, shorten the pause again.

        Never back to a pace that got rate-limited: a quarter above it at least, and not
        below the pause the user chose.
        """
        floor = max(self.base_delay, self._blocked_pace * 1.25)
        if self.delay <= floor:
            return
        self._calm_requests += 1
        if self._calm_requests >= SPEED_UP_AFTER:
            self._calm_requests = 0
            self.delay = max(floor, self.delay / 1.5)
            log.info("Пауза между запросами уменьшена до %.1f с", self.delay)

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
                f"krisha.kz ограничил доступ (HTTP {response.status_code}) для {url} и не снял ограничение "
                "за время ожидания (--block-wait). Запустите ту же команду с --resume позже: "
                "уже сохранённые объявления не будут скачиваться заново"
            )

    @staticmethod
    def _backoff(attempt: int) -> float:
        """5 s, 15 s, 30 s, 1, 2, 5, 5… min for server and network errors: rides out a dropped Wi-Fi."""
        return float(ERROR_WAITS[min(attempt, len(ERROR_WAITS) - 1)])

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


def _duration(seconds: float) -> str:
    return f"{seconds / 60:.0f} мин" if seconds >= 60 else f"{seconds:.0f} с"
