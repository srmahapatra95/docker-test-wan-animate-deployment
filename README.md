# RunPod ComfyUI Worker for Wan Animate Character and Background Replacement.

A RunPod Serverless worker for Wan Animate character and background replacement that accepts a ComfyUI API workflow, downloads its HTTPS image and video inputs, runs ComfyUI from a mounted network volume, uploads supported outputs to Cloudflare R2, and returns public output URLs.

## How it works

1. `start.sh` links the mounted RunPod network volume at `/workspace`, starts ComfyUI, and waits for its API.
2. `handler.py` validates the incoming workflow and remote media URLs.
3. Input media is downloaded into a job-specific ComfyUI input directory.
4. The workflow is queued and polled until it completes.
5. Supported video and audio outputs upload to Cloudflare R2. Local output files are deleted only after a successful upload.

## Prerequisites

- A RunPod Serverless endpoint with an NVIDIA GPU compatible with the ComfyUI installation, models, and custom nodes on the network volume.
- A working ComfyUI installation on the network volume, including its Python environment, required models, and custom nodes.
- A Cloudflare R2 bucket with S3 API credentials and a public domain or bucket URL.
- Direct HTTPS URLs for every `LoadImage` or `LoadVideo` input in the submitted workflow.

## Network volume

ComfyUI, its Python environment, compatible CUDA and PyTorch dependencies, models, and custom nodes are stored on the persistent network volume. The worker links that volume to `/workspace` at startup.

```text
/workspace/
└── ComfyUI/
    ├── main.py
    ├── .venv/
    ├── input/
    └── output/
```

`start.sh` creates `/workspace -> /runpod-volume` before launching ComfyUI.

## Required RunPod environment variables

Configure these values in the RunPod Serverless endpoint environment:

```dotenv
R2_URL_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_BUCKET_NAME=<bucket-name>
R2_ACCESS_KEY_ID=<r2-access-key-id>
R2_SECRET_ACCESS_KEY=<r2-secret-access-key>
R2_PUBLIC_URL_BASE=https://<public-r2-domain>
COMFY_DIR=/workspace/ComfyUI
```

`R2_PUBLIC_URL_BASE` must serve objects from the configured bucket. The worker creates unique object keys per job and returns public URLs from this base URL.

Optional configuration:

| Variable | Default | Purpose |
| --- | --- | --- |
| `COMFY_PYTHON` | `<COMFY_DIR>/.venv/bin/python` | ComfyUI Python executable. |
| `COMFYUI_HOST` | `127.0.0.1` | Local ComfyUI bind address. |
| `COMFYUI_PORT` | `8188` | Local ComfyUI API port. |
| `COMFY_STARTUP_MAX_RETRIES` | `120` | ComfyUI startup health-check attempts. |
| `COMFY_STARTUP_RETRY_INTERVAL` | `5` | Seconds between startup checks. |
| `R2_UPLOAD_URL_EXPIRES` | `3600` | Lifetime, in seconds, of each internal R2 upload URL. |
| `MAX_FILE_SIZE` | `52428800` | Maximum accepted input size in bytes. |
| `COMFY_API_AVAILABLE_TIMEOUT_SECONDS` | `60` | Maximum time to wait for ComfyUI to become available. |
| `COMFY_API_AVAILABLE_POLL_INTERVAL_SECONDS` | `0.5` | Seconds between ComfyUI availability checks. |
| `POLL_INTERVAL_SECONDS` | `2` | Seconds between workflow-status checks. |
| `MAX_POLL_DURATION_SECONDS` | `900` | Maximum workflow wait time in seconds. |
| `LOG_LEVEL` | `INFO` | Python log level. |

The application does not load `.env` files itself. Use RunPod endpoint variables in production or Docker's `--env-file` option locally.

## Request format

Submit a ComfyUI **API-format** workflow under `input.workflow`:

```json
{
  "input": {
    "workflow": {
      "10": {
        "class_type": "LoadImage",
        "inputs": {
          "image": "https://example.com/input.png"
        }
      }
    }
  }
}
```

`LoadImage` accepts `.jpg`, `.jpeg`, `.png`, and `.webp`. `LoadVideo` accepts `.mp4`. Input URLs must use HTTPS and cannot resolve to private, localhost, or reserved addresses.

The worker uploads `.mp4`, `.m4a`, `.mp3`, `.ogg`, and `.wav` outputs to R2. A successful response contains their public URLs in `output.outputs`.

Example `runsync` response:

```json
{
  "status": "COMPLETED",
  "output": {
    "status": "COMPLETED",
    "job_id": "job-id",
    "prompt_id": "comfy-prompt-id",
    "outputs": [
      {
        "node_id": "19",
        "output_key": "videos",
        "filename": "result.mp4",
        "url": "https://media.example.com/job-id/object-id/result.mp4"
      }
    ]
  }
}
```

## Output lifecycle and limitations

- Input files are stored in a unique job directory under `COMFY_INPUT_DIR` and removed after a completed workflow.
- Input files remain after a timeout or incomplete execution because ComfyUI may still be reading them.
- Uploaded `.mp4`, `.m4a`, `.mp3`, `.ogg`, and `.wav` files are deleted from `COMFY_OUTPUT_DIR` after R2 confirms the upload.
- Image outputs are returned by ComfyUI history but are not uploaded to R2 by this worker.
- The handler accepts direct HTTPS input URLs only and does not follow redirects.

## Build

Build and push a versioned image:

```bash
./build.sh -u <dockerhub-user> -i comfyui-runpod -v v1.0.0
```

Build without pushing:

```bash
./build.sh -u <dockerhub-user> -i comfyui-runpod -v v1.0.0 --build-only
```

## Tests

Install test dependencies and run unit and integration tests:

```bash
python -m pip install -r requirements-test.txt
pytest tests/unit tests/integration -v
```

The live end-to-end test is opt-in and submits a real RunPod job:

```bash
RUN_E2E=1 \
RUNPOD_API_KEY=<api-key> \
RUNPOD_ENDPOINT_ID=<endpoint-id> \
E2E_IMAGE_URL=https://<public-image-url> \
E2E_VIDEO_URL=https://<public-video-url> \
pytest tests/e2e -v
```

`tests/e2e/runpod_workflow.ipynb` provides the same live workflow flow in a notebook.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `ComfyUI ... is not reachable` | Confirm `COMFY_DIR`, `COMFY_PYTHON`, models, custom nodes, and ComfyUI startup logs. |
| R2 upload failure | Configure all five required R2 variables and verify the R2 API token has object-write permission. |
| Input URL validation failure | Use a direct HTTPS URL on port 443 that does not resolve to private, localhost, or reserved addresses. |
| Workflow completes with no outputs | Confirm the workflow has a supported video or audio output node such as `SaveVideo`. |
| E2E test is skipped | Set `RUN_E2E=1` and provide the required RunPod credentials and public input URLs. |

## Security

- Keep R2 API credentials and `RUNPOD_API_KEY` in RunPod environment settings or a local ignored `.env` file; never commit them.
- `R2_PUBLIC_URL_BASE` exposes uploaded objects through its configured public domain. Use an access-controlled domain if output access must be restricted.
- The handler blocks input URLs that resolve to private, loopback, link-local, multicast, reserved, or unspecified IP addresses. Use a hostname allowlist or egress proxy for stricter production controls.


## Contributing

Keep changes focused, add or update pytest coverage for behavior changes, and run the unit and integration suites before opening a pull request.

## License

This project is licensed under the MIT License.

Copyright (c) 2026 ComfyUI RunPod Worker contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
