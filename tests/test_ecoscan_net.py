import gzip
import io
import zipfile

import pytest

from tools.ecoscan.net import (
    USER_AGENT,
    Http,
    HttpError,
    RangeFile,
    RateLimiter,
    Response,
    Tally,
    TooLarge,
)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Fake:
    """A transport that replays canned responses and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url, dict(headers)))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _http(transport, clock=None, **kw):
    clock = clock or Clock()
    limiter = RateLimiter(default=5.0, clock=clock.time, sleep=clock.sleep)
    return Http(limiter, transport=transport, jitter=lambda: 0.0, **kw), clock


def ok(body=b"{}", status=200, headers=None):
    return Response(status, headers or {}, body, len(body))


def test_every_request_carries_the_research_user_agent():
    fake = Fake(ok())
    http, _ = _http(fake)
    http.get("https://example.org/x")
    # The repo URL is the contact point; no personal email in a header sent to every host.
    assert (
        fake.calls[0][2]["User-Agent"]
        == USER_AGENT
        == "agentprint-research/0.1 (+https://github.com/adighatakdas-cloud/agentprint)"
    )


def test_rate_is_at_most_five_per_second_per_host():
    fake = Fake(*[ok() for _ in range(12)])
    http, clock = _http(fake)
    for _ in range(11):
        http.get("https://a.example/x")
    assert clock.now == pytest.approx(2.0)  # 11 starts at 5/s need 2 seconds
    before = clock.now
    http.get("https://b.example/x")  # another host has its own budget
    assert clock.now == before


def test_rate_above_five_is_refused():
    with pytest.raises(ValueError):
        RateLimiter(default=6.0)
    with pytest.raises(ValueError):
        RateLimiter(rates={"a.example": 10.0})


def test_retry_after_seconds_is_honored():
    fake = Fake(ok(status=429, headers={"retry-after": "7"}), ok(b"done"))
    http, clock = _http(fake)
    assert http.get("https://a.example/x").body == b"done"
    assert 7 in clock.sleeps


def test_retry_after_http_date_is_honored():
    date = "Wed, 21 Oct 2015 07:28:00 GMT"
    fake = Fake(ok(status=503, headers={"retry-after": date}), ok())
    http, clock = _http(fake, now=lambda: 1445412470.0)  # 10 s before that date
    http.get("https://a.example/x")
    assert 10 in clock.sleeps


def test_server_errors_back_off_exponentially():
    fake = Fake(ok(status=503), ok(status=502), ok(status=500), ok(b"done"))
    http, clock = _http(fake)
    assert http.get("https://a.example/x").body == b"done"
    assert [s for s in clock.sleeps if s >= 1] == [1, 2, 4]


def test_network_errors_are_retried():
    fake = Fake(TimeoutError("slow"), ConnectionResetError("reset"), ok(b"done"))
    http, _ = _http(fake)
    assert http.get("https://a.example/x").body == b"done"


def test_gives_up_after_max_retries():
    fake = Fake(*[ok(status=503) for _ in range(4)])
    http, _ = _http(fake, max_retries=3)
    with pytest.raises(HttpError, match="503"):
        http.get("https://a.example/x")
    assert len(fake.calls) == 4


def test_client_errors_are_returned_not_retried():
    fake = Fake(ok(status=404))
    http, _ = _http(fake)
    assert http.get("https://a.example/x").status == 404
    assert len(fake.calls) == 1


def test_too_large_is_not_retried():
    fake = Fake(TooLarge("big"))
    http, _ = _http(fake)
    with pytest.raises(TooLarge):
        http.get("https://a.example/x", max_bytes=10)


def test_gzip_is_decoded_and_wire_bytes_are_tallied():
    raw = gzip.compress(b'{"a": 1}' * 100)
    fake = Fake(Response(200, {"content-encoding": "gzip"}, raw, len(raw)))
    http, _ = _http(fake)
    tally = Tally()
    resp = http.get("https://a.example/x", tally=tally)
    assert resp.body == b'{"a": 1}' * 100
    assert (tally.requests, tally.bytes) == (1, len(raw))
    assert http.total.bytes == len(raw)


def test_redirects_are_followed_with_rate_limit():
    fake = Fake(ok(status=302, headers={"location": "https://b.example/y"}), ok(b"there"))
    http, _ = _http(fake)
    assert http.get("https://a.example/x").body == b"there"
    assert fake.calls[1][1] == "https://b.example/y"


# --- RangeFile: read a zip's directory (and only wanted members) with HTTP Range requests ---


class RangeServer:
    def __init__(self, data: bytes, honor_range=True):
        self.data, self.honor_range = data, honor_range
        self.calls: list[str | None] = []
        self.sent = 0

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        rng = headers.get("Range")
        self.calls.append(rng)
        if rng is None or not self.honor_range:
            self.sent += len(self.data)
            return ok(self.data)
        start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
        chunk = self.data[start : end + 1]
        self.sent += len(chunk)
        headers = {"content-range": f"bytes {start}-{start + len(chunk) - 1}/{len(self.data)}"}
        return Response(206, headers, chunk, len(chunk))


def _big_zip() -> bytes:
    import random

    noise = random.Random(1).randbytes(2_000_000)  # incompressible, so the zip stays big
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("demo/big.bin", noise)
        for i in range(50):
            zf.writestr(f"demo/m{i}.py", f"x = {i}\n")
        zf.writestr("demo/.claude/settings.local.json", b'{"permissions": {}}')
    return buf.getvalue()


def test_range_file_lists_a_zip_without_downloading_it():
    data = _big_zip()
    server = RangeServer(data)
    http, _ = _http(server)
    with zipfile.ZipFile(RangeFile(http, "https://f.example/demo.whl", len(data))) as zf:
        names = zf.namelist()
        assert zf.read("demo/.claude/settings.local.json") == b'{"permissions": {}}'
    assert "demo/.claude/settings.local.json" in names and len(names) == 52
    assert server.sent < 200_000  # the 2 MB member was never fetched
    assert len(server.calls) <= 3


def test_range_file_falls_back_to_whole_file_when_range_is_ignored():
    data = _big_zip()
    server = RangeServer(data, honor_range=False)
    http, _ = _http(server)
    with zipfile.ZipFile(RangeFile(http, "https://f.example/demo.whl", len(data))) as zf:
        assert len(zf.namelist()) == 52
    assert len(server.calls) == 1


def test_small_file_is_fetched_without_range():
    data = b"PK" + b"x" * 100
    server = RangeServer(data)
    http, _ = _http(server)
    assert RangeFile(http, "https://f.example/s.whl", len(data)).read() == data
    assert server.calls == [None]


def test_range_file_respects_its_byte_budget():
    data = _big_zip()
    http, _ = _http(RangeServer(data))
    rf = RangeFile(http, "https://f.example/demo.whl", len(data), max_bytes=100_000)
    rf.seek(0)
    with pytest.raises(TooLarge):
        rf.read(1_500_000)
