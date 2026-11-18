"""RunPod Serverless handler for URL-driven ComfyUI workflows.

Expected RunPod input:
{
  "input": {
    "workflow": {
      "10": {
        "class_type": "LoadImage",
        "inputs": {"image": "https://host/path/image.png"}
      }
    }
  }
}

This handler uploads supported ComfyUI video/audio outputs to Cloudflare R2
and returns their download URLs.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlencode, urlparse

import requests
import runpod
from PIL import Image, UnidentifiedImageError


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COMFY_HOST = os.getenv("COMFY_HOST", "127.0.0.1:8188")
COMFY_BASE_URL = f"http://{COMFY_HOST}"
COMFY_INPUT_DIR = Path(
    os.getenv("COMFY_INPUT_DIR", "/workspace/ComfyUI/input")
)
COMFY_OUTPUT_DIR = Path(
    os.getenv("COMFY_OUTPUT_DIR", "/workspace/ComfyUI/output")
)
R2_URL_ENDPOINT = os.getenv("R2_URL_ENDPOINT")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_PUBLIC_URL_BASE = os.getenv("R2_PUBLIC_URL_BASE")
R2_UPLOAD_URL_EXPIRES = int(os.getenv("R2_UPLOAD_URL_EXPIRES", "3600"))

COMFY_API_AVAILABLE_POLL_INTERVAL_SECONDS = float(
    os.getenv("COMFY_API_AVAILABLE_POLL_INTERVAL_SECONDS", "0.5")
)
COMFY_API_AVAILABLE_TIMEOUT_SECONDS = int(
    os.getenv("COMFY_API_AVAILABLE_TIMEOUT_SECONDS", "60")
)

MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(50 * 1024 * 1024)))
DOWNLOAD_TIMEOUT = (10, 120)
REQUEST_TIMEOUT = (5, 30)
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "2"))
MAX_POLL_DURATION_SECONDS = int(os.getenv("MAX_POLL_DURATION_SECONDS", "900"))

ALLOWED_FILES = {
    "image": {
        ".jpg": {"image/jpeg"},
        ".jpeg": {"image/jpeg"},
        ".png": {"image/png"},
        ".webp": {"image/webp"},
    },
    "video": {
        ".mp4": {"video/mp4"},
    },
    "audio": {
        ".m4a": {"audio/mp4"},
        ".mp3": {"audio/mpeg"},
        ".ogg": {"audio/ogg"},
        ".wav": {"audio/wav", "audio/x-wav"},
    },
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("runpod-comfy-handler")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class HandlerError(Exception):
    error_type = "handler_error"
    retryable = False


class InputValidationError(HandlerError):
    error_type = "input_validation_error"


class URLValidationError(HandlerError):
    error_type = "url_validation_error"


class DownloadError(HandlerError):
    error_type = "download_error"


class StorageUploadError(HandlerError):
    error_type = "storage_upload_error"


class ContentValidationError(HandlerError):
    error_type = "content_validation_error"


class MaxFileSizeExceededError(ContentValidationError):
    error_type = "file_too_large"


class ComfyUnavailableError(HandlerError):
    error_type = "comfy_unavailable"
    retryable = True


class ComfyQueueError(HandlerError):
    error_type = "comfy_queue_error"


class ComfyExecutionError(HandlerError):
    error_type = "comfy_execution_error"


class ComfyTimeoutError(HandlerError):
    error_type = "comfy_timeout"
    retryable = True


# ---------------------------------------------------------------------------
# Input and URL validation
# ---------------------------------------------------------------------------

def _validate_input(job_input: Any) -> dict:
    """Return a deep-copied API-format workflow or raise InputValidationError."""
    if job_input is None:
        raise InputValidationError("The job input is missing.")

    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError as exc:
            raise InputValidationError("The job input is not valid JSON.") from exc

    if not isinstance(job_input, dict):
        raise InputValidationError("The job input must be a JSON object.")

    workflow = job_input.get("workflow")
    if not isinstance(workflow, dict) or not workflow:
        raise InputValidationError(
            "A non-empty ComfyUI API workflow is required at input.workflow."
        )

    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            raise InputValidationError(f"Workflow node {node_id} must be an object.")
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            raise InputValidationError(
                f"Workflow node {node_id} has an invalid inputs object."
            )

    return copy.deepcopy(workflow)


def _validate_url(url: Any):
    """Validate HTTPS URL and reject localhost/private/reserved destinations.

    For high-security production deployments, use a hostname allowlist and/or
    an egress proxy. A DNS check alone cannot fully prevent DNS rebinding.
    """
    if not isinstance(url, str) or not url.strip():
        raise URLValidationError("A non-empty input file URL is required.")

    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "https":
        raise URLValidationError("Only HTTPS URLs are accepted.")
    if not parsed.hostname:
        raise URLValidationError("The URL must contain a hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise URLValidationError("Credentials embedded in URLs are not allowed.")

    try:
        if parsed.port not in (None, 443):
            raise URLValidationError("Only the standard HTTPS port (443) is allowed.")
    except ValueError as exc:
        raise URLValidationError("The URL contains an invalid port.") from exc

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise URLValidationError("Localhost URLs are not allowed.")

    try:
        try:
            addresses = [ipaddress.ip_address(hostname)]
        except ValueError:
            addresses = [
                ipaddress.ip_address(result[4][0])
                for result in socket.getaddrinfo(
                    hostname, 443, type=socket.SOCK_STREAM
                )
            ]

        if not addresses:
            raise URLValidationError("The URL hostname did not resolve.")

        for address in addresses:
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
            ):
                raise URLValidationError(
                    "URLs resolving to private or reserved IP addresses are not allowed."
                )
    except socket.gaierror as exc:
        raise URLValidationError("The URL hostname could not be resolved.") from exc

    return parsed


def _validate_extensions(url: str, media_type: str) -> str:
    if media_type not in ALLOWED_FILES:
        raise ContentValidationError(f"Unsupported media type: {media_type}.")

    extension = PurePosixPath(urlparse(url).path).suffix.lower()
    if extension not in ALLOWED_FILES[media_type]:
        raise ContentValidationError(
            f"Unsupported {media_type} extension: {extension or '(missing)'}."
        )
    return extension


# ---------------------------------------------------------------------------
# Cloudflare R2 output upload
# ---------------------------------------------------------------------------

def upload_to_storage(file_path: str | Path, object_key: str) -> dict:
    """Upload one allowed video/audio file to R2 and return its download URL."""
    source = Path(file_path) if file_path is not None else None
    if source is None or not source.is_file():
        raise StorageUploadError(f"Output file is missing: {file_path!s}")

    extension = source.suffix.lower()
    media_type = next(
        (
            category
            for category in ("video", "audio")
            if extension in ALLOWED_FILES.get(category, {})
        ),
        None,
    )
    if media_type is None:
        raise ContentValidationError(
            f"Unsupported R2 upload extension: {extension or '(missing)'}. "
            "Only allowed video and audio files can be uploaded."
        )

    required_settings = {
        "R2_URL_ENDPOINT": R2_URL_ENDPOINT,
        "R2_BUCKET_NAME": R2_BUCKET_NAME,
        "R2_ACCESS_KEY_ID": R2_ACCESS_KEY_ID,
        "R2_SECRET_ACCESS_KEY": R2_SECRET_ACCESS_KEY,
        "R2_PUBLIC_URL_BASE": R2_PUBLIC_URL_BASE,
    }
    missing_settings = [name for name, value in required_settings.items() if not value]
    if missing_settings:
        raise StorageUploadError(
            "Missing R2 configuration: " + ", ".join(missing_settings)
        )

    content_type = sorted(ALLOWED_FILES[media_type][extension])[0]
    try:
        import boto3

        from botocore.exceptions import BotoCoreError, ClientError

        r2 = boto3.client(
            "s3",
            endpoint_url=R2_URL_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
        )
        upload_url = r2.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": R2_BUCKET_NAME,
                "Key": object_key,
                "ContentType": content_type,
            },
            ExpiresIn=R2_UPLOAD_URL_EXPIRES,
        )

        with source.open("rb") as file_stream, requests.Session() as session:
            with session.put(
                upload_url,
                data=file_stream,
                headers={"Content-Type": content_type},
                timeout=DOWNLOAD_TIMEOUT,
            ) as response:
                response.raise_for_status()
    except ImportError as exc:
        raise StorageUploadError("The boto3 package is required for R2 uploads.") from exc
    except (BotoCoreError, ClientError) as exc:
        raise StorageUploadError("Could not prepare the Cloudflare R2 upload.") from exc
    except requests.RequestException as exc:
        raise StorageUploadError("The Cloudflare R2 upload request failed.") from exc
    except OSError as exc:
        raise StorageUploadError(f"Could not read output file: {source}") from exc

    from urllib.parse import quote

    object_url = f"{R2_PUBLIC_URL_BASE.rstrip('/')}/{quote(object_key, safe='/')}"

    return {"filename": source.name, "uploaded": True, "url": object_url}


# ---------------------------------------------------------------------------
# Download and media validation
# ---------------------------------------------------------------------------

def download_file(url: str, temp_path: Path, media_type: str) -> int:
    """Stream an input file to disk, checking redirect, MIME type and size."""
    parsed = _validate_url(url)
    extension = _validate_extensions(parsed.geturl(), media_type)
    destination = Path(temp_path)
    downloaded = 0

    session = requests.Session()
    # Avoid silently routing user-supplied URLs through environment proxies.
    session.trust_env = False

    try:
        with session.get(
            parsed.geturl(),
            stream=True,
            allow_redirects=False,
            timeout=DOWNLOAD_TIMEOUT,
        ) as response:
            if 300 <= response.status_code < 400:
                raise DownloadError(
                    "The input URL redirected. Supply a direct file URL."
                )
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "")
            content_type = content_type.split(";", 1)[0].strip().lower()
            allowed_types = ALLOWED_FILES[media_type][extension]
            if content_type not in allowed_types:
                raise ContentValidationError(
                    f"Unexpected Content-Type '{content_type}'. "
                    f"Expected one of {sorted(allowed_types)}."
                )

            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > MAX_FILE_SIZE:
                        raise MaxFileSizeExceededError(
                            f"Input exceeds the {MAX_FILE_SIZE}-byte size limit."
                        )
                except ValueError as exc:
                    raise DownloadError("Invalid Content-Length header.") from exc

            with destination.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > MAX_FILE_SIZE:
                        raise MaxFileSizeExceededError(
                            f"Input exceeds the {MAX_FILE_SIZE}-byte size limit."
                        )
                    output.write(chunk)

        if downloaded == 0:
            raise DownloadError("The downloaded input file is empty.")
        return downloaded

    except HandlerError:
        destination.unlink(missing_ok=True)
        raise
    except requests.RequestException as exc:
        destination.unlink(missing_ok=True)
        raise DownloadError(f"HTTP download failed: {exc}") from exc
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        session.close()


def _validate_image(file_path: Path) -> dict:
    try:
        with Image.open(file_path) as image:
            image_format = (image.format or "").upper()
            if image_format not in {"JPEG", "PNG", "WEBP"}:
                raise ContentValidationError(
                    f"Unsupported decoded image format: {image_format or 'unknown'}."
                )
            width, height = image.size
            image.verify()

        return {
            "format": image_format.lower(),
            "width": width,
            "height": height,
        }
    except ContentValidationError:
        raise
    except (UnidentifiedImageError, OSError) as exc:
        raise ContentValidationError(f"Invalid or corrupted image: {exc}") from exc


def _validate_video(file_path: Path) -> dict:
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height,duration",
                "-of", "json", str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        metadata = json.loads(probe.stdout)
        streams = metadata.get("streams", [])
        if not streams:
            raise ContentValidationError("The video contains no video stream.")

        stream = streams[0]
        if not stream.get("width") or not stream.get("height"):
            raise ContentValidationError("The video has invalid dimensions.")

        # Decode at least one frame to catch basic file corruption.
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-i", str(file_path),
                "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-",
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )

        return {
            "codec": stream.get("codec_name"),
            "width": stream["width"],
            "height": stream["height"],
            "duration": stream.get("duration"),
        }
    except ContentValidationError:
        raise
    except FileNotFoundError as exc:
        raise ContentValidationError(
            "ffmpeg/ffprobe is missing from the worker image."
        ) from exc
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        raise ContentValidationError(f"Invalid or corrupted video: {exc}") from exc


def _download_to_Comfy(workflow: dict):
    """Download LoadImage/LoadVideo URL inputs into a unique input subfolder.

    Returns (updated_workflow, temp_directory_path). The caller is responsible
    for removing the directory after ComfyUI has finished using the inputs.
    """
    if not isinstance(workflow, dict):
        raise InputValidationError("Workflow must be an object.")
    if not COMFY_INPUT_DIR.is_dir():
        raise ComfyUnavailableError(
            f"ComfyUI input directory does not exist: {COMFY_INPUT_DIR}"
        )

    temp_dir = Path(
        tempfile.mkdtemp(prefix="runpod_job_", dir=str(COMFY_INPUT_DIR))
    )

    try:
        for node_id, node in workflow.items():
            class_type = node.get("class_type")
            inputs = node.get("inputs", {})

            if class_type == "LoadImage":
                media_type, input_key = "image", "image"
            elif class_type == "LoadVideo":
                media_type, input_key = "video", "file"
            else:
                continue

            url = inputs.get(input_key)
            parsed = _validate_url(url)
            extension = _validate_extensions(parsed.geturl(), media_type)
            filename = f"{uuid.uuid4().hex}{extension}"
            file_path = temp_dir / filename

            size = download_file(url, file_path, media_type)
            metadata = (
                _validate_image(file_path)
                if media_type == "image"
                else _validate_video(file_path)
            )

            # ComfyUI input widgets resolve paths relative to ComfyUI/input.
            inputs[input_key] = file_path.relative_to(COMFY_INPUT_DIR).as_posix()
            logger.info(
                "Downloaded %s input for node %s (%d bytes, %s)",
                media_type, node_id, size, metadata,
            )

        return workflow, temp_dir

    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# ComfyUI API and execution status
# ---------------------------------------------------------------------------

def _get_comfy_pid():
    try:
        return int(Path("/tmp/comfyui.pid").read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _check_comfy_process_alive():
    pid = _get_comfy_pid()
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _check_server(
    url: str,
    timeout_seconds: int = COMFY_API_AVAILABLE_TIMEOUT_SECONDS,
    poll_interval_seconds: float = COMFY_API_AVAILABLE_POLL_INTERVAL_SECONDS,
) -> bool:
    deadline = time.monotonic() + max(0, timeout_seconds)
    poll_interval_seconds = max(0.1, poll_interval_seconds)

    while time.monotonic() < deadline:
        if _check_comfy_process_alive() is False:
            logger.error("ComfyUI process is not running.")
            return False
        try:
            response = requests.get(url, timeout=5)
            if response.ok:
                return True
        except requests.RequestException:
            pass

        time.sleep(poll_interval_seconds)

    return False


def _extract_comfy_error(error_data: Any) -> str:
    if not isinstance(error_data, dict):
        return str(error_data)

    parts = []
    error = error_data.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("details") or error.get("type")
        if message:
            parts.append(str(message))
    elif error:
        parts.append(str(error))

    node_errors = error_data.get("node_errors", {})
    if isinstance(node_errors, dict):
        for node_id, node_error in node_errors.items():
            parts.append(f"Node {node_id}: {json.dumps(node_error, default=str)}")

    return "; ".join(parts) or "ComfyUI rejected the workflow."


def _queue_workflow(workflow: dict) -> dict:
    try:
        response = requests.post(
            f"{COMFY_BASE_URL}/prompt",
            json={"prompt": workflow},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ComfyQueueError(f"Could not submit workflow to ComfyUI: {exc}") from exc

    try:
        data = response.json()
    except ValueError:
        data = None

    if response.status_code == 400:
        raise ComfyQueueError(_extract_comfy_error(data or response.text))
    if not response.ok:
        raise ComfyQueueError(
            f"ComfyUI returned HTTP {response.status_code}: {response.text[:2000]}"
        )
    if not isinstance(data, dict) or not data.get("prompt_id"):
        raise ComfyQueueError(
            f"Unexpected ComfyUI queue response: {response.text[:2000]}"
        )

    return data


def get_history(prompt_id: str) -> dict:
    try:
        response = requests.get(
            f"{COMFY_BASE_URL}/history/{prompt_id}",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise ComfyExecutionError(f"Could not retrieve ComfyUI history: {exc}") from exc
    except ValueError as exc:
        raise ComfyExecutionError("ComfyUI returned invalid history JSON.") from exc

    if not isinstance(data, dict):
        raise ComfyExecutionError("Unexpected ComfyUI history response.")
    return data


def _execution_error_message(prompt_history: dict) -> str:
    status = prompt_history.get("status", {})
    messages = status.get("messages", []) if isinstance(status, dict) else []
    errors = []

    for message in messages:
        if not isinstance(message, (list, tuple)) or len(message) < 2:
            continue
        event_name, details = message[0], message[1]
        if event_name in {"execution_error", "execution_interrupted"}:
            errors.append(json.dumps(details, default=str))

    return "; ".join(errors) or "ComfyUI reported an execution failure."


def _wait_for_completion(prompt_id: str) -> dict:
    deadline = time.monotonic() + MAX_POLL_DURATION_SECONDS
    last_log = 0.0

    while time.monotonic() < deadline:
        history = get_history(prompt_id)

        # GET /history/{prompt_id} returns a dictionary keyed by prompt_id.
        # An empty dictionary can occur before ComfyUI has stored the history.
        prompt_history = history.get(prompt_id)

        if isinstance(prompt_history, dict):
            status = prompt_history.get("status", {})
            status_str = (
                status.get("status_str", "")
                if isinstance(status, dict)
                else ""
            )

            if status_str in {"error", "interrupted"}:
                raise ComfyExecutionError(_execution_error_message(prompt_history))

            if status_str == "success":
                return prompt_history

            # Some ComfyUI builds expose completed without status_str.
            if isinstance(status, dict) and status.get("completed") is True:
                return prompt_history

        now = time.monotonic()
        if now - last_log >= 15:
            logger.info("Prompt %s is still processing.", prompt_id)
            last_log = now

        time.sleep(POLL_INTERVAL_SECONDS)

    raise ComfyTimeoutError(
        f"Prompt {prompt_id} exceeded the {MAX_POLL_DURATION_SECONDS}-second timeout."
    )


# ---------------------------------------------------------------------------
# Output extraction
# ---------------------------------------------------------------------------

def _extract_outputs(prompt_id: str, prompt_history: dict) -> list[dict]:
    """Extract image/video/audio file records from ComfyUI history.

    Standard ComfyUI outputs commonly use images, gifs or video-like fields.
    This also scans other list-valued fields for objects with a filename.
    """
    outputs = prompt_history.get("outputs", {})
    if not isinstance(outputs, dict):
        return []

    records = []
    seen = set()

    for node_id, node_output in outputs.items():
        if not isinstance(node_output, dict):
            continue

        for output_key, value in node_output.items():
            items = value if isinstance(value, list) else [value]
            for item in items:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue

                filename = str(item["filename"])
                subfolder = str(item.get("subfolder", ""))
                file_type = str(item.get("type", "output"))
                identity = (str(node_id), filename, subfolder, file_type)
                if identity in seen:
                    continue
                seen.add(identity)

                query = urlencode({
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": file_type,
                })

                records.append({
                    "node_id": str(node_id),
                    "output_key": str(output_key),
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": file_type,
                    "format": item.get("format"),
                    "frame_rate": item.get("frame_rate"),
                    # This URL is internal to the worker unless exposed via a proxy.
                    "comfy_view_url": f"{COMFY_BASE_URL}/view?{query}",
                })

    return records


def _upload_outputs(job_id: str | None, output_files: list[dict]) -> list[dict]:
    """Upload supported ComfyUI output files and return serverless URL records."""
    output_root = COMFY_OUTPUT_DIR.resolve()
    supported_extensions = {
        extension
        for media_type in ("video", "audio")
        for extension in ALLOWED_FILES[media_type]
    }
    uploaded_outputs = []
    uploaded_files: dict[Path, dict] = {}

    for output in output_files:
        if output.get("type") != "output":
            continue

        filename = str(output.get("filename", ""))
        if Path(filename).suffix.lower() not in supported_extensions:
            logger.info("Skipping unsupported R2 output file: %s", filename)
            continue

        output_path = (output_root / str(output.get("subfolder", "")) / filename).resolve()
        try:
            output_path.relative_to(output_root)
        except ValueError as exc:
            raise ContentValidationError(
                "ComfyUI returned an output path outside its output directory."
            ) from exc

        stored = uploaded_files.get(output_path)
        if stored is None:
            object_key = (
                f"{job_id or 'job'}/{uuid.uuid4().hex}/{Path(filename).name}"
            )
            stored = upload_to_storage(output_path, object_key)

            try:
                output_path.unlink()
                logger.info("Deleted uploaded ComfyUI output: %s", output_path)
            except OSError as exc:
                logger.warning(
                    "Uploaded ComfyUI output to R2 but could not delete %s: %s",
                    output_path,
                    exc,
                )
            uploaded_files[output_path] = stored

        uploaded_outputs.append({
            "node_id": output.get("node_id"),
            "output_key": output.get("output_key"),
            "filename": stored["filename"],
            "url": stored["url"],
        })

    return uploaded_outputs


# ---------------------------------------------------------------------------
# RunPod handler
# ---------------------------------------------------------------------------

def handler(job: dict) -> dict:
    job_id = job.get("id") if isinstance(job, dict) else None
    temp_dir = None
    prompt_id = None
    workflow_was_queued = False
    execution_finished = False

    try:
        if not isinstance(job, dict):
            raise InputValidationError("RunPod job must be an object.")

        workflow = _validate_input(job.get("input"))

        if not _check_server(f"{COMFY_BASE_URL}/system_stats"):
            raise ComfyUnavailableError(
                f"ComfyUI at {COMFY_BASE_URL} is not reachable."
            )

        workflow, temp_dir = _download_to_Comfy(workflow)

        queue_response = _queue_workflow(workflow)
        prompt_id = queue_response["prompt_id"]
        workflow_was_queued = True
        logger.info(
            "RunPod job %s queued in ComfyUI with prompt_id %s",
            job_id, prompt_id,
        )

        prompt_history = _wait_for_completion(prompt_id)
        execution_finished = True

        output_files = _extract_outputs(prompt_id, prompt_history)
        serverless_outputs = _upload_outputs(job_id, output_files)

        return {
            "status": "COMPLETED",
            "job_id": job_id,
            "prompt_id": prompt_id,
            "outputs": serverless_outputs,
            "message": (
                "Workflow completed."
                if serverless_outputs
                else "Workflow completed, but no supported video/audio outputs were found."
            ),
        }

    except HandlerError as exc:
        logger.warning(
            "RunPod job %s failed (%s): %s",
            job_id, exc.error_type, exc,
        )
        return {
            "status": "FAILED",
            "job_id": job_id,
            "prompt_id": prompt_id,
            "error": {
                "type": exc.error_type,
                "message": str(exc),
                "retryable": exc.retryable,
            },
        }

    except Exception:
        logger.exception("Unexpected error processing RunPod job %s", job_id)
        return {
            "status": "FAILED",
            "job_id": job_id,
            "prompt_id": prompt_id,
            "error": {
                "type": "internal_error",
                "message": "An unexpected worker error occurred.",
                "retryable": False,
            },
        }

    finally:
        # Keep inputs after a timeout because ComfyUI may still be using them.
        if temp_dir is not None:
            if not workflow_was_queued or execution_finished:
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.info("Cleaned temporary input directory for job %s", job_id)
            else:
                logger.error(
                    "Preserving input directory because prompt %s may still be running: %s",
                    prompt_id, temp_dir,
                )


logger.info("Starting RunPod Serverless handler...")
runpod.serverless.start({"handler": handler})
