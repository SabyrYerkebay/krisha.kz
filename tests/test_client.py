import pytest
import requests

from krisha_scraper.client import KrishaClient, RobotsDisallowed
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


def test_retries_on_429_and_network_errors():
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text="")],
        "https://krisha.kz/a/show/1": [
            FakeResponse(429, headers={"Retry-After": "7"}),
            requests.ConnectionError("reset"),
            FakeResponse(text="ok"),
        ],
    })
    assert client.get("https://krisha.kz/a/show/1") == "ok"
    assert client.sleeps == [7.0, 4.0]


def test_gives_up_after_retries():
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text="")],
        "https://krisha.kz/a/show/1": [FakeResponse(502)] * 3,
    }, retries=2)
    with pytest.raises(requests.HTTPError):
        client.get("https://krisha.kz/a/show/1")
    assert client.sleeps == [2.0, 4.0]


def test_defaults_to_utf8_without_charset():
    response = FakeResponse(text="ok", headers={"Content-Type": "text/html"})
    response.encoding = "ISO-8859-1"
    client = make_client({
        "https://krisha.kz/robots.txt": [FakeResponse(text="")],
        "https://krisha.kz/a/show/1": [response],
    })
    client.get("https://krisha.kz/a/show/1")
    assert response.encoding == "utf-8"
