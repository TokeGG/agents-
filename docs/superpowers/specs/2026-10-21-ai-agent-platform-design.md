# AI Agent Platform — Design Spec

Date: 2026-10-21
Status: Approved by user, pending implementation planning

## 1. Purpose & Success Criteria

A personally-owned platform where the user describes a task in natural
language and an agent executes it — with the agent asking for
clarification when uncertain, learning from corrections and praise over
time, and being able to build its own integrations when a needed
connector doesn't exist yet. Multiple agents run concurrently on
different tasks. The platform is hosted on a VPS the user controls, not
on a third-party vendor platform.

Success looks like:
- User can type/say a task and an agent plans and executes it, pausing
  to ask when genuinely uncertain, and refusing to silently fail when
  blocked — it reports *why* and *how* to unblock it.
- Several agents can run at once, each visible on a live dashboard.
- The agent can write and register a new tool/connector when one is
  missing, test it, and reuse it later.
- User feedback (👍/👎) measurably changes future agent behavior, first
  via retrieval/memory, later via actual model fine-tuning.
- Four specialized agent profiles exist on day one: personal assistant
  (incl. email), pentesting assistant (scoped engagements only), game
  designer, and a coding agent that builds software from scratch
  (including connectors for the forge).

## 2. Deployment Model

- **Host:** a single VPS (e.g. Hetzner/DigitalOcean, 4 vCPU / 8GB RAM /
  80GB+ NVMe, Ubuntu + Docker). Runs everything except the model.
- **Model (brain):** Phase 1 — hosted open-weight model via a
  pay-per-token API (e.g. Together/Fireworks/OpenRouter serving a
  Qwen3/Llama-class model). Phase 2 — rent a GPU by the hour to
  fine-tune (LoRA/DPO) on accumulated praised trajectories + public
  coding/agent datasets; serve the fine-tuned model on scale-to-zero
  serverless GPU or an hourly-rented box, swapped in behind a single
  model-adapter interface (OpenAI-compatible) so no app code changes.
- **Repo/CI:** GitHub repo, deploy to the VPS via Docker Compose,
  mirroring the user's existing games-on-Render workflow but targeting
  the VPS instead.
- **Cost shape:** ~$15–40/mo while building (VPS + token API), GPU costs
  only incurred per fine-tune run (~$5–30/run) until usage justifies
  always-on GPU serving.

## 3. Architecture

**Concurrency & resource limits:** the VPS (4 vCPU/8GB) caps concurrent
sandbox containers at a configurable max (default 3); additional agent
tasks queue until a slot frees. Each sandbox has hard CPU/RAM/time
limits (Section 3 table, Sandbox manager) so one runaway task can't
starve the others or the host.

| Component | Job | Talks to |
|---|---|---|
| Gateway (Caddy) | HTTPS + login (passkey/password + 2FA); only public entry point | Web, API |
| Web UI (React) | Chat to create tasks, live multi-agent dashboard, approval prompts, 👍/👎 | API (WebSocket) |
| API (FastAPI) | Auth, task/agent CRUD, streams agent events | Postgres, Runner |
| Runner | Executes N concurrent agent loops (plan→act→observe→ask/finish) | Model adapter, Tools, Memory |
| Model adapter | Single interface; points at hosted API now, own GPU later | Model provider |
| Tool layer | shell/code (sandboxed), headless browser + downloads, HTTP, files, email, ask-user, report-gap | Sandbox, Browser |
| Sandbox manager | Per-task throwaway Docker container, no host access, resource/time limits, controlled network egress | Docker |
| Connector forge | Invokes coding agent to write/test/register new integrations | Tool layer, Vault |
| Knowledge base | Ingested docs/APIs/repos, chunked + embedded (pgvector), searchable by agents | Postgres |
| Skills library | Versioned playbooks (how-tos) agents load on demand; populated from day-one seed + promoted lessons | filesystem/Postgres |
| Memory & learning | Lessons, praise ratings, task history, retrieval at prompt time | Postgres |
| Vault | Encrypted per-service credentials, filled once by user | API |
| Postgres (+pgvector) | Single store for all persistent state | everything |

**Sub-task agents** (e.g. the coding agent invoked by the connector
forge) are children of the parent task: they share the parent's
resource quota and sandbox slot, and all their events are logged under
the parent task ID for traceability.

### Data flow (one task)
1. User submits task → API creates agent record.
2. Runner retrieves relevant lessons + knowledge-base chunks + skills.
3. Loop: model emits a tool call → permission check → sandbox/tool runs
   → observation recorded → repeat.
4. On uncertainty: `ask-user(question)` pauses the agent; UI surfaces it;
   user's reply resumes the loop.
5. On missing capability: `report-gap("can't, because X", options)` —
   never a silent failure. May trigger connector forge automatically if
   an option is "build the integration."
6. On completion: reflection pass writes a lesson; user rates 👍/👎;
   rating attaches to the full trace for future retrieval and later
   fine-tuning.

## 4. Agent Loop, Permissions, Ask-User Protocol

```
load task + retrieved memory/lessons + knowledge-base/skill context
loop:
  model proposes next step (tool call or final answer)
  if step is irreversible AND agent not in full-auto mode:
      pause -> UI approval request -> wait for user response
  if model is uncertain / missing required info:
      ask-user(question) -> pause -> wait -> resume with answer
  execute tool (sandbox / browser / email / etc.)
  record observation
  if blocked by missing capability:
      report-gap("can't, because X", concrete options) -> ask-user
      or auto-attempt an approved option (e.g. invoke connector forge)
  if task complete:
      reflect -> write lesson -> await 👍/👎 -> end
```

**Default autonomy (per agent, toggleable):** auto-execute everything
except irreversible actions, which require explicit approval unless the
agent has been switched to full-auto. Irreversible actions include:
sending email/messages, deleting/overwriting files, spending money,
posting publicly, any pentest action outside declared scope, pushing to
a remote git repo, destructive shell/db commands (`rm`, `DROP`,
force-push).

**ask-user vs report-gap:** `ask-user` is for decisions/info only the
user has. `report-gap` is for a technical blocker, always paired with
concrete unblocking options (e.g. "need an API key for X — add it to
vault, or I can automate via the web UI instead").

## 5. Agent Roster (day one)

1. **Personal assistant** — general tasks, email, calendar, files; uses
   whatever connectors exist/are forged.
2. **Pentesting assistant** — operates only within an explicit **scope
   record** required before any active tool runs: target list
   (IPs/domains/CIDRs), authorization basis (own asset or signed
   engagement), start/end dates. Every tool checks target-in-scope
   before executing; out-of-scope targets trigger `report-gap`, never a
   silent skip. All actions are logged and stamped with the scope record
   for reporting deliverables. Baseline preinstalled toolkit in the
   pentest sandbox image: Nmap, Metasploit, OWASP ZAP, Gobuster/ffuf,
   sqlmap, Hydra — additional tools added per engagement via the
   connector forge as needed.
3. **Game designer** — designs and builds games (e.g. Phaser-based),
   seeded with game-dev knowledge base content.
4. **Coding agent** — builds software/code from scratch for any purpose,
   including writing and testing the integrations the connector forge
   needs.

## 6. Connector Forge

When a tool call needs an integration that doesn't exist: the coding
agent is invoked as a sub-task → writes client code → tests it in the
sandbox against the real API (using a vault credential if available, or
a stub) → registers it as a reusable tool with a schema other agents can
call. Failures route back through `report-gap` with concrete next steps
(e.g. missing credential → add to vault; no API available → fall back
to browser automation).

## 7. Knowledge Import (Day One)

| Layer | Content | Mechanism | Timing |
|---|---|---|---|
| Base model | General knowledge, code, reasoning | Choice of open model | Day 1 |
| Knowledge base (retrieval) | Programming/API docs (Python, JS, web frameworks, Gmail/Google/GitHub APIs, Playwright, Docker), game-dev docs (Phaser, engine docs, design patterns) | Ingest → chunk → embed (pgvector) | Day 1, ongoing |
| Skills library | Step-by-step playbooks (OAuth login flows, building a Phaser game, scraping/downloading) | Seed files + promoted lessons | Day 1, ongoing |
| Seed fine-tune data | Public coding + agent-trajectory datasets, permissively licensed/public-domain only | LoRA/SFT on rented GPU | Phase 2 |
| User's own data | User's repos, files, later emails (with permission) | Same ingestion pipeline | When connected |

Licensing constraint: only permissively licensed/public-domain material
is used for actual model training; other private material stays in the
retrieval store only.

## 8. Learning Loop

| Stage | Mechanism |
|---|---|
| Per-task reflection | Structured lesson written after every task (what worked/failed/do differently), tagged by agent type + task category |
| Praise signal | 👍/👎 (+ optional comment) attached to the full task trace — the reward signal |
| Immediate effect | Relevant lessons + highly-praised similar transcripts retrieved into the prompt for every new task — works from day one, no retraining needed |
| Background consolidation | Scheduled worker re-clusters lessons, dedupes contradictions, promotes consistently-praised patterns into the skills library |
| Phase 2 fine-tuning | Once enough rated trajectories exist (hundreds+): export + combine with seed datasets → LoRA/DPO on rented GPU → swap adapter behind model-adapter interface. Repeatable on a schedule as more data accumulates. |

Each fine-tuning run is tagged with the snapshot ID of the lesson/
dataset export it used. Resulting model adapters are versioned and kept
swappable, so a regression can be rolled back to the prior adapter
without losing the underlying lesson/memory data.

## 9. Security Model

- Single-user login: passkey or password + mandatory 2FA, behind HTTPS
  (Caddy) — the only public entry point.
- Agent-run code never executes on the app server; always in a
  disposable, network-restricted Docker sandbox container.
- Vault for *runtime/connector* credentials (email, APIs, etc.),
  encrypted at rest, filled once per service by the user via the UI.
  Separately, *deployment* secrets (model API key, DB password, Caddy
  config) live only in a VPS-local `.env` outside git, provisioned
  manually once during setup — never in the app-facing vault, never
  committed.
- Pentest scope guard enforced at the tool layer, not just policy — a
  tool call is allowed only if the target is in the scope record's list
  AND the current date falls within [start_date, end_date]; otherwise
  it's blocked in code and routed through `report-gap`.
- Irreversible-action approval gate as described in Section 4.
- **Token spend guardrail:** a configurable daily/monthly spend cap on
  the model API. Crossing a warning threshold alerts the user; crossing
  the hard cap auto-pauses all agents until raised or reset.
- **Global kill switch:** a single UI control that immediately halts
  every running agent and sandbox container, for use if something is
  behaving unexpectedly.
- **Postgres backups:** automated nightly `pg_dump` to off-VPS object
  storage, retained on a rolling window (e.g. 14 days), since Postgres
  is the single store for all memory, lessons, vault data and scope
  records — losing it loses all learning to date.

## 10. Testing & Rollout

| Layer | Test approach |
|---|---|
| Agent loop | Unit tests with mocked model: ask-user pause/resume, approval gating, report-gap formatting |
| Tools/sandbox | Integration tests per tool against a real disposable container |
| Connector forge | Generated connector must pass a generated test before registration |
| Pentest scope guard | Tests proving out-of-scope targets are always blocked |
| End-to-end | One scripted scenario per agent type run before each deploy (assistant email task, pentest against a local deliberately-vulnerable target, small game build, coding-agent task) |
| Rollout | Build/test locally first, then deploy to VPS via Docker Compose + CI from GitHub |

## 11. Build Order

1. Agent core (loop, model adapter, API, UI, Postgres, ask-user/report-gap, permissions)
2. Multi-agent runtime (concurrent runner, dashboard)
3. Sandboxed execution (Docker sandbox manager, headless browser)
4. Connector forge
5. Learning loop (memory, praise, retrieval; fine-tuning pipeline deferred to Phase 2 GPU usage)
6. Specialized agents: personal assistant (+email), pentesting assistant, game designer, coding agent

Each sub-project above gets implemented in this order; this spec covers
all of them at the design level, with #1 to be planned and built first.
