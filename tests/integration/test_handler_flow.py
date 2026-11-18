from __future__ import annotations

from unittest.mock import Mock

from tests.support import handler


def test_completed_workflow_uploads_output_returns_url_and_cleans_files(
    tmp_path, monkeypatch
):
    input_dir = tmp_path / "input" / "runpod_job_test"
    input_dir.mkdir(parents=True)
    output_dir = tmp_path / "output"
    output_path = output_dir / "videos" / "result.mp4"
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"generated-video")
    prompt_history = {
        "outputs": {
            "42": {
                "videos": [
                    {
                        "filename": "result.mp4",
                        "subfolder": "videos",
                        "type": "output",
                    }
                ]
            }
        }
    }
    upload = Mock(
        return_value={
            "filename": "result.mp4",
            "uploaded": True,
            "url": "https://media.example.com/job-1/result.mp4",
        }
    )
    monkeypatch.setattr(handler, "COMFY_OUTPUT_DIR", output_dir)
    monkeypatch.setattr(handler, "_check_server", Mock(return_value=True))
    monkeypatch.setattr(
        handler,
        "_download_to_Comfy",
        Mock(side_effect=lambda workflow: (workflow, input_dir)),
    )
    monkeypatch.setattr(
        handler, "_queue_workflow", Mock(return_value={"prompt_id": "prompt-1"})
    )
    monkeypatch.setattr(handler, "_wait_for_completion", Mock(return_value=prompt_history))
    monkeypatch.setattr(handler, "upload_to_storage", upload)

    response = handler.handler(
        {"id": "job-1", "input": {"workflow": {"1": {"inputs": {}}}}}
    )

    assert response["status"] == "COMPLETED"
    assert response["prompt_id"] == "prompt-1"
    assert response["outputs"] == [
        {
            "node_id": "42",
            "output_key": "videos",
            "filename": "result.mp4",
            "url": "https://media.example.com/job-1/result.mp4",
        }
    ]
    upload.assert_called_once()
    assert not output_path.exists()
    assert not input_dir.exists()
