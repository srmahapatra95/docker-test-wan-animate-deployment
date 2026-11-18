"""RunPod Serverless and Cloudflare R2 smoke test.

Set RUN_E2E=1 and configure the required environment variables before running
this test. It submits a real job and can incur RunPod usage charges.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIRECTORY = PROJECT_ROOT / "tests" / "workflow"
RUNPOD_API_BASE = os.getenv("RUNPOD_API_BASE", "https://api.runpod.ai/v2")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")
E2E_IMAGE_URL = os.getenv("E2E_IMAGE_URL")
E2E_VIDEO_URL = os.getenv("E2E_VIDEO_URL")
E2E_TIMEOUT_SECONDS = int(os.getenv("E2E_TIMEOUT_SECONDS", "1800"))
E2E_WORKFLOW_PATH = os.getenv("E2E_WORKFLOW_PATH")


def _skip_unless_e2e_is_configured():
    if os.getenv("RUN_E2E") != "1":
        pytest.skip("Set RUN_E2E=1 to submit a live RunPod job.")

    required = {
        "RUNPOD_API_KEY": RUNPOD_API_KEY,
        "RUNPOD_ENDPOINT_ID": RUNPOD_ENDPOINT_ID,
        "E2E_IMAGE_URL": E2E_IMAGE_URL,
        "E2E_VIDEO_URL": E2E_VIDEO_URL,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        pytest.skip("Missing E2E configuration: " + ", ".join(missing))


def _workflow_path() -> Path:
    if E2E_WORKFLOW_PATH:
        path = Path(E2E_WORKFLOW_PATH)
        return path if path.is_absolute() else PROJECT_ROOT / path

    workflow_files = sorted(WORKFLOW_DIRECTORY.glob("*.json"))
    if len(workflow_files) != 1:
        pytest.fail(
            "Set E2E_WORKFLOW_PATH to the API workflow JSON file to run."
        )
    return workflow_files[0]


def _load_workflow() -> dict:
    workflow_path = _workflow_path()
    try:
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        pytest.fail(f"Could not load E2E workflow {workflow_path}: {exc}")

    if not isinstance(workflow, dict) or not workflow:
        pytest.fail(f"E2E workflow must be a non-empty API workflow: {workflow_path}")
    return workflow


def _set_test_media_urls(workflow: dict) -> dict:
    """Return a workflow copy with external test media assigned to input nodes."""
    configured_workflow = copy.deepcopy(workflow)
    image_nodes = 0
    video_nodes = 0

    for node in configured_workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue

        if node.get("class_type") == "LoadImage":
            inputs["image"] = E2E_IMAGE_URL
            image_nodes += 1
        elif node.get("class_type") == "LoadVideo":
            inputs["file"] = E2E_VIDEO_URL
            video_nodes += 1

    if image_nodes == 0:
        pytest.fail("The E2E workflow must include at least one LoadImage node.")
    if video_nodes == 0:
        pytest.fail("The E2E workflow must include at least one LoadVideo node.")

    return configured_workflow


def test_runpod_workflow_uploads_public_r2_outputs():
    """Submit the real workflow and verify every returned public R2 URL."""
    _skip_unless_e2e_is_configured()
    workflow = _set_test_media_urls(_load_workflow())
    endpoint_url = f"{RUNPOD_API_BASE.rstrip('/')}/{RUNPOD_ENDPOINT_ID}/runsync"

    try:
        response = requests.post(
            endpoint_url,
            headers={
                "Authorization": f"Bearer {RUNPOD_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"input": {"workflow": workflow}},
            timeout=E2E_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        job = response.json()
    except (requests.RequestException, ValueError) as exc:
        pytest.fail(f"RunPod E2E request failed: {exc}")

    assert job.get("status") == "COMPLETED", job
    output = job.get("output")
    assert isinstance(output, dict), job
    assert output.get("status") == "COMPLETED", output
    outputs = output.get("outputs")
    assert isinstance(outputs, list) and outputs, output

    for generated_output in outputs:
        url = generated_output.get("url")
        assert isinstance(url, str) and url.startswith("https://"), generated_output

        public_response = None
        try:
            public_response = requests.get(url, stream=True, timeout=(10, 60))
            public_response.raise_for_status()
        except requests.RequestException as exc:
            pytest.fail(f"R2 public URL is not reachable: {exc}")
        finally:
            if public_response is not None:
                public_response.close()
