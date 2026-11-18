# Build argument for base image selection
ARG BASE_IMAGE=nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

# Stage 1: Base image with common dependencies
FROM ${BASE_IMAGE} AS base

# Prevents prompts from packages asking for user input during installation
ENV DEBIAN_FRONTEND=noninteractive
# Prefer binary wheels over source distributions for faster pip installations
ENV PIP_PREFER_BINARY=1
# Ensures output from python is printed immediately to the terminal without buffering
ENV PYTHONUNBUFFERED=1
# Speed up some cmake builds
ENV CMAKE_BUILD_PARALLEL_LEVEL=8
ENV HANDLER_PYTHON=/opt/handler-venv/bin/python

# Install Python, git and other necessary tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    python3.12 \
    python3.12-venv \
    curl \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    openssh-server \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Application directory
WORKDIR /app

# Install handler dependencies into a container-local Python environment.
COPY requirements.txt .
RUN python3.12 -m venv /opt/handler-venv \
    && /opt/handler-venv/bin/python -m pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY handler.py .
COPY start.sh .

# Make startup script executable
RUN chmod +x /app/start.sh

# Set the default command to run when starting the container
ENTRYPOINT ["/app/start.sh"]
