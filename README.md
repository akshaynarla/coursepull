# CoursePull

Point CoursePull at an open online course and it collects what a student needs to study it offline:
lecture slides, notes, recordings, readings, assignments, solutions, code and data. It sorts them into
a folder per module and writes an index that explains why each item is there.

A team of LLM agents decides what the course needs. A download engine fetches exactly that. Only public
material is collected: anything that needs a login, or is paywalled with no open copy, is listed in
`skipped.md`, never worked around.

## How it works

```mermaid
flowchart LR
    U([course URL]) --> O[Orchestrator]
    O -- "Send × N" --> S[Scouts]
    S --> O
    O --> T[Tutor]
    T --> R[Reviewer]
    R -- "changes requested" --> T
    R --> E[[Download engine]]
    E --> X[Examiner]
    X -- "recover" --> O
    X --> F([index + skipped.md])
```

The workflow is a [LangGraph](https://github.com/langchain-ai/langgraph) state graph
([scraper/graph/flow.py](scraper/graph/flow.py)). The agents share a typed blackboard
([scraper/state.py](scraper/state.py)) and pass each other Pydantic objects, never free text.

| Agent | Decides | May use ([policy](scraper/agents/policy.py)) | Pattern |
|---|---|---|---|
| **Orchestrator** | How the site is organised, which term to collect, where to send Scouts, when discovery is done, what to recover | read pages, `dispatch`, `decide`, `recover` | orchestrator–workers |
| **Scouts** (parallel) | Which links on their pages are course materials, and what each one is | read pages, `record_materials`, `drop_materials` | parallel workers, isolated contexts |
| **Tutor** | What a student needs: modules, file names, required vs optional, what to leave out and why | `set_course_map`, `plan`, `exclude`, `unplan` | synthesis |
| **Reviewer** | Whether the plan is complete and free of noise; it never edits the plan | `submit_review` only | evaluator–optimizer |
| **Examiner** | Whether each downloaded file is what the plan says, and what is worth recovering | `inspect_file`, `submit_examination` | independent verification |
| **Download engine** | Nothing: it carries out the plan | the only part that writes files | executor |

### Code establishes facts, agents decide

LLMs are good at judgement and unreliable at bookkeeping, so code supplies the facts and checks the
decisions:

- [identify.py](scraper/identify.py) tells what a link is (Google Slides, Drive folder, arXiv paper,
  GitHub folder, video, plain file, …) and whether it probably needs a login.
- Scouts see links as short ids (`L12`) and record by id, so a URL is never retyped by a model.
  Recording a profile page as a "lecture video" comes back with a warning; `drop_materials` undoes it.
- [terms.py](scraper/agents/terms.py) works out which page is which offering of the course (from page
  titles and term-switcher links) and how much material each links. `dispatch` refuses a term
  choice that contradicts those facts.
- The Reviewer gets automatic checks: profile pages planned, items labelled as recordings that
  aren't videos, whole repositories as readings, files inside a planned Drive folder, planned
  items per source page, duplicate file paths.
- The Tutor's `finish` refuses a plan that leaves any found material neither planned nor excluded.
- One Scout per page: two Scouts of a wave may not start on the same page, and a later wave may
  not re-read pages that were already read.
- Scouts read only the course site. Links to GitHub, papers, blogs or videos are recorded, not
  read.

### Keeping cost down

Every LLM call re-sends the whole conversation. Without care, an agent's tenth step pays again for
every page listing it has already worked through, so cost grows quadratically. Only the three newest
large tool results stay in an agent's context (a fully revealed page is about three listings).
Older ones become a one-line stub that names the page and link range it replaced; the link ids
stay valid, and asking again shows the listing again. Long pages are paged so nothing is cut off.

### Guardrails

- **Least privilege:** an agent built with a tool outside its role refuses to start. Only the
  engine writes to disk.
- **Untrusted content:** every agent is told that page text, link labels and file contents are
  data, never instructions.
- **Safe paths:** names are sanitised for Windows, and nothing is written outside the output folder.
- **Budgets:**
  - **Run-wide token budget.** Past 80%, the run winds down: no new waves, reviews or recoveries.
    When it is used up, the agents stop thinking, but an existing plan is still downloaded.
  - **Per-agent token and step caps**, and a cap on pages read.
- **Stop:** cancelling reaches every agent and download, and the finish step still writes the index.
- **Checkpoints:** the state is saved to SQLite after every step. An interrupted run resumes where it
  stopped; finished Scouts and finished downloads are not repeated.

## What it downloads

| Source | Saved as |
|---|---|
| Google Slides / Docs / Sheets (incl. "published to the web" and embedded) | PDF + PPTX / PDF / XLSX; published decks are captured slide by slide into a PDF |
| Google Drive files and public folders | the original files; folders keep their structure |
| YouTube and any site yt-dlp supports, incl. playlists | MP4 up to 1080p, plus English subtitles |
| arXiv, OpenReview, ACL, PMLR, CVF, NeurIPS, bioRxiv | PDF |
| DOI links | an open-access copy via OpenAlex, arXiv title search or Semantic Scholar (no key, no email); arXiv preferred |
| GitHub / GitLab repositories, folders, single files, gists; Colab notebooks | ZIP of the repo or folder, or the single file. When one term is collected and a GitHub link points to the default branch, the branch or tag named after that term (`fall2025`, `fa25`, …) is used if it has the file. |
| Any direct file (PDF, PPTX, ZIP, IPYNB, MP4, …) | the file, with its real extension |
| Web pages (lecture notes, articles) | PDF, rendered in Edge |
| Dead links | The Internet Archive's copy, if it has one (the index says so) |

Course pages that hide their schedule behind tabs or accordions are rendered in Edge and every tab
is clicked. That is how the 14 lecture decks of
[themodernsoftware.dev](https://themodernsoftware.dev/) were found: they sit behind a "Syllabus" tab
that has no URL of its own.

## Setup

```bash
uv sync
```

- **Microsoft Edge** is used headless for JavaScript pages, tabs and PDFs. It ships with Windows, so
  there is no `playwright install` step.
- **ffmpeg** on `PATH` merges 1080p video and audio.
- **Deno** is recommended: recent yt-dlp versions want a JavaScript runtime for full YouTube support.
- **An LLM endpoint** (see [Models](#models)): a HuggingFace token by default, or a local Ollama.

## Usage

### The app

```bash
uv run python app.py
```

It opens in your browser, served on this machine only (`127.0.0.1`). Add `--native` to open it in its
own window instead, which needs `uv add pywebview`.

![CoursePull replaying a scripted test run](docs/ui-replay.png)

Paste the course URL and your HuggingFace token into the header and press **Start**. The token is
used for that run only and never saved; leave it empty to use `HF_TOKEN` or your
`huggingface-cli login`. While it runs, you can watch:
- **the stat tiles**: the agent at work, items downloaded, data, time and token use against the budget;
- **the agents**, in the order a run passes through them, with the one at work lit up and what each
  has done so far, plus the Reviewer's and Examiner's verdicts;
- **the course map**, one card per module, filling in as the Tutor plans, with each item going from
  planned to downloading to done or failed;
- **the live feed** of what each agent says and does, colour-coded by agent.

**Stop** ends the run cleanly; the finish step still writes the course index. When the run ends,
the finish banner opens the folder, the course index and the list of what was left out.

**Options** sets the output folder, plan-only, the token budget, and the model for all agents or
per agent. The moon/sun icon switches between dark and light; the choice is kept in
`.nicegui/`.

Every run records its events in `.coursepull/events.jsonl`, and the app can replay them without any
LLM calls:

```bash
uv run python app.py --replay downloads/cs146s/.coursepull/events.jsonl
```

### The command line

```bash
uv run python -m scraper https://themodernsoftware.dev/ downloads/cs146s --ask-token
```

| Option | |
|---|---|
| `--plan-only` | Stop after the reviewed plan; download nothing. The index lists the plan. |
| `--ask-token` | Prompt for the HuggingFace token (hidden input, used for this run only, never saved). Otherwise `HF_TOKEN` or the `huggingface-cli` login is used. |
| `--model ROLE=ENDPOINT:MODEL` | Override a role's model, e.g. `scout=ollama:qwen3.5:4b` or `all=hf:<model>`. Repeatable. |
| `--budget N` | Token budget for all agents together (default 3,000,000). |
| `--retry-failed` | Re-download only what failed in this folder's finished run, with the same plan: no agents, no tokens. The app's finish card has the same as a button. |
| `--fresh` | Ignore an interrupted run and start over. |
| `-v` | Debug logging. |

The terminal shows what each agent is thinking and doing, then the downloads as they finish.
Ctrl+C stops cleanly. Running the same command again resumes an interrupted run. A run that
finished starts afresh, and files already on disk are not downloaded again.

### Output

```
<output_dir>/
├── README.md               course index: modules → files, why each is there, the term collected
├── skipped.md              what was left out (with reasons) and what failed
├── 01 - Week 1/
│   ├── slides/             Lecture 01 - Introduction.pdf, .pptx
│   ├── readings/
│   └── videos/
├── Course/                 course-wide items: syllabus, calendar
└── .coursepull/            checkpoints.sqlite (resume), manifest.json (what is on disk),
                            events.jsonl (everything each agent said and did)
```

## Models

Every role can run on its own OpenAI-compatible endpoint and model ([scraper/config.py](scraper/config.py)):

```python
HF_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
AGENT_MODELS = {role: {"endpoint": "hf", "model": HF_MODEL} for role in AGENT_ROLES}
```

| Endpoint | |
|---|---|
| `hf` | HuggingFace Inference router (default) |
| `ollama` | Local Ollama via its native API, which sets the context window and keeps thinking off |

Local models work, but on a laptop CPU one agent step takes 3–7 minutes and a full run takes hours.
A hosted model finishes a plan in a few minutes.

## Development

```bash
uv run pytest
```

```bash
uv run pytest --run-network -m network
```

The first runs the offline suite (195 tests, about 10 s). The second runs the live tests, which hit
Google, GitHub, arXiv and YouTube (about 40 s).

The offline suite drives the whole graph end to end against a local course site, with scripted
stand-ins for the LLM (`tests/scripted_agents.py`). It covers the review loop, recovery after a failed
download, Stop, both budget limits, resuming after a crash, and plan-only runs. The UI's view model
is tested on the events of the same scripted runs.
`tests/fixtures/modernsw_hf_materials.json` holds the materials from a real run, used as a regression
test for the noise checks.

```
app.py                   the app (NiceGUI)
evals/<course>/          eval set: a frozen course site and its answer key
scraper/
├── __main__.py          command line
├── evals.py             eval runs and plan scoring
├── ui/                  view (events → what the UI shows), controller (run in background, replay), page
├── config.py            models, endpoints, budgets, limits
├── state.py             the blackboard: typed state shared by agents and engine
├── llm.py               switchable LLM clients (OpenAI-compatible, native Ollama)
├── run_context.py       per-run resources: LLM router, budget, cancel signal, page store
├── identify.py          what a URL points at, offline
├── extract.py           links, embeds and media with heading and row context
├── fetch.py, browser.py plain fetch; Edge rendering with tab clicking
├── graph/               LangGraph workflow, runner with checkpoints, course index
├── agents/              orchestrator, scout, tutor, reviewer, examiner
│                        loop (tool-calling loop), toolbox (tools, page cache, saved pages),
│                        policy (least privilege), checks, terms
└── engine/              download engine: Google, video, papers, repos, files, web pages,
                         safe paths, resume manifest
```

## Status

1. ✅ Foundations: link and embed extraction, URL identification, download engine, tests
2. ✅ LangGraph workflow: parallel Scouts, review loop, recovery, checkpoints, budgets, Stop
3. ✅ Agent tuning on real runs (below): plan-only runs now produce a complete, correct plan
4. ✅ NiceGUI app: agents at work, course map, live feed, progress, Stop, replay of recorded runs; dark and light
5. ✅ Full live runs, including downloads (runs 8 and 9 below)
6. ✅ Eval set: frozen course sites, plans scored against hand-made answer keys (below)

## Evaluation log

Plan-only runs on [themodernsoftware.dev](https://themodernsoftware.dev/), runs 1–4 with
`Qwen/Qwen3.6-35B-A3B` and runs 5–6 with `deepseek-ai/DeepSeek-V4.1-Flash-1017`. Each run's findings
drove the next round of changes.

| Run | Tokens | What happened | What changed |
|---|---|---|---|
| 1 | 357k | All 14 lecture decks found behind the "Syllabus" tab. But 7 guest speakers' homepage and LinkedIn links were planned as "lecture videos", the calendar on the homepage was missed, and whole repositories were planned as readings. | Scout warnings and `drop_materials`; Reviewer automatic checks; homepage coverage; repositories as README pages |
| 2 | 1.04M | Profiles gone, calendar found, repositories saved as pages. The site now hosts Fall 2026 (in progress) alongside Fall 2025, and the two terms were mixed. One page was split across three Scouts, and a second wave re-read covered pages. | Collect only the most complete term; one Scout per page; redundant waves refused; per-agent token caps |
| 3 | 165k | Cost fixed. The Orchestrator guessed `/fall2026`, got a 404, and labelled the in-progress term "Fall 2025". | Term facts derived by code from page titles and term-switcher links; `dispatch` refuses contradicting choices |
| 4 | 210k | Terms labelled correctly, but "most complete" was misjudged (Fall 2026: 20 material links, Fall 2025: 39). A Drive folder and the files inside it were both planned. | Offering sizes counted by code and checked at `dispatch`; Reviewer check for files inside planned folders; default model switched to DeepSeek-V4.1-Flash |
| 5 | 1.46M | Fall 2025 chosen correctly, with the counts as the reason. But five Scouts were sent to the same page (one per material type), 43 of 57 page reads were on GitHub and its API, and every Scout hit its 150k cap. The Reviewer spotted that the assignment links point to `master`, which now holds the Fall 2026 versions. | One Scout per page enforced; Scouts restricted to the course site; older tool results dropped from context; GitHub downloads use the chosen term's branch |
| 6 | 238k | **Nothing found.** Two of run 5's fixes interacted. Masking dropped the Scout's first listing; "never repeat links already seen" then made those links invisible for good. The Scout tried seven URL variants to get the page back, and hit its cap. The Orchestrator could not send a new Scout, because the page counted as covered. | Listings are shown again on request; three newest results kept, with stubs naming what they replaced; pages of a Scout that failed may be scouted again |
| 7 | 408k | **Complete and correct plan.** Fall 2025 chosen from the counts; one Scout, no off-site reads; all 14 lecture decks plus the Figma and Drive decks, 8 assignments, readings; no profiles; the X post excluded. The Scout reached its cap just as it finished. The Reviewer asked for decks of guest talks whose rows only link the speaker's profile. | Scout cap raised to 200k; Reviewer judges against what Scouts found, not what courses usually have |
| 8 | 853k | **First full download** (13.6 min, 2.0 GB, 80 files): slides as PDF + PPTX, videos with subtitles, readings as PDFs, assignment folders from the `fall2025` branch. 8 failures: 4 pages never reached "network idle"; a Windows file lock on the manifest failed a finished download; GitHub files planned as web pages skipped the term branch (Weeks 3, 4, 6); a dead blog. The Examiner hit its cap without a verdict; a Scout crawled GitHub for nothing. | Page loads no longer wait for "network idle"; manifest save retries and never fails a download; GitHub files always go to the repo downloader; Internet Archive fallback; agents told to hand in at 75% of their cap; Scouts only on course pages; `--retry-failed` (left 3 failures: one private deck, one dead site, one slow page) |
| 9 | 392k | **Second course, ETH Robot Learning** (9 min, 2.9 GB, 87 files): 66 of 67 items; the one failure is an OpenReview 403. The Examiner passed it (the hand-in fix worked), but reported the 11 slide decks as unreadable, and 10 Guest Spotlight talks were labelled "paper discussion" because the link text was cut off before its table column. | The slide decks really need a course password (pypdf could not even try without `cryptography`): the Examiner's file check now says "password-protected"; links in a table row lead with their own cell under its column header |
| 10 | 877k | **Third course, MIT 6.S191**, the first run started from the app (8 min, 1.7 GB, 31 files): all 22 items of the 2026 edition (9 lectures with slides and video, 3 labs, 1 paper), and the Examiner passed it. But after the first Scout had covered 2026, a second wave read the archived 2017–2023 editions (most of the 545k Scout tokens), which put 146 other-offering items in skipped.md and ran the Reviewer out of its token cap before it finished. The year-only archive labels ("2025") aren't detected as terms by code. | The Orchestrator's next-wave prompt now rules out pages of other offerings |

## Eval set

A run is judged by its plan, so the eval set scores plans. Each course in `evals/<name>/` has:
- `pages.json.gz`: the course site as the agents read it. Every run saves the pages it read to
  `.coursepull/pages.json.gz`; copy that file here to freeze a site. An eval run reads only these pages,
  so a site that changes (a new term, a dead link) never moves the score.
- `truth.toml`: the answer key, made by hand. It lists each item a complete plan holds, with its module
  and role; some are optional (fine to plan, not needed). The format is described at the top of
  `evals/robot-learning/truth.toml`.

```bash
uv run python -m scraper.evals list
uv run python -m scraper.evals run all --ask-token
uv run python -m scraper.evals score robot-learning downloads/robot-learning
```

`run` plans each saved site with the agents (plan-only) in `runs/eval/<name>-<time>/`. `score` scores a
run that already happened. Both write `eval.md` and `eval.json` next to the run. The scores:
- **recall**: required items planned;
- **precision**: planned items that are on the key (profiles, sponsors and the wrong term's items are not);
- **module** and **role**: of the items matched, how many are filed correctly;
- **term**: whether the right offering was collected.

Items match by resource, not by URL: an arXiv `/abs/` link equals its `/pdf/`, `youtu.be` equals
`youtube.com`, and a GitHub folder matches whatever the branch. Scored after the fact, the CS146S
runs reproduce the log above: runs 3, 4 and 6 score 0% recall (wrong term, or nothing found), run 2 has
20 extras (mixed terms), and runs 7 and 8 score 100% / 100%. The keys were drafted from runs 8 and 9 and
then checked by hand against the pages, so those two runs score 100% by construction. The set earns its
keep on the next change.

| Course | Required items | What makes it hard |
|---|---|---|
| `themodernsoftware-dev` | 72 | two terms on one site, the right one is archived; assignments on a term branch; decks behind a tab |
| `robot-learning` | 66 | one big schedule table: slides, two recordings and three papers per row; speaker homepages mixed in |

## Principles

- Public material only; no logins, nothing paywalled worked around.
- No personal data sent to any service: DOI lookups use keyless open indexes.
- Free to run apart from the LLM, and that runs locally too.
