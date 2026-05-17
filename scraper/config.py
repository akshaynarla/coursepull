"""Constants. Edit model + tunables here."""

from __future__ import annotations

HF_MODEL = "Qwen/Qwen3.6-35B-A3B"
HF_BASE_URL = "https://router.huggingface.co/v1"

LLM_TEMPERATURE = 0.1
LLM_MAX_OUTPUT_TOKENS = 8192

# Per-agent step caps. One "step" = one LLM call + (optionally) tool execution.
MAX_STEPS_DISCOVERER = 15
MAX_STEPS_CLASSIFIER = 10
MAX_STEPS_VERIFIER = 30

# Tool result truncation cap. Big results blow up agent context fast.
TOOL_RESULT_CHAR_CAP = 6000

# Discoverer crawl cap (defense in depth; prompt also tells the agent to limit itself).
MAX_PAGES_TO_CRAWL = 15
MAX_LINKS_IN_FETCH_RESULT = 80

REQUEST_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 60
PLAYWRIGHT_TIMEOUT_MS = 30_000
DOWNLOAD_CHUNK_BYTES = 64 * 1024
DOWNLOAD_CONCURRENCY = 8
INSPECT_TIMEOUT = 10

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 CoursePull/0.1"
)

ESCALATE_MIN_BODY_CHARS = 200
ESCALATE_MIN_ANCHORS = 5
