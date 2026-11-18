"""Shared test helpers that keep tests independent of external services."""

from __future__ import annotations

import sys
import types
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import runpod  # noqa: F401
except ModuleNotFoundError:
    # handler.py imports runpod at module load time, but tests invoke handler()
    # directly and do not need the production serverless runtime.
    sys.modules["runpod"] = types.ModuleType("runpod")

import handler  # noqa: E402


class FakeResponse:
    """Minimal requests response object for successful PUT uploads."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def raise_for_status(self):
        return None


class RecordingSession:
    """Records a streamed PUT request without sending it over the network."""

    def __init__(self):
        self.put_calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def put(self, url, data, headers, timeout):
        self.put_calls.append(
            {
                "url": url,
                "body": data.read(),
                "headers": headers,
                "timeout": timeout,
            }
        )
        return FakeResponse()


class FakeR2Client:
    """Records presigned-URL requests made through the boto3 client."""

    def __init__(self):
        self.presign_calls = []

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.presign_calls.append(
            {
                "operation": operation,
                "params": Params,
                "expires_in": ExpiresIn,
            }
        )
        return "https://r2-upload.example.test/presigned-put"


def fake_boto_modules(client: FakeR2Client) -> dict[str, types.ModuleType]:
    """Return module replacements used by upload_to_storage()."""
    boto3_module = types.ModuleType("boto3")
    boto3_module.client = lambda *args, **kwargs: client

    botocore_module = types.ModuleType("botocore")
    exceptions_module = types.ModuleType("botocore.exceptions")

    class BotoCoreError(Exception):
        pass

    class ClientError(Exception):
        pass

    exceptions_module.BotoCoreError = BotoCoreError
    exceptions_module.ClientError = ClientError

    return {
        "boto3": boto3_module,
        "botocore": botocore_module,
        "botocore.exceptions": exceptions_module,
    }
