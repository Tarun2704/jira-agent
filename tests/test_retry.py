import httpx
import pytest

from app.retry import RetryingClient, is_transient_git_error, wait_for


def client(handler, sleeps=None):
    return RetryingClient(transport=httpx.MockTransport(handler), base_url="https://x.test",
                          sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def test_get_retries_5xx_then_succeeds():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(502 if len(calls) < 3 else 200, json={"ok": True})

    assert client(h).get("/a").json() == {"ok": True} and len(calls) == 3


def test_gives_up_after_three_tries_and_returns_the_last_response():
    calls = []
    r = client(lambda req: (calls.append(1), httpx.Response(503))[1]).get("/a")
    assert r.status_code == 503 and len(calls) == 3


def test_post_is_not_repeated_after_a_5xx_or_a_read_timeout_but_is_after_429():
    calls = []
    assert client(lambda req: (calls.append(1), httpx.Response(500))[1]).post("/a").status_code == 500 and len(calls) == 1

    def timeout(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow")

    calls.clear()
    with pytest.raises(httpx.ReadTimeout):
        client(timeout).post("/a")
    assert len(calls) == 1

    calls.clear()
    sleeps = []
    r = client(lambda req: (calls.append(1), httpx.Response(429, headers={"Retry-After": "2"}) if len(calls) == 1 else httpx.Response(200))[1], sleeps).post("/a")
    assert r.status_code == 200 and sleeps == [2.0]


def test_connect_errors_retry_for_any_method():
    calls = []

    def h(req):
        calls.append(1)
        if len(calls) < 2:
            raise httpx.ConnectError("down")
        return httpx.Response(200)

    assert client(h).post("/a").status_code == 200 and len(calls) == 2


def test_helpers():
    assert wait_for(1, None, 1.0) == 1.0 and wait_for(3, None, 1.0) == 4.0 and wait_for(1, "999", 1.0) == 30.0
    assert is_transient_git_error("fatal: unable to access ...: Could not resolve host: github.com")
    assert not is_transient_git_error("fatal: Remote branch nope not found in upstream origin")
