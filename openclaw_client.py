"""
Calls the OpenClaw CLI to get a reply from your agent. Same brain/persona
as your Instagram bot, just a separate agent workspace.
"""
import json
import subprocess
import config


class OpenClawError(RuntimeError):
    pass


def _extract_text(payload):
    """OpenClaw --json nests the reply under `result`. Check there first,
    then fall back to known fields and a top-level `payloads`."""
    result = payload.get("result") or {}

    payloads = result.get("payloads") or payload.get("payloads") or []
    if payloads and payloads[0].get("text"):
        return payloads[0]["text"]

    for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
        if result.get(key):
            return result[key]

    return None


def get_reply(lead_number: str, incoming_message: str) -> str:
    session_key = f"agent:{config.OPENCLAW_AGENT_ID}:{lead_number}"

    cmd = [
        config.OPENCLAW_BIN,
        "agent",
        "--agent", config.OPENCLAW_AGENT_ID,
        "--session-key", session_key,
        "--message", incoming_message,
        "--json",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.OPENCLAW_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise OpenClawError(f"openclaw agent timed out after {config.OPENCLAW_TIMEOUT_SECONDS}s") from e

    if result.returncode != 0:
        raise OpenClawError(f"openclaw agent exited {result.returncode}: {result.stderr.strip()}")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise OpenClawError(f"Could not parse openclaw JSON output: {result.stdout[:500]}") from e

    text = _extract_text(payload)
    if not text:
        raise OpenClawError(f"openclaw returned no reply text: {payload}")

    return text
