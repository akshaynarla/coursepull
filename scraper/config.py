"""Constants. Edit model + tunables here."""

from __future__ import annotations

HF_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
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

# Interactive rendering: how many tab/accordion controls to click on one page.
MAX_CONTROL_CLICKS = 15
CONTROL_CLICK_SETTLE_MS = 800

# ---------- download engine ----------

# Engine bookkeeping (manifest, temp files) lives in this folder inside the output dir.
STATE_DIRNAME = ".coursepull"

# Parallel downloads per provider, on top of DOWNLOAD_CONCURRENCY overall.
# "browser" covers everything rendered through Edge (web pages, published slides).
PROVIDER_CONCURRENCY = {"google": 4, "youtube": 2, "arxiv": 2, "browser": 2}
DEFAULT_PROVIDER_CONCURRENCY = 4

MAX_FILE_BYTES = 4 * 1024**3
PROGRESS_EVENT_INTERVAL_S = 0.25

GOOGLE_DOC_FORMATS = ("pdf",)
GOOGLE_SLIDES_FORMATS = ("pdf", "pptx")
GOOGLE_SHEET_FORMATS = ("xlsx",)
DRIVE_FOLDER_MAX_DEPTH = 3
PUBLISHED_SLIDES_MAX = 400

VIDEO_MAX_HEIGHT = 1080
VIDEO_SUBTITLES = True
VIDEO_SUBTITLE_LANGS = ("en", "en-US", "en-GB")

# ---------- LLM backends ----------

# Any OpenAI-compatible endpoint works. The HF key comes from the UI, HF_TOKEN, or
# the token file written by `huggingface-cli login`.
LLM_ENDPOINTS = {
    "hf": {"base_url": HF_BASE_URL, "api_key_env": "HF_TOKEN"},
    # Native Ollama API: sets the context window and keeps thinking off (much faster on a CPU).
    "ollama": {"base_url": "http://localhost:11434", "api": "ollama", "num_ctx": 32768, "think": False},
}
AGENT_ROLES = ("orchestrator", "scout", "tutor", "reviewer", "examiner")
# Each role can run on its own endpoint and model, e.g. a small fast model for Scouts.
AGENT_MODELS = {role: {"endpoint": "hf", "model": HF_MODEL} for role in AGENT_ROLES}

# ---------- run limits ----------

RUN_TOKEN_BUDGET = 3_000_000  # all agents together; a run stops gracefully when it is spent
BUDGET_SOFT_RATIO = 0.8  # past this, no new scouting waves, review rounds or recoveries
AGENT_MAX_STEPS = {"orchestrator": 12, "scout": 30, "tutor": 40, "reviewer": 6, "examiner": 20}
# Per-agent token caps: an agent that reaches its cap stops and keeps what it has so far.
AGENT_MAX_TOKENS = {"orchestrator": 120_000, "scout": 200_000, "tutor": 250_000, "reviewer": 40_000,
                    "examiner": 150_000}
LLM_CALL_RETRIES = 2

MAX_SCOUTS_PER_WAVE = 6
MAX_SCOUT_WAVES = 3
MAX_REVIEW_ROUNDS = 2
MAX_RECOVERY_ROUNDS = 2
MAX_PAGES_PER_RUN = 60  # pages all Scouts together may read
LINKS_PER_PAGE_RESULT = 60  # links shown to an agent per fetch; it can page through the rest
AGENT_TOOL_RESULT_CHARS = 12_000
# Context masking: only the newest large tool results (page listings) stay in an agent's context.
CONTEXT_KEEP_LARGE_RESULTS = 3  # a fully revealed page is about three listings
CONTEXT_LARGE_RESULT_CHARS = 2_000
