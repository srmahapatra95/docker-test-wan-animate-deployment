from __future__ import annotations

import json
import subprocess
from unittest.mock import Mock

import pytest
from PIL import Image

from tests.support import handler


@pytest.mark.parametrize(
    ("url", "media_type", "expected_extension"),
    [
        ("https://cdn.example.com/image.JPG", "image", ".jpg"),
        ("https://cdn.example.com/video.mp4?token=value", "video", ".mp4"),
        ("https://cdn.example.com/audio.mp3", "audio", ".mp3"),
    ],
)
def test_allowed_extensions_are_normalized(url, media_type, expected_extension):
    assert handler._validate_extensions(url, media_type) == expected_extension


def test_unknown_media_type_is_rejected():
    with pytest.raises(handler.ContentValidationError, match="Unsupported media type"):
        handler._validate_extensions("https://cdn.example.com/file.mp4", "document")


@pytest.mark.parametrize("url", ["https://cdn.example.com/file.gif", "https://cdn.example.com/file"])
def test_disallowed_or_missing_extension_is_rejected(url):
    with pytest.raises(handler.ContentValidationError, match="Unsupported image extension"):
        handler._validate_extensions(url, "image")


def test_valid_png_returns_image_metadata(tmp_path):
    image_path = tmp_path / "image.png"
    Image.new("RGB", (8, 6), "blue").save(image_path)

    metadata = handler._validate_image(image_path)

    assert metadata == {"format": "png", "width": 8, "height": 6}


def test_corrupt_image_is_rejected(tmp_path):
    image_path = tmp_path / "corrupt.png"
    image_path.write_bytes(b"not an image")

    with pytest.raises(handler.ContentValidationError, match="Invalid or corrupted image"):
        handler._validate_image(image_path)


def test_unsupported_decoded_image_format_is_rejected(tmp_path):
    image_path = tmp_path / "image.bmp"
    Image.new("RGB", (8, 6), "blue").save(image_path)

    with pytest.raises(handler.ContentValidationError, match="Unsupported decoded image format: BMP"):
        handler._validate_image(image_path)


def test_valid_video_returns_probe_metadata(tmp_path, monkeypatch):
    video_path = tmp_path / "video.mp4"
    probe_result = subprocess.CompletedProcess(
        args=["ffprobe"],
        returncode=0,
        stdout=json.dumps(
            {
                "streams": [
                    {
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "duration": "3.5",
                    }
                ]
            }
        ),
    )
    decode_result = subprocess.CompletedProcess(args=["ffmpeg"], returncode=0)
    run = Mock(side_effect=[probe_result, decode_result])
    monkeypatch.setattr(handler.subprocess, "run", run)

    metadata = handler._validate_video(video_path)

    assert metadata == {
        "codec": "h264",
        "width": 1920,
        "height": 1080,
        "duration": "3.5",
    }
    assert run.call_count == 2


def test_video_without_a_video_stream_is_rejected(tmp_path, monkeypatch):
    probe_result = subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0, stdout=json.dumps({"streams": []})
    )
    monkeypatch.setattr(handler.subprocess, "run", Mock(return_value=probe_result))

    with pytest.raises(handler.ContentValidationError, match="contains no video stream"):
        handler._validate_video(tmp_path / "video.mp4")


def test_video_with_invalid_dimensions_is_rejected(tmp_path, monkeypatch):
    probe_result = subprocess.CompletedProcess(
        args=["ffprobe"],
        returncode=0,
        stdout=json.dumps({"streams": [{"width": 0, "height": 1080}]}),
    )
    monkeypatch.setattr(handler.subprocess, "run", Mock(return_value=probe_result))

    with pytest.raises(handler.ContentValidationError, match="invalid dimensions"):
        handler._validate_video(tmp_path / "video.mp4")


@pytest.mark.parametrize(
    ("side_effect", "message"),
    [
        (FileNotFoundError(), "ffmpeg/ffprobe is missing"),
        (subprocess.CalledProcessError(1, ["ffprobe"]), "Invalid or corrupted video"),
        (subprocess.TimeoutExpired(["ffprobe"], 30), "Invalid or corrupted video"),
    ],
)
def test_video_tool_failures_are_reported_as_validation_errors(
    tmp_path, monkeypatch, side_effect, message
):
    monkeypatch.setattr(handler.subprocess, "run", Mock(side_effect=side_effect))

    with pytest.raises(handler.ContentValidationError, match=message):
        handler._validate_video(tmp_path / "video.mp4")


def test_video_with_invalid_probe_json_is_rejected(tmp_path, monkeypatch):
    probe_result = subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0, stdout="not json"
    )
    monkeypatch.setattr(handler.subprocess, "run", Mock(return_value=probe_result))

    with pytest.raises(handler.ContentValidationError, match="Invalid or corrupted video"):
        handler._validate_video(tmp_path / "video.mp4")
