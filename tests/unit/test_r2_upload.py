from __future__ import annotations

import sys

import pytest

from tests.support import (
    FakeR2Client,
    RecordingSession,
    fake_boto_modules,
    handler,
)


@pytest.fixture
def r2_settings(monkeypatch):
    monkeypatch.setattr(
        handler, "R2_URL_ENDPOINT", "https://account-id.r2.cloudflarestorage.com"
    )
    monkeypatch.setattr(handler, "R2_BUCKET_NAME", "outputs")
    monkeypatch.setattr(handler, "R2_ACCESS_KEY_ID", "access-key")
    monkeypatch.setattr(handler, "R2_SECRET_ACCESS_KEY", "secret-key")
    monkeypatch.setattr(handler, "R2_PUBLIC_URL_BASE", "https://media.example.com")
    monkeypatch.setattr(handler, "R2_UPLOAD_URL_EXPIRES", 120)


def test_missing_file_raises_storage_error(tmp_path, r2_settings):
    missing_file = tmp_path / "missing.mp4"

    with pytest.raises(handler.StorageUploadError, match="Output file is missing"):
        handler.upload_to_storage(missing_file, "job/output.mp4")


def test_disallowed_extension_is_rejected_before_upload(tmp_path, r2_settings):
    image_file = tmp_path / "image.png"
    image_file.write_bytes(b"not a video")

    with pytest.raises(
        handler.ContentValidationError, match="Unsupported R2 upload extension"
    ):
        handler.upload_to_storage(image_file, "job/image.png")


def test_upload_streams_file_and_returns_public_url(tmp_path, r2_settings, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video-bytes")
    r2_client = FakeR2Client()
    session = RecordingSession()

    for name, module in fake_boto_modules(r2_client).items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(handler.requests, "Session", lambda: session)

    result = handler.upload_to_storage(source, "job-1/clip.mp4")

    assert result == {
        "filename": "clip.mp4",
        "uploaded": True,
        "url": "https://media.example.com/job-1/clip.mp4",
    }
    assert len(r2_client.presign_calls) == 1
    assert r2_client.presign_calls[0]["operation"] == "put_object"
    assert r2_client.presign_calls[0]["params"] == {
        "Bucket": "outputs",
        "Key": "job-1/clip.mp4",
        "ContentType": "video/mp4",
    }
    assert r2_client.presign_calls[0]["expires_in"] == 120
    assert session.put_calls[0]["body"] == b"video-bytes"
    assert session.put_calls[0]["headers"] == {"Content-Type": "video/mp4"}
    assert session.put_calls[0]["timeout"] == handler.DOWNLOAD_TIMEOUT
