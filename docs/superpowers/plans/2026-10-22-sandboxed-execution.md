# Sandboxed Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give agents a real, persistent per-task Docker dev environment (shell/file tools, resource limits, egress control), a sandbox-specific concurrency cap that never starves the general Scheduler cap, an idle-container reaper, tiered worker/reviewer models with stuck-detection escalation and a bounded post-task review, and a shared per-task-isolated headless browser.

**Architecture:** A new `SandboxManager` owns container/volume lifecycle behind a small `ContainerRuntime` protocol (a `FakeContainerRuntime` for tests, a `DockerContainerRuntime` for real deployment using the mounted host Docker socket). New sandboxed tools (`run_shell`/`read_file`/`write_file`/`edit_file`, a re-routed `http_get`) execute through it. The Scheduler gains a second, independent concurrency cap for sandboxed tasks, with a non-blocking slot-acquisition mechanism that pauses a task (`awaiting_sandbox_slot`) rather than wasting its general-cap slot while it waits. Model tiering extends `run_task_loop` to pick between a cheap `worker_adapter` and a pricier `reviewer_adapter` per step.

**Tech Stack:** Same as prior sub-projects (FastAPI, SQLAlchemy, pytest, asyncio), plus the `docker` Python SDK (real runtime only — not exercised against a live daemon in this environment; tests use the fake runtime) and Playwright (already a project dependency per the platform spec's browser tool).

**Spec:** `docs/superpowers/specs/2026-10-22-sandboxed-execution-design.md`

## Global Constraints

- Per-container resource limits are concrete, not just documented: `--memory=2g` (`SANDBOX_MEMORY_LIMIT`), `--cpus=1` (`SANDBOX_CPU_LIMIT`), 5GB disk (`SANDBOX_DISK_LIMIT`) — spec §3.
- `SANDBOX_MAX_CONCURRENCY` defaults to 2, independent of the Scheduler's general cap (default 3) — spec §3/§5.
- `SANDBOX_IDLE_TIMEOUT` defaults to 4 hours; the reaper auto-commits uncommitted changes before stopping a container, never silently discards work — spec §3.
- Docker access is via a mounted host socket (`/var/run/docker.sock`), not Docker-in-Docker — spec §3 "Docker access".
- A task's sandbox need is discovered lazily; if the sandbox cap is full, the task releases its general Scheduler slot and pauses as `awaiting_sandbox_slot` instead of blocking while holding it — spec §3 "Cap interaction", §5.
- Model tiering is additive-cost-only: a task with no sandbox need runs with `reviewer_adapter` defaulted to the same instance as `worker_adapter` — spec §3.
- The post-task review pass runs **exactly once**, with **exactly one** resulting fix step if it finds a problem, then the task completes regardless — never an open-ended loop — spec §4.4.
- Stuck detection threshold: `STUCK_REPEAT_THRESHOLD` = 3 repeats of the same tool + same error signature — spec §4.3.
- Credentials injected into a sandbox container are short-lived and scoped to exactly the services that task's credential set allows, never the whole vault — spec §3 "Credential vault access" (the vault itself is a later sub-project; this plan stubs the injection point but does not build the vault).

## Review Focus

- **Two tasks needing a sandbox submitted back-to-back with the sandbox cap already full (1)** — the 2nd must release its general Scheduler slot and pause as `awaiting_sandbox_slot`, not block holding that slot, and must actually resume once a slot frees.
- **A `run_shell` command that writes megabytes of output** — the tool result must be truncated/bounded, not stored or replayed into the model's message history unbounded (an analogous truncation already exists for `http_get`'s response body).
- **The idle reaper firing while a container is mid-`run_shell` command (not actually idle, just between polls)** — must not stop a container that's genuinely executing something; "paused" is defined by `Task.status`, not by "no recent activity timestamp", so this should already be safe, but needs a test proving it.
- **A kill landing on a task that's paused `awaiting_sandbox_slot` (not currently running, no live interrupt_event)** — must be treated like any other `WAITING_STATUSES` task (marked `"interrupted"` directly), not silently ignored because it isn't in `self._interrupt_events`.
- **Egress allowlist with an empty/misconfigured list** — must fail closed (block everything) rather than fail open (allow everything) if the configured allowlist is empty or unparseable.

---

## File Structure

```
backend/
  app/
    sandbox/
      __init__.py
      runtime.py             # CREATE: ContainerRuntime protocol,
                              #   FakeContainerRuntime, DockerContainerRuntime,
                              #   SandboxSlotUnavailable exception
      manager.py              # CREATE: SandboxManager (lifecycle, slot
                              #   acquisition/wait-queue, idle reaper)
    agent/
      tools.py                 # MODIFY: ToolContext gains sandbox_manager
                              #   field; new run_shell/read_file/write_file/
                              #   edit_file tools; http_get re-routed through
                              #   the sandbox
      loop.py                   # MODIFY: catches SandboxSlotUnavailable ->
                              #   awaiting_sandbox_slot pause; adapter
                              #   selection (worker/reviewer), stuck
                              #   detection, bounded post-task review
      model_tiering.py           # CREATE: stuck-detection signature tracking,
                              #   adapter selection helper
    scheduler.py                 # MODIFY: constructs/owns a SandboxManager;
                              #   wires its re-enqueue-on-slot-freed callback;
                              #   container teardown folded into _run_one's
                              #   finally and _kill_subtrees
    api/
      tasks.py                   # MODIFY: register new sandbox tools in
                              #   get_registry(); wire worker/reviewer
                              #   adapter factories
    browser/
      __init__.py
      service.py                 # CREATE: shared Playwright browser process,
                              #   per-task browser context, browse/
                              #   download_file tool
  tests/
    test_sandbox_runtime.py        # CREATE
    test_sandbox_manager.py         # CREATE
    test_sandbox_tools.py            # CREATE
    test_scheduler.py                 # MODIFY: cap-interaction tests
    test_loop.py                       # MODIFY: awaiting_sandbox_slot pause,
                                    #   adapter tiering, stuck detection,
                                    #   post-task review
    test_model_tiering.py                # CREATE
    test_browser_service.py                # CREATE
```

## Interfaces reference (cross-task contract)

- `ContainerRuntime` protocol: `create_volume(name) -> None`, `remove_volume(name) -> None`, `start_container(name, volume, image, memory_limit, cpu_limit, network) -> str` (container id), `exec(container_id, command, timeout) -> tuple[int, str]` (exit code, combined stdout+stderr, truncated to a bounded length — e.g. 4000 chars — inside the runtime implementation itself, not left to callers), `stop_container(container_id) -> None`, `remove_container(container_id) -> None`.
- `SandboxSlotUnavailable(Exception)` — raised by `SandboxManager.get_or_create_container(task_id)` when no sandbox slot is available and this task doesn't already hold one.
- `SandboxManager(runtime: ContainerRuntime, session_factory, scheduler=None, cap: int = 2, image="agents-sandbox:latest", memory_limit="2g", cpu_limit=1.0, idle_timeout_seconds=14400)`:
  - `get_or_create_container(task_id: int) -> str` (container id; raises `SandboxSlotUnavailable` per above)
  - `run_shell(task_id: int, command: str, timeout: int = 120) -> dict` (`{"ok", "output", "data"}` shape, matching existing `Tool.run` results)
  - `read_file(task_id, path) -> dict`, `write_file(task_id, path, content) -> dict`, `edit_file(task_id, path, old, new) -> dict`
  - `teardown(task_id: int) -> None` (stop+remove container and volume; releases the slot, triggering the next waiting task's re-enqueue if any)
  - `reap_idle(session: Session) -> None` (one sweep; auto-commits then stops — not removes — any container whose task has been in `WAITING_STATUSES` longer than `idle_timeout_seconds`; does not release the concurrency slot, since the task may resume)
- `ToolContext` gains: `sandbox_manager: object | None = None`.
- `run_task_loop(..., worker_adapter: ModelAdapter | None = None, reviewer_adapter: ModelAdapter | None = None, sandbox_manager=None, ...)` — `worker_adapter` replaces the single `adapter` parameter (Task 6); `reviewer_adapter` defaults to `worker_adapter` when not given (Task 6); `sandbox_manager` (Task 7) is how the loop knows this task has an active sandbox container, gating the post-task review pass.
- `model_tiering.should_escalate(session, task_id) -> bool`, `model_tiering.record_tool_result(session, task_id, tool_name, result) -> None`.

---

### Task 1: Container runtime abstraction (fake + real)

**Files:**
- Create: `backend/app/sandbox/__init__.py`, `backend/app/sandbox/runtime.py`
- Test: `backend/tests/test_sandbox_runtime.py`

**Interfaces:**
- Consumes: none.
- Produces: `ContainerRuntime` protocol, `SandboxSlotUnavailable`, `FakeContainerRuntime` (an in-memory runtime backed by real local temp directories per volume — `exec` runs the given command as a real subprocess with `cwd` set to that temp directory, so `run_shell`/file tools are genuinely exercised in tests without needing Docker), `DockerContainerRuntime` (thin wrapper around the `docker` Python SDK — not exercised against a live daemon in this environment's tests, but structured so its methods are unit-testable via a mocked `docker.DockerClient`).

- [ ] **Step 1: Write the failing test: `FakeContainerRuntime` creates a volume (temp dir), starts a container (returns an id), and `exec` runs a real shell command against that dir's contents**

```python
def test_fake_runtime_exec_runs_in_volume_dir(tmp_path, monkeypatch):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    runtime.create_volume("vol-1")
    container_id = runtime.start_container("c-1", "vol-1", "agents-sandbox:latest", "2g", 1.0, "sandbox-net")
    exit_code, output = runtime.exec(container_id, "echo hello > out.txt && cat out.txt", timeout=10)
    assert exit_code == 0
    assert "hello" in output
```

- [ ] **Step 2: Run test to verify it fails** — Run: `cd backend && PYTHONPATH=. python3 -m pytest tests/test_sandbox_runtime.py -v` — Expected: FAIL (not defined).
- [ ] **Step 3: Implement `FakeContainerRuntime`** — one line on approach: `create_volume` makes `base_dir/name`; `start_container` just records `container_id -> volume dir` in an internal dict (no real process stays running — a "container" here is just a directory plus a record of its resource-limit args, for later assertion in tests); `exec` runs `subprocess.run(command, shell=True, cwd=volume_dir, timeout=timeout, capture_output=True, text=True)`, truncates combined stdout+stderr to 4000 chars, returns `(returncode, truncated_output)`; `stop_container`/`remove_container` just remove the container_id record (and the volume dir for `remove_volume`).
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: `exec`'s output is truncated to 4000 chars for a command producing more**

```python
def test_fake_runtime_truncates_long_output(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    runtime.create_volume("vol-2")
    container_id = runtime.start_container("c-2", "vol-2", "img", "2g", 1.0, "net")
    _, output = runtime.exec(container_id, "python3 -c \"print('x' * 10000)\"", timeout=10)
    assert len(output) <= 4000
```

- [ ] **Step 6: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 7: Add truncation to `exec`.**
- [ ] **Step 8: Run test to verify it passes.**
- [ ] **Step 9: Implement `DockerContainerRuntime`** — a class with the same method signatures delegating to `docker.from_env()` (imported lazily inside `__init__`, so importing `runtime.py` never requires the `docker` package to be installed unless this class is actually instantiated); `start_container` passes `mem_limit=memory_limit`, `nano_cpus=int(cpu_cpu_limit * 1e9)`, `network=network`, a bind-mounted named volume at `/workspace`, and `working_dir="/workspace"`; `exec` uses `container.exec_run(command, demux=False)` with the same 4000-char truncation as the fake runtime (extract the truncation into a small shared helper both classes call, to avoid duplicating the limit). No test exercises this against a live daemon (none is available in this environment) — write one test that mocks `docker.from_env()` and asserts `start_container` is called with the exact resource-limit kwargs above, so the config values are at least pinned by a test.
- [ ] **Step 10: Write that mocked test, run it, confirm it fails, implement, confirm it passes.**
- [ ] **Step 11: Commit**

```bash
git add backend/app/sandbox/ backend/tests/test_sandbox_runtime.py
git commit -m "feat: container runtime abstraction (fake + Docker-backed)"
```

---

### Task 2: SandboxManager lifecycle, slot cap, and teardown

**Files:**
- Create: `backend/app/sandbox/manager.py`
- Test: `backend/tests/test_sandbox_manager.py`

**Interfaces:**
- Consumes: `ContainerRuntime`/`FakeContainerRuntime`/`SandboxSlotUnavailable` (Task 1).
- Produces: `SandboxManager` as in the Interfaces reference, minus `reap_idle` (Task 5 fills that in — a no-op/absent method is fine here) and minus any Scheduler-side wiring (Task 3 does that). `get_or_create_container` acquires a slot the first time it's called for a given `task_id` (tracked in `self._slots: dict[int, str]` task_id->container_id) and raises `SandboxSlotUnavailable` if the cap (`len(self._slots)`) is already at `cap` and this task doesn't already hold a slot; a waiting task_id is recorded in `self._waiting: list[int]` (FIFO) at that point. `teardown(task_id)` removes the slot, and if `self._waiting` is non-empty, pops the next task_id and — if `self.scheduler` was provided — calls `self.scheduler.enqueue(self.session_factory(), waiting_task_id, front=True)` (closing that session after).

- [ ] **Step 1: Write the failing test: `get_or_create_container` returns a container id on first call, reuses it on a second call for the same task**

```python
def test_get_or_create_reuses_existing_container(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=None, cap=2)
    first = manager.get_or_create_container(task_id=1)
    second = manager.get_or_create_container(task_id=1)
    assert first == second
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 3: Implement `get_or_create_container`, `__init__`.**
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: a 3rd task raises `SandboxSlotUnavailable` when cap=2 and two other tasks already hold slots**

```python
def test_third_task_raises_slot_unavailable_at_cap(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=None, cap=2)
    manager.get_or_create_container(task_id=1)
    manager.get_or_create_container(task_id=2)
    with pytest.raises(SandboxSlotUnavailable):
        manager.get_or_create_container(task_id=3)
```

- [ ] **Step 6: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 7: Add the cap check and waiting-list recording.**
- [ ] **Step 8: Run test to verify it passes.**
- [ ] **Step 9: Write the failing test: `teardown` frees the slot and re-enqueues the next waiting task via the scheduler**

```python
def test_teardown_frees_slot_and_reenqueues_waiting_task(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    enqueued = []
    fake_scheduler = SimpleNamespace(enqueue=lambda session, task_id, front: enqueued.append((task_id, front)))
    manager = SandboxManager(runtime, session_factory=lambda: None, scheduler=fake_scheduler, cap=1)
    manager.get_or_create_container(task_id=1)
    with pytest.raises(SandboxSlotUnavailable):
        manager.get_or_create_container(task_id=2)
    manager.teardown(task_id=1)
    assert enqueued == [(2, True)]
```

- [ ] **Step 10: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 11: Implement `teardown`'s slot-release and re-enqueue logic.**
- [ ] **Step 12: Run test to verify it passes.**
- [ ] **Step 13: Commit**

```bash
git add backend/app/sandbox/manager.py backend/tests/test_sandbox_manager.py
git commit -m "feat: sandbox manager with slot cap and wait-queue re-enqueue"
```

---

### Task 3: Sandboxed tools + `awaiting_sandbox_slot` loop integration

**Files:**
- Modify: `backend/app/agent/tools.py`, `backend/app/agent/loop.py`, `backend/app/models.py` (no new columns — `status` already a free-text string, just a new value used)
- Test: `backend/tests/test_sandbox_tools.py`, `backend/tests/test_loop.py` (extend)

**Interfaces:**
- Consumes: `SandboxManager` (Task 2), `ToolContext.sandbox_manager` (new field, added in this task — not pre-existing, same pattern as `ToolContext.scheduler` in the multi-agent-runtime plan).
- Produces: `run_shell_tool`, `read_file_tool`, `write_file_tool`, `edit_file_tool` (each calling the matching `SandboxManager` method via `ctx.sandbox_manager`, returning its `{"ok","output","data"}` result directly); `http_get_tool` modified to call `ctx.sandbox_manager.run_shell(ctx.task_id, f"curl -sS {shlex.quote(url)}")` instead of `httpx.get` directly (re-routed through the sandbox's egress-controlled network, per spec §3) — falls back to today's direct `httpx.get` behavior only if `ctx.sandbox_manager` is `None` (so non-sandboxed tasks/tests from earlier sub-projects keep working unchanged). `run_task_loop` catches `SandboxSlotUnavailable` specifically around the tool-execution try/except (a new except clause, separate from the existing `KeyError/TypeError/ValueError/httpx.HTTPError` tuple), setting `task.status = "awaiting_sandbox_slot"` and returning (after the same `_interrupt_if_requested` check used by the other pause points, per the Review Focus item on killing a slot-waiting task).

- [ ] **Step 1: Write the failing test: `run_shell_tool.run` executes a command via the sandbox manager and returns its result**

```python
def test_run_shell_tool_executes_via_sandbox_manager(tmp_path):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: None, cap=2)
    ctx = ToolContext(task_id=1, session=None, sandbox_manager=manager)
    result = run_shell_tool.run({"command": "echo hi"}, ctx)
    assert result["ok"] is True
    assert "hi" in result["output"]
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 3: Implement `run_shell_tool`/`read_file_tool`/`write_file_tool`/`edit_file_tool`** (each a thin wrapper delegating to the matching `SandboxManager` method; `read_file`/`write_file`/`edit_file` on `SandboxManager` can be implemented as `run_shell` calls under the hood — e.g. `write_file` as `cat > path <<'EOF'\n...\nEOF`, `read_file` as `cat path`, `edit_file` as a small inline Python one-liner doing a string replace and rewriting the file — your choice of exact shell incantation, as long as it's robust to the content containing quotes/special characters; prefer writing content to a temp file and using `docker cp`-equivalent or piping via stdin over naive shell-quoting a whole file's content inline).
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: `http_get_tool` with a sandbox manager present routes through `run_shell`/`curl`, not `httpx.get`, and still returns the expected shape**

```python
def test_http_get_routes_through_sandbox_when_manager_present(tmp_path, monkeypatch):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: None, cap=2)
    ctx = ToolContext(task_id=1, session=None, sandbox_manager=manager)
    calls = []
    monkeypatch.setattr(manager, "run_shell", lambda task_id, command, timeout=120: calls.append(command) or {"ok": True, "output": "status=200", "data": None})
    result = http_get_tool.run({"url": "http://example.com"}, ctx)
    assert result["ok"] is True
    assert calls  # routed through run_shell, not httpx.get directly
```

- [ ] **Step 6: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 7: Modify `http_get_tool.run`** to branch on `ctx.sandbox_manager is not None`.
- [ ] **Step 8: Run test to verify it passes; confirm the existing (pre-sandbox) `http_get` tests without a `sandbox_manager` still pass unchanged** — Run: `cd backend && PYTHONPATH=. python3 -m pytest tests/test_tools.py -v`.
- [ ] **Step 9: Write the failing test: the loop pauses with `awaiting_sandbox_slot` when a sandboxed tool call raises `SandboxSlotUnavailable`**

```python
def test_loop_pauses_on_sandbox_slot_unavailable(db_session, task):
    class _AlwaysUnavailableTool:
        name = "run_shell"
        description = "x"
        parameters_schema = {"type": "object", "properties": {}}
        def run(self, arguments, ctx):
            raise SandboxSlotUnavailable(ctx.task_id)

    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "run_shell", "arguments": {}},
    ])
    registry = ToolRegistry()
    registry.register(_AlwaysUnavailableTool())
    run_task_loop(task.id, db_session, adapter, registry)
    assert db_session.get(Task, task.id).status == "awaiting_sandbox_slot"
```

- [ ] **Step 10: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 11: Add the `SandboxSlotUnavailable` except clause to `run_task_loop`**, mirroring the `ask_user`/`awaiting_approval` pause pattern (interrupt check, then set status and return).
- [ ] **Step 12: Run test to verify it passes, then run the full loop test file** — Run: `cd backend && PYTHONPATH=. python3 -m pytest tests/test_loop.py -v` — Expected: all PASS.
- [ ] **Step 13: Commit**

```bash
git add backend/app/agent/tools.py backend/app/agent/loop.py backend/tests/test_sandbox_tools.py backend/tests/test_loop.py
git commit -m "feat: sandboxed shell/file tools, re-routed http_get, awaiting_sandbox_slot pause"
```

---

### Task 4: Wire SandboxManager into the Scheduler (teardown, kill, resume)

**Files:**
- Modify: `backend/app/scheduler.py`, `backend/app/api/tasks.py` (register sandbox tools, construct/pass `SandboxManager`)
- Test: `backend/tests/test_scheduler.py` (extend)

**Interfaces:**
- Consumes: `SandboxManager` (Task 2), `SandboxSlotUnavailable`/`awaiting_sandbox_slot` (Task 3).
- Produces: `Scheduler.__init__` gains an optional `sandbox_manager: SandboxManager | None = None` parameter, passed into `ToolContext` construction wherever the Scheduler builds one for a task run (the `registry_factory`/tool wiring path — same place `scheduler=self` is already threaded through). `_run_one`'s `finally` block calls `self.sandbox_manager.teardown(task_id)` when the task reached a terminal state (`completed`/`failed`/`interrupted`) — not on every pause, since a paused task (including `awaiting_sandbox_slot` itself, which by definition never acquired a slot) should keep its container alive across an `awaiting_answer`/`awaiting_approval`/`awaiting_subtask` pause. **Note on session lifetime**: by the point `finally` runs its existing cleanup, `session` (the one passed into `run_fn`) has already been closed by an earlier line in the same `finally` block — do not read `Task.status` from it. Open a **fresh** session (`self.session_factory()`) to check the task's current status, same as `_on_task_terminal` already does for its own lookup immediately after, and close that fresh session when done. `WAITING_STATUSES` (in `scheduler.py`) gains `"awaiting_sandbox_slot"`, so `_mark_waiting_tasks_interrupted` (kill handling) already covers it per the Review Focus item — no additional change needed there beyond the set literal.

- [ ] **Step 1: Write the failing test: `_run_one` tears down the sandbox container when a task completes, but not when it merely pauses**

```python
async def test_run_one_tears_down_sandbox_only_on_terminal_status(session_factory):
    teardown_calls = []
    fake_sandbox = SimpleNamespace(teardown=lambda task_id: teardown_calls.append(task_id))

    async def fake_run_completes(task_id, session, adapter, registry, scheduler=None, interrupt_event=None):
        t = session.get(Task, task_id)
        t.status = "completed"
        session.commit()

    scheduler = Scheduler(session_factory, lambda: None, lambda: None, cap=1, run_fn=fake_run_completes, sandbox_manager=fake_sandbox)
    task_id = make_queued_task(session_factory())
    await scheduler._run_one(task_id, threading.Event())
    assert teardown_calls == [task_id]
```

(a second case in the same test file covers the "pauses, not torn down" branch similarly, with `fake_run_pauses` setting `status="awaiting_answer"` and asserting `teardown_calls == []`)

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 3: Add the `sandbox_manager` parameter and the terminal-status teardown call in `_run_one`'s `finally`** (check `session.get(Task, task_id).status in {"completed", "failed", "interrupted"}` before calling teardown; guard for `self.sandbox_manager is not None` since not every deployment/test wires one).
- [ ] **Step 4: Run both cases, then the full scheduler test file** — Run: `cd backend && PYTHONPATH=. python3 -m pytest tests/test_scheduler.py -v` — Expected: all PASS.
- [ ] **Step 5: Add `"awaiting_sandbox_slot"` to `WAITING_STATUSES`.**
- [ ] **Step 6: Write the failing test: killing a task parked in `awaiting_sandbox_slot` marks it `"interrupted"` (via the existing waiting-task kill path, now covering this status too)** — a small direct test calling `_mark_waiting_tasks_interrupted` on a task seeded with that status, asserting it flips to `"interrupted"`.
- [ ] **Step 7: Run test to verify it fails, then passes once Step 5 is in place** (it should pass immediately once the set literal is updated — this step exists to prove it, per the Review Focus item, not to add new logic).
- [ ] **Step 8: Register the four new sandbox tools (`run_shell_tool`, `read_file_tool`, `write_file_tool`, `edit_file_tool`) in `get_registry()`** in `backend/app/api/tasks.py`, and construct a `SandboxManager` (using `DockerContainerRuntime` in the real `get_scheduler`-equivalent wiring path) passed into the `Scheduler` constructor — mirror whatever lazy-override-resolution pattern the multi-agent-runtime plan established for `adapter_factory`/`registry_factory`, so tests can still override it per-test with a fake.
- [ ] **Step 9: Run the full backend suite** — Run: `cd backend && PYTHONPATH=. python3 -m pytest -q` — Expected: all PASS.
- [ ] **Step 10: Commit**

```bash
git add backend/app/scheduler.py backend/app/api/tasks.py backend/tests/test_scheduler.py
git commit -m "feat: wire sandbox manager into scheduler lifecycle and kill handling"
```

---

### Task 5: Idle-container reaper

**Files:**
- Modify: `backend/app/sandbox/manager.py`, `backend/app/main.py` (start a periodic reaper task in the lifespan, alongside the Scheduler)
- Test: `backend/tests/test_sandbox_manager.py` (extend)

**Interfaces:**
- Consumes: `WAITING_STATUSES` concept (Task 4), `SandboxManager` internals (Task 2).
- Produces: `SandboxManager.reap_idle(session: Session) -> None` as described in the Interfaces reference — for each `task_id` in `self._slots`, loads the `Task`, and if its `status` is in `{"awaiting_answer", "awaiting_approval", "awaiting_subtask", "awaiting_sandbox_slot"}` and `Event`-log evidence (or a tracked `self._paused_since: dict[int, float]` timestamp, set when the task's status is observed to have changed into a waiting state — simplest: record `time.monotonic()` the first time `reap_idle` sees a given task_id in a waiting status, clear it when the status is no longer waiting) shows it's been paused longer than `idle_timeout_seconds`: runs `self.run_shell(task_id, "git add -A && git commit -m 'auto-commit before idle reap' || true")` (the `|| true` so "nothing to commit" doesn't count as a failure), then `stop_container` (not `remove_container` — the volume and container both stay, just stopped, cheaper to resume than a full teardown+recreate, and avoids re-triggering the slot wait-queue logic since the slot itself is NOT released by an idle stop).

- [ ] **Step 1: Write the failing test: a task paused longer than the idle timeout gets auto-committed and its container stopped (not removed)**

```python
def test_reap_idle_commits_and_stops_long_paused_task(tmp_path, db_session):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: db_session, cap=2, idle_timeout_seconds=0)
    task = Task(description="x", status="awaiting_answer", full_auto=False)
    db_session.add(task); db_session.commit()
    container_id = manager.get_or_create_container(task.id)
    (tmp_path / manager._volume_name(task.id) / "file.txt").write_text("hello")
    manager.reap_idle(db_session)
    assert runtime.is_stopped(container_id)
    # committed, not lost: a fresh container on the same volume still has it
    assert (tmp_path / manager._volume_name(task.id) / "file.txt").exists()
```

(`runtime.is_stopped` and `manager._volume_name` are small additions the implementer adds to `FakeContainerRuntime`/`SandboxManager` as needed to make this assertion checkable — not otherwise specified, use your judgment on the simplest shape.)

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 3: Implement `reap_idle`.**
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: a task that's been paused for less than the timeout is NOT reaped** (same shape, `idle_timeout_seconds` large, assert `runtime.is_stopped(container_id)` is `False`).
- [ ] **Step 6: Run test to verify it fails, then passes** (should already pass given Step 3's time-comparison logic — this step exists to pin the "not yet idle" branch with its own test, per the plan's Review Focus item on not reaping active work).
- [ ] **Step 7: Wire a periodic call to `reap_idle`** into `main.py`'s `lifespan` — a simple `asyncio.create_task` loop sleeping `REAPER_POLL_INTERVAL_SECONDS` (e.g. 60s) between sweeps, started alongside `scheduler.start()` and cancelled alongside `scheduler.stop()`.
- [ ] **Step 8: Run the full backend suite** — Expected: all PASS.
- [ ] **Step 9: Commit**

```bash
git add backend/app/sandbox/manager.py backend/app/main.py backend/tests/test_sandbox_manager.py
git commit -m "feat: idle-container reaper with auto-commit before stop"
```

---

### Task 6: Model tiering — worker/reviewer adapters + stuck detection

**Files:**
- Create: `backend/app/agent/model_tiering.py`
- Modify: `backend/app/agent/loop.py`
- Test: `backend/tests/test_model_tiering.py`, `backend/tests/test_loop.py` (extend)

**Interfaces:**
- Consumes: `ModelAdapter` protocol (existing, sub-project 1).
- Produces: `model_tiering.ToolFailureTracker` — a small class with `record(tool_name: str, error_signature: str) -> int` (returns the current consecutive-repeat count for that exact tool+signature pair, resetting to 1 whenever a *different* tool/signature is recorded) and `should_escalate() -> bool` (`True` once the most recent `record()` call's returned count reaches `STUCK_REPEAT_THRESHOLD` = 3). `run_task_loop`'s signature changes to `run_task_loop(task_id, session, worker_adapter, registry, scheduler=None, interrupt_event=None, reviewer_adapter=None)` — `reviewer_adapter` defaults to `worker_adapter` if not given (per the Global Constraints additive-cost rule). Each step picks `reviewer_adapter` instead of `worker_adapter` when a `ToolFailureTracker` (one instance per `run_task_loop` call, tracking only that call's own history — it does not need to persist across pause/resume, since a resumed task gets a fresh tracker and re-derives its recent-failure picture from replayed `tool_result` events if you choose to seed it that way, or simply starts clean on resume — your call, document which you choose) says to escalate.

- [ ] **Step 1: Write the failing test: three consecutive identical tool-failure signatures trigger escalation; a fourth call with a different error does not inherit the escalated count**

```python
def test_tracker_escalates_after_three_identical_failures():
    tracker = ToolFailureTracker()
    assert tracker.record("run_shell", "ModuleNotFoundError: foo") == 1
    assert tracker.should_escalate() is False
    tracker.record("run_shell", "ModuleNotFoundError: foo")
    assert tracker.record("run_shell", "ModuleNotFoundError: foo") == 3
    assert tracker.should_escalate() is True
    assert tracker.record("run_shell", "SyntaxError: bar") == 1
    assert tracker.should_escalate() is False
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 3: Implement `ToolFailureTracker`.**
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: `run_task_loop` with a failing tool and a tracker-driven escalation calls `reviewer_adapter.complete`, not `worker_adapter.complete`, on the escalated step**

```python
def test_loop_escalates_to_reviewer_after_repeated_failures(db_session, task):
    class _AlwaysFailsTool:
        name = "run_shell"
        description = "x"
        parameters_schema = {"type": "object", "properties": {}}
        def run(self, arguments, ctx):
            raise ValueError("same error every time")

    worker = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "run_shell", "arguments": {}},
    ] * 3)
    reviewer = FakeModelAdapter(script=[{"type": "final_answer", "content": "giving up"}])
    registry = ToolRegistry()
    registry.register(_AlwaysFailsTool())
    run_task_loop(task.id, db_session, worker, registry, reviewer_adapter=reviewer)
    assert db_session.get(Task, task.id).status == "completed"  # reviewer's final_answer was used
```

- [ ] **Step 6: Run test to verify it fails** — Expected: FAIL (signature mismatch / reviewer never called).
- [ ] **Step 7: Implement the per-step adapter selection** — one line on approach: instantiate one `ToolFailureTracker` at the top of `run_task_loop`; after each failed `tool_result` (the existing `except (KeyError, TypeError, ValueError, httpx.HTTPError)` branch, plus the `SandboxSlotUnavailable` case does NOT count as a failure for this tracker — it's a scheduling pause, not the model being stuck), call `tracker.record(tool_name, str(exc))`; at the top of the loop, pick `adapter = reviewer_adapter if tracker.should_escalate() else worker_adapter` for that step's `adapter.complete(...)` call. Rename the existing parameter from `adapter` to `worker_adapter` throughout the function, updating every call site in the codebase (the Scheduler's `_run_one`, any direct test calls) to match.
- [ ] **Step 8: Wire a real, distinct reviewer adapter in production** — without this step, `reviewer_adapter` only ever gets a real value in tests that pass one explicitly; the Scheduler/API layer never supplies one, so tiering would be dead code in an actual deployment. Add `reviewer_adapter_factory: Callable` as a new `Scheduler.__init__` parameter (alongside the existing `adapter_factory`, which is now understood as the *worker* factory — no rename needed on the Scheduler side, just a doc comment), and have `_run_one` call both factories and pass `reviewer_adapter=self.reviewer_adapter_factory()` into `run_fn`. In `backend/app/api/tasks.py`, add a `get_reviewer_adapter()` dependency function (same `OpenAICompatAdapter` shape as the existing `get_adapter()`, but reading `REVIEWER_MODEL_API_BASE`/`REVIEWER_MODEL_API_KEY`/`REVIEWER_MODEL_NAME` env vars — falling back to the same values as `get_adapter()`'s if the `REVIEWER_*` ones are unset, so an operator who hasn't configured a separate reviewer model yet gets the additive-cost-free default from the Global Constraints, not a crash), and thread it through the same lazy-override-resolution lambda pattern `main.py`'s `lifespan` already uses for `adapter_factory`/`registry_factory`.
- [ ] **Step 9: Write a test confirming `_run_one` calls `reviewer_adapter_factory` and passes its result through** (same shape as any existing `Scheduler` factory test — assert the fake `run_fn` it drives received the expected `reviewer_adapter` object).
- [ ] **Step 10: Run that test, then the full loop test file and full backend suite** — Run: `cd backend && PYTHONPATH=. python3 -m pytest -q` — Expected: all PASS (this step touches a widely-used function signature, so confirm nothing else broke).
- [ ] **Step 11: Commit**

```bash
git add backend/app/agent/model_tiering.py backend/app/agent/loop.py backend/app/scheduler.py backend/tests/test_model_tiering.py backend/tests/test_loop.py
git commit -m "feat: worker/reviewer model tiering with stuck-step escalation"
```

---

### Task 7: Bounded post-task review pass

**Files:**
- Modify: `backend/app/agent/loop.py`
- Test: `backend/tests/test_loop.py` (extend)

**Interfaces:**
- Consumes: `reviewer_adapter` (Task 6).
- Produces: `run_task_loop` gains one more optional parameter, `sandbox_manager=None` (mirroring how `scheduler`/`interrupt_event`/`reviewer_adapter` were each added as one new optional kwarg in earlier sub-projects — this is the explicit mechanism for the loop to know "this task has an active sandbox container," replacing the vaguer "reuse SandboxManager presence" phrasing). Before `run_task_loop` commits `status="completed"` on reaching `final_answer`, if `reviewer_adapter is not worker_adapter` (i.e. tiering is actually active for this task — a non-sandboxed task where they're the same instance skips this entirely, per the additive-cost constraint) **and** `sandbox_manager is not None` (the review pass is specifically about reviewing code changes, so it only makes sense when there's a container with a `git diff` to look at): run exactly one extra `reviewer_adapter.complete(...)` call with a review-framed prompt (a system/user message asking it to review the sandbox's `git diff` against the task description, appended as a one-off addition to `messages`, not persisted as a replayed event type — simplest: treat its response as a `tool_call` to `report_gap` if it finds a problem, or a plain message otherwise). If the reviewer's response amounts to flagging a problem, run exactly one more `worker_adapter.complete(...)` step to address it (bounded — do not loop back into review again regardless of what that fix step produces), then complete the task regardless, same as before.

- [ ] **Step 1: Write the failing test: a task with a sandbox manager and distinct reviewer/worker adapters gets one extra reviewer call before completing, and completes regardless of the reviewer's verdict**

```python
def test_post_task_review_runs_once_then_completes_regardless(tmp_path, db_session, task):
    runtime = FakeContainerRuntime(base_dir=tmp_path)
    manager = SandboxManager(runtime, session_factory=lambda: db_session, cap=2)
    manager.get_or_create_container(task.id)  # task has a sandbox
    worker = FakeModelAdapter(script=[{"type": "final_answer", "content": "done"}])
    reviewer_calls = []
    class _RecordingReviewer:
        def complete(self, messages, tools):
            reviewer_calls.append(messages)
            return {"type": "final_answer", "content": "looks fine"}
    run_task_loop(
        task.id, db_session, worker, ToolRegistry(),
        reviewer_adapter=_RecordingReviewer(), sandbox_manager=manager,
    )
    assert len(reviewer_calls) == 1
    assert db_session.get(Task, task.id).status == "completed"
```

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL.
- [ ] **Step 3: Implement the bounded review pass**, gated exactly as described above.
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: a task with NO sandbox manager (or `reviewer_adapter is worker_adapter`) completes with no extra reviewer call** — same shape, assert `reviewer_calls == []` (or just assert the existing non-sandboxed completion tests from earlier sub-projects still pass unchanged, which is an equally valid way to prove this).
- [ ] **Step 6: Run test to verify it fails, then passes.**
- [ ] **Step 7: Run the full backend suite** — Expected: all PASS.
- [ ] **Step 8: Commit**

```bash
git add backend/app/agent/loop.py backend/tests/test_loop.py
git commit -m "feat: bounded one-pass post-task code review for sandboxed tasks"
```

---

### Task 8: Headless browser service

**Files:**
- Create: `backend/app/browser/__init__.py`, `backend/app/browser/service.py`
- Modify: `backend/app/agent/tools.py` (new `browse_tool`/`download_file_tool`), `backend/app/api/tasks.py` (register them)
- Test: `backend/tests/test_browser_service.py`, `backend/tests/test_sandbox_tools.py` (extend)

**Interfaces:**
- Consumes: Playwright (new dependency, add to `backend/requirements.txt`).
- Produces: `BrowserService` — `get_or_create_context(task_id: int)` (returns a Playwright `BrowserContext`, launching the shared `Browser` lazily on first use across the whole process, then one isolated `new_context()` per `task_id`, cached), `close_context(task_id: int) -> None`. `browse_tool` (name `"browse"`, params `{url: str}` — navigates the task's context to a page, returns `{"ok", "output": <page title + truncated visible text>, "data": None}`); `download_file_tool` (name `"download_file"`, params `{url: str, save_as: str}` — downloads via the task's browser context into the task's sandbox container's volume at `save_as`, so downloaded files are usable by `run_shell`/`read_file` afterward — requires calling into `SandboxManager` to know the volume path, so `ToolContext` needs both `sandbox_manager` and a way to reach `BrowserService`; add `browser_service: object | None = None` to `ToolContext`, same pattern as `scheduler`/`sandbox_manager`).

- [ ] **Step 1: Write the failing test: `get_or_create_context` returns the same context on a second call for the same task_id, a different one for a different task_id**

```python
def test_get_or_create_context_is_per_task(monkeypatch):
    service = BrowserService()
    # Patch the underlying Playwright browser launch to a lightweight fake
    # for this test (do not launch a real browser in unit tests) — your
    # judgment on the cleanest fake, e.g. a SimpleNamespace with a
    # new_context() method returning a distinguishable fake context object.
    ...
    ctx_a1 = service.get_or_create_context(task_id=1)
    ctx_a2 = service.get_or_create_context(task_id=1)
    ctx_b = service.get_or_create_context(task_id=2)
    assert ctx_a1 is ctx_a2
    assert ctx_a1 is not ctx_b
```

- [ ] **Step 2: Run test to verify it fails** — Run: `cd backend && PYTHONPATH=. python3 -m pytest tests/test_browser_service.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement `BrowserService`** (lazy `playwright.sync_api.sync_playwright().start()` + `.chromium.launch()` on first use, `self._contexts: dict[int, BrowserContext]`).
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: `close_context` removes a task's context so a later `get_or_create_context` call for that task_id returns a new one** (same faking approach as Step 1).
- [ ] **Step 6: Run test to verify it fails, then passes.**
- [ ] **Step 7: Implement `browse_tool`** (navigate, extract title/visible text, truncate like other tool outputs) **and `download_file_tool`** (use the context's request API to fetch the URL's bytes, write them via `ctx.sandbox_manager.write_file(ctx.task_id, save_as, <bytes-as-text-or-base64>)` — note `write_file`'s existing signature takes string `content`; for binary downloads, base64-encode and have the sandbox's shell-side write step decode it, or extend `SandboxManager.write_file` to accept bytes — your judgment on the cleanest extension, document the choice).
- [ ] **Step 8: Write a test for each new tool** following the existing tool-test pattern (`ToolContext` with a fake `BrowserService`/`SandboxManager`, assert the result shape).
- [ ] **Step 9: Run tests to verify they fail, then pass.**
- [ ] **Step 10: Wire teardown**: `Scheduler._run_one`'s terminal-status cleanup (Task 4) also calls `browser_service.close_context(task_id)` alongside `sandbox_manager.teardown(task_id)`, if a `browser_service` was wired in.
- [ ] **Step 11: Register `browse_tool`/`download_file_tool` in `get_registry()`**, add `playwright` to `backend/requirements.txt`.
- [ ] **Step 12: Run the full backend suite** — Run: `cd backend && PYTHONPATH=. python3 -m pytest -q` — Expected: all PASS.
- [ ] **Step 13: Commit**

```bash
git add backend/app/browser/ backend/app/agent/tools.py backend/app/api/tasks.py backend/app/scheduler.py backend/requirements.txt backend/tests/test_browser_service.py backend/tests/test_sandbox_tools.py
git commit -m "feat: headless browser service with per-task isolated contexts"
```

---

## Self-Review Notes

- **Spec coverage:** §3 components (Sandbox Manager, image, tools, browser service, egress allowlist, resource limits, credential access stub, idle reaper, concurrency cap, model tiering, Docker access, cap interaction) → Tasks 1–8 cover every one except the credential vault itself (explicitly noted in the spec as "the vault itself is a later sub-project" and in this plan's Global Constraints as stubbed, not built — no gap, a deliberate scope boundary). §4 model tiering → Tasks 6–7. §5 data flow → Tasks 3–5 (steps 1,4,5) and 6–7 (step 2). §6 error handling → Review Focus items plus Task 1's truncation and Task 2's cap-exceeded test cover most rows; the "container OOM-killed" and "egress allowlist blocks a host" rows are properties of the real `DockerContainerRuntime` (network config, Docker's own OOM killer) that this plan's fake-runtime-based tests cannot directly exercise — flagging this as an accepted gap, consistent with the spec's own testing section ("against a real local Docker daemon where available... otherwise a fake/mocked runtime"), not a silently dropped requirement. §7 testing list → each bullet maps onto a task's own tests. §8 build order → matches this plan's task order exactly (Tasks 1-2 ≈ build-order item 1, Task 3 ≈ item 2, Tasks 4-5 ≈ items 1/3, Tasks 6-7 ≈ item 4, Task 8 ≈ item 5 — slightly reordered for dependency reasons, e.g. Scheduler wiring needs the manager to exist first).
- **Step scan:** passed — each code step pins exact signatures/names/defaults; a few steps (Task 1 Step 3's exact shell-exec mechanics, Task 3 Step 3's file-tool shell incantations, Task 8's binary-download encoding) explicitly leave an implementation choice to the implementer while still being unambiguous about the required behavior and test — flagged inline as "your judgment" rather than silently underspecified.
- **Type consistency:** `SandboxManager(runtime, session_factory, scheduler=None, cap=2, ...)`, `ToolContext(task_id, session, scheduler=None, sandbox_manager=None, browser_service=None)`, `run_task_loop(task_id, session, worker_adapter, registry, scheduler=None, interrupt_event=None, reviewer_adapter=None)` are used identically across Tasks 2–8; `SandboxSlotUnavailable` is raised in Task 2/3 and caught in Task 3 with the same name/import path.
- **Review Focus:** all five items have an owning task and test — cap-exhaustion-pauses-not-blocks (Task 2 Step 5, Task 3 Step 9), unbounded output (Task 1 Step 5), idle-reaper-not-firing-on-active-work (Task 5 Step 5), kill-on-awaiting-sandbox-slot (Task 4 Step 6), egress-allowlist-fail-closed — **gap found and fixed inline**: no task explicitly tests the empty-allowlist-fails-closed case, since `DockerContainerRuntime`'s network config isn't unit-testable without a real daemon per the accepted gap above; added as an explicit manual-verification note in Task 1 rather than silently dropping it — **Task 1 should additionally document in its `DockerContainerRuntime` implementation a code comment and a startup-time config validation (fail to start the app if `SANDBOX_EGRESS_ALLOWLIST` is unset/empty while any sandbox feature is enabled) so "fail closed" is enforced by refusing to boot misconfigured, not left to a runtime network test this plan can't write** — adding this as Task 1 Step 12 below.
- **Proportion:** this plan is long because it spans a new subsystem (containers), a new cross-cutting concurrency primitive (the second cap + wait-queue), a loop signature change affecting every prior sub-project's call sites, and a new external service (browser) — proportionate to the spec's own length and complexity; the bulk of the length is test code and signatures, not prose duplication.

- [ ] **Task 1, Step 12 (added by self-review): implement fail-closed startup validation for the egress allowlist**

**Files:** Modify `backend/app/sandbox/runtime.py` (or a small `backend/app/sandbox/config.py` if cleaner), `backend/app/main.py` (call the validator during `lifespan`, before `scheduler.start()`).

- [ ] Write the failing test: a validator function raises/returns an error when `SANDBOX_EGRESS_ALLOWLIST` is unset or empty while sandbox features are enabled (e.g. a `SANDBOX_ENABLED` flag, or simply "whenever this app starts" if sandboxing is always-on in this deployment — your call per how `main.py` is structured by this point in the plan).
- [ ] Run test to verify it fails.
- [ ] Implement the validator (`validate_sandbox_egress_config() -> None`, raising `RuntimeError` with a clear message on misconfiguration) and call it from `lifespan` before anything else starts.
- [ ] Run test to verify it passes.
- [ ] Commit: `git commit -m "feat: fail closed at startup on an empty/missing egress allowlist"`.
