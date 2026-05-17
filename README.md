# CoursePull

A **3-agent pipeline** that downloads openly-available resources from a course homepage.

| Agent | Tools | Job |
|---|---|---|
| **Discoverer** | `fetch_page` | Crawls the course site, finds "Schedule"/"Resources"/"Week N" subpages. |
| **Classifier** | `inspect_url` | Reads the union of all discovered links, returns a download plan. May HEAD-peek ambiguous URLs. |
| **Verifier** | `inspect_url`, `revise` | Looks at failed downloads, optionally retries with a different action (e.g. failed `download_pdf` → `download_gdrive`). |

Downloads run between Classifier and Verifier; verified retries run after.

## What it handles

- PDFs hosted on the course site or institutional domain
- arxiv papers (auto-rewrites `/abs/` → `/pdf/`)
- GitHub repos and subdirectories (zip + selective re-zip)
- YouTube videos (via `yt-dlp`, no browser)
- Google Drive / Docs / Slides / Sheets (public files; folders → `skipped.md`)
- Paywalled items: logged to `skipped.md`, never attempted

## Setup

```powershell
uv sync

# Edge must be installed system-wide (it is on Windows). Playwright reuses it via
# channel="msedge" — no `playwright install` step needed.

uv run streamlit run app.py
```

Paste a HuggingFace token in the sidebar. Default model is `Qwen/Qwen3.6-35B-A3B` —
change `HF_MODEL` in `scraper/config.py`.

## Output

```
<output_dir>/
├── README.md         summary of the run (pages crawled, counts by action)
├── coursepull.log    full agent transcripts + tool-call debug logs
├── skipped.md        paywalled / unreachable items
└── <chapter>/<subfolder>/<filename>
```

## Project layout

```
CoursePull/
├── pyproject.toml
├── README.md
├── app.py                          Streamlit UI
└── scraper/
    ├── __init__.py
    ├── config.py                   model + tunables
    ├── models.py                   Link, PlanItem, DownloadResult
    ├── fetch.py                    requests + Playwright/Edge escalation
    ├── extract.py                  link + heading-based context
    ├── download.py                 PDF / arxiv / github / youtube / gdrive
    ├── pipeline.py                 Discoverer → Classifier → downloads → Verifier
    └── agents/
        ├── __init__.py
        ├── runtime.py              chat-with-tools loop (one runtime, three agents)
        ├── tools.py                fetch_page, inspect_url implementations
        ├── discoverer.py           system prompt + tool list + driver
        ├── classifier.py           system prompt + tool list + driver + plan parser
        └── verifier.py             system prompt + tool list + driver + retry parser
```

## Knobs (`scraper/config.py`)

| Setting | Default | Notes |
|---|---|---|
| `HF_MODEL` | `Qwen/Qwen3.6-35B-A3B` | Any tool-calling chat model on the HF Inference router |
| `MAX_STEPS_DISCOVERER` | 15 | Cap on `fetch_page` calls |
| `MAX_STEPS_CLASSIFIER` | 10 | Cap on the classifier's tool turns |
| `MAX_STEPS_VERIFIER` | 30 | One inspect + one revise per failure is two steps |
| `MAX_PAGES_TO_CRAWL` | 15 | Defense in depth alongside Discoverer's step cap |
| `TOOL_RESULT_CHAR_CAP` | 6000 | Truncate tool results to keep context manageable |
| `DOWNLOAD_CONCURRENCY` | 8 | Parallel download workers |

## Acceptance URLs

- `https://cvg.ethz.ch/lectures/Robot-Learning/` — static HTML with `video.ethz.ch` SSO-walled videos.
- `https://themodernsoftware.dev/` — JS-rendered; Discoverer's `fetch_page` tool auto-escalates to Playwright/Edge.
