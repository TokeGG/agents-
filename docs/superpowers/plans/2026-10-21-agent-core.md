# Agent Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single agent can take one natural-language task, run a plan→act→observe loop against a real model, pause to ask the user or request approval before irreversible actions, report capability gaps explicitly instead of failing silently, finish with a reflection/lesson, and be rated 👍/👎 by the user — all visible live in a minimal web UI.

**Architecture:** FastAPI backend owns a Postgres-backed task/event log and an async agent loop; a model-adapter module gives one interface over an OpenAI-compatible chat/tool-calling API; a small tool registry exposes `ask_user` and `report_gap` (plus one real tool, `http_get`, to prove the loop end-to-end — shell/sandbox/browser tools are out of scope, built in sub-project 3). A WebSocket streams events to a minimal React page where the user answers questions, approves/denies irreversible actions, and rates completed tasks. No multi-agent concurrency, sandboxing, connector forge, or fine-tuning yet — those are later sub-projects per the spec's build order.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy + Postgres, httpx (for the model API and the `http_get` tool), pytest, React + TypeScript (Vite), Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-21-ai-agent-platform-design.md`

## Global Constraints

- Model access goes through one adapter interface (OpenAI-compatible chat completions with tool calling) — spec §2, §3. No direct provider-specific calls elsewhere in the code.
- Default autonomy per task: auto-execute everything except irreversible actions, which require explicit UI approval unless the task is flagged full-auto — spec §4.
- Irreversible actions (this plan's scope): none of the built-in tools in this plan (`ask_user`, `report_gap`, `http_get`) are irreversible, but the permission-gating mechanism itself must exist and be exercised by a test tool, since later sub-projects (email, shell, forge) plug into it — spec §4, §9.
- `ask_user` is for decisions only the user has; `report_gap` is for technical blockers and must always carry concrete unblock options, never a silent failure — spec §4.
- Deploy secrets (model API key, DB password) live only in a VPS-local `.env`, never committed, never in application tables — spec §9.
- Every event (model step, tool call, pause, resume, approval, rating) is persisted so the task's full trace exists for later retrieval/fine-tuning — spec §3 data flow, §8.
- Single-user: no multi-tenant auth model needed yet, but the API must sit behind a login check — a stub bearer-token check is enough for this plan; full passkey/2FA is deploy-time config, not in scope here.

## Review Focus

- **Model response omits a tool call and just returns prose** when a tool call was expected — the loop must still record it as a step and either treat it as the final answer or re-prompt, not crash.
- **User answers an `ask_user` question with empty/whitespace input** — the loop must treat this as "no new info" and re-ask or let the agent proceed with explicit uncertainty noted, not hang or loop forever on blank input.
- **Approval request times out / user never responds** — a task awaiting approval must be resumable later (state persisted), and must not be silently dropped if the server restarts.
- **Model requests a tool that doesn't exist in the registry** — must surface as `report_gap` with options, not a stack trace.
- **Two tool-call arguments fail JSON parsing** — the loop must capture the malformed call as a failed observation (visible in the trace) and continue, not crash the task.

---

## File Structure

```
backend/
  app/
    main.py                 # FastAPI app, lifespan/startup, router mounting
    db.py                   # engine/session setup
    models.py                # SQLAlchemy models: Task, Event, Rating
    schemas.py                # Pydantic request/response models
    agent/
      model_adapter.py        # ModelAdapter protocol + OpenAICompatAdapter
      tools.py                 # Tool protocol, registry, ask_user, report_gap, http_get
      permissions.py           # is_irreversible(), approval gating helper
      loop.py                   # run_task_loop(): the plan/act/observe/ask loop
    api/
      tasks.py                  # POST /tasks, GET /tasks/{id}, POST /tasks/{id}/rate
      events.py                  # WS /tasks/{id}/stream, POST /tasks/{id}/answer, POST /tasks/{id}/approve
  tests/
    test_model_adapter.py
    test_tools.py
    test_permissions.py
    test_loop.py
    test_api_tasks.py
    test_api_events.py
frontend/
  src/
    App.tsx                     # task creation form + task list
    TaskView.tsx                 # event stream, ask_user answer box, approve/deny, rate buttons
    api.ts                        # fetch/WS client helpers
docker-compose.yml               # postgres + backend + frontend services
.env.example
```

## Interfaces reference (cross-task contract)

- `ModelAdapter.complete(messages: list[dict], tools: list[ToolSpec]) -> ModelStep` where `ModelStep = {"type": "tool_call", "tool_name": str, "arguments": dict} | {"type": "final_answer", "content": str}`.
- `Tool.name: str`, `Tool.description: str`, `Tool.parameters_schema: dict` (JSON schema), `Tool.run(arguments: dict, ctx: ToolContext) -> ToolResult` where `ToolResult = {"ok": bool, "output": str, "data": dict | None}`.
- `is_irreversible(tool_name: str) -> bool`.
- `run_task_loop(task_id: int, session: Session) -> None` — runs until the task reaches a terminal or paused state, persisting `Event` rows as it goes.

---

### Task 1: Project scaffold, DB models, Postgres via Docker Compose

**Files:**
- Create: `backend/app/db.py`, `backend/app/models.py`, `backend/pyproject.toml` (or `requirements.txt`), `docker-compose.yml`, `.env.example`
- Test: `backend/tests/test_models.py`

**Interfaces:**
- Produces: `Task` (id, description: str, status: str — one of `running|awaiting_answer|awaiting_approval|completed|failed`, full_auto: bool, created_at), `Event` (id, task_id FK, type: str — one of `model_step|tool_call|tool_result|ask_user|user_answer|approval_request|approval_response|report_gap|reflection`, payload: JSON, created_at), `Rating` (id, task_id FK, value: int — `1` or `-1`, comment: str | None, created_at). `get_session()` dependency-injectable generator.

- [ ] **Step 1: Write failing test creating a Task, adding an Event and a Rating, and reading them back**

```python
def test_task_event_rating_roundtrip(db_session):
    task = Task(description="say hello", status="running", full_auto=False)
    db_session.add(task)
    db_session.commit()
    event = Event(task_id=task.id, type="model_step", payload={"note": "started"})
    rating = Rating(task_id=task.id, value=1)
    db_session.add_all([event, rating])
    db_session.commit()
    assert db_session.get(Task, task.id).status == "running"
    assert db_session.query(Event).filter_by(task_id=task.id).count() == 1
    assert db_session.query(Rating).filter_by(task_id=task.id).first().value == 1
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest backend/tests/test_models.py -v` — Expected: FAIL (`Task` not defined / no DB).
- [ ] **Step 3: Implement `db.py` (SQLAlchemy engine/session from `DATABASE_URL` env var) and `models.py`** (models as described above; `status`/`type`/`value` as plain strings/ints, no enum class needed yet).
- [ ] **Step 4: Add `docker-compose.yml` with a `postgres` service and `.env.example` documenting `DATABASE_URL`, `MODEL_API_BASE`, `MODEL_API_KEY`.**
- [ ] **Step 5: Run test to verify it passes (against a local/dockerized Postgres)** — Run: `docker compose up -d postgres && pytest backend/tests/test_models.py -v` — Expected: PASS.
- [ ] **Step 6: Commit**

```bash
git add backend/app/db.py backend/app/models.py backend/tests/test_models.py docker-compose.yml .env.example
git commit -m "feat: scaffold backend and DB models"
```

---

### Task 2: Model adapter

**Files:**
- Create: `backend/app/agent/model_adapter.py`
- Test: `backend/tests/test_model_adapter.py`

**Interfaces:**
- Consumes: none (env vars `MODEL_API_BASE`, `MODEL_API_KEY`).
- Produces: `ModelAdapter` protocol with `complete(messages, tools) -> ModelStep` (see reference above); `OpenAICompatAdapter(base_url, api_key, model_name)` implementing it via `httpx.post` to `{base_url}/chat/completions` with `tools` passed as OpenAI tool-calling schema; a `FakeModelAdapter(script: list[ModelStep])` test double that returns each scripted step in order, for use by Task 5's loop tests.

- [ ] **Step 1: Write failing test for `FakeModelAdapter` returning scripted steps in order**

```python
def test_fake_adapter_returns_scripted_steps():
    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "http_get", "arguments": {"url": "http://x"}},
        {"type": "final_answer", "content": "done"},
    ])
    assert adapter.complete([], [])["type"] == "tool_call"
    assert adapter.complete([], [])["type"] == "final_answer"
```

- [ ] **Step 2: Run test to verify it fails** — Run: `pytest backend/tests/test_model_adapter.py -v` — Expected: FAIL (not defined).
- [ ] **Step 3: Implement `FakeModelAdapter`.**
- [ ] **Step 4: Write failing test for `OpenAICompatAdapter.complete` parsing a mocked HTTP response into a `tool_call` `ModelStep`** (use `httpx` mock/respx or a monkeypatched `httpx.Client.post`; assert the request body includes the `tools` schema and the parsed return matches `{"type": "tool_call", "tool_name": ..., "arguments": {...}}`).
- [ ] **Step 5: Run test to verify it fails** — Expected: FAIL (not defined).
- [ ] **Step 6: Implement `OpenAICompatAdapter`** — one line: map OpenAI's `tool_calls[0].function.{name,arguments}` (arguments is a JSON string — parse it; on parse failure return `{"type": "final_answer", "content": "<raw text>"}` so a malformed call never crashes the adapter) into `ModelStep`; if no `tool_calls`, return `{"type": "final_answer", "content": message.content}`.
- [ ] **Step 7: Run both tests to verify they pass** — Run: `pytest backend/tests/test_model_adapter.py -v` — Expected: PASS.
- [ ] **Step 8: Commit**

```bash
git add backend/app/agent/model_adapter.py backend/tests/test_model_adapter.py
git commit -m "feat: model adapter with OpenAI-compatible and fake implementations"
```

---

### Task 3: Tool registry + ask_user, report_gap, http_get

**Files:**
- Create: `backend/app/agent/tools.py`
- Test: `backend/tests/test_tools.py`

**Interfaces:**
- Consumes: none.
- Produces: `ToolContext` (dataclass: `task_id: int`, `session: Session`); `Tool` protocol (see reference); `ToolRegistry` with `.register(tool)`, `.get(name) -> Tool | None`, `.specs() -> list[dict]` (JSON-schema tool specs for the model adapter); `ask_user_tool` (writes an `ask_user` Event and returns `{"ok": True, "output": "awaiting user answer", "data": None}` — the loop, not this tool, handles pausing); `report_gap_tool(reason: str, options: list[str])` (writes a `report_gap` Event with payload `{"reason": reason, "options": options}`); `http_get_tool(url: str)` (does a real `httpx.get`, returns status+truncated body as `output`).

- [ ] **Step 1: Write failing test that registry returns `None` for an unknown tool and the registered tool's spec by name**

```python
def test_registry_lookup():
    registry = ToolRegistry()
    registry.register(http_get_tool)
    assert registry.get("http_get") is http_get_tool
    assert registry.get("nonexistent") is None
    assert any(s["name"] == "http_get" for s in registry.specs())
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 3: Implement `ToolRegistry`.**
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write failing test that `report_gap_tool` persists an `Event` of type `report_gap` with the given reason and options**

```python
def test_report_gap_persists_event(db_session, task):
    ctx = ToolContext(task_id=task.id, session=db_session)
    result = report_gap_tool.run({"reason": "no API key for X", "options": ["add key to vault", "use browser instead"]}, ctx)
    assert result["ok"] is True
    event = db_session.query(Event).filter_by(task_id=task.id, type="report_gap").one()
    assert event.payload["options"] == ["add key to vault", "use browser instead"]
```

- [ ] **Step 6: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 7: Implement `report_gap_tool` and `ask_user_tool`** (same persistence pattern, `ask_user` Event payload is `{"question": str}`).
- [ ] **Step 8: Write failing test for `http_get_tool` against a local mock server or monkeypatched `httpx.get`, asserting `ok` is True and `output` contains the response status.**
- [ ] **Step 9: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 10: Implement `http_get_tool`.**
- [ ] **Step 11: Run full test file to verify all pass** — Run: `pytest backend/tests/test_tools.py -v` — Expected: PASS.
- [ ] **Step 12: Commit**

```bash
git add backend/app/agent/tools.py backend/tests/test_tools.py
git commit -m "feat: tool registry with ask_user, report_gap, http_get"
```

---

### Task 4: Permissions (irreversible-action gating)

**Files:**
- Create: `backend/app/agent/permissions.py`
- Test: `backend/tests/test_permissions.py`

**Interfaces:**
- Consumes: none for `is_irreversible`; `Task.full_auto` field from Task 1 for `needs_approval`.
- Produces: `IRREVERSIBLE_TOOL_NAMES: set[str]` (seeded with a placeholder name, `"send_email"`, documenting the mechanism for later sub-projects — no real email tool exists in this plan); `is_irreversible(tool_name: str) -> bool`; `needs_approval(tool_name: str, task: Task) -> bool` (`True` iff `is_irreversible(tool_name) and not task.full_auto`).

- [ ] **Step 1: Write failing tests for both the full-auto and non-full-auto cases**

```python
def test_needs_approval_for_irreversible_when_not_full_auto():
    task = Task(full_auto=False)
    assert needs_approval("send_email", task) is True

def test_no_approval_needed_when_full_auto():
    task = Task(full_auto=True)
    assert needs_approval("send_email", task) is False

def test_no_approval_needed_for_reversible_tool():
    task = Task(full_auto=False)
    assert needs_approval("http_get", task) is False
```

- [ ] **Step 2: Run tests to verify they fail** — Expected: FAIL.
- [ ] **Step 3: Implement `is_irreversible` and `needs_approval`.**
- [ ] **Step 4: Run tests to verify they pass.**
- [ ] **Step 5: Commit**

```bash
git add backend/app/agent/permissions.py backend/tests/test_permissions.py
git commit -m "feat: irreversible-action permission gating"
```

---

### Task 5: Agent loop

**Files:**
- Create: `backend/app/agent/loop.py`
- Test: `backend/tests/test_loop.py`

**Interfaces:**
- Consumes: `ModelAdapter.complete`, `Tool`/`ToolRegistry` (Task 3), `needs_approval` (Task 4), `Task`/`Event` models (Task 1).
- Produces: `run_task_loop(task_id: int, session: Session, adapter: ModelAdapter, registry: ToolRegistry) -> None`. Sets `task.status` to `awaiting_answer` after an `ask_user` tool call, `awaiting_approval` before running an irreversible tool when approval is needed, `completed` on `final_answer`, `failed` if the model requests a tool not in the registry (after writing a `report_gap` event with options `["register this tool via the connector forge (not yet built)", "rephrase the task to avoid it"]`). Writes a `reflection` Event on completion with payload `{"summary": str}` (a one-line heuristic summary for this plan; richer reflection is a Task 5/learning-loop sub-project item, not here — note that explicitly in the payload as `"note": "minimal reflection, see spec §8 for full learning loop"`).

- [ ] **Step 1: Write failing test: loop runs a scripted tool_call then final_answer to completion**

```python
def test_loop_runs_tool_then_finishes(db_session, task):
    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "http_get", "arguments": {"url": "http://x"}},
        {"type": "final_answer", "content": "done"},
    ])
    registry = ToolRegistry(); registry.register(http_get_tool)
    run_task_loop(task.id, db_session, adapter, registry)
    refreshed = db_session.get(Task, task.id)
    assert refreshed.status == "completed"
    assert db_session.query(Event).filter_by(task_id=task.id, type="reflection").count() == 1
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 3: Write failing test: loop pauses with `awaiting_answer` on an `ask_user` tool call and does not continue past it**

```python
def test_loop_pauses_on_ask_user(db_session, task):
    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "ask_user", "arguments": {"question": "which email?"}},
    ])
    registry = ToolRegistry(); registry.register(ask_user_tool)
    run_task_loop(task.id, db_session, adapter, registry)
    assert db_session.get(Task, task.id).status == "awaiting_answer"
```

- [ ] **Step 4: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 5: Write failing test: loop pauses with `awaiting_approval` before an irreversible tool when `task.full_auto` is False, and does not execute the tool** (use a registered dummy tool named `"send_email"` with a `run` that raises if called, to prove it wasn't executed).
- [ ] **Step 6: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 7: Write failing test: unknown tool name produces a `report_gap` Event and sets status `failed`.**
- [ ] **Step 8: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 9: Write failing test: a model_step with malformed/missing arguments is recorded as a failed `tool_result` Event and the loop continues to the next scripted step rather than raising** (Review Focus item).
- [ ] **Step 10: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 11: Implement `run_task_loop`** — one line on approach: a plain `while True` driven by `adapter.complete(messages, registry.specs())`; append each `model_step`/`tool_call`/`tool_result` as an `Event` and to the in-memory `messages` list before the next call; check `needs_approval` before invoking any tool other than `ask_user`/`report_gap`; catch `KeyError`/`json`-type errors from argument handling around each tool call and record them as a failed `tool_result` rather than letting them propagate.
- [ ] **Step 12: Run all loop tests to verify they pass** — Run: `pytest backend/tests/test_loop.py -v` — Expected: PASS.
- [ ] **Step 13: Commit**

```bash
git add backend/app/agent/loop.py backend/tests/test_loop.py
git commit -m "feat: agent plan/act/observe loop with ask-user and approval gating"
```

---

### Task 6: Task API (create, get, rate) + resume endpoints

**Files:**
- Create: `backend/app/schemas.py`, `backend/app/api/tasks.py`
- Modify: `backend/app/main.py` (mount router)
- Test: `backend/tests/test_api_tasks.py`

**Interfaces:**
- Consumes: `Task`/`Event`/`Rating` models (Task 1), `run_task_loop` (Task 5).
- Produces: `POST /tasks {description, full_auto}` → `201 {id, status}` (enqueues `run_task_loop` as an `asyncio` background task — no multi-agent queue yet, that's sub-project 2); `GET /tasks/{id}` → full task + events; `POST /tasks/{id}/rate {value, comment?}` → `201`, only allowed when `status == "completed"`; `POST /tasks/{id}/answer {answer}` → resumes a task in `awaiting_answer` by appending a `user_answer` Event and re-invoking `run_task_loop`; `POST /tasks/{id}/approve {approved: bool}` → resumes a task in `awaiting_approval`, either running the pending tool call or recording denial and re-prompting the model.

- [ ] **Step 1: Write failing test: `POST /tasks` returns 201 and the task becomes queryable via `GET /tasks/{id}`** (use `FakeModelAdapter` wired in via dependency override so the test doesn't hit a real model).
- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (endpoint doesn't exist).
- [ ] **Step 3: Implement `schemas.py` (Pydantic request/response models) and the `POST`/`GET` handlers in `tasks.py`.**
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write failing test: `POST /tasks/{id}/rate` succeeds only when `status == "completed"`, returns 409 otherwise.**
- [ ] **Step 6: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 7: Implement the `rate` handler.**
- [ ] **Step 8: Write failing test: `POST /tasks/{id}/answer` on a task in `awaiting_answer` moves it back to `running`/`completed` by re-invoking the loop with the answer appended.**
- [ ] **Step 9: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 10: Implement the `answer` handler** (append `user_answer` Event, call `run_task_loop` again — same function handles resume since it reconstructs `messages` from persisted `Event`s).
- [ ] **Step 10a: Write failing test: `POST /tasks/{id}/answer` with `answer: ""` (blank) still returns 201 and records the event verbatim — it is not rejected by validation** (Review Focus item: blank input must not hang or error, the loop's own re-ask behavior handles it).
- [ ] **Step 10b: Run test to verify it fails, then adjust `schemas.py`'s answer model to allow empty strings (no `min_length`) if needed, then verify it passes.**
- [ ] **Step 11: Write failing test: `POST /tasks/{id}/approve {approved: false}` on a task in `awaiting_approval` records denial and does not execute the pending tool.**
- [ ] **Step 12: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 13: Implement the `approve` handler.**
- [ ] **Step 14: Run full test file to verify all pass** — Run: `pytest backend/tests/test_api_tasks.py -v` — Expected: PASS.
- [ ] **Step 15: Commit**

```bash
git add backend/app/schemas.py backend/app/api/tasks.py backend/app/main.py backend/tests/test_api_tasks.py
git commit -m "feat: task API with create, rate, answer, and approve endpoints"
```

---

### Task 7: Event stream (WebSocket)

**Files:**
- Create: `backend/app/api/events.py`
- Modify: `backend/app/main.py` (mount router)
- Test: `backend/tests/test_api_events.py`

**Interfaces:**
- Consumes: `Task`/`Event` models.
- Produces: `WS /tasks/{id}/stream` — on connect, sends all existing `Event` rows for the task as JSON messages in order, then pushes new ones as they're persisted (simplest correct approach for this plan: poll the DB for new events every 200ms and push deltas — a pub/sub bus is a later optimization, not required here).

- [ ] **Step 1: Write failing test using FastAPI's `TestClient` WebSocket test support: connecting to `/tasks/{id}/stream` for a task with two existing events receives both as JSON messages.**
- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 3: Implement the WebSocket handler** (send existing events, then poll-and-push loop with a 200ms sleep, breaking on disconnect).
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Commit**

```bash
git add backend/app/api/events.py backend/app/main.py backend/tests/test_api_events.py
git commit -m "feat: websocket event stream for task events"
```

---

### Task 8: Minimal web UI

**Files:**
- Create: `frontend/src/App.tsx`, `frontend/src/TaskView.tsx`, `frontend/src/api.ts`, `frontend/package.json`, `frontend/vite.config.ts`
- Test: `frontend/src/api.test.ts` (unit test for the API client's request shaping; full UI interaction is verified manually per spec §10's end-to-end rollout step, not unit-tested here)

**Interfaces:**
- Consumes: the Task 6/7 HTTP+WS API.
- Produces: a page where the user types a task description, submits it, sees the live event stream (model steps, tool calls, ask_user prompts, approval requests), can type an answer or click approve/deny, and after `completed` sees 👍/👎 buttons wired to `POST /tasks/{id}/rate`.

- [ ] **Step 1: Write failing test for `api.ts`'s `createTask(description, fullAuto)` building the correct `POST /tasks` request body.**
- [ ] **Step 2: Run test to verify it fails** — Run: `npm --prefix frontend test` — Expected: FAIL.
- [ ] **Step 3: Implement `api.ts`** (`createTask`, `getTask`, `rateTask`, `answerTask`, `approveTask`, `streamTask(id, onEvent)` wrapping `WebSocket`).
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Implement `App.tsx`** (task-creation form, list of past tasks) and `TaskView.tsx` (renders the event stream from `streamTask`; shows an answer box when the latest event is `ask_user` and the task status is `awaiting_answer`; shows approve/deny buttons when status is `awaiting_approval`; shows 👍/👎 when status is `completed`) — no dedicated automated test for these two components in this plan; verify manually by running the dev server against the backend.
- [ ] **Step 6: Manually verify**: run `docker compose up`, create a task from the UI, confirm the event stream renders live, confirm an `ask_user`-triggering task (use a task description that asks the fake/real model to request clarification) pauses and resumes on answer, confirm rating succeeds after completion.
- [ ] **Step 7: Commit**

```bash
git add frontend/
git commit -m "feat: minimal web UI for task creation, event stream, and rating"
```

---

## Self-Review Notes

- **Spec coverage:** §3 (API/Runner/model adapter/tool layer/Postgres) → Tasks 1–7. §4 (loop, permissions, ask-user/report-gap) → Tasks 3–5. §8 (reflection, rating) → Task 5 (minimal reflection) + Task 6 (`rate` endpoint) — full learning-loop retrieval/consolidation is explicitly out of scope per build order (sub-project 5) and noted as such in Task 5. §5/§6 (specialized agents, connector forge) are out of scope per build order (sub-projects 4, 6) — this plan only proves the core loop with one placeholder real tool (`http_get`). §9 security items (kill switch, spend cap, backups, vault/secret split) are infra-level and deploy-time concerns for later sub-projects/deploy work, not agent-core code — not included here; flagging this gap explicitly rather than silently dropping it: **a follow-up plan should cover deploy-time security hardening (kill switch endpoint, spend cap middleware, backup cron) before this goes live on the VPS, since sub-project 2 (multi-agent runtime) is the natural place for the kill switch (it already needs to track all running loops).**
- **Step scan:** passed — each code step gives exact signatures/files, bodies left to the implementer except where parsing logic is pinned (Task 2 step 6, Task 5 step 11 approach note).
- **Type consistency:** `ModelStep`, `ToolResult`, `Tool.run(arguments, ctx)`, `ToolRegistry.specs()`, `needs_approval(tool_name, task)`, `run_task_loop(task_id, session, adapter, registry)` are used identically across Tasks 2–7.
- **Review Focus:** all five items have an owning task and test step (malformed tool call → Task 5 step 9; blank ask_user answer → add as Task 6 step 8a: test that an empty-string `answer` is accepted but recorded verbatim, letting the loop's own prompt handle re-asking rather than the API rejecting it — **gap found and fixed inline**: adding this as an explicit sub-step of Task 6 Step 8; unresumable approval across restart → covered by persistence design (status lives in DB, not memory) across Tasks 1/5/6 — no separate test needed beyond existing resume tests; unknown tool → Task 5 step 7; missing tool_calls in model response → Task 2 step 6.
- **Proportion:** plan is longer than the spec section it covers but is dominated by test code and signatures, not prose duplication — acceptable for 8 tasks spanning DB, adapter, loop, API, WS, and UI.
