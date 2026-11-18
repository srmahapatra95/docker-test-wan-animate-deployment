from __future__ import annotations

from urllib.parse import urlparse

import pytest
import requests

from tests.support import handler


class FakeDownloadResponse:
    def __init__(
        self,
        *,
        status_code=200,
        headers=None,
        chunks=(),
        request_error=None,
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self.chunks = chunks
        self.request_error = request_error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def raise_for_status(self):
        if self.request_error is not None:
            raise self.request_error

    def iter_content(self, chunk_size):
        assert chunk_size == 1024 * 1024
        yield from self.chunks


class FakeDownloadSession:
    def __init__(self, response=None, request_error=None):
        self.response = response
        self.request_error = request_error
        self.trust_env = True
        self.closed = False
        self.get_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        if self.request_error is not None:
            raise self.request_error
        return self.response

    def close(self):
        self.closed = True


@pytest.fixture
def download_dependencies(monkeypatch):
    monkeypatch.setattr(
        handler, "_validate_url", lambda url: urlparse("https://cdn.example.com/video.mp4")
    )
    monkeypatch.setattr(handler, "_validate_extensions", lambda url, media_type: ".mp4")


def test_download_streams_valid_video_to_disk(tmp_path, monkeypatch, download_dependencies):
    destination = tmp_path / "video.mp4"
    session = FakeDownloadSession(
        FakeDownloadResponse(
            headers={
                "Content-Type": "video/mp4; charset=binary",
                "Content-Length": "11",
            },
            chunks=[b"video-", b"bytes"],
        )
    )
    monkeypatch.setattr(handler.requests, "Session", lambda: session)

    downloaded = handler.download_file(
        "https://cdn.example.com/video.mp4", destination, "video"
    )

    assert downloaded == 11
    assert destination.read_bytes() == b"video-bytes"
    assert session.trust_env is False
    assert session.closed is True
    assert session.get_calls == [
        {
            "url": "https://cdn.example.com/video.mp4",
            "stream": True,
            "allow_redirects": False,
            "timeout": handler.DOWNLOAD_TIMEOUT,
        }
    ]


def test_redirect_response_is_rejected_and_file_is_not_created(
    tmp_path, monkeypatch, download_dependencies
):
    destination = tmp_path / "video.mp4"
    session = FakeDownloadSession(FakeDownloadResponse(status_code=302))
    monkeypatch.setattr(handler.requests, "Session", lambda: session)

    with pytest.raises(handler.DownloadError, match="redirected"):
        handler.download_file("https://cdn.example.com/video.mp4", destination, "video")

    assert not destination.exists()
    assert session.closed is True


def test_unexpected_content_type_is_rejected_and_partial_file_is_removed(
    tmp_path, monkeypatch, download_dependencies
):
    destination = tmp_path / "video.mp4"
    session = FakeDownloadSession(
        FakeDownloadResponse(
            headers={"Content-Type": "text/html"},
            chunks=[b"not a video"],
        )
    )
    monkeypatch.setattr(handler.requests, "Session", lambda: session)

    with pytest.raises(handler.ContentValidationError, match="Unexpected Content-Type"):
        handler.download_file("https://cdn.example.com/video.mp4", destination, "video")

    assert not destination.exists()


def test_declared_oversized_file_is_rejected_before_writing(
    tmp_path, monkeypatch, download_dependencies
):
    destination = tmp_path / "video.mp4"
    monkeypatch.setattr(handler, "MAX_FILE_SIZE", 4)
    session = FakeDownloadSession(
        FakeDownloadResponse(
            headers={"Content-Type": "video/mp4", "Content-Length": "5"},
            chunks=[b"12345"],
        )
    )
    monkeypatch.setattr(handler.requests, "Session", lambda: session)

    with pytest.raises(handler.MaxFileSizeExceededError, match="size limit"):
        handler.download_file("https://cdn.example.com/video.mp4", destination, "video")

    assert not destination.exists()


def test_streamed_oversized_file_is_removed(tmp_path, monkeypatch, download_dependencies):
    destination = tmp_path / "video.mp4"
    monkeypatch.setattr(handler, "MAX_FILE_SIZE", 4)
    session = FakeDownloadSession(
        FakeDownloadResponse(
            headers={"Content-Type": "video/mp4"},
            chunks=[b"123", b"45"],
        )
    )
    monkeypatch.setattr(handler.requests, "Session", lambda: session)

    with pytest.raises(handler.MaxFileSizeExceededError, match="size limit"):
        handler.download_file("https://cdn.example.com/video.mp4", destination, "video")

    assert not destination.exists()


def test_empty_download_is_rejected_and_file_is_removed(
    tmp_path, monkeypatch, download_dependencies
):
    destination = tmp_path / "video.mp4"
    session = FakeDownloadSession(
        FakeDownloadResponse(headers={"Content-Type": "video/mp4"}, chunks=[b""])
    )
    monkeypatch.setattr(handler.requests, "Session", lambda: session)

    with pytest.raises(handler.DownloadError, match="downloaded input file is empty"):
        handler.download_file("https://cdn.example.com/video.mp4", destination, "video")

    assert not destination.exists()


def test_invalid_content_length_header_is_rejected(tmp_path, monkeypatch, download_dependencies):
    destination = tmp_path / "video.mp4"
    session = FakeDownloadSession(
        FakeDownloadResponse(
            headers={"Content-Type": "video/mp4", "Content-Length": "unknown"}
        )
    )
    monkeypatch.setattr(handler.requests, "Session", lambda: session)

    with pytest.raises(handler.DownloadError, match="Invalid Content-Length header"):
        handler.download_file("https://cdn.example.com/video.mp4", destination, "video")

    assert not destination.exists()


def test_request_failure_is_wrapped_and_session_is_closed(
    tmp_path, monkeypatch, download_dependencies
):
    destination = tmp_path / "video.mp4"
    session = FakeDownloadSession(request_error=requests.Timeout("timed out"))
    monkeypatch.setattr(handler.requests, "Session", lambda: session)

    with pytest.raises(handler.DownloadError, match="HTTP download failed"):
        handler.download_file("https://cdn.example.com/video.mp4", destination, "video")

    assert not destination.exists()
    assert session.closed is True
