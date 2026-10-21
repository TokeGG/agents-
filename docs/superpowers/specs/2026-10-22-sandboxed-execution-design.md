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
| **Sandbox Manager** | Starts one Docker container per task on its first sandbox-needing tool call (not per-command); keeps it alive and reused across that task's tool calls; stops and removes it on task termination, kill, or idle-timeout. Runs as a sibling-container launcher — see "Docker access" below for how it reaches the Docker daemon from inside the app's own container. |
| **Sandbox container image** | Base dev image: Python 3, Node.js (LTS), Go, plus `git`, `curl`, `make`, `build-essential`. Extensible later per-language by the connector forge (sub-project 4) when a task needs something not preinstalled. |
| **Sandbox tools** | `run_shell(command)`, `read_file(path)`, `write_file(path, content)`, `edit_file(path, old, new)` — all execute inside the task's container. `http_get` is re-routed through the sandbox's egress-controlled network (see §6) instead of running in-process ungoverned. |
| **Headless browser service** | One shared, long-lived Playwright/Chromium process; one isolated browser *context* per task (not a whole separate browser, not one shared session) via a `browse`/`download_file` tool. |
| **Egress allowlist** | Docker network per container reaching only explicitly allowed hosts (package registries, the model API endpoint) — nothing else by default. |
| **Per-container resource limits** | Every sandbox container is started with concrete Docker limits, not just the word "capped": `--memory=2g`, `--cpus=1`, and a disk quota of 5GB (via a size-capped tmpfs or overlay storage option, tunable per deploy). These are config values (`SANDBOX_MEMORY_LIMIT`, `SANDBOX_CPU_LIMIT`, `SANDBOX_DISK_LIMIT`), not hardcoded, but these are the defaults this spec commits to. |
| **Credential vault access** | A sandbox container that needs a credential (e.g. a git push token, a package registry auth token) gets it injected as an environment variable or a short-lived mounted file at container start, sourced from the platform's existing Vault (platform spec §3/§9) — scoped to exactly the services that task's connector/credential set allows, never the whole vault. The agent's own shell commands can read it (it has to, to use it), so a credential handed to a sandbox is treated as consumed/exposed for that task's lifetime — the vault entry should be a scoped, revocable token where the target service supports it, not a long-lived master credential. |
| **Idle-container reaper** | A background sweep stopping any sandbox container whose task has been paused (`awaiting_answer`/`awaiting_approval`/`awaiting_subtask`) longer than `SANDBOX_IDLE_TIMEOUT` (default 4h); the task stays paused in the DB. Before stopping, the reaper runs `git add -A && git commit -m "auto-commit before idle reap"` inside the container if there are uncommitted changes, so a long pause never silently loses in-progress edits — only a container that's actively mid-command (not just paused) is never reaped. The container is recreated from that committed state (lazily, on the next sandbox tool call for that task — same first-use path as initial creation) if/when work resumes. |
| **Sandbox concurrency cap** | A separate `SANDBOX_MAX_CONCURRENCY` (default 2), independent of the Scheduler's general task cap (sub-project 2, default 3) — sized conservatively since containers are a heavier resource class than lightweight API-only tasks. Delegation fan-out (sub-project 2, up to `MAX_CHILDREN_PER_TASK`=10 children) is naturally throttled by this cap — children needing a sandbox simply queue for a slot like any task. See "Cap interaction" below for how this avoids wasting a general-cap slot while a task waits on a sandbox slot. |
| **Model tiering** | Two `ModelAdapter` references per task: `worker_adapter` (cheap, coding-specialized open model — default for every step) and `reviewer_adapter` (pricier, stronger model — used only for stuck-step escalation and the one post-task review pass). For a task that never needs the sandbox (most sub-project-1/2 tasks — a plain personal-assistant or email task), `reviewer_adapter` defaults to the *same* adapter instance as `worker_adapter` — tiering is strictly additive cost for sandboxed/coding tasks, never a tax on every task platform-wide. |

**Docker access:** the app backend runs inside its own container (per the
existing `docker-compose.yml`). The Sandbox Manager needs to launch
*sibling* containers, which requires either (a) mounting the host's
`/var/run/docker.sock` into the app container (simplest; the app container
effectively has host-level Docker control, so the app container itself
must be treated as a trusted/privileged component, same trust level as
the host), or (b) a Docker-in-Docker (`dind`) sidecar service the app
talks to over the network instead of a host socket (better isolation,
more moving parts, slower container starts). **This spec picks (a),
mounted host socket**, as the pragmatic default for a single-operator VPS
deployment — it is the standard pattern for "an app that manages
containers" and matches this platform's existing single-user trust model
(platform spec §9 already treats the app server as the thing with broad
system access, gated by login). Revisit (b) only if this platform is ever
deployed multi-tenant.

**Cap interaction:** a task's sandbox-need is only discovered lazily, at
its first sandbox-needing tool call — by then it may already hold a
general Scheduler slot (sub-project 2). If the sandbox cap is full at that
moment, the task does **not** block while holding its general slot
(which would waste that slot on a task doing nothing but waiting). Instead
it releases its general slot the same way an `ask_user`/`awaiting_approval`
pause does: status becomes a new `awaiting_sandbox_slot`, the task
re-enqueues (front of queue, same mechanics as other resumes) once a
sandbox slot frees, and resumes from exactly that tool call. This keeps
the two caps from fighting each other — the general cap always reflects
real Scheduler throughput, never silently reduced by sandbox contention.

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
   context) for its `task_id` on first use; subsequent calls reuse it. If
   the sandbox cap is full at that moment, the task releases its general
   Scheduler slot and pauses as `awaiting_sandbox_slot` instead (see
   "Cap interaction" in §3), resuming once a slot frees.
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
| Container hits its memory/CPU/disk limit mid-command | Same as the OOM case above: the next tool call fails to reach it, task `failed` with a `report_gap` naming which limit was hit, so the user can decide whether to raise `SANDBOX_MEMORY_LIMIT`/etc. for a retry rather than guessing. |
| A sandboxed task's container is compromised or misused (credential exfiltration attempt via `run_shell`) | Out of scope to detect automatically in this sub-project (no sandbox output scanning/DLP) — the mitigation is the credential-scoping design itself (§3: short-lived, service-scoped tokens, never the master vault), so a worst-case leak exposes only that one task's narrowly-scoped credential, not the whole vault. |

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
- Resource limits: a container actually refuses to exceed its configured
  memory/CPU, proving the limits are real Docker-enforced caps, not just
  documented intentions.
- Idle reaper: a paused task with uncommitted container changes gets them
  auto-committed before the container is stopped; resuming recreates the
  container from that commit with the work intact.
- Cap interaction: a task that discovers its sandbox need while the
  sandbox cap is full releases its general Scheduler slot (verified by a
  second, unrelated non-sandboxed task being able to run concurrently
  rather than queueing behind it).

## 8. Build Order (within this sub-project)

1. Sandbox Manager + container image + lifecycle (start/reuse/teardown) +
   mounted-Docker-socket access + concrete per-container resource limits,
   wired to sub-project 2's task termination/kill paths.
2. Sandbox tools (`run_shell`/`read_file`/`write_file`/`edit_file`) +
   egress allowlist + re-routed `http_get`.
3. Idle-container reaper + sandbox-specific concurrency cap.
4. Model tiering (`worker_adapter`/`reviewer_adapter`, stuck detection,
   bounded post-task review).
5. Headless browser service + per-task browser context + `browse`/
   `download_file` tool.
