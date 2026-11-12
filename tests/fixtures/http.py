import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from evbot.adapters.http_client import RequestsJsonHttpClient


@dataclass(frozen=True, slots=True)
class SeenUrl:
    path: str
    params: dict[str, str]


@dataclass(frozen=True, slots=True)
class SeenRequest:
    """What a test needs from a sent request: the path, decoded query params and headers."""

    url: SeenUrl
    headers: Mapping[str, str]
    method: str = "GET"
    body: str | None = None


Handler = Callable[[SeenRequest], requests.Response]

NO_BODY = object()


def seen_request(prepared: requests.PreparedRequest) -> SeenRequest:
    parts = urlsplit(prepared.url or "")
    query = parse_qs(parts.query, keep_blank_values=True)
    params = {name: values[0] for name, values in query.items()}
    body = prepared.body.decode() if isinstance(prepared.body, bytes) else prepared.body
    return SeenRequest(
        SeenUrl(parts.path, params),
        CaseInsensitiveDict(prepared.headers),
        prepared.method or "GET",
        body,
    )


def response(
    status: int,
    json_body: Any = NO_BODY,
    headers: Mapping[str, str] | None = None,
    content: bytes | None = None,
) -> requests.Response:
    built = requests.Response()
    built.status_code = status
    if content is not None:
        built._content = content
    elif json_body is not NO_BODY:
        built._content = json.dumps(json_body).encode()
    else:
        built._content = b""
    built.headers = CaseInsensitiveDict(headers or {})
    return built


class FakeTransport(BaseAdapter):
    """Answers a real requests.Session from a handler, so no test touches the network."""

    def __init__(self, handler: Handler) -> None:
        super().__init__()
        self._handler = handler
        self.seen: list[SeenRequest] = []
        self.timeouts: list[Any] = []

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        seen = seen_request(request)
        self.seen.append(seen)
        self.timeouts.append(kwargs.get("timeout"))
        answer = self._handler(seen)
        answer.request = request
        answer.url = request.url or ""
        return answer

    def close(self) -> None:
        pass


def session_on(transport: FakeTransport) -> requests.Session:
    session = requests.Session()
    session.mount("https://", transport)
    session.mount("http://", transport)
    return session


def json_client(handler: Handler, max_retries: int = 2) -> RequestsJsonHttpClient:
    """A real RequestsJsonHttpClient on an in-process transport."""
    return RequestsJsonHttpClient(
        session_on(FakeTransport(handler)),
        timeout=5.0,
        max_retries=max_retries,
        backoff_seconds=1.0,
        sleep=lambda _: None,
    )
