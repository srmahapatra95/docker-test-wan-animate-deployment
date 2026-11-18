from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests

from tests.support import handler


class FakeApiResponse:
    def __init__(self, *, status_code=200, payload=None, text="", request_error=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.payload = payload
        self.text = text
        self.request_error = request_error

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def raise_for_status(self):
        if self.request_error is not None:
            raise self.request_error


@pytest.mark.parametrize(
    ("error_data", "expected"),
    [
        ("plain error", "plain error"),
        ({"error": {"message": "invalid prompt"}}, "invalid prompt"),
        ({"error": {"details": "missing input"}}, "missing input"),
        ({"error": {"type": "validation_error"}}, "validation_error"),
        ({}, "ComfyUI rejected the workflow."),
    ],
)
def test_extract_comfy_error_returns_useful_message(error_data, expected):
    assert handler._extract_comfy_error(error_data) == expected


def test_extract_comfy_error_includes_node_errors():
    message = handler._extract_comfy_error(
        {
            "error": {"message": "invalid workflow"},
            "node_errors": {"12": {"errors": ["missing image"]}},
        }
    )

    assert message.startswith("invalid workflow; Node 12: ")
    assert '"missing image"' in message


def test_queue_workflow_returns_valid_comfy_response(monkeypatch):
    post = Mock(return_value=FakeApiResponse(payload={"prompt_id": "prompt-1", "number": 3}))
    monkeypatch.setattr(handler.requests, "post", post)
    workflow = {"1": {"class_type": "CLIPTextEncode", "inputs": {}}}

    response = handler._queue_workflow(workflow)

    assert response == {"prompt_id": "prompt-1", "number": 3}
    post.assert_called_once_with(
        f"{handler.COMFY_BASE_URL}/prompt",
        json={"prompt": workflow},
        timeout=handler.REQUEST_TIMEOUT,
    )


def test_queue_workflow_reports_comfy_validation_error(monkeypatch):
    monkeypatch.setattr(
        handler.requests,
        "post",
        Mock(
            return_value=FakeApiResponse(
                status_code=400,
                payload={"error": {"message": "invalid prompt"}},
                text="ignored",
            )
        ),
    )

    with pytest.raises(handler.ComfyQueueError, match="invalid prompt"):
        handler._queue_workflow({})


def test_queue_workflow_rejects_invalid_success_response(monkeypatch):
    monkeypatch.setattr(
        handler.requests,
        "post",
        Mock(return_value=FakeApiResponse(payload={"number": 3}, text="missing prompt id")),
    )

    with pytest.raises(handler.ComfyQueueError, match="Unexpected ComfyUI queue response"):
        handler._queue_workflow({})


def test_queue_workflow_wraps_network_error(monkeypatch):
    monkeypatch.setattr(
        handler.requests,
        "post",
        Mock(side_effect=requests.ConnectionError("connection refused")),
    )

    with pytest.raises(handler.ComfyQueueError, match="Could not submit workflow"):
        handler._queue_workflow({})


def test_get_history_returns_valid_history(monkeypatch):
    get = Mock(return_value=FakeApiResponse(payload={"prompt-1": {"status": {}}}))
    monkeypatch.setattr(handler.requests, "get", get)

    history = handler.get_history("prompt-1")

    assert history == {"prompt-1": {"status": {}}}
    get.assert_called_once_with(
        f"{handler.COMFY_BASE_URL}/history/prompt-1",
        timeout=handler.REQUEST_TIMEOUT,
    )


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            FakeApiResponse(request_error=requests.Timeout("timed out")),
            "Could not retrieve ComfyUI history",
        ),
        (FakeApiResponse(payload=ValueError("invalid JSON")), "invalid history JSON"),
        (FakeApiResponse(payload=[]), "Unexpected ComfyUI history response"),
    ],
)
def test_get_history_rejects_invalid_responses(monkeypatch, response, message):
    monkeypatch.setattr(handler.requests, "get", Mock(return_value=response))

    with pytest.raises(handler.ComfyExecutionError, match=message):
        handler.get_history("prompt-1")


def test_execution_error_message_extracts_relevant_events():
    prompt_history = {
        "status": {
            "messages": [
                ["execution_error", {"node_id": "12", "message": "bad input"}],
                ["execution_interrupted", "cancelled"],
                ["execution_start", {"ignored": True}],
                "invalid event",
            ]
        }
    }

    message = handler._execution_error_message(prompt_history)

    assert message == '{"node_id": "12", "message": "bad input"}; "cancelled"'


def test_execution_error_message_has_fallback():
    assert (
        handler._execution_error_message({"status": {"messages": []}})
        == "ComfyUI reported an execution failure."
    )


def test_wait_for_completion_returns_successful_prompt_history(monkeypatch):
    prompt_history = {"status": {"status_str": "success"}}
    get_history = Mock(return_value={"prompt-1": prompt_history})
    monkeypatch.setattr(handler, "get_history", get_history)
    monkeypatch.setattr(handler.time, "monotonic", Mock(side_effect=[0, 0]))

    assert handler._wait_for_completion("prompt-1") == prompt_history
    get_history.assert_called_once_with("prompt-1")


def test_wait_for_completion_accepts_completed_status_without_status_string(monkeypatch):
    prompt_history = {"status": {"completed": True}}
    monkeypatch.setattr(handler, "get_history", Mock(return_value={"prompt-1": prompt_history}))
    monkeypatch.setattr(handler.time, "monotonic", Mock(side_effect=[0, 0]))

    assert handler._wait_for_completion("prompt-1") == prompt_history


def test_wait_for_completion_retries_until_success(monkeypatch):
    prompt_history = {"status": {"status_str": "success"}}
    get_history = Mock(side_effect=[{}, {"prompt-1": prompt_history}])
    sleep = Mock()
    monkeypatch.setattr(handler, "get_history", get_history)
    monkeypatch.setattr(handler.time, "monotonic", Mock(side_effect=[0, 0, 0, 1]))
    monkeypatch.setattr(handler.time, "sleep", sleep)

    assert handler._wait_for_completion("prompt-1") == prompt_history
    assert get_history.call_count == 2
    sleep.assert_called_once_with(handler.POLL_INTERVAL_SECONDS)


def test_wait_for_completion_raises_execution_error(monkeypatch):
    prompt_history = {
        "status": {"status_str": "error", "messages": [["execution_error", "bad node"]]}
    }
    monkeypatch.setattr(handler, "get_history", Mock(return_value={"prompt-1": prompt_history}))
    monkeypatch.setattr(handler.time, "monotonic", Mock(side_effect=[0, 0]))

    with pytest.raises(handler.ComfyExecutionError, match='"bad node"'):
        handler._wait_for_completion("prompt-1")


def test_wait_for_completion_raises_timeout(monkeypatch):
    monkeypatch.setattr(handler, "MAX_POLL_DURATION_SECONDS", 5)
    monkeypatch.setattr(handler.time, "monotonic", Mock(side_effect=[0, 5]))
    get_history = Mock()
    monkeypatch.setattr(handler, "get_history", get_history)

    with pytest.raises(handler.ComfyTimeoutError, match="exceeded the 5-second timeout"):
        handler._wait_for_completion("prompt-1")

    get_history.assert_not_called()


def test_extract_outputs_collects_records_and_removes_duplicates():
    prompt_history = {
        "outputs": {
            "12": {
                "videos": [
                    {
                        "filename": "result.mp4",
                        "subfolder": "runs/001",
                        "type": "output",
                        "format": "mp4",
                        "frame_rate": 24,
                    },
                    {
                        "filename": "result.mp4",
                        "subfolder": "runs/001",
                        "type": "output",
                    },
                ],
                "audio": {"filename": "sound.mp3", "type": "temp"},
                "ignored": [{"not_filename": "skip"}, "skip"],
            }
        }
    }

    records = handler._extract_outputs("prompt-1", prompt_history)

    assert records == [
        {
            "node_id": "12",
            "output_key": "videos",
            "filename": "result.mp4",
            "subfolder": "runs/001",
            "type": "output",
            "format": "mp4",
            "frame_rate": 24,
            "comfy_view_url": (
                f"{handler.COMFY_BASE_URL}/view?filename=result.mp4&subfolder=runs%2F001&type=output"
            ),
        },
        {
            "node_id": "12",
            "output_key": "audio",
            "filename": "sound.mp3",
            "subfolder": "",
            "type": "temp",
            "format": None,
            "frame_rate": None,
            "comfy_view_url": f"{handler.COMFY_BASE_URL}/view?filename=sound.mp3&subfolder=&type=temp",
        },
    ]


@pytest.mark.parametrize("prompt_history", [{}, {"outputs": []}, {"outputs": "invalid"}])
def test_extract_outputs_returns_empty_list_for_invalid_history(prompt_history):
    assert handler._extract_outputs("prompt-1", prompt_history) == []
