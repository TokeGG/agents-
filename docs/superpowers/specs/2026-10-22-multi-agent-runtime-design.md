# Multi-Agent Runtime — Design Spec

Date: 2026-10-22
Status: Approved by user, pending implementation planning
Sub-project: #2 in the platform build order (see
`docs/superpowers/specs/2026-10-21-ai-agent-platform-design.md` §11)

## 1. Purpose & Success Criteria

Today (agent core, sub-project 1), each task runs immediately via
`BackgroundTasks` with no cap on concurrency, no queue, and no way for one
task to call on another. This sub-project adds:

- A bounded, FIFO(+urgent) task queue with a configurable concurrency cap.
- A two-mode kill switch (pause new work vs. hard-stop everything).
- A dark-mode "Command Center" dashboard: live stat strip + card grid,
  with inline actions (answer/approve/deny/kill) without opening a task.
- **Delegation**: any running task can spawn a sub-task to another agent,
  either blocking (pause and resume with the result, like a foreground
  subagent call) or background (continue immediately, get notified later,
  like a backgrounded subagent call) — mirroring how Claude Code's own
  subagent-calling workflow works.

Success looks like: you can have several tasks running at once (within
the cap), a dashboard that shows and lets you act on all of them at a
glance, a kill switch that actually stops things, and tasks that can
delegate to other tasks without deadlocking the scheduler.

## 2. Scope Note

A privileged "orchestrator" agent that can modify and redeploy the
platform's own source code — raised during this brainstorm — is **out of
scope here**. It is queued as a future sub-project, "Platform
Self-Extension" (see the platform spec's §11, added 2026-10-21), deferred
until after the connector forge and sandbox infrastructure exist, since it
needs the same sandboxed-build-and-test machinery plus a deploy/rollback
layer that must survive a self-inflicted breakage.

## 3. Data Model Changes

Extends the `Task` table from sub-project 1:

| Field | Type | Notes |
|---|---|---|
| `queue_position` | `int \| None` | Null once `status` leaves `"queued"`. Recomputed whenever the queue changes. |
| `urgent` | `bool` (default `false`) | Urgent tasks are placed ahead of all non-urgent queued tasks, FIFO among themselves. |
| `parent_task_id` | `int \| None` (FK to `tasks.id`) | Set when this task was created via `delegate_task`. |
| `agent_type` | `str` (default `"general"`) | Free-text label (e.g. `"pentest"`, `"coding"`). Not enforced in this sub-project — specialization behavior is sub-project 6's job. |
| `delegation_depth` | `int` (default `0`) | `0` for a top-level task; a child's depth is `parent.delegation_depth + 1`. Enforces the depth cap in §5. |
| `steps_taken` | `int` (default `0`) | Persisted cumulative step count (moved from the agent-core loop's local variable), so it survives pause/resume and can't be reset by delegating. |

`Task.status` gains two new values: `"queued"` (new default on creation,
replacing the old immediate `"running"`) and `"awaiting_subtask"`
(blocking delegation) and `"interrupted"` (killed mid-flight).

## 4. Components

| Component | Job |
|---|---|
| **Scheduler** | Single long-lived asyncio task (started in the FastAPI lifespan). Owns a bounded worker pool (`asyncio.Semaphore`, default 3, configurable) and the queue. Pops the next task (urgent-first, then FIFO) whenever a slot is free and the kill switch isn't engaged; spawns `run_task_loop` as a tracked asyncio task. |
| **Queue state** | Persisted on `Task` (`queue_position`, `urgent`), so dashboard state survives a reconnect. |
| **Kill switch** | `POST /kill-switch {mode: "pause" \| "stop"}`. `pause`: Scheduler stops popping the queue; in-flight tasks finish normally. `stop`: same, plus every in-flight task's asyncio task is cancelled and marked `"interrupted"` with a `report_gap` event explaining why. |
| **Per-task kill** | `POST /tasks/{id}/kill` — same cancel+`interrupted` mechanics as kill-switch `"stop"`, scoped to one task. |
| **Delegation tool** | `delegate_task(description, mode, agent_type?) -> {subtask_id}`, available to every running task (see §5). |
| **Dashboard read model** | `GET /dashboard`: tasks grouped by status, with sub-tasks nested under `parent_task_id`; running/queued/needs-you/done counts; kill-switch state. The `done` group is paginated (most recent 50 by default); full history stays available via `GET /tasks/{id}` and `GET /tasks?status=completed`. |
| **Dashboard UI** | Command Center layout, dark mode: stat strip (counts + pause/stop controls) + card grid; each card has inline answer/approve/deny/kill controls; delegation chains shown as nested cards. |

## 5. Delegation

Any running task can call `delegate_task(description, mode, agent_type?)`:

- **`mode: "blocking"`** — the parent's status becomes `"awaiting_subtask"`
  and it **releases its worker slot** (it is not a real blocked Python
  call — this is the same pause/resume mechanism as today's
  `awaiting_answer`/`awaiting_approval`). A child `Task` is created with
  `parent_task_id` set and enqueued normally. When the child reaches a
  terminal state, the Scheduler re-enqueues the parent at the **front** of
  the queue, appends the child's result as a new event/message, and
  resumes its loop. Because a blocking parent holds no worker slot, many
  blocking parents can be waiting on children simultaneously without
  exhausting the concurrency cap — this is what prevents deadlock.
- **`mode: "background"`** — a child `Task` is created and enqueued; the
  parent's loop continues immediately with `{subtask_id}` as the tool
  result. A `check_subtask(subtask_id)` tool lets the parent poll later.
  The Scheduler posts a `subtask_update` event onto the **parent's own**
  event stream whenever the child finishes or needs approval/input,
  visible in its trace and on the dashboard. This also gives the parent
  **one re-enqueue** (front of queue, same as a resume) so its loop gets a
  chance to notice and react — but only once per `subtask_update`; if the
  parent's own turn doesn't call `check_subtask`, the update isn't
  re-surfaced again. This avoids a background result being silently lost
  while still not forcing the parent to handle it synchronously.

`agent_type` on the child is a label only in this sub-project — no
behavioral specialization is enforced (that's sub-project 6).

**Depth and fan-out limits:** `delegate_task` is refused (routed through
`report_gap`, with the option "the task is too deeply nested — finish or
simplify the current chain") when the new child's `delegation_depth` would
exceed `MAX_DELEGATION_DEPTH` (default 5), or when the parent already has
`MAX_CHILDREN_PER_TASK` (default 10) non-terminal children. This also
bounds the damage of an accidental delegation cycle (a child delegating
back toward an ancestor) without needing separate cycle detection — the
depth cap catches it within a few hops.

**Step-cap persistence:** sub-project 1's `MAX_STEPS` cap (25) now checks
against `Task.steps_taken`, persisted across pause/resume instead of a
local variable the loop re-initializes to zero on every call — so a task
can't dodge the runaway-loop guard by repeatedly delegating and resuming.

**Queue-mutation safety:** every operation that changes queue membership
or ordering (`POST /tasks`, `answer`/`approve`/delegation resume, the
Scheduler's own pop) executes inside a single DB transaction (or, for the
in-process Scheduler, a scheduler-owned `asyncio.Lock`) so concurrent
requests can't race on recomputing `queue_position`.

## 6. Data Flow

1. `POST /tasks` creates the `Task` with `status="queued"`, computes
   `queue_position` (front if `urgent`, else back), and returns
   immediately. No `BackgroundTasks` call here anymore.
2. The Scheduler loop: while a worker slot is free, the queue is
   non-empty, and the kill switch isn't engaged — pop the next task
   (urgent-first, FIFO otherwise), set `status="running"`, spawn
   `run_task_loop` as a tracked asyncio task, recompute remaining
   `queue_position`s.
3. `answer`/`approve` (from sub-project 1) and a resumed `delegate_task`
   parent all re-enqueue **at the front** of the queue — a task you just
   unblocked shouldn't wait behind brand-new tasks.
4. `POST /kill-switch {mode:"pause"}` — Scheduler stops popping the queue.
5. `POST /kill-switch {mode:"stop"}` — same as pause, plus cancels every
   in-flight asyncio task; the loop catches `asyncio.CancelledError`,
   marks the task `"interrupted"`, and records a `report_gap` event
   ("stopped by kill switch"). Cancellation is only honored at the loop's
   step boundaries (checked between steps, never mid-tool-call), so an
   event is always either fully written or not started — never torn.
   Killing (via kill-switch `"stop"` or the per-task kill endpoint)
   cascades to the task's entire subtree: every descendant (children,
   grandchildren, …) also gets cancelled/marked `"interrupted"`, so
   nothing is left running under a parent that will never resume it.
6. Dashboard polls `GET /dashboard` (same poll-based pattern as the
   existing WS event stream) for live counts/cards; inline actions call
   the existing `answer`/`approve`/`rate` endpoints plus the new
   per-task `kill` endpoint.

## 7. Error Handling

| Case | Behavior |
|---|---|
| Child fails or is denied an irreversible action | Parent resumes (blocking) or gets a `subtask_update` (background) carrying the failure/denial as the result; the parent's own model decides how to react. |
| Blocking parent's child never reaches a terminal state (e.g. the child itself is stuck on `ask_user`) | Parent stays `awaiting_subtask` indefinitely — same accepted gap as today's unresolved approval/answer pauses; no new timeout logic in this sub-project. |
| Kill-switch `"stop"` hits a delegation chain | Every in-flight (actually running) task in the subtree is cancelled → `"interrupted"` (cascades from whichever task is killed down through all descendants — see §6). A blocking parent in `awaiting_subtask` holds no asyncio task, so it isn't directly cancelled; when it would otherwise resume and finds its child was interrupted, it resumes with that result (parent decides: retry, `report_gap`, etc.) |
| Many blocking parents delegating at once | Cannot deadlock the cap: a blocking parent holds no worker slot, so the concurrency cap only ever limits truly `"running"` tasks. |
| `delegate_task` with an unrecognized `agent_type` | Not rejected — label only in this sub-project. |
| `delegate_task` exceeds depth or fan-out limits | Refused via `report_gap` with a concrete option (finish/simplify the current chain) rather than silently creating an unbounded tree. |
| Queued tasks pile up over time | `GET /dashboard`'s `done` group is paginated; doesn't bound the queue itself, since a long active queue is legitimate (you submitted more work than the cap can run at once) — only the *display* of old finished tasks is capped. |

## 8. Testing

- **Scheduler**: cap enforcement (a 4th task stays queued while 3 run),
  urgent-jumps-queue ordering, pause vs. stop kill-switch semantics,
  single-task kill.
- **Delegation**: blocking parent releases its slot and resumes with the
  child's result; background parent continues immediately and later sees
  a `subtask_update`; a cap-exhausting fan-out of blocking parents still
  lets their children run (the no-deadlock property, directly tested).
- **Dashboard read model**: grouping by status and by `parent_task_id`,
  counts match actual DB state.
- **End-to-end**: a 2-level delegation chain (parent → child →
  grandchild) running to completion through the real scheduler.

## 9. Build Order (within this sub-project)

1. Scheduler + queue (cap, FIFO/urgent, kill switch) — no delegation yet,
   wired to the existing `run_task_loop` from sub-project 1.
2. Per-task kill endpoint.
3. Delegation tool (`delegate_task`, `check_subtask`) + parent resume
   mechanics.
4. Dashboard read model (`GET /dashboard`).
5. Dashboard UI (Command Center layout, dark mode, inline actions).
