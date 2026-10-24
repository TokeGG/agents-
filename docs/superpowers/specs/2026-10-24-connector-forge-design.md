# Connector Forge — Design Spec

Sub-project 4 of the AI agent platform build order
(`docs/superpowers/specs/2026-10-21-ai-agent-platform-design.md`, §11).

## 1. Purpose & Success Criteria

When an agent hits a tool call it can't satisfy because no integration
exists for the target service (e.g. "post to Slack," "query the GitHub
API"), the platform should be able to build one: write client code,
test it against the real service in a sandbox, and register it as a
reusable tool other agents can call by name — without the user
returning to a terminal or writing code themselves.

Success criteria:
- An agent that hits a missing-capability case for a well-documented
  REST/HTTP API can get a working, approved connector registered
  within one `request_connector` → coding sub-task → approval cycle,
  with no human-written code.
- A forged connector is never silently trusted: new agent-written code
  is reviewed by a human before it's callable by anything other than
  the task that requested it, and every call to it afterward executes
  inside a sandbox, isolated from the main process and from other
  tasks' credentials.
- Missing credentials, failed builds, and unsupported services all
  produce a `report_gap` with concrete next steps — never a silent
  failure or an infinite retry.

## 2. Scope

**In scope:**
- A minimal credential **Vault**: encrypted-at-rest secret storage in
  Postgres, a small get/set/list-by-service API, no UI beyond what's
  needed to add/view-redacted/delete a credential from the dashboard.
- `request_connector(service_name, purpose)` tool: any agent can call
  this when it hits a missing integration.
- The **forge pipeline**: `request_connector` → delegates a coding
  sub-task (`agent_type="coding"`, existing `delegate_task` machinery)
  → sub-task writes + sandbox-tests client code → registers a
  **pending** connector → human approval → **active** connector,
  callable by name like any built-in tool.
- Dynamic tool execution: an active connector's code always runs
  inside a sandbox container at call time (never loaded into the main
  process), using the existing `SandboxManager`/`ContainerRuntime`
  from sub-project 3.
- Dashboard surface: a pending-connectors list (what it does, which
  service/credential, generated code, sandbox test transcript),
  approve/reject actions.

**Out of scope (explicit deferrals, not silent gaps):**
- A full secrets-management product (rotation policies, audit log UI,
  per-user vault scoping, HSM-backed keys). The Vault here is "enough
  to unblock real connectors," matching this platform's single-user
  deployment model (§2 of the platform spec).
- OAuth-flow automation (a connector needing a 3-leg OAuth dance
  reports_gap with "complete the OAuth flow in the web UI" as an
  option — browser-automation-assisted OAuth is a candidate for a
  later connector-forge enhancement, not this version).
- Updating/versioning an existing approved connector when the target
  API changes shape (out of scope; the current design treats a new
  `request_connector` call for an already-active service as the
  trigger to rebuild it from scratch — see §5).

## 3. Components

| Component | Job | Talks to |
|---|---|---|
| Vault | Encrypted per-service secret storage; get/set/list by service name | Postgres |
| `request_connector` tool | Entry point; validates inputs, delegates the coding sub-task | Scheduler (`delegate_task`), Vault |
| Coding sub-task (existing `agent_type="coding"`) | Writes client code, tests it in a sandbox (real API if a credential exists, else a stub), writes the connector row as `pending_review` | Sandbox manager, Vault |
| Connector registry | Postgres table of connector metadata (name, schema, source, required credential, status, test transcript) | Postgres |
| Dynamic tool loader | At `ToolRegistry` build time, loads every `active` connector row and registers a thin `DynamicConnectorTool` proxy for each, by name | Connector registry |
| `DynamicConnectorTool` | A `Tool` whose `run()` executes the connector's stored source as a `run_shell` command inside the CALLING task's own sandbox (see §6), via `SandboxManager`, injecting the scoped credential | Sandbox manager, Vault |
| Dashboard: pending connectors | Approve/reject UI: shows purpose, service, generated code, sandbox test output | API |

## 4. Data Model

```
vault_secrets
  id, service_name (unique), ciphertext, nonce, created_at, updated_at

connectors
  id, name (unique, e.g. "github_create_issue"), service_name,
  purpose (free text, from the original request_connector call),
  parameters_schema (JSON, same shape as existing Tool.parameters_schema),
  source_code (text — the generated Python module),
  required_hosts (JSON list[str] — hostnames the connector calls,
    recorded by the coding sub-task for the approval-time egress-
    allowlist check, §5 step 3),
  status (pending_review | active | rejected),
  requested_by_task_id (FK tasks.id),
  test_transcript (JSON — sandbox run output from the build/test step),
  created_at, approved_at, approved_by (nullable — "user" for now,
    single-user deployment; a placeholder for multi-user later)
```

A `connectors` row is the unit of review and the unit of registration.
One row = one callable tool. A coding sub-task may register multiple
rows in one run (e.g. `github_create_issue` and `github_list_issues`
from one "integrate with GitHub" request) — each is reviewed and
approved independently.

## 5. The Forge Pipeline

1. **Trigger.** An agent calls `request_connector(service_name,
   purpose)`. This tool:
   - Checks the `connectors` table for an existing `active` row with
     that `service_name` + overlapping purpose — if found, returns
     that tool's name directly (no need to rebuild) rather than
     delegating.
   - Checks for an existing `pending_review` row for the same
     service — if found, returns `{"ok": True, "output": "a connector
     for <service> is already awaiting approval", ...}` rather than
     spawning a duplicate coding sub-task.
   - Otherwise, calls `delegate_task(description=<forge brief built
     from service_name+purpose>, mode="background", agent_type="coding")`
     and returns the child task id, same as any other delegation.
2. **Build + test.** The coding sub-task's brief instructs it to:
   - Write a single-file Python module exposing a function matching
     the existing `Tool` shape (`name`, `description`,
     `parameters_schema`, and a `run(arguments, ctx)`-equivalent
     entry point — the coding agent writes plain request/parsing
     logic; it does NOT need to know about `ToolContext` internals,
     since the forge's sandbox harness (§6) adapts the call).
   - Look up `Vault.get(service_name)`. If present, test against the
     real API inside its own sandbox container. If absent, test
     against a stub/mock it writes itself, and call `report_gap`
     with options `["add a credential for <service_name> to the
     vault, then call request_connector again to retest and
     activate", "describe the integration differently if this isn't
     the right service"]`.
   - Insert one `connectors` row per tool it built, `status =
     "pending_review"`, with `test_transcript` capturing what ran.
3. **Approval.** The dashboard lists every `pending_review` row with
   its purpose, service, full source code, test transcript, and the
   hostname(s) the coding sub-task's brief instructs it to record the
   connector as calling (a `required_hosts: list[str]` field alongside
   `source_code` in the `connectors` row — see §4). If any of those
   hosts aren't already present in the deployment's configured
   `SANDBOX_EGRESS_ALLOWLIST` (sub-project 3, Task 9 — a comma-
   separated env var, checked only for non-emptiness at startup, not
   parsed/enforced anywhere in application code today), the approval
   UI surfaces a plain warning naming the missing host(s) and that
   `SANDBOX_EGRESS_ALLOWLIST` needs updating (and the app restarted,
   since it's read once at startup) before this connector will be able
   to reach the network — approval can still proceed (the check is
   informational, not a hard gate, since this version doesn't parse or
   enforce the allowlist's contents in code), but the human isn't left
   discovering a confusing network failure later with no idea why.
   The user approves (→ `active`, `approved_at`/`approved_by` set) or
   rejects (→ `rejected`, kept for audit, never loaded by the tool
   loader). Approval is a one-time gate per row — once `active`, every
   future call runs with no further human step unless the connector's
   own behavior independently trips `needs_approval` (e.g. if it turns
   out to send email — see §7).
4. **Activation.** The next time `ToolRegistry` is built (or
   immediately, if the registry is told to hot-reload — see §6), the
   new tool is loadable by name by any agent.
5. **Re-request for an already-active service** (e.g. the live API
   changed shape and the connector now fails): the rule is simply
   keyed on the exact `(service_name, purpose)` pair — an exact repeat
   always short-circuits to the existing active tool (§5 step 1); any
   different `purpose` string for the same `service_name` always
   builds a new row under a new tool name, never silently overwriting
   an active one. Fixing a broken connector is therefore "build a new
   one with a clarified purpose and let the old one rot" for this
   version — acceptable for a single-user deployment, revisited if it
   becomes a real pain point.

## 6. Dynamic Tool Execution (sandboxed at call time)

`run_shell(task_id, command: str)` is a plain string executed via
shell inside the container — there is no separate side-channel for
secrets. Naively embedding a credential directly into that command
string is unsafe: `run_shell`'s own result (and any exception message
that happens to echo the command it was running) becomes a
`tool_result` Event, which is both persisted to Postgres in plaintext
*and* replayed into the model's own message history on the next step
(`_build_initial_messages`, `loop.py`). A credential reaching either
place is a real leak, not a theoretical one — this governs the whole
design below.

An `active` connector is loaded as a `DynamicConnectorTool(name=...,
description=..., parameters_schema=...)`. Its `run(arguments, ctx)`:

1. **Cache the source once per task.** On first use of a given
   connector within a given task's sandbox, write `source_code` to the
   sandbox volume via the existing `SandboxManager.write_file` (e.g.
   `/connectors/<connector.name>.py`) — not secret, safe to log/persist
   like any other file write. Skip the write on subsequent calls for
   the same `(task_id, connector.name)` (check existence first via
   `read_file`, or track it in an in-memory set keyed by
   `(task_id, connector_id)` — implementer's call, either is fine).
2. **Write the credential to a file, never into a command string or
   env var.** If `Vault.get(connector.service_name)` returns a secret,
   write it via `write_file` to a path containing an unguessable random
   component (e.g. `/secrets/<uuid4>.token`) — the path itself isn't
   secret and is fine to appear in a logged command; the file's
   *contents* never are. The generated connector script is written
   (by the coding sub-task, per its brief — see §5 step 2) to read its
   credential from a file path passed as a plain CLI argument/env var
   *naming the path*, never the secret value itself directly on the
   command line.
3. **Invoke and immediately shred.** `run_shell(task_id, f"python3
   /connectors/{name}.py {secret_path} <<< '{arguments_json}'; rc=$?;
   rm -f {secret_path}; exit $rc")` — the `rm -f` runs unconditionally
   (chained after capturing the real exit code) so the secret file
   never outlives one call, regardless of whether the script succeeds.
4. **Scrub before returning.** Before the captured stdout/stderr is
   turned into the tool's `{"ok", "output", "data"}` result, run it
   through a simple substring-redaction pass against the raw secret
   value the harness itself just wrote to disk (replace any literal
   occurrence of the secret with `"***REDACTED***"`) — defense in
   depth against the connector's own code or error output accidentally
   echoing it (an HTTP client logging full request headers, a
   traceback dumping a variable). This redaction happens in the
   `DynamicConnectorTool.run()` harness itself, which holds the raw
   secret value in memory for exactly this one call and never logs it
   directly.
5. A non-zero exit or unparseable stdout becomes `{"ok": False,
   "output": "<redacted stderr/parse error>", ...}`, same failure shape
   every other sandboxed tool already uses.
6. If `ctx.sandbox_manager` is `None` (task has no sandbox), the tool
   returns the same `_NO_SANDBOX_RESULT`-style failure the Task 3
   sandboxed tools already use — a forged connector simply isn't
   callable by a non-sandboxed task. (Stated plainly, not hidden:
   forged connectors are a sandboxed-task-only capability in this
   version.)

This all reuses the calling task's OWN sandbox container/slot (point
3's `run_shell` call does not consume an extra cap slot) — a forged
tool is just another command the task's sandbox runs, same trust
boundary `run_shell`/`read_file` already established in sub-project 3.

**Registry hot-reload:** `ToolRegistry` gains a `reload_dynamic_tools
(session)` method that re-queries `active` connectors and
registers/replaces their `DynamicConnectorTool` entries, called once
right after an approval action (via the dashboard's approve endpoint)
so a newly approved connector is usable without a server restart.

**Tool-name collision.** Before a connector row can move from
`pending_review` to `active` (approval endpoint), check its `name`
against the live `ToolRegistry`'s built-in tool names (`run_shell`,
`read_file`, `write_file`, `edit_file`, `http_get`, `browse`,
`download_file`, `ask_user`, `report_gap`, `delegate_task`,
`check_subtask`, `request_connector`, and any other already-active
connector's name) — reject the approval with a clear error if it
collides, rather than silently shadowing a built-in. The coding
sub-task's brief (§5 step 2) should also instruct it to prefix every
connector name with its `service_name` (e.g. `github_create_issue`,
never bare `create_issue`) to make collisions structurally rare in the
first place, not just caught at the gate.

## 7. Security Model

- **Approval gate.** Every connector starts `pending_review`; no code
  it contains runs against anything but its own sandbox test until a
  human approves it. This is the primary control — it is the one
  place a human reviews agent-written code before it becomes
  platform-wide infrastructure.
- **Credential scoping.** A credential is written to a random-named
  file for exactly one call, shredded immediately after regardless of
  outcome, read by the connector script by path (never embedded in a
  command string, env var assignment, or anything that could appear in
  a logged `tool_call`/`tool_result` Event), and output is scrubbed
  for the literal secret value before it's returned (§6) — never
  loaded into the main process. Matches/extends the existing
  sandboxed-execution credential treatment, tightened further here
  because a forged connector may be called many times across many
  unrelated tasks and its generated code wasn't written by a human.
- **`needs_approval` still applies.** If a forged connector's generated
  code is later recognized as doing something irreversible (the
  `IRREVERSIBLE_TOOL_NAMES` set in `permissions.py` is keyed by tool
  *name* — a connector named, say, `slack_post_message` isn't
  automatically in that set). This version does NOT attempt to
  auto-detect irreversibility from generated code; the approval step
  (§5.3) is the control, and the dashboard's approval UI should
  surface a plain-language note generated by the coding sub-task
  itself describing whether the connector can take an action with
  side effects (send/post/delete/pay) so the human approver has that
  context. A future version could extend `IRREVERSIBLE_TOOL_NAMES`
  with a flag set at connector-creation time; deferred here to keep
  this version's scope bounded.
- **Vault access control.** Only the forge pipeline (the coding
  sub-task via a `vault_lookup`-style internal call, not a
  general-purpose agent tool) and `DynamicConnectorTool.run()` read
  secrets. There is no general `get_credential` tool exposed to
  arbitrary agents — this prevents a compromised/misbehaving task from
  fishing the vault for unrelated secrets. Adding a credential to the
  vault happens only via the dashboard (human-initiated), never via an
  agent tool call.
- **No cross-task sandbox slot consumption.** Running a forged
  connector inside the CALLING task's own sandbox (§6, point 3) means
  the forge doesn't need its own separate concurrency cap — it rides
  the existing one from sub-project 3. The only new per-call cost is
  one more `run_shell` invocation.

## 8. Error Handling

| Scenario | Handling |
|---|---|
| `request_connector` for a service with no credential and the coding sub-task's stub test fails too | `report_gap`: "could not build or stub-test an integration for `<service>` — `<reason>`", options: retry with a clearer purpose, provide API docs, or do it manually |
| Coding sub-task never finishes (crashes, times out, hits `MAX_STEPS`) | Standard existing `delegate_task`/child-task failure handling (sub-project 1/2) — the parent that called `request_connector` sees the child failed via `check_subtask`, same as any other delegation failure |
| A forged connector's sandboxed call exceeds the existing 2-minute `run_shell` timeout | Same existing timeout/failure shape as any other `run_shell` call — no special casing needed, this is why reusing `run_shell` (§6) rather than a bespoke execution path matters |
| The connector's stored `source_code` fails to even parse/run (e.g. a Python syntax error slipped past review) | `DynamicConnectorTool.run()` returns `{"ok": False, ...}` with the captured stderr — same failure shape as a tool hitting a runtime error; does not crash the task, model can `report_gap` from there |
| Two agents call `request_connector` for the exact same `(service_name, purpose)` concurrently before either's child task finishes | Accepted race for this version: the duplicate-check in §5 step 1 is best-effort (a DB read, not locked) — worst case is two coding sub-tasks build near-duplicate connectors, both landing as separate `pending_review` rows the human sees and can reject one of. Not worth a lock for a single-user deployment's low concurrency. |
| Vault secret can't be decrypted (key rotated, corrupted ciphertext) | `Vault.get` raises; `DynamicConnectorTool.run()` catches it and returns `{"ok": False, "output": "credential for <service> could not be read — it may need to be re-added to the vault", ...}` |

## 9. Testing

- Vault: encrypt/decrypt round-trip, get/set/list, missing-key
  behavior.
- `request_connector`: short-circuits correctly for
  active/pending-duplicate cases; delegates correctly otherwise
  (reusing the existing `delegate_task` test patterns from
  sub-project 1/2).
- Connector registry: CRUD, status transitions, that a `rejected` row
  is never loaded by the tool loader.
- `DynamicConnectorTool`: a fake `SandboxManager` proving (a) the
  credential never appears as a literal substring in any
  `run_shell`/`write_file` command string the harness builds — only
  the secret's file *path* does; (b) the credential file is removed
  (the `rm -f` chain) after both a successful and a failing script
  run; (c) the redaction pass replaces a literal occurrence of the
  secret in a fake script's stdout/stderr with `***REDACTED***` before
  it reaches the returned `{"ok", "output", ...}` result; (d) the
  source-code caching logic writes once per `(task_id, connector)` and
  skips on a second call; (e) the stdout-parsing/failure-shape paths;
  (f) the no-sandbox-manager failure path.
- An end-to-end assertion (reusing `db_session`/`FakeContainerRuntime`
  fixtures) that after a full forge-and-call cycle with a fake secret
  value, that value never appears anywhere in the `events` table for
  the calling task — the concrete regression test for the leak risk
  identified in §6.
- Hot-reload: approving a connector makes it immediately callable by
  name without a registry rebuild/restart.
- End-to-end (sandbox-backed, no real external API): a fake "service"
  the coding sub-task's test harness can hit locally, proving one full
  forge-pipeline run start to finish against the `FakeContainerRuntime`.

## 10. Open Questions Accepted as Version-1 Scope Boundaries

(Carried forward explicitly, not silently dropped — matches the style
established in the sandboxed-execution spec's own accepted-gaps
section.)

- No OAuth automation (§2).
- No connector versioning/update-in-place (§5.5) — a changed purpose
  just creates a new tool name.
- No automatic irreversibility detection for generated connector code
  (§7) — approval-time human judgment is the control.
- Single-user vault (no per-user scoping) — matches this platform's
  deployment model.
