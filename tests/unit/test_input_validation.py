from __future__ import annotations

import json

import pytest

from tests.support import handler


def test_missing_job_input_is_rejected():
    with pytest.raises(handler.InputValidationError, match="job input is missing"):
        handler._validate_input(None)


def test_malformed_json_input_is_rejected():
    with pytest.raises(handler.InputValidationError, match="not valid JSON"):
        handler._validate_input("{invalid json")


@pytest.mark.parametrize("job_input", [[], 42, True])
def test_non_object_job_input_is_rejected(job_input):
    with pytest.raises(handler.InputValidationError, match="must be a JSON object"):
        handler._validate_input(job_input)


@pytest.mark.parametrize("job_input", [{}, {"workflow": {}}, {"workflow": []}])
def test_missing_or_empty_workflow_is_rejected(job_input):
    with pytest.raises(handler.InputValidationError, match="non-empty ComfyUI API workflow"):
        handler._validate_input(job_input)


def test_non_object_workflow_node_is_rejected():
    with pytest.raises(handler.InputValidationError, match="Workflow node 10 must be an object"):
        handler._validate_input({"workflow": {"10": "invalid"}})


def test_non_object_node_inputs_are_rejected():
    with pytest.raises(handler.InputValidationError, match="node 10 has an invalid inputs object"):
        handler._validate_input({"workflow": {"10": {"inputs": []}}})


def test_valid_json_workflow_returns_an_independent_copy():
    job_input = {
        "workflow": {
            "10": {
                "class_type": "LoadImage",
                "inputs": {"image": "source.png"},
            }
        }
    }

    workflow = handler._validate_input(json.dumps(job_input))
    workflow["10"]["inputs"]["image"] = "changed.png"

    assert workflow["10"]["class_type"] == "LoadImage"
    assert job_input["workflow"]["10"]["inputs"]["image"] == "source.png"
