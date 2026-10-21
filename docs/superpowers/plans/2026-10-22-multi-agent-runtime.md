# Multi-Agent Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace agent-core's immediate `BackgroundTasks` execution with a bounded, queued, killable Scheduler; add task delegation (blocking/background sub-tasks); and add a dashboard read model + UI to see and act on everything at once.

**Architecture:** A new `Scheduler` owns an in-process worker pool (`asyncio.Semaphore`-style cap) and the FIFO(+urgent) queue, persisted on `Task`. It runs the existing (lightly extended) `run_task_loop` via `asyncio.to_thread` (the loop and its tools are synchronous), using a cooperative `threading.Event` for cancellation checked only at step boundaries. Delegation is a new tool pair (`delegate_task`, `check_subtask`) plus Scheduler-side logic that reacts when a child task reaches a terminal state. The dashboard is a read model (`GET /dashboard`) plus a React page.

**Tech Stack:** Same as agent-core (FastAPI, SQLAlchemy, pytest, React/TypeScript, Vite). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-22-multi-agent-runtime-design.md`

## Global Constraints

- Concurrency cap default is 3, configurable (env var `SCHEDULER_MAX_CONCURRENCY`) — spec §4.
- `MAX_DELEGATION_DEPTH` default 5, `MAX_CHILDREN_PER_TASK` default 10 — spec §5.
- Kill-switch modes are exactly `"pause"` and `"stop"` — spec §4. Kill-switch mode is **in-memory only** (not persisted); a process restart resets it to normal — a plan-level decision not spelled out in the spec, made here to avoid an extra DB round-trip on every scheduler tick for a value that only matters while the process is alive.
- Cancellation is cooperative and checked only at the loop's step boundaries, never mid-tool-call — spec §6 step 5. Realized here via a `threading.Event` passed into `run_task_loop`, because the loop and every tool's `.run()` are synchronous blocking calls — true `asyncio.Task.cancel()` cannot interrupt a blocking call running in a worker thread, so a cooperative flag is the only mechanism that is actually honest about what "stop" can do.
- `queue_position` is a sortable key, not a contiguous rank. Front-insertion (answer/approve/delegation resume) uses `(min existing queued position, default 0) - 1`; back-insertion (`POST /tasks`, a non-urgent `delegate_task` child) uses `(max existing queued position, default -1) + 1`. The Scheduler always prefers a queued task with `urgent=True` (smallest position among urgent ones) over any non-urgent task, regardless of front/back insertion — a plan-level ordering decision: urgent tasks are a strictly higher priority class than "resumed." `GET /dashboard` computes a display rank by sorting, never by trusting the raw stored value as a rank.
- Terminal `Task.status` values are `"completed"`, `"failed"`, `"interrupted"` — used by startup recovery and delegation-resume logic to decide what's "done."
- All new endpoints sit behind the existing `require_auth` dependency (see `backend/app/auth.py`); the WebSocket dashboard poll (if added later) would use `require_auth_ws` — not needed in this plan since the dashboard uses plain polling, like today's task view.
- **Testability of the Scheduler's factories:** tests override `get_adapter`/`get_registry`/`get_session_factory` per-test, often *after* the app (and its `lifespan`-constructed `Scheduler`) has already started (see `tests/conftest.py`'s `client` fixture, which enters the `TestClient` context — running `lifespan` — before most tests then set their own `app.dependency_overrides[tasks_module.get_adapter]`). If the `Scheduler` captured `adapter_factory`/`registry_factory`/`session_factory` once at construction time, later per-test overrides would be invisible to it. So `main.py`'s `lifespan` must pass factories that re-resolve the current override **every time they're called**, not just once — e.g. `adapter_factory=lambda: app.dependency_overrides.get(get_adapter, get_adapter)()` — so each task picked up by the Scheduler sees whatever the test most recently configured. Task 4 Step 3 spells this out exactly; every later task's tests depend on this being right.

## Review Focus

- **Two tasks submitted back-to-back with the cap already full** — the 2nd (and 3rd+) must land in `"queued"` with a real `queue_position`, not silently dropped or run anyway past the cap.
- **A blocking `delegate_task` whose child never finishes** (child itself pauses on `ask_user`) — the parent must sit in `"awaiting_subtask"` indefinitely without the Scheduler treating it as stuck/erroring, consistent with the spec's accepted-gap stance.
- **Kill-switch `"stop"` while a task has in-flight children** — every descendant actually running must end up `"interrupted"`, not just the one task directly targeted; a child that was only `"queued"` (not yet started) must not be silently left in `"queued"` forever after its parent is gone — spec cascades `"stop"` to the whole subtree.
- **`delegate_task` called from a task that's already at `MAX_DELEGATION_DEPTH`** — must be refused via `report_gap` and NOT create a child row at all, not create-then-fail.
- **The dashboard's `done` pagination with more than 50 completed tasks** — the 51st-oldest completed task must not appear in the default dashboard response, but must still be reachable via `GET /tasks/{id}`.

---

## File Structure

```
backend/
  app/
    models.py                     # MODIFY: add Task.queue_position, urgent,
                                   #   parent_task_id, agent_type,
                                   #   delegation_depth, steps_taken
    scheduler.py                   # CREATE: Scheduler class
    agent/
      loop.py                      # MODIFY: persisted steps_taken, cooperative
                                   #   interrupt_event, delegate_task pause
      tools.py                      # MODIFY: ToolContext.scheduler field;
                                   #   delegate_task_tool, check_subtask_tool
    api/
      tasks.py                      # MODIFY: create/answer/approve use
                                   #   Scheduler.enqueue instead of
                                   #   BackgroundTasks; new POST /tasks/{id}/kill
      kill_switch.py                 # CREATE: POST /kill-switch
      dashboard.py                    # CREATE: GET /dashboard
    main.py                           # MODIFY: lifespan builds the Scheduler,
                                   #   runs startup recovery, starts/stops it;
                                   #   mounts the two new routers
  tests/
    test_models.py                     # MODIFY: new-field round-trip
    test_scheduler.py                   # CREATE
    test_loop.py                         # MODIFY: steps persistence, interrupt
    test_tools.py                         # MODIFY: delegate_task, check_subtask
    test_api_kill_switch.py                # CREATE
    test_api_dashboard.py                   # CREATE
    test_delegation_e2e.py                   # CREATE
frontend/
  src/
    api.ts                              # MODIFY: getDashboard, killTask,
                                       #   setKillSwitch
    Dashboard.tsx                        # CREATE: Command Center UI
    App.tsx                               # MODIFY: route to Dashboard
```

## Interfaces reference (cross-task contract)

- `Task` gains: `queue_position: int | None`, `urgent: bool` (default `False`), `parent_task_id: int | None`, `agent_type: str` (default `"general"`), `delegation_depth: int` (default `0`), `steps_taken: int` (default `0`).
- `run_task_loop(task_id: int, session: Session, adapter: ModelAdapter, registry: ToolRegistry, scheduler: "Scheduler | None" = None, interrupt_event: "threading.Event | None" = None) -> None`.
- `ToolContext(task_id: int, session: Session, scheduler: "Scheduler | None" = None)`.
- `Scheduler(session_factory, adapter_factory: Callable[[], ModelAdapter], registry_factory: Callable[[], ToolRegistry], cap: int = 3, run_fn: Callable = run_task_loop)`.
  - `async def start(self) -> None` / `async def stop(self) -> None`
  - `def enqueue(self, session: Session, task_id: int, front: bool = False) -> None` (sync — called both from request handlers with their own session and from the Scheduler's own async code via a short-lived session)
  - `async def kill_switch(self, mode: str) -> None` (`mode` is `"normal"`, `"pause"`, or `"stop"`)
  - `async def kill_task(self, task_id: int) -> None`
  - `def status(self) -> dict` — `{"mode": str, "running": int, "cap": int}`

---

### Task 1: Data model fields

**Files:**
- Modify: `backend/app/models.py` (the `Task` class)
- Test: `backend/tests/test_models.py`

**Interfaces:**
- Produces: the six new `Task` fields listed in the Interfaces reference, all with the stated defaults.

- [ ] **Step 1: Write the failing test**

```python
def test_task_new_fields_have_expected_defaults(db_session):
    task = Task(description="x", status="queued", full_auto=False)
    db_session.add(task)
    db_session.commit()
    assert task.queue_position is None
    assert task.urgent is False
    assert task.parent_task_id is None
    assert task.agent_type == "general"
    assert task.delegation_depth == 0
    assert task.steps_taken == 0
```

- [ ] **Step 2: Run test to verify it fails** — Run: `cd backend && PYTHONPATH=. python3 -m pytest tests/test_models.py -v` — Expected: FAIL (`AttributeError` or missing column).
- [ ] **Step 3: Add the six columns to `Task`** in `backend/app/models.py` — `queue_position: Mapped[int | None]` (nullable `Integer`), `urgent: Mapped[bool]` (`Boolean`, default `False`), `parent_task_id: Mapped[int | None]` (nullable `ForeignKey("tasks.id")`), `agent_type: Mapped[str]` (`String`, default `"general"`), `delegation_depth: Mapped[int]` (`Integer`, default `0`), `steps_taken: Mapped[int]` (`Integer`, default `0`).
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Commit**

```bash
git add backend/app/models.py backend/tests/test_models.py
git commit -m "feat: add scheduler/delegation fields to Task"
```

---

### Task 2: Scheduler core (queue + cap, no delegation/kill yet)

**Files:**
- Create: `backend/app/scheduler.py`
- Test: `backend/tests/test_scheduler.py`

**Interfaces:**
- Consumes: `Task` (Task 1).
- Produces: `Scheduler` as described in the Interfaces reference, minus `kill_switch`/`kill_task` (added in Task 5) and the delegation-aware parts of `_on_task_terminal` (added in Task 6). For this task, a terminated task's `status` is just whatever `run_fn` left it as — no parent handling yet.

- [ ] **Step 1: Write the failing test: enqueueing 4 tasks with cap=3 runs exactly 3 concurrently and the 4th stays queued until a slot frees**

```python
async def test_cap_enforced_and_fourth_task_waits(db_session, session_factory):
    started = []
    async def fake_run(task_id, session, adapter, registry, scheduler=None, interrupt_event=None):
        started.append(task_id)
        await asyncio.sleep(0.05)
        task = session.get(Task, task_id)
        task.status = "completed"
        session.commit()

    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=3, run_fn=fake_run)
    await scheduler.start()
    task_ids = [make_queued_task(db_session) for _ in range(4)]
    for tid in task_ids:
        scheduler.enqueue(db_session, tid)
    await asyncio.sleep(0.02)
    assert len(started) == 3  # 4th hasn't started yet, cap holds
    await asyncio.sleep(0.1)
    assert len(started) == 4  # 4th started once a slot freed
    await scheduler.stop()
```

(`make_queued_task` is a small test helper creating a `Task(status="queued", ...)` and returning its id; `session_factory` is a fixture returning a `sessionmaker` bound to a StaticPool in-memory SQLite engine, matching the pattern in `tests/conftest.py`'s `client` fixture — add it as a new fixture in `conftest.py` since both this test file and later ones need it.)

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (`Scheduler` not defined).
- [ ] **Step 3: Implement `Scheduler`** — one line on approach: `start()` launches `asyncio.create_task(self._loop_forever())`; the loop ticks every `POLL_INTERVAL_SECONDS` (0.05), and each tick, while `len(self._running) < self._cap`, pops the next queued task (urgent-first by smallest `queue_position` among `urgent=True` rows, else smallest among all queued rows — a fresh `session_factory()` session per tick's query) and spawns `asyncio.create_task(self._run_one(task_id))`, tracking it in `self._running: dict[int, asyncio.Task]`. `_run_one` opens its own session, calls `await asyncio.to_thread(self.run_fn, task_id, session, self.adapter_factory(), self.registry_factory(), self, interrupt_event)` (an `interrupt_event` is created and stored per task even though nothing sets it yet in this task — Task 5 uses it), removes itself from `self._running` in a `finally`, and calls `self._on_task_terminal(task_id)` (a no-op stub in this task, filled in by Task 6). `enqueue(session, task_id, front=False)` sets `status="queued"` and `queue_position` per the Global Constraints formula, within the Scheduler's own `asyncio.Lock` held via `async with` — since `enqueue` itself is `def` not `async def` per the Interfaces reference (called from sync request handlers too), use `threading.Lock` instead of `asyncio.Lock` for this specific piece of shared state to keep it callable from both sync and async code safely.
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: urgent task jumps ahead of two already-queued non-urgent tasks**

```python
def test_urgent_task_is_popped_before_older_non_urgent_tasks(db_session, session_factory):
    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=1)
    old1 = make_queued_task(db_session)
    old2 = make_queued_task(db_session)
    urgent = make_queued_task(db_session, urgent=True)
    for tid in (old1, old2, urgent):
        scheduler.enqueue(db_session, tid)
    popped = scheduler._pop_next(db_session)
    assert popped.id == urgent
```

- [ ] **Step 6: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 7: Implement `_pop_next(session) -> Task | None`** as the query described in Step 3 (urgent-first).
- [ ] **Step 8: Run test to verify it passes.**
- [ ] **Step 9: Commit**

```bash
git add backend/app/scheduler.py backend/tests/test_scheduler.py backend/tests/conftest.py
git commit -m "feat: scheduler core with bounded concurrency and urgent queue ordering"
```

---

### Task 3: Loop: persisted step count + cooperative interrupt

**Files:**
- Modify: `backend/app/agent/loop.py`
- Test: `backend/tests/test_loop.py`

**Interfaces:**
- Consumes: `Task.steps_taken` (Task 1).
- Produces: `run_task_loop` with the extended signature from the Interfaces reference (`scheduler` and `interrupt_event` both optional, default `None`, unused by this task's own logic except `interrupt_event`). When `interrupt_event is not None and interrupt_event.is_set()` is checked at the top of each loop iteration (same point as the existing `MAX_STEPS` check): the task is marked `status="interrupted"`, a `report_gap` event is written with `reason="stopped by kill switch"` and options `["retry the task", "it stopped partway — review its trace before retrying"]`, and the function returns.

- [ ] **Step 1: Write the failing test: an already-set `interrupt_event` stops the loop before its first model call**

```python
def test_loop_stops_immediately_when_interrupt_event_already_set(db_session, task):
    adapter = FakeModelAdapter(script=[{"type": "final_answer", "content": "should not run"}])
    registry = ToolRegistry()
    event = threading.Event()
    event.set()
    run_task_loop(task.id, db_session, adapter, registry, interrupt_event=event)
    refreshed = db_session.get(Task, task.id)
    assert refreshed.status == "interrupted"
    gap = db_session.query(Event).filter_by(task_id=task.id, type="report_gap").one()
    assert "stopped by kill switch" in gap.payload["reason"]
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (signature doesn't accept `interrupt_event`, or it's accepted but ignored).
- [ ] **Step 3: Add the interrupt check to `run_task_loop`** — insert it immediately after the existing `MAX_STEPS` check inside the `while True:` loop, same structure (write the `report_gap` event, set status, `return`).
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: `steps_taken` persists across two separate calls to `run_task_loop` on the same task (simulating a pause/resume) instead of resetting to zero**

```python
def test_steps_taken_persists_across_resume(db_session, task):
    adapter1 = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "ask_user", "arguments": {"question": "q"}},
    ])
    registry = ToolRegistry()
    registry.register(ask_user_tool)
    run_task_loop(task.id, db_session, adapter1, registry)
    after_first = db_session.get(Task, task.id).steps_taken
    assert after_first == 1

    adapter2 = FakeModelAdapter(script=[{"type": "final_answer", "content": "done"}])
    run_task_loop(task.id, db_session, adapter2, registry)
    assert db_session.get(Task, task.id).steps_taken == 2
```

- [ ] **Step 6: Run test to verify it fails** — Expected: FAIL (`steps_taken` stays 0, or resets).
- [ ] **Step 7: Replace the local `steps_taken = 0` with reading/writing `task.steps_taken`** — at the top of `run_task_loop`, start from `task.steps_taken` instead of `0`; every increment also commits it to the `Task` row (fold into the existing `task.status`/`session.commit()` calls where convenient, or add one `session.commit()` right after incrementing).
- [ ] **Step 8: Run test to verify it passes, then run the full loop test file to confirm no regression** — Run: `cd backend && PYTHONPATH=. python3 -m pytest tests/test_loop.py -v` — Expected: all PASS (existing `MAX_STEPS` test included).
- [ ] **Step 9: Commit**

```bash
git add backend/app/agent/loop.py backend/tests/test_loop.py
git commit -m "feat: persist steps_taken and add cooperative interrupt check"
```

---

### Task 4: Wire the Scheduler into FastAPI (replacing BackgroundTasks) + startup recovery

**Files:**
- Modify: `backend/app/main.py`, `backend/app/api/tasks.py`
- Test: `backend/tests/test_api_tasks.py` (extend), new `backend/tests/test_startup_recovery.py`

**Interfaces:**
- Consumes: `Scheduler` (Task 2).
- Produces: `app.state.scheduler: Scheduler` set in a FastAPI `lifespan` context manager in `main.py`; `get_scheduler(request: Request) -> Scheduler` dependency in `tasks.py`; `create_task`/`answer_task`/`approve_task` call `scheduler.enqueue(session, task.id, front=...)` instead of `background_tasks.add_task(_run_loop_in_background, ...)` (`front=True` for answer/approve resumes, `front=False` for new tasks); `Task.status` default on creation is now `"queued"`, not `"running"`.

- [ ] **Step 1: Write the failing test: `POST /tasks` creates a task with `status="queued"` and it reaches `"completed"` once the real (test-wired) Scheduler picks it up**

```python
def test_create_task_runs_through_scheduler(client):
    response = client.post("/tasks", json={"description": "say hello", "full_auto": False})
    assert response.json()["status"] == "queued"
    import time
    for _ in range(20):
        if client.get(f"/tasks/{response.json()['id']}").json()["task"]["status"] == "completed":
            break
        time.sleep(0.05)
    assert client.get(f"/tasks/{response.json()['id']}").json()["task"]["status"] == "completed"
```

(This requires the `client` fixture in `conftest.py` to build its `TestClient` using FastAPI's `lifespan="on"` behavior so the app's `lifespan` context manager — and thus the Scheduler — actually runs during the test; `TestClient` does this by default when used as a context manager, which the fixture already does.)

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (`status` is `"running"`, not `"queued"`, since nothing's wired yet).
- [ ] **Step 3: Implement the wiring** — one line on approach: `main.py` gets an `@asynccontextmanager async def lifespan(app): ...`. Inside it, build the Scheduler with **lazily re-resolving** factories, per the Global Constraints note on testability:

```python
from app.api.tasks import get_adapter, get_registry, router as tasks_router
from app.db import get_session_factory

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = Scheduler(
        session_factory=lambda: app.dependency_overrides.get(get_session_factory, get_session_factory)()(),
        adapter_factory=lambda: app.dependency_overrides.get(get_adapter, get_adapter)(),
        registry_factory=lambda: app.dependency_overrides.get(get_registry, get_registry)(),
    )
    app.state.scheduler = scheduler
    run_startup_recovery(scheduler.session_factory)  # Step 7
    await scheduler.start()
    yield
    await scheduler.stop()

app = FastAPI(lifespan=lifespan)
```

  (`get_session_factory` itself returns a `sessionmaker`, hence the double call in `session_factory=lambda: ...()()` — once to resolve the override/default function, once to invoke it and get the actual `sessionmaker`.) In `tasks.py`, add `get_scheduler(request: Request) -> Scheduler: return request.app.state.scheduler`, remove `background_tasks`/`_run_loop_in_background` from `create_task`, replace with `task.status = "queued"` + `scheduler.enqueue(session, task.id)`. Likewise for `answer_task`/`approve_task`, replacing their `background_tasks.add_task(...)` call with `scheduler.enqueue(session, task_id, front=True)`.
- [ ] **Step 4: Run test to verify it passes, then run the whole `test_api_tasks.py` file** — Run: `cd backend && PYTHONPATH=. python3 -m pytest tests/test_api_tasks.py -v` — Expected: all PASS (existing tests must still pass against the real Scheduler now, not `BackgroundTasks`).
- [ ] **Step 5: Write the failing test: a task seeded directly into `status="running"` (simulating a crash) is marked `"failed"` with the restart `report_gap` the next time the app starts, before any new task can run**

```python
def test_startup_recovery_marks_orphaned_running_task_failed(session_factory):
    session = session_factory()
    orphan = Task(description="was running", status="running", full_auto=False)
    session.add(orphan)
    session.commit()
    orphan_id = orphan.id
    session.close()

    run_startup_recovery(session_factory)

    session2 = session_factory()
    refreshed = session2.get(Task, orphan_id)
    assert refreshed.status == "failed"
    gap = session2.query(Event).filter_by(task_id=orphan_id, type="report_gap").one()
    assert "server restarted" in gap.payload["reason"]
```

- [ ] **Step 6: Run test to verify it fails** — Expected: FAIL (`run_startup_recovery` not defined).
- [ ] **Step 7: Implement `run_startup_recovery(session_factory) -> None`** in `main.py` (or a small new `backend/app/startup.py` if `main.py` is getting crowded — reasonable either way): opens a session, queries `Task` where `status in ("running", "awaiting_subtask")`, for each writes the `report_gap` event (`reason="server restarted while this task was running — its in-progress state could not be resumed safely"`, options from the spec §6 step 0) and sets `status="failed"`, commits. Call it from the `lifespan` function before `scheduler.start()`.
- [ ] **Step 8: Run test to verify it passes.**
- [ ] **Step 9: Commit**

```bash
git add backend/app/main.py backend/app/api/tasks.py backend/tests/test_api_tasks.py backend/tests/test_startup_recovery.py
git commit -m "feat: wire scheduler into FastAPI, replace BackgroundTasks, add startup recovery"
```

---

### Task 5: Kill switch (global + per-task) with cascading interrupt

**Files:**
- Create: `backend/app/api/kill_switch.py`
- Modify: `backend/app/scheduler.py`, `backend/app/api/tasks.py`, `backend/app/main.py` (mount router)
- Test: `backend/tests/test_api_kill_switch.py`

**Interfaces:**
- Consumes: `Scheduler` (Task 2), `interrupt_event` plumbing (Task 3).
- Produces: `POST /kill-switch {mode: "normal"|"pause"|"stop"}` → `200 {mode, running, cap}` (the `status()` dict); `POST /tasks/{task_id}/kill` → `200 {id, status}` — cascades to the task's whole subtree. `Scheduler.kill_switch`/`Scheduler.kill_task` as in the Interfaces reference; `Scheduler` now actually creates and stores a `threading.Event` per running task (in `self._interrupt_events: dict[int, threading.Event]`, set in `_run_one` before calling `run_fn`) and passes it through.

- [ ] **Step 1: Write the failing test: `kill_switch("pause")` stops new queued tasks from starting while an in-flight one finishes normally**

```python
async def test_pause_stops_new_starts_but_lets_running_finish(db_session, session_factory):
    ...
    await scheduler.kill_switch("pause")
    tid = make_queued_task(db_session)
    scheduler.enqueue(db_session, tid)
    await asyncio.sleep(0.1)
    assert db_session.get(Task, tid).status == "queued"  # never started
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (`kill_switch` not defined, or pause doesn't stop popping).
- [ ] **Step 3: Implement `kill_switch(mode)`** — sets `self._mode`; `_loop_forever`'s pop-loop is gated on `self._mode == "normal"`.
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: `kill_task` on a running task's parent also interrupts its still-running child**

```python
async def test_kill_task_cascades_to_running_child(db_session, session_factory):
    ...
    parent_id = make_running_task(db_session)
    child_id = make_running_task(db_session, parent_task_id=parent_id)
    scheduler._running[parent_id] = ...  # register both as "in flight" per the test's fake run_fn
    scheduler._running[child_id] = ...
    await scheduler.kill_task(parent_id)
    await asyncio.sleep(0.05)
    assert db_session.get(Task, parent_id).status == "interrupted"
    assert db_session.get(Task, child_id).status == "interrupted"
```

(Exact fake-task bookkeeping is left to the implementer, following Task 2's `fake_run` pattern — the point under test is that both rows end up `"interrupted"`, not the mechanics of registering fake async tasks.)

- [ ] **Step 6: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 7: Implement `kill_task(task_id)`** — one line on approach: recursively collect `task_id` plus every descendant's id (a small recursive query walking `parent_task_id`), and for each one present in `self._interrupt_events`, call `.set()` on its event — the loop's own cooperative check (Task 3) does the rest (marks `"interrupted"`, writes the `report_gap`, returns). `kill_switch("stop")` calls the same collection logic across every currently-running task.
- [ ] **Step 8: Run test to verify it passes.**
- [ ] **Step 9: Implement `POST /kill-switch` and `POST /tasks/{task_id}/kill`** in `kill_switch.py` / `tasks.py` respectively, each a thin wrapper calling the Scheduler methods above via `get_scheduler` (Task 4).
- [ ] **Step 10: Write the failing test for the HTTP layer: `POST /kill-switch {"mode":"pause"}` returns 200 with the expected body**

```python
def test_kill_switch_endpoint_returns_status(client):
    response = client.post("/kill-switch", json={"mode": "pause"})
    assert response.status_code == 200
    assert response.json()["mode"] == "pause"
```

- [ ] **Step 11: Run test to verify it fails** — Expected: FAIL (404, route doesn't exist).
- [ ] **Step 12: Run test to verify it passes** (the endpoint from Step 9 should already satisfy it — this step exists to prove the HTTP wiring, not add new logic).
- [ ] **Step 13: Commit**

```bash
git add backend/app/api/kill_switch.py backend/app/scheduler.py backend/app/api/tasks.py backend/app/main.py backend/tests/test_api_kill_switch.py
git commit -m "feat: global and per-task kill switch with cascading interrupt"
```

---

### Task 6: Delegation (`delegate_task`, `check_subtask`, parent resume)

**Files:**
- Modify: `backend/app/agent/tools.py`, `backend/app/agent/loop.py`, `backend/app/scheduler.py`, `backend/app/api/tasks.py` (register new tools in `get_registry`)
- Test: `backend/tests/test_tools.py` (extend), `backend/tests/test_loop.py` (extend), `backend/tests/test_delegation_e2e.py` (new)

**Interfaces:**
- Consumes: `ToolContext.scheduler` (Interfaces reference), `Scheduler.enqueue` (Task 2).
- Produces: `delegate_task_tool` (name `"delegate_task"`, parameters `{description: str, mode: "blocking"|"background", agent_type?: str}`) and `check_subtask_tool` (name `"check_subtask"`, parameters `{subtask_id: int}`), both following the existing `Tool` protocol. `run_task_loop` gains one more special case: after a successful `delegate_task` tool call whose `arguments["mode"] == "blocking"`, set `status="awaiting_subtask"` and return (same pattern as the existing `ask_user` case). `Scheduler._on_task_terminal` is filled in with the parent-resume/`subtask_update` logic from the plan's Global Constraints note.

- [ ] **Step 1: Write the failing test: `delegate_task_tool.run` creates a child `Task` with `parent_task_id` and `delegation_depth = parent.delegation_depth + 1`, and enqueues it**

```python
def test_delegate_task_creates_and_enqueues_child(db_session, task):
    scheduler = FakeSchedulerSpy()  # records enqueue(session, task_id, front) calls
    ctx = ToolContext(task_id=task.id, session=db_session, scheduler=scheduler)
    result = delegate_task_tool.run({"description": "do X", "mode": "background"}, ctx)
    assert result["ok"] is True
    child_id = result["data"]["subtask_id"]
    child = db_session.get(Task, child_id)
    assert child.parent_task_id == task.id
    assert child.delegation_depth == 1
    assert scheduler.enqueued == [child_id]
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (not defined).
- [ ] **Step 3: Implement `delegate_task_tool`** — creates the child `Task` (`status="queued"`, `description`, `agent_type=arguments.get("agent_type", "general")`, `parent_task_id=ctx.task_id`, `delegation_depth = parent.delegation_depth + 1`), commits, calls `ctx.scheduler.enqueue(ctx.session, child.id)`, returns `{"ok": True, "output": f"delegated as subtask {child.id}", "data": {"subtask_id": child.id}}`.
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: `delegate_task` at `MAX_DELEGATION_DEPTH` is refused via `report_gap` and creates no child row**

```python
def test_delegate_task_refuses_past_depth_limit(db_session, task):
    task.delegation_depth = MAX_DELEGATION_DEPTH
    db_session.commit()
    scheduler = FakeSchedulerSpy()
    ctx = ToolContext(task_id=task.id, session=db_session, scheduler=scheduler)
    before_count = db_session.query(Task).count()
    result = delegate_task_tool.run({"description": "too deep", "mode": "background"}, ctx)
    assert result["ok"] is False
    assert db_session.query(Task).count() == before_count
    gap = db_session.query(Event).filter_by(task_id=task.id, type="report_gap").one()
    assert "too deeply nested" in gap.payload["reason"]
```

- [ ] **Step 6: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 7: Add the depth check** (and, identically structured, a fan-out check counting the parent's non-terminal children against `MAX_CHILDREN_PER_TASK`) at the top of `delegate_task_tool.run`, before creating anything — on violation, write the `report_gap` event via `ctx.session`/`ctx.task_id` (same pattern as the existing `report_gap_tool`) and return `{"ok": False, "output": "...", "data": None}` without touching the `Task` table.
- [ ] **Step 8: Run test to verify it passes.**
- [ ] **Step 9: Implement `check_subtask_tool`** — looks up the child `Task` by `subtask_id`, returns `{"ok": True, "output": f"subtask status: {child.status}", "data": {"status": child.status}}`, or `{"ok": False, "output": "no such subtask", "data": None}` if not found. (No dedicated TDD sub-steps — small enough to fold into this task's single test pass; add one direct test for it alongside Step 1's test file.)
- [ ] **Step 10: Write the failing test: the loop pauses with `awaiting_subtask` after a blocking `delegate_task` call**

```python
def test_loop_pauses_on_blocking_delegate_task(db_session, task):
    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "delegate_task", "arguments": {"description": "x", "mode": "blocking"}},
    ])
    registry = ToolRegistry()
    registry.register(delegate_task_tool)
    scheduler = FakeSchedulerSpy()
    run_task_loop(task.id, db_session, adapter, registry, scheduler=scheduler)
    assert db_session.get(Task, task.id).status == "awaiting_subtask"
```

- [ ] **Step 11: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 12: Add the special case to `run_task_loop`** — mirrors the existing `ask_user` branch: `if tool_name == "delegate_task" and result.get("ok") and arguments.get("mode") == "blocking": task.status = "awaiting_subtask"; session.commit(); return`.
- [ ] **Step 13: Run test to verify it passes.**
- [ ] **Step 14: Write the failing end-to-end test: a blocking parent resumes with its child's result once the child completes, driven by the real Scheduler**

```python
async def test_blocking_parent_resumes_with_child_result(session_factory):
    # parent script: delegate_task(mode=blocking) then final_answer
    # child script: final_answer immediately
    # drive both through a real Scheduler (cap=2) until parent reaches "completed"
    ...
    assert parent_task.status == "completed"
    subtask_update_events = [e for e in parent_events if e.type in ("tool_result", "subtask_update")]
    assert any("child" in str(e.payload) or "done" in str(e.payload) for e in subtask_update_events)
```

(Leave the adapter-script wiring to the implementer — the shape matches Task 5's fake-task tests; the point under test is that the parent actually reaches `"completed"` without manual intervention, proving the resume path end-to-end.)

- [ ] **Step 15: Run test to verify it fails** — Expected: FAIL (`_on_task_terminal` is still the Task 2 no-op stub).
- [ ] **Step 16: Implement `Scheduler._on_task_terminal(task_id)`** per the plan's Global Constraints note: load the task; if `parent_task_id` is set, load the parent; always append a `subtask_update` event onto the parent (payload includes the child's final `status` and its last `model_step`/`final_answer` content if any); if the parent's status is `"awaiting_subtask"` (the blocking case — specifically waiting on *this* child) or one of `"awaiting_answer"`/`"awaiting_approval"` (the one-shot background nudge), re-enqueue the parent via `self.enqueue(session, parent.id, front=True)`; if the parent is `"running"` or already terminal, do nothing further.
- [ ] **Step 17: Run test to verify it passes, then run the full backend suite** — Run: `cd backend && PYTHONPATH=. python3 -m pytest -v` — Expected: all PASS.
- [ ] **Step 18: Register both new tools in `get_registry()`** in `backend/app/api/tasks.py`.
- [ ] **Step 19: Commit**

```bash
git add backend/app/agent/tools.py backend/app/agent/loop.py backend/app/scheduler.py backend/app/api/tasks.py backend/tests/test_tools.py backend/tests/test_loop.py backend/tests/test_delegation_e2e.py
git commit -m "feat: task delegation (blocking/background sub-tasks) with parent resume"
```

---

### Task 7: Dashboard read model

**Files:**
- Create: `backend/app/api/dashboard.py`
- Modify: `backend/app/main.py` (mount router)
- Test: `backend/tests/test_api_dashboard.py`

**Interfaces:**
- Consumes: `Task`/`Event` (Task 1), `Scheduler.status()` (Task 5).
- Produces: `GET /dashboard` → `{"kill_switch": {...}, "queued": [...], "running": [...], "needs_you": [...], "done": [...]}`. Each task entry is `{"id", "description", "status", "agent_type", "parent_task_id", "children": [<nested entries, recursively>]}`. `"needs_you"` is every task whose `status` is `"awaiting_answer"` or `"awaiting_approval"` (not `"awaiting_subtask"` — that one is waiting on another task, not on you). `"done"` is the 50 most recently created (by `id` descending) tasks with `status` in `("completed", "failed", "interrupted")`, top-level only (a `done` task's children, if any, nest under it the same way).

- [ ] **Step 1: Write the failing test: dashboard groups a queued, a running, an awaiting-answer, and a completed task into their correct buckets**

```python
def test_dashboard_groups_tasks_by_status(client):
    # create 4 tasks via the API, driving each (via adapter script overrides) to
    # queued / running / awaiting_answer / completed respectively
    ...
    body = client.get("/dashboard").json()
    assert len(body["queued"]) == 1
    assert len(body["running"]) == 1
    assert len(body["needs_you"]) == 1
    assert len(body["done"]) == 1
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (404).
- [ ] **Step 3: Implement `GET /dashboard`** — one line on approach: four separate queries by `status` (grouping `"awaiting_answer"`/`"awaiting_approval"` together for `needs_you`), building the nested `children` list by a second query per task on `parent_task_id` (fine at this scale — no need for a recursive CTE given `MAX_DELEGATION_DEPTH` bounds the depth to 5).
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: the `done` list is capped at 50 and older completed tasks are excluded**

```python
def test_dashboard_done_list_is_paginated(client):
    # create and complete 55 tasks
    ...
    body = client.get("/dashboard").json()
    assert len(body["done"]) == 50
```

- [ ] **Step 6: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 7: Add `.order_by(Task.id.desc()).limit(50)`** to the `done` query.
- [ ] **Step 8: Run test to verify it passes.**
- [ ] **Step 9: Commit**

```bash
git add backend/app/api/dashboard.py backend/app/main.py backend/tests/test_api_dashboard.py
git commit -m "feat: dashboard read model grouping tasks by status and delegation tree"
```

---

### Task 8: Dashboard UI (Command Center, dark mode)

**Files:**
- Create: `frontend/src/Dashboard.tsx`
- Modify: `frontend/src/api.ts`, `frontend/src/App.tsx`
- Test: `frontend/src/api.test.ts` (extend)

**Interfaces:**
- Consumes: `GET /dashboard` (Task 7), `POST /kill-switch`, `POST /tasks/{id}/kill` (Task 5), existing `answerTask`/`approveTask`/`rateTask` (sub-project 1).
- Produces: `getDashboard(): Promise<DashboardResponse>`, `setKillSwitch(mode: "normal"|"pause"|"stop"): Promise<{mode:string}>`, `killTask(id: number): Promise<{id:number,status:string}>` in `api.ts`; `Dashboard.tsx` rendering the stat strip (counts per bucket + pause/stop buttons) and a card grid (queued/running/needs-you sections, each card showing inline answer/approve/deny/kill controls, `done` as a collapsed/muted list) in a dark theme (`background: #121212`-class palette); `App.tsx` adds a top-level toggle or route between "create task" and "dashboard" views.

- [ ] **Step 1: Write the failing test for `getDashboard()`'s request shape**

```ts
it("GETs /dashboard", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ queued: [], running: [], needs_you: [], done: [], kill_switch: { mode: "normal" } }) });
  vi.stubGlobal("fetch", fetchMock);
  await getDashboard();
  expect(fetchMock).toHaveBeenCalledWith("/dashboard", expect.objectContaining({ headers: expect.objectContaining({ Authorization: expect.stringMatching(/^Bearer .+/) }) }));
});
```

- [ ] **Step 2: Run test to verify it fails** — Run: `cd frontend && npm test` — Expected: FAIL.
- [ ] **Step 3: Implement `getDashboard`, `setKillSwitch`, `killTask`** in `api.ts`, following the existing `postJson`/`authHeaders` pattern.
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Implement `Dashboard.tsx`** — no dedicated automated test for the component itself, same precedent as `TaskView.tsx`/`App.tsx` in the agent-core plan; verify manually once a backend is reachable (dev server + the existing `docker compose` setup).
- [ ] **Step 6: Wire `Dashboard.tsx` into `App.tsx`** (a simple tab/toggle between "New task" and "Dashboard" is sufficient — no router dependency needed for two views).
- [ ] **Step 7: Run the frontend test suite and `tsc --noEmit`** — Run: `cd frontend && npm test && npx tsc --noEmit` — Expected: PASS / no errors.
- [ ] **Step 8: Commit**

```bash
git add frontend/src/Dashboard.tsx frontend/src/api.ts frontend/src/api.test.ts frontend/src/App.tsx
git commit -m "feat: Command Center dashboard UI (dark mode, inline actions)"
```

---

## Self-Review Notes

- **Spec coverage:** §3 (data model) → Task 1. §4 components (Scheduler, queue, kill switch, per-task kill, delegation tool, dashboard read model/UI) → Tasks 2, 5, 6, 7, 8 respectively. §5 (delegation modes, depth/fan-out, step-cap persistence, queue-mutation safety) → Tasks 3, 6 (the queue-mutation lock is in Task 2's `enqueue` implementation). §6 data flow steps 0–6 → step 0 (startup recovery) Task 4; steps 1–3 (enqueue/pop/resume-at-front) Tasks 2 & 4; steps 4–5 (kill switch, cascade, step-boundary-only cancellation) Task 5; step 6 (dashboard polling + actions) Tasks 7–8. §7 error-handling table → covered by the Review Focus tests plus Task 6's depth/fan-out tests and Task 5's cascade test. §8 testing list → every bullet maps onto a task's tests one-to-one (scheduler/startup-recovery/delegation/dashboard/end-to-end).
- **Step scan:** passed — code steps give exact signatures, names, and defaults; bodies are left to the implementer except where an algorithm is pinned (the queue-position formula, the `_on_task_terminal` resume rule, the depth/fan-out check placement), each called out explicitly as a plan-level decision rather than left implicit.
- **Type consistency:** `Scheduler.enqueue(session, task_id, front)`, `ToolContext(task_id, session, scheduler)`, `run_task_loop(..., scheduler, interrupt_event)`, `delegate_task_tool`/`check_subtask_tool` names and argument shapes are used identically across Tasks 2, 3, 4, 5, 6.
- **Review Focus:** all five items have an owning task and test — cap-enforcement-with-overflow (Task 2 Step 1), blocking-parent-never-resolves (implicitly proven safe by Task 6's design: `awaiting_subtask` has no timeout anywhere in the plan, matching the spec's accepted gap — no test asserts a *lack* of behavior, so this is a design-level satisfaction, not a test; noting that here rather than silently skipping it), kill-switch cascade to descendants (Task 5 Step 5), depth-limit-refuses-before-creating (Task 6 Step 5), dashboard `done` pagination boundary (Task 7 Step 5).
- **Proportion:** this plan is long because it spans a real concurrency primitive, a new control mechanism (kill switch), a new agent capability (delegation), and two new endpoints plus UI — proportionate to the spec it implements (also one of the longer specs), and the bulk of its length is test code/signatures, not prose.
