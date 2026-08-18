from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import pytest


@dataclass(frozen=True)
class MultipartPart:
    name: str
    filename: str | None
    content_type: str
    body: bytes


@dataclass(frozen=True)
class ExpectedRequest:
    method: str
    path: str
    headers: dict[str, str]
    json_body: object
    multipart_body: tuple[MultipartPart, ...] | None
    raw_body: bytes | None
    response_status: int
    response_json: object
    response_body: bytes | None
    response_headers: dict[str, str]
    disconnect: bool


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    json_body: object
    multipart_body: tuple[MultipartPart, ...] | None
    raw_body: bytes


@dataclass
class _ServerState:
    expectations: list[ExpectedRequest] = field(default_factory=list)
    requests: list[RecordedRequest] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class _MockHTTPServer(ThreadingHTTPServer):
    state: _ServerState


class _RequestHandler(BaseHTTPRequestHandler):
    server: _MockHTTPServer

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length else b""
        headers = {key.lower(): value for key, value in self.headers.items()}
        content_type = headers.get("content-type", "")
        multipart_body: tuple[MultipartPart, ...] | None = None
        decoded_body: object = None
        if raw_body and content_type.casefold().startswith("multipart/form-data"):
            message = cast(
                EmailMessage,
                BytesParser(policy=policy.default).parsebytes(
                    f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw_body
                ),
            )
            parts: list[MultipartPart] = []
            for part in message.iter_parts():
                payload = part.get_payload(decode=True)
                raw_name = part.get_param("name", header="content-disposition")
                parts.append(
                    MultipartPart(
                        name=raw_name if isinstance(raw_name, str) else "",
                        filename=part.get_filename(),
                        content_type=part.get_content_type(),
                        body=payload if isinstance(payload, bytes) else b"",
                    )
                )
            multipart_body = tuple(parts)
        elif raw_body:
            try:
                decoded_body = json.loads(raw_body)
            except json.JSONDecodeError:
                decoded_body = raw_body.decode("utf-8", errors="replace")
        actual = RecordedRequest(
            self.command,
            self.path,
            headers,
            decoded_body,
            multipart_body,
            raw_body,
        )

        with self.server.state.lock:
            self.server.state.requests.append(actual)
            if self.server.state.expectations:
                expected = self.server.state.expectations.pop(0)
            else:
                expected = None
                self.server.state.violations.append(
                    f"Unexpected request: {self.command} {self.path}"
                )

        if expected is None:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        mismatches: list[str] = []
        if actual.method != expected.method:
            mismatches.append(f"method expected {expected.method!r}, received {actual.method!r}")
        if actual.path != expected.path:
            mismatches.append(f"path expected {expected.path!r}, received {actual.path!r}")
        for key, value in expected.headers.items():
            actual_value = actual.headers.get(key.lower())
            if actual_value != value:
                mismatches.append(f"header {key!r} expected {value!r}, received {actual_value!r}")
        if expected.multipart_body is not None and actual.multipart_body != expected.multipart_body:
            mismatches.append(
                "multipart body expected "
                f"{expected.multipart_body!r}, received {actual.multipart_body!r}"
            )
        elif expected.raw_body is not None and actual.raw_body != expected.raw_body:
            mismatches.append(
                f"raw body expected {expected.raw_body!r}, received {actual.raw_body!r}"
            )
        elif expected.multipart_body is None:
            if actual.multipart_body is not None:
                mismatches.append(
                    f"JSON body expected {expected.json_body!r}, received multipart data"
                )
            elif actual.json_body != expected.json_body:
                mismatches.append(
                    f"JSON body expected {expected.json_body!r}, received {actual.json_body!r}"
                )
        if mismatches:
            with self.server.state.lock:
                self.server.state.violations.extend(mismatches)

        if expected.disconnect:
            self.close_connection = True
            return

        response_body = expected.response_body
        if response_body is None:
            response_body = (
                b""
                if expected.response_json is None
                else json.dumps(expected.response_json).encode("utf-8")
            )
        self.send_response(expected.response_status)
        if expected.response_body is None and response_body:
            self.send_header("Content-Type", "application/json")
        for key, value in expected.response_headers.items():
            self.send_header(key, value)
        if "content-length" not in {key.casefold() for key in expected.response_headers}:
            self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        if response_body:
            self.wfile.write(response_body)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle

    def log_message(self, _format: str, *_args: object) -> None:
        return


class MockClickUpAPI:
    def __init__(self) -> None:
        self.state = _ServerState()
        self.server = _MockHTTPServer(("127.0.0.1", 0), _RequestHandler)
        self.server.state = self.state
        host, port = cast(tuple[str, int], self.server.server_address)
        self.base_url = f"http://{host}:{port}/api"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def expect(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: object = None,
        multipart_body: tuple[MultipartPart, ...] | None = None,
        raw_body: bytes | None = None,
        response_status: int = 200,
        response_json: object = None,
        response_body: bytes | None = None,
        response_headers: dict[str, str] | None = None,
        disconnect: bool = False,
    ) -> None:
        self.state.expectations.append(
            ExpectedRequest(
                method=method,
                path=path,
                headers=headers or {},
                json_body=json_body,
                multipart_body=multipart_body,
                raw_body=raw_body,
                response_status=response_status,
                response_json=response_json,
                response_body=response_body,
                response_headers=response_headers or {},
                disconnect=disconnect,
            )
        )

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def assert_done(self) -> None:
        remaining = [f"{item.method} {item.path}" for item in self.state.expectations]
        assert not remaining, f"Expected requests were not received: {remaining}"
        assert not self.state.violations, "\n".join(self.state.violations)


@pytest.fixture
def mock_api() -> Iterator[MockClickUpAPI]:
    api = MockClickUpAPI()
    try:
        yield api
        api.assert_done()
    finally:
        api.close()


@pytest.fixture(autouse=True)
def block_non_local_network(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    if request.node.get_closest_marker("live") and os.environ.get("CLICKUP_LIVE_TEST") == "1":
        return

    original_connect = socket.socket.connect

    def guarded_connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) and address else None
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError(f"Non-local network access is forbidden in tests: {host}")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
