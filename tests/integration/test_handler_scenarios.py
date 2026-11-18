from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlparse

import pytest

from tests.support import handler


@pytest.fixture
def handler_workspace(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    monkeypatch.setattr(handler, "COMFY_INPUT_DIR", input_dir)
    monkeypatch.setattr(handler, "COMFY_OUTPUT_DIR", output_dir)
    return {"input_dir": input_dir, "output_dir": output_dir}


def _job(workflow=None):
    return {
        "id": "job-1",
        "input": {
            "workflow": workflow
            or {"1": {"class_type": "CLIPTextEncode", "inputs": {}}}
        },
    }


@pytest.mark.parametrize(
    ("class_type", "input_key", "media_type", "extension"),
    [
        ("LoadImage", "image", "image", ".png"),
        ("LoadVideo", "file", "video", ".mp4"),
    ],
)
def test_handler_downloads_validates_and_rewrites_media_input_before_queueing(
    handler_workspace,
    monkeypatch,
    class_type,
    input_key,
    media_type,
    extension,
):
    source_url = f"https://cdn.example.com/source{extension}"
    workflow = {"1": {"class_type": class_type, "inputs": {input_key: source_url}}}
    queued = {}
    download = Mock(
        side_effect=lambda url, destination, kind: Path(destination).write_bytes(b"media") or 5
    )
    validate_media = Mock(return_value={"valid": True})

    def queue_workflow(updated_workflow):
        queued["workflow"] = updated_workflow
        return {"prompt_id": "prompt-1"}

    monkeypatch.setattr(handler, "_check_server", Mock(return_value=True))
    monkeypatch.setattr(handler, "_validate_url", lambda url: urlparse(url))
    monkeypatch.setattr(
        handler,
        "_validate_extensions",
        lambda url, kind: extension if kind == media_type else pytest.fail("unexpected media type"),
    )
    monkeypatch.setattr(handler, "download_file", download)
    monkeypatch.setattr(handler, f"_validate_{media_type}", validate_media)
    monkeypatch.setattr(handler, "_queue_workflow", queue_workflow)
    monkeypatch.setattr(handler, "_wait_for_completion", Mock(return_value={"outputs": {}}))

    response = handler.handler(_job(workflow))

    rewritten_path = Path(queued["workflow"]["1"]["inputs"][input_key])
    assert response["status"] == "COMPLETED"
    assert rewritten_path.suffix == extension
    assert rewritten_path.parent.name.startswith("runpod_job_")
    assert list(handler_workspace["input_dir"].iterdir()) == []
    assert download.call_args.args[0] == source_url
    assert download.call_args.args[2] == media_type
    validate_media.assert_called_once()


def test_handler_fails_before_download_when_comfyui_is_unavailable(
    handler_workspace, monkeypatch
):
    download_to_comfy = Mock()
    monkeypatch.setattr(handler, "_check_server", Mock(return_value=False))
    monkeypatch.setattr(handler, "_download_to_Comfy", download_to_comfy)

    response = handler.handler(_job())

    assert response["status"] == "FAILED"
    assert response["error"]["type"] == "comfy_unavailable"
    download_to_comfy.assert_not_called()


def test_handler_returns_queue_failure_and_cleans_unqueued_inputs(
    handler_workspace, monkeypatch
):
    temp_input_dir = handler_workspace["input_dir"] / "runpod_job_test"
    temp_input_dir.mkdir()
    monkeypatch.setattr(handler, "_check_server", Mock(return_value=True))
    monkeypatch.setattr(
        handler,
        "_download_to_Comfy",
        lambda workflow: (workflow, temp_input_dir),
    )
    monkeypatch.setattr(
        handler,
        "_queue_workflow",
        Mock(side_effect=handler.ComfyQueueError("invalid workflow")),
    )

    response = handler.handler(_job())

    assert response["status"] == "FAILED"
    assert response["error"] == {
        "type": "comfy_queue_error",
        "message": "invalid workflow",
        "retryable": False,
    }
    assert not temp_input_dir.exists()


@pytest.mark.parametrize(
    ("error", "error_type"),
    [
        (handler.ComfyExecutionError("node failed"), "comfy_execution_error"),
        (handler.ComfyTimeoutError("prompt timed out"), "comfy_timeout"),
    ],
)
def test_handler_preserves_inputs_when_queued_execution_does_not_finish(
    handler_workspace, monkeypatch, error, error_type
):
    temp_input_dir = handler_workspace["input_dir"] / "runpod_job_test"
    temp_input_dir.mkdir()
    monkeypatch.setattr(handler, "_check_server", Mock(return_value=True))
    monkeypatch.setattr(
        handler,
        "_download_to_Comfy",
        lambda workflow: (workflow, temp_input_dir),
    )
    monkeypatch.setattr(handler, "_queue_workflow", Mock(return_value={"prompt_id": "prompt-1"}))
    monkeypatch.setattr(handler, "_wait_for_completion", Mock(side_effect=error))

    response = handler.handler(_job())

    assert response["status"] == "FAILED"
    assert response["error"]["type"] == error_type
    assert temp_input_dir.exists()


def test_handler_completes_when_no_supported_video_or_audio_output_exists(
    handler_workspace, monkeypatch
):
    temp_input_dir = handler_workspace["input_dir"] / "runpod_job_test"
    temp_input_dir.mkdir()
    prompt_history = {
        "outputs": {
            "1": {
                "images": [
                    {"filename": "image.png", "subfolder": "", "type": "output"}
                ]
            }
        }
    }
    monkeypatch.setattr(handler, "_check_server", Mock(return_value=True))
    monkeypatch.setattr(
        handler,
        "_download_to_Comfy",
        lambda workflow: (workflow, temp_input_dir),
    )
    monkeypatch.setattr(handler, "_queue_workflow", Mock(return_value={"prompt_id": "prompt-1"}))
    monkeypatch.setattr(handler, "_wait_for_completion", Mock(return_value=prompt_history))

    response = handler.handler(_job())

    assert response["status"] == "COMPLETED"
    assert response["outputs"] == []
    assert response["message"] == "Workflow completed, but no supported video/audio outputs were found."
    assert not temp_input_dir.exists()


def test_handler_uploads_multiple_outputs_returns_urls_and_deletes_local_files(
    handler_workspace, monkeypatch
):
    temp_input_dir = handler_workspace["input_dir"] / "runpod_job_test"
    temp_input_dir.mkdir()
    video_path = handler_workspace["output_dir"] / "result.mp4"
    audio_path = handler_workspace["output_dir"] / "sound.mp3"
    video_path.write_bytes(b"video")
    audio_path.write_bytes(b"audio")
    prompt_history = {
        "outputs": {
            "5": {
                "videos": [{"filename": "result.mp4", "type": "output"}],
                "audio": [{"filename": "sound.mp3", "type": "output"}],
            }
        }
    }

    def upload(path, object_key):
        filename = Path(path).name
        return {
            "filename": filename,
            "uploaded": True,
            "url": f"https://media.example.com/{object_key}",
        }

    upload_to_storage = Mock(side_effect=upload)
    monkeypatch.setattr(handler, "_check_server", Mock(return_value=True))
    monkeypatch.setattr(
        handler,
        "_download_to_Comfy",
        lambda workflow: (workflow, temp_input_dir),
    )
    monkeypatch.setattr(handler, "_queue_workflow", Mock(return_value={"prompt_id": "prompt-1"}))
    monkeypatch.setattr(handler, "_wait_for_completion", Mock(return_value=prompt_history))
    monkeypatch.setattr(handler, "upload_to_storage", upload_to_storage)

    response = handler.handler(_job())

    assert response["status"] == "COMPLETED"
    assert len(response["outputs"]) == 2
    assert all(output["url"].startswith("https://media.example.com/job-1/") for output in response["outputs"])
    assert upload_to_storage.call_count == 2
    assert not video_path.exists()
    assert not audio_path.exists()
    assert not temp_input_dir.exists()


def test_handler_reports_r2_upload_failure_and_keeps_failed_output(
    handler_workspace, monkeypatch
):
    temp_input_dir = handler_workspace["input_dir"] / "runpod_job_test"
    temp_input_dir.mkdir()
    output_path = handler_workspace["output_dir"] / "result.mp4"
    output_path.write_bytes(b"video")
    prompt_history = {
        "outputs": {"5": {"videos": [{"filename": "result.mp4", "type": "output"}]}}
    }
    monkeypatch.setattr(handler, "_check_server", Mock(return_value=True))
    monkeypatch.setattr(
        handler,
        "_download_to_Comfy",
        lambda workflow: (workflow, temp_input_dir),
    )
    monkeypatch.setattr(handler, "_queue_workflow", Mock(return_value={"prompt_id": "prompt-1"}))
    monkeypatch.setattr(handler, "_wait_for_completion", Mock(return_value=prompt_history))
    monkeypatch.setattr(
        handler,
        "upload_to_storage",
        Mock(side_effect=handler.StorageUploadError("R2 upload failed")),
    )

    response = handler.handler(_job())

    assert response["status"] == "FAILED"
    assert response["error"]["type"] == "storage_upload_error"
    assert output_path.exists()
    assert not temp_input_dir.exists()


def test_handler_rejects_comfy_output_path_outside_output_directory(
    handler_workspace, monkeypatch
):
    temp_input_dir = handler_workspace["input_dir"] / "runpod_job_test"
    temp_input_dir.mkdir()
    prompt_history = {
        "outputs": {
            "5": {"videos": [{"filename": "../outside.mp4", "type": "output"}]}
        }
    }
    upload_to_storage = Mock()
    monkeypatch.setattr(handler, "_check_server", Mock(return_value=True))
    monkeypatch.setattr(
        handler,
        "_download_to_Comfy",
        lambda workflow: (workflow, temp_input_dir),
    )
    monkeypatch.setattr(handler, "_queue_workflow", Mock(return_value={"prompt_id": "prompt-1"}))
    monkeypatch.setattr(handler, "_wait_for_completion", Mock(return_value=prompt_history))
    monkeypatch.setattr(handler, "upload_to_storage", upload_to_storage)

    response = handler.handler(_job())

    assert response["status"] == "FAILED"
    assert response["error"]["type"] == "content_validation_error"
    upload_to_storage.assert_not_called()
    assert not temp_input_dir.exists()
