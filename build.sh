#!/bin/bash
set -Eeuo pipefail

# Defaults
BUILD_ONLY=false
NO_CACHE=false
PLATFORM=""
BUILD_ARGS=()

# Error handler
trap 'status=$?; echo "Error: Script failed at line ${LINENO} (exit code: ${status})" >&2' ERR

# Display help
show_help() {
    cat <<EOF
Usage: $0 [OPTIONS]

Required:
    -u, --username USERNAME    Docker Hub username
    -i, --image IMAGE          Docker image name
    -v, --version VERSION      Image version (e.g. v1.0.0)

Optional:
    -p, --platform PLATFORM    Target platform (e.g. linux/amd64)
    -b, --build-arg ARG        Docker build argument (repeatable)
        --no-cache             Build without cache
        --build-only           Build without pushing
    -h, --help                 Display help

Examples:
    $0 -u myusername -i comfyui-runpod -v v1.0.0

    $0 -u myusername -i comfyui-runpod -v v1.0.0 --no-cache

    $0 -u myusername -i comfyui-runpod -v v1.0.0 --build-only

    $0 -u myusername -i comfyui-runpod -v v1.0.0 -b CUDA_VERSION=12.8

Each build receives one version-specific tag.
EOF
}

# Parse command-line arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -u|--username)
            if [[ -z "${2:-}" || "$2" == -* ]]; then
                echo "Error: $1 requires a value." >&2
                exit 1
            fi
            DOCKERHUB_USERNAME="$2"
            shift 2
            ;;

        -i|--image)
            if [[ -z "${2:-}" || "$2" == -* ]]; then
                echo "Error: $1 requires a value." >&2
                exit 1
            fi
            IMAGE_NAME="$2"
            shift 2
            ;;

        -v|--version)
            if [[ -z "${2:-}" || "$2" == -* ]]; then
                echo "Error: $1 requires a value." >&2
                exit 1
            fi
            IMAGE_VERSION="$2"
            shift 2
            ;;

        -p|--platform)
            if [[ -z "${2:-}" || "$2" == -* ]]; then
                echo "Error: $1 requires a value." >&2
                exit 1
            fi
            PLATFORM="$2"
            shift 2
            ;;

        -b|--build-arg)
            if [[ -z "${2:-}" ]]; then
                echo "Error: $1 requires a value." >&2
                exit 1
            fi
            BUILD_ARGS+=(--build-arg "$2")
            shift 2
            ;;

        --no-cache)
            NO_CACHE=true
            shift
            ;;

        --build-only)
            BUILD_ONLY=true
            shift
            ;;

        -h|--help)
            show_help
            exit 0
            ;;

        *)
            echo "Unknown option or unexpected argument: $1" >&2
            show_help >&2
            exit 1
            ;;
    esac
done

# Validate required arguments
if [[ -z "${DOCKERHUB_USERNAME:-}" ]]; then
    echo "Error: Docker Hub username is required." >&2
    exit 1
fi

if [[ -z "${IMAGE_NAME:-}" ]]; then
    echo "Error: Image name is required." >&2
    exit 1
fi

if [[ -z "${IMAGE_VERSION:-}" ]]; then
    echo "Error: Image version is required." >&2
    exit 1
fi

# Validate Docker image version tag
if [[ ! "$IMAGE_VERSION" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]]; then
    echo "Error: Invalid Docker image version: $IMAGE_VERSION" >&2
    exit 1
fi

# Construct versioned image reference
IMAGE="docker.io/${DOCKERHUB_USERNAME}/${IMAGE_NAME}:${IMAGE_VERSION}"

# Resolve script directory
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Check Docker installation and daemon
check_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "Error: Docker is not installed or not in PATH." >&2
        exit 1
    fi

    if ! docker info >/dev/null 2>&1; then
        echo "Error: Docker daemon is not running or inaccessible." >&2
        exit 1
    fi
}

check_docker

# Prepare Docker build command
BUILD_CMD=(
    docker build
    --file "${SCRIPT_DIR}/Dockerfile"
    --tag "${IMAGE}"
)

if [[ "$NO_CACHE" == true ]]; then
    BUILD_CMD+=(--no-cache)
fi

if [[ -n "$PLATFORM" ]]; then
    BUILD_CMD+=(--platform "$PLATFORM")
fi

BUILD_CMD+=("${BUILD_ARGS[@]}")
BUILD_CMD+=("${SCRIPT_DIR}")

# Build image
echo "========================================"
echo "Building Docker image:"
echo "  ${IMAGE}"
echo "========================================"

"${BUILD_CMD[@]}"

echo "Docker image built successfully."

# Push image unless build-only is enabled
if [[ "$BUILD_ONLY" == true ]]; then
    echo "Build-only mode enabled. Skipping push."
    exit 0
fi

echo "========================================"
echo "Pushing Docker image: ${IMAGE}"
echo "========================================"

docker push "${IMAGE}"

echo "========================================"
echo "Successfully built and pushed:"
echo "  ${IMAGE}"
echo "========================================"