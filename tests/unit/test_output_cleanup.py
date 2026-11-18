from __future__ import annotations

from unittest.mock import Mock

import pytest

from tests.support import handler


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    directory = tmp_path / "output"
    directory.mkdir()
    monkeypatch.setattr(handler, "COMFY_OUTPUT_DIR", directory)
    return directory


def test_uploaded_output_is_deleted_after_successful_r2_upload(output_dir, monkeypatch):
    output_path = output_dir / "video" / "clip.mp4"
    output_path.parent.mkdir()
    output_path.write_bytes(b"video")
    records = [
        {
            "node_id": "12",
            "output_key": "videos",
            "filename": "clip.mp4",
            "subfolder": "video",
            "type": "output",
        }
    ]
    upload = Mock(
        return_value={
            "filename": "clip.mp4",
            "uploaded": True,
            "url": "https://media.example.com/job/clip.mp4",
        }
    )
    monkeypatch.setattr(handler, "upload_to_storage", upload)

    result = handler._upload_outputs("job-1", records)

    assert not output_path.exists()
    upload.assert_called_once()
    assert result[0]["url"] == "https://media.example.com/job/clip.mp4"


def test_failed_upload_keeps_the_local_output_file(output_dir, monkeypatch):
    output_path = output_dir / "clip.mp4"
    output_path.write_bytes(b"video")
    records = [{"filename": "clip.mp4", "subfolder": "", "type": "output"}]
    upload = Mock(side_effect=handler.StorageUploadError("R2 is unavailable"))
    monkeypatch.setattr(handler, "upload_to_storage", upload)

    with pytest.raises(handler.StorageUploadError, match="R2 is unavailable"):
        handler._upload_outputs("job-1", records)

    assert output_path.exists()


def test_same_output_file_is_uploaded_and_deleted_once(output_dir, monkeypatch):
    output_path = output_dir / "clip.mp4"
    output_path.write_bytes(b"video")
    records = [
        {"node_id": "1", "filename": "clip.mp4", "subfolder": "", "type": "output"},
        {"node_id": "2", "filename": "clip.mp4", "subfolder": "", "type": "output"},
    ]
    upload = Mock(
        return_value={
            "filename": "clip.mp4",
            "uploaded": True,
            "url": "https://media.example.com/clip.mp4",
        }
    )
    monkeypatch.setattr(handler, "upload_to_storage", upload)

    result = handler._upload_outputs("job-1", records)

    upload.assert_called_once()
    assert not output_path.exists()
    assert len(result) == 2
    assert result[0]["url"] == result[1]["url"]


def test_path_outside_comfy_output_directory_is_rejected(output_dir):
    records = [{"filename": "../outside.mp4", "subfolder": "", "type": "output"}]

    with pytest.raises(handler.ContentValidationError):
        handler._upload_outputs("job-1", records)
