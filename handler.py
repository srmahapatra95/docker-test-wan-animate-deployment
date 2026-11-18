"""Minimal RunPod Serverless echo handler."""

import runpod


def handler(job: dict) -> dict:
    """Return the submitted input unchanged as the job output."""
    job_input = job.get("input") if isinstance(job, dict) else None
    return {"output": job_input}


runpod.serverless.start({"handler": handler})
