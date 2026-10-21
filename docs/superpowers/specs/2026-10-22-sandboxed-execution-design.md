# Sandboxed Execution — Design Spec

Date: 2026-10-22
Status: Approved by user, pending implementation planning
Sub-project: #3 in the platform build order (see
`docs/superpowers/specs/2026-10-21-ai-agent-platform-design.md` §11)

## 1. Purpose & Success Criteria

Today (sub-projects 1–2), the only "real" tool an agent has is `http_get`,
run in-process on the app server with no isolation and no network
governance. This sub-project gives agents a real, persistent development
environment per task — a genuine edit/test/iterate loop, as close to a
real Claude Code session as achievable on an open/self-hosted model — plus
a shared headless browser for browsing and downloads.

Success looks like: a coding task can write a file, run the project's
real test command, see a failure, fix one line, and re-run — entirely
inside an isolated, resource-capped, network-governed Docker container
that persists for the task's lifetime (not recreated per command), with
no path to the host or to arbitrary network destinations.

## 2. Scope Note

"As close to Claude Code as possible at minimum cost" is addressed here as
an architecture decision (model tiering + review pass), not by embedding
the actual Claude Code CLI (rejected — ties the platform to Anthropic's
product/billing for every sandboxed task, against the platform's
model-agnostic, self-hostable goals). The sandbox mechanism works
identically regardless of which model sits behind the adapter; closing
the capability gap further is primarily sub-project 1's model-adapter
concern (upgrading the underlying model, eventually via the Phase 2
fine-tuning pipeline), not this sub-project's.

## 3. Components

| Component | Job |
|---|---|
| **Sandbox Manager** | Starts one Docker container per task on its first sandbox-needing tool call (not per-command); keeps it alive and reused across that task's tool calls; stops and removes it on task termination, kill, or idle-timeout. |
| **Sandbox container image** | Base dev image: Python 3, Node.js (LTS), Go, plus `git`, `curl`, `make`, `build-essential`. Extensible later per-language by the connector forge (sub-project 4) when a task needs something not preinstalled. |
| **Sandbox tools** | `run_shell(command)`, `read_file(path)`, `write_file(path, content)`, `edit_file(path, old, new)` — all execute inside the task's container. `http_get` is re-routed through the sandbox's egress-controlled network (see §6) instead of running in-process ungoverned. |
| **Headless browser service** | One shared, long-lived Playwright/Chromium process; one isolated browser *context* per task (not a whole separate browser, not one shared session) via a `browse`/`download_file` tool. |
| **Egress allowlist** | Docker network per container reaching only explicitly allowed hosts (package registries, the model API endpoint) — nothing else by default. |
| **Idle-container reaper** | A background sweep stopping any sandbox container whose task has been paused (`awaiting_answer`/`awaiting_approval`/`awaiting_subtask`) longer than `SANDBOX_IDLE_TIMEOUT` (default 4h); the task stays paused in the DB, the container is recreated from the last committed git state if/when work resumes. |
| **Sandbox concurrency cap** | A separate `SANDBOX_MAX_CONCURRENCY` (default 2), independent of the Scheduler's general task cap — sized conservatively since containers are a heavier resource class than lightweight API-only tasks. Delegation fan-out (sub-project 2, up to `MAX_CHILDREN_PER_TASK`=10 children) is naturally throttled by this cap — children needing a sandbox simply queue for a slot like any task. |
| **Model tiering** | Two `ModelAdapter` instances per task: `worker_adapter` (cheap, coding-specialized open model — default for every step) and `reviewer_adapter` (pricier, stronger model — used only for stuck-step escalation and the one post-task review pass). |

## 4. Model Tiering for the Coding Workflow

This is the concrete mechanism for "as close to Claude Code as possible
at minimum cost":

1. **Model choice**: the worker tier should be a coding-specialized open
   model (not a generalist model of the same size) — pin the exact model
   at implementation-plan time, since the field moves monthly.
2. **Cheap-implementer / strong-reviewer split**: mirrors exactly how
   sub-project 2 itself was built (a cheap/standard model implements, a
   stronger model reviews and drives a bounded fix loop). The reviewer
   tier is invoked far less often than the worker tier, so its higher
   per-token cost applies to a small fraction of total tokens.
3. **Stuck detection**: if the same tool + the same error signature repeats
   `STUCK_REPEAT_THRESHOLD` (default 3) times in a row for a task, the next
   step for that task routes to `reviewer_adapter` instead of
   `worker_adapter`, with the recent failure history emphasized in context.
4. **Bounded post-task review**: once a task reaches `final_answer`, before
   marking it `completed`, exactly one `reviewer_adapter` call reviews the
   container's `git diff` against the task description. If it finds a real
   problem, exactly one fix step runs (via `worker_adapter`); after that,
   the task completes regardless of any remaining reviewer concern (surfaced
   in the `reflection` event, never looped further) — this never becomes an
   open-ended review/fix cycle, and still respects the existing `MAX_STEPS`
   cap from sub-project 1/2.
5. **`run_task_loop`'s signature changes** to accept both adapters (or an
   adapter-selector callback) instead of a single fixed adapter, so the
   per-step tier choice described above is possible.

## 5. Data Flow

1. A task that needs sandboxed tools (`run_shell`, `read_file`,
   `write_file`, `edit_file`, the re-routed `http_get`, or the browser
   tool) triggers the Sandbox Manager to start a container (or browser
   context) for its `task_id` on first use; subsequent calls reuse it.
2. Each step defaults to `worker_adapter`; stuck-detection (§4.3) or the
   post-task review (§4.4) route that specific step to `reviewer_adapter`
   instead.
3. Tool calls execute inside the container/browser context, never on the
   host, never against an unlisted network destination.
4. On task termination (any terminal status) or a kill (sub-project 2's
   cascading interrupt), the Sandbox Manager tears down that task's
   container as the final step of the existing cleanup path — not a
   separate mechanism.
5. On a long pause, the idle-container reaper stops the container; the
   task stays paused; resuming later recreates the container from the
   last committed git state.

## 6. Error Handling

| Case | Behavior |
|---|---|
| Container fails to start (Docker daemon issue, image pull failure) | `report_gap` with retry/check-config options, task `failed` — same pattern as an adapter failure today. |
| `run_shell` command hangs | Per-command timeout (default 2 min); killed, result is a failed `tool_result`, loop continues — one hung command doesn't fail the whole task. |
| Container OOM-killed by Docker's cgroup limits | Detected on the next tool call failing to reach the container; task `failed` with a `report_gap` explaining the resource limit was hit, not silently retried. |
| Kill-switch/per-task kill on a sandboxed task | Existing cascading interrupt (sub-project 2) fires as today; container teardown is folded into that same cleanup path. |
| Egress allowlist blocks a host the agent needs | Surfaces as a failed tool result ("blocked by sandbox network policy"), `report_gap`-able by the agent — the real fix is a human adding the host to the allowlist, deliberately not self-serve by the agent (a safety boundary, not a bug). |

## 7. Testing

- Sandbox Manager: container lifecycle (start-once/reuse, stop-on-terminal,
  idle-timeout reap) — against a real local Docker daemon where available,
  a fake/mocked container runtime otherwise, for fast unit tests.
- Tool execution: `run_shell`/`read_file`/`write_file`/`edit_file`
  round-trip against a real container.
- Model tiering: stuck-detection triggers `reviewer_adapter` after N
  repeated failures; the post-task review runs exactly once and never
  loops a second time regardless of what it finds.
- Security: a test proving a non-allowlisted host is actually blocked at
  the network layer (not merely "the agent chooses not to call it").
- End-to-end: a coding task that writes a failing test, fixes it, re-runs
  it passing, and completes — proving the real iterate-and-test loop
  works through the real Sandbox Manager and Scheduler together.

## 8. Build Order (within this sub-project)

1. Sandbox Manager + container image + lifecycle (start/reuse/teardown),
   wired to sub-project 2's task termination/kill paths.
2. Sandbox tools (`run_shell`/`read_file`/`write_file`/`edit_file`) +
   egress allowlist + re-routed `http_get`.
3. Idle-container reaper + sandbox-specific concurrency cap.
4. Model tiering (`worker_adapter`/`reviewer_adapter`, stuck detection,
   bounded post-task review).
5. Headless browser service + per-task browser context + `browse`/
   `download_file` tool.
