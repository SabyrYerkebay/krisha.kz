from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests

from krisha_scraper.client import KrishaClient, RobotsDisallowed, SiteBlocked
from krisha_scraper.robots import RobotsRules

ROBOTS = """
User-agent: Googlebot
Disallow: /

User-agent: *
Disallow: /my/
Disallow: /a/ajaxPhones
Disallow: /*?*das[
Allow: /my/public$
"""


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = responses  # url -> list of responses/exceptions, consumed in order
        self.headers = {}
        self.calls = []

    def get(self, url, timeout):
        self.calls.append(url)
        result = self.responses[url].pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_client(responses, **kwargs):
    client = KrishaClient(delay=0, session=FakeSession(responses), **kwargs)
    client.sleeps = []
    client._sleep = client.sleeps.append
    return client


@pytest.mark.parametrize("url, allowed", [
    ("https://krisha.kz/prodazha/kvartiry/almaty/", True),
    ("https://krisha.kz/prodazha/kvartiry/almaty/?page=2", True),
    ("https://krisha.kz/a/show/123", True),
    ("https://krisha.kz/my/adverts", False),
    ("https://krisha.kz/my/public", True),
    ("https://krisha.kz/my/public/x", False),
    ("https://krisha.kz/a/ajaxPhones?id=1", False),
    ("https://krisha.kz/prodazha/kvartiry/?das[live.rooms]=2", False),
    ("https://krisha.kz/prodazha/kvartiry/?das%5Blive.rooms%5D=2", False),
])
def test_robots_rules(url, allowed):
    rules = RobotsRules.parse(ROBOTS, "Mozilla/5.0 Chrome/129.0")
    assert rules.can_fetch(url) is allowed


def test_robots_rules_with_bom():
    rules = RobotsRules.parse("\ufeffUser-agent: *\nDisallow: /my/\n", "Mozilla/5.0")
    assert not rules.can_fetch("https://krisha.kz/my/adverts")


def test_robots_rules_specific_group_wins():
    rules = RobotsRules.parse(ROBOTS, "Mozilla/5.0 (compatible; Googlebot/2.1)")
    assert not rules.can_fetch("https://krisha.kz/a/show/1")


def test_get_checks_robots():
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text=ROBOTS)],
        "https://krisha.kz/a/show/1": [FakeResponse(text="<html>ok</html>")],
    })
    assert client.get("https://krisha.kz/a/show/1") == "<html>ok</html>"
    with pytest.raises(RobotsDisallowed):
        client.get("https://krisha.kz/my/adverts")
    assert client.session.calls.count("https://krisha.kz/robots.txt") == 1


def test_missing_robots_allows_everything():
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(404)],
        "https://krisha.kz/my/adverts": [FakeResponse(text="ok")],
    })
    assert client.get("https://krisha.kz/my/adverts") == "ok"


def test_robots_server_error_disallows_everything():
    client = make_client({"https://krisha.kz/robots.txt": [FakeResponse(503)] * 2}, retries=1)
    with pytest.raises(RobotsDisallowed):
        client.get("https://krisha.kz/a/show/1")


def test_unreachable_site_raises_network_error():
    client = make_client({"https://krisha.kz/robots.txt": [requests.ConnectionError("refused")]}, retries=0)
    with pytest.raises(requests.ConnectionError):
        client.get("https://krisha.kz/a/show/1")


def test_retries_on_server_and_network_errors():
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text="")],
        "https://krisha.kz/a/show/1": [
            FakeResponse(503, headers={"Retry-After": "7"}),
            requests.ConnectionError("reset"),
            FakeResponse(text="ok"),
        ],
    })
    assert client.get("https://krisha.kz/a/show/1") == "ok"
    assert client.sleeps == [7.0, 15.0]


@pytest.mark.parametrize("retry_after, expected", [("5", 30.0), ("90", 90.0)])
def test_rate_limit_waits_at_least_the_backoff(retry_after, expected):
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text="")],
        "https://krisha.kz/a/show/1": [FakeResponse(429, headers={"Retry-After": retry_after}), FakeResponse(text="ok")],
    })
    assert client.get("https://krisha.kz/a/show/1") == "ok"
    assert client.sleeps == [expected]


def test_retry_after_http_date():
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text="")],
        "https://krisha.kz/a/show/1": [FakeResponse(503, headers={"Retry-After": when}), FakeResponse(text="ok")],
    })
    assert client.get("https://krisha.kz/a/show/1") == "ok"
    assert 55 <= client.sleeps[0] <= 60


@pytest.mark.parametrize("value", ["", "soon", "\u00b2", "Wed, 01 Jan 2020 00:00:00 GMT"])
def test_unusable_retry_after_falls_back_to_backoff(value):
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text="")],
        "https://krisha.kz/a/show/1": [FakeResponse(429, headers={"Retry-After": value}), FakeResponse(text="ok")],
    })
    assert client.get("https://krisha.kz/a/show/1") == "ok"
    assert client.sleeps == [30.0]


def test_retries_krisha_468_with_long_pauses():
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text="")],
        "https://krisha.kz/a/show/1": [FakeResponse(468), FakeResponse(468), FakeResponse(text="ok")],
    })
    assert client.get("https://krisha.kz/a/show/1") == "ok"
    assert client.sleeps == [30.0, 60.0]


def test_rate_limit_longer_than_block_wait_raises_site_blocked():
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text="")],
        "https://krisha.kz/a/show/1": [FakeResponse(468)] * 4,
    }, block_wait=210)
    with pytest.raises(SiteBlocked, match="--resume"):
        client.get("https://krisha.kz/a/show/1")
    assert client.sleeps == [30, 60, 120]  # the next wait (5 min) would go past --block-wait


def test_waits_out_a_rate_limit_of_half_an_hour():
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text="")],
        "https://krisha.kz/a/show/1": [FakeResponse(468)] * 7 + [FakeResponse(text="ok")],
    })
    assert client.get("https://krisha.kz/a/show/1") == "ok"
    assert client.sleeps == [30, 60, 120, 300, 600, 900, 1200]


def test_rate_limit_slows_the_client_down_then_it_recovers():
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text="")],
        "https://krisha.kz/a/show/1": [FakeResponse(468), FakeResponse(468), FakeResponse(text="ok")],
        "https://krisha.kz/a/show/2": [FakeResponse(text="ok")] * 600,
    })
    client.delay = client.base_delay = 1.5
    client.get("https://krisha.kz/a/show/1")
    assert client.delay == 3.0  # doubled once per rate limit, not once per retry

    for _ in range(300):
        client.get("https://krisha.kz/a/show/2")
    assert client.delay == 2.0
    for _ in range(300):
        client.get("https://krisha.kz/a/show/2")
    assert client.delay == 1.875  # never back to the 1.5 s that got rate-limited


def test_pace_settles_above_every_pace_that_got_rate_limited():
    client = make_client({"https://krisha.kz/robots.txt": [FakeResponse(text="")]})
    client.delay = client.base_delay = 1.5
    for _ in range(3):  # each time the client sped up, the site limited it again
        client._slow_down()
        for _ in range(3000):
            client._calm_down()
    blocked = client._blocked_pace
    assert client.delay >= blocked * 1.25
    assert blocked > 1.5


def test_rate_limited_robots_raises_site_blocked():
    # not cached as "no robots.txt, everything allowed"
    client = make_client({"https://krisha.kz/robots.txt": [FakeResponse(468)] * 2}, block_wait=30)
    with pytest.raises(SiteBlocked):
        client.get("https://krisha.kz/a/show/1")


def test_gives_up_after_retries():
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text="")],
        "https://krisha.kz/a/show/1": [FakeResponse(502)] * 3,
    }, retries=2)
    with pytest.raises(requests.HTTPError):
        client.get("https://krisha.kz/a/show/1")
    assert client.sleeps == [5.0, 15.0]


def test_rides_out_a_few_minutes_without_network():
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text="")],
        "https://krisha.kz/a/show/1": [requests.ConnectionError("no network")] * 6 + [FakeResponse(text="ok")],
    })
    assert client.get("https://krisha.kz/a/show/1") == "ok"
    assert client.sleeps == [5, 15, 30, 60, 120, 300]


def test_defaults_to_utf8_without_charset():
    response = FakeResponse(text="ok", headers={"Content-Type": "text/html"})
    response.encoding = "ISO-8859-1"
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text="")],
        "https://krisha.kz/a/show/1": [response],
    })
    client.get("https://krisha.kz/a/show/1")
    assert response.encoding == "utf-8"


def test_robots_without_charset_is_read_as_utf8():
    robots = requests.Response()
    robots.status_code = 200
    robots._content = "User-agent: *\nDisallow: /поиск/\n".encode("utf-8")
    robots.headers["Content-Type"] = "text/plain"
    robots.encoding = "ISO-8859-1"  # what requests picks for text/* without a charset
    client = make_client({"https://krisha.kz/robots.txt": [robots]})
    assert not client.allowed("https://krisha.kz/%D0%BF%D0%BE%D0%B8%D1%81%D0%BA/1")
