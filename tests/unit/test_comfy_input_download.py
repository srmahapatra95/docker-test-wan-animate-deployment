from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlparse

import pytest

from tests.support import handler


@pytest.fixture
def comfy_input_dir(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    monkeypatch.setattr(handler, "COMFY_INPUT_DIR", input_dir)
    return input_dir


def test_non_object_workflow_is_rejected():
    with pytest.raises(handler.InputValidationError, match="Workflow must be an object"):
        handler._download_to_Comfy([])


def test_missing_comfy_input_directory_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(handler, "COMFY_INPUT_DIR", tmp_path / "missing-input")

    with pytest.raises(handler.ComfyUnavailableError, match="input directory does not exist"):
        handler._download_to_Comfy({})


def test_image_and_video_inputs_are_downloaded_validated_and_rewritten(
    comfy_input_dir, monkeypatch
):
    workflow = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": "https://cdn.example.com/source.png"},
        },
        "2": {
            "class_type": "LoadVideo",
            "inputs": {"file": "https://cdn.example.com/source.mp4"},
        },
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "unchanged"}},
    }
    validate_url = Mock(side_effect=lambda url: urlparse(url))
    validate_extensions = Mock(
        side_effect=lambda _url, media_type: {"image": ".png", "video": ".mp4"}[media_type]
    )

    def write_download(url, destination, media_type):
        Path(destination).write_bytes(media_type.encode())
        return len(media_type)

    download = Mock(side_effect=write_download)
    validate_image = Mock(return_value={"format": "png", "width": 8, "height": 6})
    validate_video = Mock(
        return_value={"codec": "h264", "width": 1920, "height": 1080, "duration": "1"}
    )
    monkeypatch.setattr(handler, "_validate_url", validate_url)
    monkeypatch.setattr(handler, "_validate_extensions", validate_extensions)
    monkeypatch.setattr(handler, "download_file", download)
    monkeypatch.setattr(handler, "_validate_image", validate_image)
    monkeypatch.setattr(handler, "_validate_video", validate_video)

    updated_workflow, temp_dir = handler._download_to_Comfy(workflow)

    image_path = Path(updated_workflow["1"]["inputs"]["image"])
    video_path = Path(updated_workflow["2"]["inputs"]["file"])
    assert updated_workflow is workflow
    assert temp_dir.parent == comfy_input_dir
    assert image_path.parent == Path(temp_dir.name)
    assert video_path.parent == Path(temp_dir.name)
    assert image_path.suffix == ".png"
    assert video_path.suffix == ".mp4"
    assert (comfy_input_dir / image_path).read_bytes() == b"image"
    assert (comfy_input_dir / video_path).read_bytes() == b"video"
    assert workflow["3"]["inputs"]["text"] == "unchanged"
    assert download.call_args_list[0].args[2] == "image"
    assert download.call_args_list[1].args[2] == "video"
    validate_image.assert_called_once()
    validate_video.assert_called_once()


def test_download_failure_removes_temporary_input_directory(comfy_input_dir, monkeypatch):
    workflow = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": "https://cdn.example.com/source.png"},
        }
    }
    monkeypatch.setattr(handler, "_validate_url", lambda url: urlparse(url))
    monkeypatch.setattr(handler, "_validate_extensions", lambda url, media_type: ".png")

    def failed_download(url, destination, media_type):
        Path(destination).write_bytes(b"partial file")
        raise handler.DownloadError("download failed")

    monkeypatch.setattr(handler, "download_file", failed_download)

    with pytest.raises(handler.DownloadError, match="download failed"):
        handler._download_to_Comfy(workflow)

    assert list(comfy_input_dir.iterdir()) == []


def test_media_validation_failure_removes_temporary_input_directory(
    comfy_input_dir, monkeypatch
):
    workflow = {
        "1": {
            "class_type": "LoadVideo",
            "inputs": {"file": "https://cdn.example.com/source.mp4"},
        }
    }
    monkeypatch.setattr(handler, "_validate_url", lambda url: urlparse(url))
    monkeypatch.setattr(handler, "_validate_extensions", lambda url, media_type: ".mp4")
    monkeypatch.setattr(
        handler,
        "download_file",
        lambda url, destination, media_type: Path(destination).write_bytes(b"video") or 5,
    )
    monkeypatch.setattr(
        handler,
        "_validate_video",
        Mock(side_effect=handler.ContentValidationError("invalid video")),
    )

    with pytest.raises(handler.ContentValidationError, match="invalid video"):
        handler._download_to_Comfy(workflow)

    assert list(comfy_input_dir.iterdir()) == []
