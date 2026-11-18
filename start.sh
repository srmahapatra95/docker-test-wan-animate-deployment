#!/bin/bash
set -Eeuo pipefail
trap 'status=$?; echo "ERROR: start.sh failed at line ${LINENO} (exit code ${status})." >&2' ERR

# RunPod mounts the network volume at /runpod-volume; expose it at /workspace.
if [[ ! -d /runpod-volume ]]; then
    echo "ERROR: RunPod network volume is not mounted at /runpod-volume." >&2
    exit 1
fi

if [[ -e /workspace && ! -L /workspace ]]; then
    echo "ERROR: /workspace exists and is not a symlink; refusing to remove it." >&2
    exit 1
fi

ln -sfn /runpod-volume /workspace

HANDLER_PYTHON="${HANDLER_PYTHON:-/opt/handler-venv/bin/python}"
if [[ ! -x "${HANDLER_PYTHON}" ]]; then
    echo "ERROR: Handler Python is not executable: ${HANDLER_PYTHON}" >&2
    exit 1
fi

COMFY_DIR="${COMFY_DIR:-/workspace/ComfyUI}"
COMFY_PYTHON="${COMFY_PYTHON:-${COMFY_DIR}/.venv/bin/python}"
COMFYUI_HOST="${COMFYUI_HOST:-127.0.0.1}"
COMFYUI_PORT="${COMFYUI_PORT:-8188}"

if [[ ! -d "${COMFY_DIR}" ]]; then
    echo "ERROR: ComfyUI directory not found: ${COMFY_DIR}" >&2
    exit 1
fi
if [[ ! -x "${COMFY_PYTHON}" ]]; then
    echo "ERROR: ComfyUI Python not executable: ${COMFY_PYTHON}" >&2
    exit 1
fi
if [[ ! -f "${COMFY_DIR}/main.py" ]]; then
    echo "ERROR: ${COMFY_DIR}/main.py not found." >&2
    exit 1
fi

export COMFY_HOST="${COMFYUI_HOST}:${COMFYUI_PORT}"
export COMFY_INPUT_DIR="${COMFY_DIR}/input"
export COMFY_OUTPUT_DIR="${COMFY_DIR}/output"
mkdir -p "${COMFY_INPUT_DIR}" "${COMFY_OUTPUT_DIR}"

COMFY_PID_FILE="/tmp/comfyui.pid"
HANDLER_PID=""

cleanup() {
    local exit_code=$?
    trap - EXIT INT TERM

    if [[ -n "${HANDLER_PID}" ]] && kill -0 "${HANDLER_PID}" 2>/dev/null; then
        kill -TERM "${HANDLER_PID}" 2>/dev/null || true
        wait "${HANDLER_PID}" 2>/dev/null || true
    fi

    if [[ -n "${COMFYUI_PID:-}" ]] && kill -0 "${COMFYUI_PID}" 2>/dev/null; then
        kill -TERM "${COMFYUI_PID}" 2>/dev/null || true
        wait "${COMFYUI_PID}" 2>/dev/null || true
    fi

    rm -f "${COMFY_PID_FILE}"
    exit "${exit_code}"
}
trap cleanup EXIT INT TERM

cd "${COMFY_DIR}"

# Do not use "source" here: this is an executable, not a shell script.
"${COMFY_PYTHON}" main.py \
    --listen "${COMFYUI_HOST}" \
    --port "${COMFYUI_PORT}" &
COMFYUI_PID=$!
echo "${COMFYUI_PID}" > "${COMFY_PID_FILE}"

echo "Started ComfyUI (PID ${COMFYUI_PID})"

MAX_RETRIES="${COMFY_STARTUP_MAX_RETRIES:-120}"
RETRY_INTERVAL="${COMFY_STARTUP_RETRY_INTERVAL:-5}"
READY=0

for ((attempt=1; attempt<=MAX_RETRIES; attempt++)); do
    if ! kill -0 "${COMFYUI_PID}" 2>/dev/null; then
        echo "ERROR: ComfyUI exited during startup." >&2
        wait "${COMFYUI_PID}" || true
        exit 1
    fi

    if curl -fsS "http://${COMFYUI_HOST}:${COMFYUI_PORT}/system_stats" >/dev/null; then
        READY=1
        echo "ComfyUI API is ready."
        break
    fi

    echo "Waiting for ComfyUI (${attempt}/${MAX_RETRIES})..."
    sleep "${RETRY_INTERVAL}"
done

if [[ "${READY}" -ne 1 ]]; then
    echo "ERROR: ComfyUI startup timed out." >&2
    exit 1
fi

cd /app
echo "Starting RunPod handler..."

"${HANDLER_PYTHON}" /app/handler.py &
HANDLER_PID=$!

set +e
wait "${HANDLER_PID}"
HANDLER_STATUS=$?
set -e

HANDLER_PID=""
exit "${HANDLER_STATUS}"
