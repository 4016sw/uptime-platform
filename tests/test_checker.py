import httpx

from uptime.worker.checker import check_url


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_up_when_status_matches():
    client = _client(lambda request: httpx.Response(200))
    outcome = check_url("https://example.com", 200, 5, client=client)
    assert outcome.is_up
    assert outcome.status_code == 200
    assert outcome.error is None
    assert outcome.response_time_ms is not None


def test_down_when_status_differs():
    client = _client(lambda request: httpx.Response(503))
    outcome = check_url("https://example.com", 200, 5, client=client)
    assert not outcome.is_up
    assert outcome.status_code == 503
    assert "503" in outcome.error


def test_custom_expected_status():
    client = _client(lambda request: httpx.Response(204))
    outcome = check_url("https://example.com", 204, 5, client=client)
    assert outcome.is_up


def test_down_on_timeout():
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    outcome = check_url("https://example.com", 200, 5, client=_client(handler))
    assert not outcome.is_up
    assert outcome.status_code is None
    assert "Timed out" in outcome.error


def test_down_on_connection_error():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    outcome = check_url("https://example.com", 200, 5, client=_client(handler))
    assert not outcome.is_up
    assert "ConnectError" in outcome.error
