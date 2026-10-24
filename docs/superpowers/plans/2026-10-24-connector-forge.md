# Connector Forge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent that hits a missing integration trigger a coding
sub-task that writes, sandbox-tests, and (after human approval) registers
a reusable tool other agents can call by name — with credentials handled
so they never reach the model's own context or Postgres in plaintext.

**Architecture:** A minimal encrypted Vault backs a `connectors` registry
table. `request_connector` delegates an existing `agent_type="coding"`
sub-task, which calls `register_connector` to land a `pending_review` row.
A human approves it via a new dashboard endpoint. Once `active`,
`ToolRegistry` (already rebuilt fresh per task by the Scheduler) loads it
as a `DynamicConnectorTool` that runs the connector's cached source inside
the calling task's own sandbox via the existing `run_shell`, injecting any
credential through a shredded-after-use file rather than a logged command
string.

**Tech Stack:** Python/FastAPI/SQLAlchemy backend (existing), `cryptography`
(Fernet) for the vault, the existing `SandboxManager`/`ContainerRuntime`
from sub-project 3, the existing `delegate_task`/`ToolRegistry`/agent-loop
machinery from sub-projects 1–2.

**Spec:** `docs/superpowers/specs/2026-10-24-connector-forge-design.md`

## Global Constraints

- `request_connector(service_name, purpose)` short-circuits to an existing
  `active` row with the exact same `(service_name, purpose)` pair; a
  different `purpose` for the same `service_name` always builds a new row
  under a new tool name (spec §5.5) — never overwrites an active row.
- A credential is never embedded in a `run_shell` command string or env
  var literal. It is written to a random-named file, read by path, and the
  file is removed immediately after one use regardless of outcome (spec
  §6). Tool output is scrubbed for the literal secret value before it is
  returned.
- A connector's `source_code` is written to the calling task's sandbox
  volume once per `(task_id, connector.name)` and reused on later calls in
  the same task, not re-embedded in every call's command (spec §6 step 1).
- A connector cannot be approved if its `name` collides with a built-in
  tool name (`run_shell`, `read_file`, `write_file`, `edit_file`,
  `http_get`, `browse`, `download_file`, `ask_user`, `report_gap`,
  `delegate_task`, `check_subtask`, `request_connector`,
  `register_connector`, `vault_provision_test_credential`) or with another
  already-`active` connector's name (spec §6, "Tool-name collision").
- A `rejected` connector row is never loaded by the dynamic tool loader
  (spec §5 step 3).
- A forged connector is callable only by a task that has a
  `sandbox_manager` configured (`ctx.sandbox_manager is None` → the same
  `_NO_SANDBOX_RESULT`-shaped failure the existing sandboxed tools use,
  per spec §6 step 6).
- No general `get_credential`-style tool is exposed to arbitrary agents —
  only the forge pipeline's own tools read vault secrets (spec §7).
- Approving a connector surfaces (not blocks on) any `required_hosts` not
  present in the deployment's `SANDBOX_EGRESS_ALLOWLIST` env var (spec §5
  step 3) — informational only, this version does not parse/enforce the
  allowlist's contents in code.

## Review Focus

- A connector call where the generated script's own error output or
  traceback echoes the credential value verbatim — the redaction pass
  must catch it even though the harness didn't put it there itself.
- Two different tasks calling the same `active` connector concurrently —
  each must get its own shredded-per-call secret file; one task's
  in-flight secret file must never be visible to or reused by the other.
- A connector approved while its `source_code` has already been cached in
  some OTHER unrelated task's sandbox from an earlier (rejected, then
  resubmitted under the same name — can't happen per Global Constraints,
  but a stale cache from a *same-named, same-task* earlier call) run —
  the cache key must be `(task_id, connector.name)`, not just
  `connector.name`, so a different task never sees another task's cached
  file path assumptions.
- `request_connector` called with a `service_name`/`purpose` that exactly
  matches an existing `pending_review` row versus one that exactly matches
  an `active` row — these are two different short-circuit paths (spec §5
  step 1) and must not be conflated (e.g. returning "already active" for a
  pending-only row, or re-delegating when a pending row already exists).
- A `register_connector` call with a `parameters_schema` that isn't valid
  JSON-schema-shaped (missing `type`/`properties`) — must not corrupt the
  row or crash the tool; store it as given (this version doesn't validate
  JSON Schema correctness) but never let a malformed schema prevent the
  row from landing as `pending_review` for human review.

---

## Task 1: Vault

**Files:**
- Modify: `backend/app/models.py` (add `VaultSecret`)
- Create: `backend/app/vault.py`
- Modify: `backend/requirements.txt` (add `cryptography`)
- Test: `backend/tests/test_vault.py`

**Interfaces:**
- Consumes: `Base` (existing, `app.models`).
- Produces: `VaultSecret` model (`id`, `service_name: str` unique,
  `ciphertext: str`, `created_at`, `updated_at` — no separate `nonce`
  column: this deviates from the spec's literal §4 data model, but
  `cryptography.fernet.Fernet` embeds its own IV/nonce and authentication
  tag inside the token it returns, so a second column would be redundant).
  `Vault(key: bytes)` with
  `get(service_name: str) -> str | None`, `set(service_name: str, value:
  str) -> None`, `list_service_names() -> list[str]`, `delete(service_name:
  str) -> None`. `Vault.generate_key() -> bytes` (staticmethod, thin
  wrapper over `cryptography.fernet.Fernet.generate_key()`, for an
  operator provisioning `VAULT_ENCRYPTION_KEY`).

- [ ] **Step 1: Write the failing test: set then get round-trips a secret; get on an unknown service returns None**

```python
def test_vault_set_then_get_round_trips(db_session):
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault.set("github", "ghp_abc123")
    assert vault.get("github") == "ghp_abc123"
    assert vault.get("unknown_service") is None
```

- [ ] **Step 2: Run test to verify it fails** — Run: `cd backend && PYTHONPATH=. python3 -m pytest tests/test_vault.py -v` — Expected: FAIL (`ImportError`/`ModuleNotFoundError`).
- [ ] **Step 3: Add the `VaultSecret` model to `backend/app/models.py`**, following the existing `Task`/`Event` style (`Mapped`/`mapped_column`, `_utcnow` default for `created_at`/`updated_at`, `service_name` with a unique constraint).
- [ ] **Step 4: Add `cryptography` to `backend/requirements.txt`.**
- [ ] **Step 5: Implement `Vault` in `backend/app/vault.py`.** `__init__(self, key: bytes, session_factory: Callable)`. `set`/`get`/`delete` open a session via `session_factory()`, query/upsert/delete the `VaultSecret` row by `service_name`, and close the session (`try/finally`, matching the pattern in `app/sandbox/manager.py`). Encrypt/decrypt via `cryptography.fernet.Fernet(key)`. `get` on a missing row returns `None`.
- [ ] **Step 6: Run test to verify it passes.**
- [ ] **Step 7: Write the failing test: `set` on an existing service overwrites the old value (upsert, not duplicate rows)**

```python
def test_vault_set_overwrites_existing_secret(db_session):
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault.set("github", "old")
    vault.set("github", "new")
    assert vault.get("github") == "new"
    assert db_session.query(VaultSecret).filter_by(service_name="github").count() == 1
```

- [ ] **Step 8: Run test to verify it fails, then passes.**
- [ ] **Step 9: Write the failing test: `list_service_names` and `delete`**

```python
def test_vault_list_and_delete(db_session):
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault.set("github", "x")
    vault.set("slack", "y")
    assert sorted(vault.list_service_names()) == ["github", "slack"]
    vault.delete("github")
    assert vault.list_service_names() == ["slack"]
    assert vault.get("github") is None
```

- [ ] **Step 10: Run test to verify it fails, then passes.**
- [ ] **Step 11: Write the failing test: a `Vault` built with the wrong key can't decrypt a secret written by another key — raises, doesn't return garbage**

```python
def test_vault_wrong_key_raises_on_decrypt(db_session):
    vault_a = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault_a.set("github", "secret")
    vault_b = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    with pytest.raises(Exception):  # cryptography.fernet.InvalidToken
        vault_b.get("github")
```

- [ ] **Step 12: Run test to verify it fails, then passes** (Fernet already raises `InvalidToken` on a bad key/ciphertext — this step should pass once Step 5's `get` doesn't swallow the exception).
- [ ] **Step 13: Run the full backend suite** — Run: `cd backend && PYTHONPATH=. python3 -m pytest -q` — Expected: all PASS (the known pre-existing flaky `test_kill_task_cascades_to_running_grandchild` may fail under full-suite ordering — re-run in isolation with `-k` to confirm it passes alone, don't treat it as new breakage).
- [ ] **Step 14: Commit**

```bash
git add backend/app/models.py backend/app/vault.py backend/requirements.txt backend/tests/test_vault.py
git commit -m "feat: add encrypted credential vault"
```

---

## Task 2: Connector registry

**Files:**
- Modify: `backend/app/models.py` (add `Connector`)
- Create: `backend/app/connectors/__init__.py`, `backend/app/connectors/registry.py`
- Test: `backend/tests/test_connector_registry.py`

**Interfaces:**
- Consumes: `Base` (`app.models`).
- Produces: `Connector` model (`id`, `name: str` unique, `service_name:
  str`, `purpose: str`, `parameters_schema: dict` JSON, `source_code:
  str`, `required_hosts: list[str]` JSON, `status: str` default
  `"pending_review"`, `requested_by_task_id: int | None` FK `tasks.id`,
  `test_transcript: dict` JSON nullable, `created_at`, `approved_at:
  datetime | None`, `approved_by: str | None`).
  `BUILTIN_TOOL_NAMES: set[str]` (the literal set from Global Constraints).
  Functions in `backend/app/connectors/registry.py`: `create_pending
  (session, *, name, service_name, purpose, parameters_schema,
  source_code, required_hosts, requested_by_task_id, test_transcript) ->
  Connector`; `find_active(session, service_name, purpose) -> Connector |
  None`; `find_pending(session, service_name) -> Connector | None`;
  `list_pending(session) -> list[Connector]`; `name_collision(session,
  name) -> bool` (True if `name` is in `BUILTIN_TOOL_NAMES` or matches any
  `active` connector's `name`); `approve(session, connector_id, *,
  approved_by) -> Connector` (raises `ValueError` if `name_collision` or if
  the row isn't `pending_review`); `reject(session, connector_id) ->
  Connector`; `list_active(session) -> list[Connector]`.

- [ ] **Step 1: Write the failing test: `create_pending` then `find_pending`/`find_active` round-trip correctly by status**

```python
def test_create_pending_then_find_pending_not_active(db_session, task):
    create_pending(
        db_session, name="github_create_issue", service_name="github",
        purpose="create issues", parameters_schema={"type": "object"},
        source_code="print('x')", required_hosts=["api.github.com"],
        requested_by_task_id=task.id, test_transcript={"ok": True},
    )
    assert find_pending(db_session, "github") is not None
    assert find_active(db_session, "github", "create issues") is None
```

- [ ] **Step 2: Run test to verify it fails.**
- [ ] **Step 3: Add the `Connector` model to `backend/app/models.py`** (FK pattern mirrors `Task.parent_task_id`).
- [ ] **Step 4: Implement `create_pending`/`find_pending`/`find_active` in `backend/app/connectors/registry.py`.** `find_active`/`find_pending` filter by exact `service_name` (+`purpose` for `find_active`, per Global Constraints' exact-pair rule) and `status`.
- [ ] **Step 5: Run test to verify it passes.**
- [ ] **Step 6: Write the failing test: `approve` flips status and sets `approved_at`/`approved_by`; raises on a name collision with a built-in**

```python
def test_approve_sets_active_and_rejects_builtin_name_collision(db_session, task):
    c = create_pending(
        db_session, name="run_shell", service_name="evil", purpose="x",
        parameters_schema={}, source_code="", required_hosts=[],
        requested_by_task_id=task.id, test_transcript={},
    )
    with pytest.raises(ValueError):
        approve(db_session, c.id, approved_by="user")

    ok = create_pending(
        db_session, name="github_create_issue", service_name="github",
        purpose="x", parameters_schema={}, source_code="", required_hosts=[],
        requested_by_task_id=task.id, test_transcript={},
    )
    approved = approve(db_session, ok.id, approved_by="user")
    assert approved.status == "active"
    assert approved.approved_by == "user"
    assert approved.approved_at is not None
```

- [ ] **Step 7: Run test to verify it fails.**
- [ ] **Step 8: Implement `name_collision`/`approve`/`reject`/`list_active`/`list_pending` in `backend/app/connectors/registry.py`.**
- [ ] **Step 9: Run test to verify it passes.**
- [ ] **Step 10: Write the failing test: `approve` also rejects a name that collides with an already-`active` connector (not just a built-in)**

```python
def test_approve_rejects_collision_with_active_connector(db_session, task):
    first = create_pending(
        db_session, name="github_create_issue", service_name="github",
        purpose="a", parameters_schema={}, source_code="", required_hosts=[],
        requested_by_task_id=task.id, test_transcript={},
    )
    approve(db_session, first.id, approved_by="user")
    dup = create_pending(
        db_session, name="github_create_issue", service_name="github",
        purpose="b (a different purpose, different row)", parameters_schema={},
        source_code="", required_hosts=[], requested_by_task_id=task.id,
        test_transcript={},
    )
    with pytest.raises(ValueError):
        approve(db_session, dup.id, approved_by="user")
```

- [ ] **Step 11: Run test to verify it fails, then passes.**
- [ ] **Step 12: Run the full backend suite** — Expected: all PASS.
- [ ] **Step 13: Commit**

```bash
git add backend/app/models.py backend/app/connectors/ backend/tests/test_connector_registry.py
git commit -m "feat: add connector registry (pending/active/rejected lifecycle)"
```

---

## Task 3: Secret injection helpers

**Files:**
- Create: `backend/app/connectors/secret_injection.py`
- Test: `backend/tests/test_secret_injection.py`

**Interfaces:**
- Consumes: `SandboxManager.write_file(task_id, path, content) -> dict`
  (Task 2 of sub-project 3), `SandboxManager.run_shell(task_id, command,
  timeout=120) -> dict` (same).
- Produces: `provision_secret_file(sandbox_manager, task_id: int, secret_value:
  str) -> str` (writes `secret_value` to a path like
  `/secrets/<uuid4>.token` via `write_file`, returns the path — raises if
  the underlying `write_file` call fails). `redact_secret(text: str,
  secret_value: str | None) -> str` (returns `text` unchanged if
  `secret_value` is `None` or empty; otherwise replaces every literal
  occurrence of `secret_value` in `text` with `"***REDACTED***"`).

- [ ] **Step 1: Write the failing test: `provision_secret_file` writes the secret and returns a path under `/secrets/`**

```python
def test_provision_secret_file_writes_and_returns_path(tmp_path, db_session, task):
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=lambda: db_session, cap=2)
    manager.get_or_create_container(task.id)
    path = provision_secret_file(manager, task.id, "s3cr3t-value")
    assert path.startswith("/secrets/")
    result = manager.read_file(task.id, path)
    assert result["output"].strip() == "s3cr3t-value"
```

- [ ] **Step 2: Run test to verify it fails.**
- [ ] **Step 3: Implement `provision_secret_file`.** Build the path as `f"/secrets/{uuid.uuid4().hex}.token"`, call `sandbox_manager.write_file(task_id, path, secret_value)`, return `path`. Raise `RuntimeError(result["output"])` if `write_file`'s result has `ok=False`.
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: `redact_secret` replaces every occurrence; is a no-op for `None`/empty**

```python
def test_redact_secret_replaces_every_occurrence_and_handles_none():
    text = "error: token s3cr3t-value rejected (raw: s3cr3t-value)"
    assert redact_secret(text, "s3cr3t-value") == (
        "error: token ***REDACTED*** rejected (raw: ***REDACTED***)"
    )
    assert redact_secret(text, None) == text
    assert redact_secret(text, "") == text
```

- [ ] **Step 6: Run test to verify it fails, then passes.**
- [ ] **Step 7: Run the full backend suite** — Expected: all PASS.
- [ ] **Step 8: Commit**

```bash
git add backend/app/connectors/secret_injection.py backend/tests/test_secret_injection.py
git commit -m "feat: add shared secret-file provisioning and output redaction helpers"
```

---

## Task 4: Forge-trigger tools (`request_connector`, `register_connector`, `vault_provision_test_credential`)

**Files:**
- Modify: `backend/app/agent/tools.py`
- Test: `backend/tests/test_connector_tools.py`

**Interfaces:**
- Consumes: `ToolContext` (gains a `vault: object | None = None` field,
  mirroring `sandbox_manager`/`browser_service`), `delegate_task_tool`
  (existing), `app.connectors.registry.{create_pending, find_active,
  find_pending}` (Task 2), `app.connectors.secret_injection.
  provision_secret_file` (Task 3).
- Produces: `request_connector_tool` (name `"request_connector"`, params
  `{service_name: str, purpose: str}`); `register_connector_tool` (name
  `"register_connector"`, params `{name: str, service_name: str, purpose:
  str, parameters_schema: object, source_code: str, required_hosts: array
  of str, test_transcript: object}`); `vault_provision_test_credential_tool`
  (name `"vault_provision_test_credential"`, params `{service_name: str}`,
  returns `{"ok": True, "output": "credential available at <path>" |
  "no credential configured for <service_name>", "data": {"path": str |
  None}}` — never the raw secret value).

- [ ] **Step 1: Write the failing test: `request_connector` delegates a coding sub-task when nothing exists yet for that service**

```python
def test_request_connector_delegates_coding_subtask_when_none_exists(db_session, task):
    scheduler = FakeScheduler()
    ctx = ToolContext(task_id=task.id, session=db_session, scheduler=scheduler)
    result = request_connector_tool.run({"service_name": "github", "purpose": "create issues"}, ctx)
    assert result["ok"] is True
    assert len(scheduler.enqueued) == 1
    child = db_session.get(Task, scheduler.enqueued[0])
    assert child.agent_type == "coding"
    assert "github" in child.description
```

(`FakeScheduler` with an `enqueue(session, task_id, front=False)` recording
call, matching the shape `delegate_task_tool` already expects from
`ctx.scheduler` — reuse whatever fake the existing `test_scheduler.py`/
`test_sandbox_tools.py` tests already define for this, or add a small one
local to this test file if none fits.)

- [ ] **Step 2: Run test to verify it fails.**
- [ ] **Step 3: Implement `request_connector_tool`.** Check `find_active(ctx.session, service_name, purpose)` first — if found, return `{"ok": True, "output": f"connector '{found.name}' already active for {service_name}", "data": {"connector_name": found.name}}`. Else check `find_pending(ctx.session, service_name)` — if found, return `{"ok": True, "output": f"a connector for {service_name} is already awaiting approval", "data": None}`. Else call `delegate_task_tool.run({"description": <forge brief — see Step 3a>, "mode": "background", "agent_type": "coding"}, ctx)` and return its result.
- [ ] **Step 3a: Write the forge brief as a plain f-string** inside `request_connector_tool.run`, e.g.: `f"Build and sandbox-test an integration for the service '{service_name}'. Purpose: {purpose}. Use vault_provision_test_credential('{service_name}') to get a credential file path if one exists (read it by path, never hardcode a value). Write a single Python module exposing a function with the shape (name, description, parameters_schema, and a callable entry point reading JSON arguments from stdin and printing a JSON {{'ok','output','data'}} result to stdout). When it passes its own test, call register_connector(...) with that module's source as source_code. If no credential exists, test against a stub you write yourself and call report_gap with options to add a credential to the vault and retry."` (exact wording is the implementer's call; the required elements — mention `vault_provision_test_credential`, the stdin/stdout JSON contract, and the `register_connector` call — are not).
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: `request_connector` short-circuits to the existing tool when an identical `(service_name, purpose)` pair is already `active`, and separately when one is already `pending_review`**

```python
def test_request_connector_short_circuits_active_and_pending(db_session, task):
    create_pending(
        db_session, name="github_create_issue", service_name="github",
        purpose="create issues", parameters_schema={}, source_code="",
        required_hosts=[], requested_by_task_id=task.id, test_transcript={},
    )
    scheduler = FakeScheduler()
    ctx = ToolContext(task_id=task.id, session=db_session, scheduler=scheduler)
    pending_result = request_connector_tool.run({"service_name": "github", "purpose": "create issues"}, ctx)
    assert pending_result["ok"] is True
    assert scheduler.enqueued == []

    approve(db_session, db_session.query(Connector).one().id, approved_by="user")
    active_result = request_connector_tool.run({"service_name": "github", "purpose": "create issues"}, ctx)
    assert active_result["data"]["connector_name"] == "github_create_issue"
    assert scheduler.enqueued == []
```

- [ ] **Step 6: Run test to verify it fails, then passes.**
- [ ] **Step 7: Write the failing test: `register_connector` inserts a `pending_review` row with `requested_by_task_id` set to the calling task**

```python
def test_register_connector_inserts_pending_row(db_session, task):
    ctx = ToolContext(task_id=task.id, session=db_session)
    result = register_connector_tool.run(
        {
            "name": "github_create_issue", "service_name": "github", "purpose": "create issues",
            "parameters_schema": {"type": "object"}, "source_code": "print('hi')",
            "required_hosts": ["api.github.com"], "test_transcript": {"ok": True},
        },
        ctx,
    )
    assert result["ok"] is True
    row = db_session.query(Connector).one()
    assert row.status == "pending_review"
    assert row.requested_by_task_id == task.id
```

- [ ] **Step 8: Run test to verify it fails.**
- [ ] **Step 9: Implement `register_connector_tool`** via `create_pending(ctx.session, ..., requested_by_task_id=ctx.task_id)`; return `{"ok": True, "output": f"connector '{name}' registered, pending review", "data": {"connector_id": row.id}}`.
- [ ] **Step 10: Run test to verify it passes.**
- [ ] **Step 11: Write the failing-or-passing test: `register_connector` with a malformed `parameters_schema` (missing `type`/`properties`) still lands a `pending_review` row rather than crashing or being rejected**

```python
def test_register_connector_accepts_malformed_parameters_schema(db_session, task):
    ctx = ToolContext(task_id=task.id, session=db_session)
    result = register_connector_tool.run(
        {
            "name": "weird_tool", "service_name": "weird", "purpose": "x",
            "parameters_schema": {"not_a_real_schema": True}, "source_code": "",
            "required_hosts": [], "test_transcript": {},
        },
        ctx,
    )
    assert result["ok"] is True
    row = db_session.query(Connector).filter_by(name="weird_tool").one()
    assert row.status == "pending_review"
    assert row.parameters_schema == {"not_a_real_schema": True}
```

- [ ] **Step 12: Run the test** — if Step 9's implementation stores `parameters_schema` as given with no validation (as specified), this should already pass; a failure means something in `create_pending`/`register_connector_tool` is validating or mutating the schema when it shouldn't — fix that, not this test.
- [ ] **Step 13: Write the failing test: `vault_provision_test_credential` returns a path when a credential exists, and a clear "no credential" message (not an error) when it doesn't — never the raw value either way**

```python
def test_vault_provision_test_credential_present_and_absent(tmp_path, db_session, task):
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=lambda: db_session, cap=2)
    manager.get_or_create_container(task.id)
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault.set("github", "ghp_abc123")
    ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=vault)

    present = vault_provision_test_credential_tool.run({"service_name": "github"}, ctx)
    assert present["ok"] is True
    assert "ghp_abc123" not in present["output"]
    assert present["data"]["path"].startswith("/secrets/")

    absent = vault_provision_test_credential_tool.run({"service_name": "slack"}, ctx)
    assert absent["ok"] is True
    assert absent["data"]["path"] is None
```

- [ ] **Step 14: Run test to verify it fails.**
- [ ] **Step 15: Implement `vault_provision_test_credential_tool`** using `ctx.vault.get(service_name)` and, if present, `provision_secret_file(ctx.sandbox_manager, ctx.task_id, secret)` from Task 3.
- [ ] **Step 16: Run test to verify it passes.**
- [ ] **Step 17: Add `vault: object | None = None` to `ToolContext`.**
- [ ] **Step 18: Run the full backend suite** — Expected: all PASS.
- [ ] **Step 19: Commit**

```bash
git add backend/app/agent/tools.py backend/tests/test_connector_tools.py
git commit -m "feat: add request_connector/register_connector/vault_provision_test_credential tools"
```

---

## Task 5: `DynamicConnectorTool`

**Files:**
- Create: `backend/app/connectors/dynamic_tool.py`
- Test: `backend/tests/test_dynamic_connector_tool.py`

**Interfaces:**
- Consumes: `Connector` model (Task 2), `provision_secret_file`/
  `redact_secret` (Task 3), `ToolContext.vault`/`ToolContext.
  sandbox_manager` (Task 4/existing), `SandboxManager.write_file`/
  `run_shell`/`read_file` (sub-project 3).
- Produces: `DynamicConnectorTool(connector: Connector)` — a `Tool` whose
  `.name`/`.description`/`.parameters_schema` mirror the `connector` row's
  fields, and whose `run(arguments, ctx) -> dict` implements spec §6.
  `_NO_SANDBOX_RESULT`-shaped constant reused from `app.agent.tools` (import
  it rather than redefining — check its exact current name/shape in
  `backend/app/agent/tools.py` before writing this).

- [ ] **Step 1: Write the failing test: calling a connector with no sandbox manager returns the existing no-sandbox failure shape**

```python
def test_dynamic_connector_tool_no_sandbox_manager_fails_gracefully(db_session, task):
    connector = Connector(name="github_create_issue", service_name="github", purpose="x",
                           parameters_schema={"type": "object"}, source_code="print('hi')",
                           required_hosts=[], status="active")
    db_session.add(connector); db_session.commit()
    tool = DynamicConnectorTool(connector)
    ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=None)
    result = tool.run({}, ctx)
    assert result["ok"] is False
```

- [ ] **Step 2: Run test to verify it fails.**
- [ ] **Step 3: Implement the `ctx.sandbox_manager is None` guard** at the top of `run`, returning `app.agent.tools._NO_SANDBOX_RESULT` (the existing module-level constant `{"ok": False, "output": "sandboxed execution is not available for this task", "data": None}`) — import and reuse it, don't redefine it.
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: a connector with no credential needed runs via `run_shell` and returns the parsed stdout JSON**

```python
def test_dynamic_connector_tool_runs_without_credential(tmp_path, db_session, task):
    source = (
        "import sys, json\n"
        "args = json.load(sys.stdin)\n"
        "print(json.dumps({'ok': True, 'output': f\"got {args}\", 'data': None}))\n"
    )
    connector = Connector(name="echo_tool", service_name="echo", purpose="x",
                           parameters_schema={"type": "object"}, source_code=source,
                           required_hosts=[], status="active")
    db_session.add(connector); db_session.commit()
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=lambda: db_session, cap=2)
    tool = DynamicConnectorTool(connector)
    ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager)
    result = tool.run({"x": 1}, ctx)
    assert result["ok"] is True
    assert "got" in result["output"]
```

- [ ] **Step 6: Run test to verify it fails.**
- [ ] **Step 7: Implement the source-caching + invoke + parse path.** Cache `source_code` to `/connectors/{connector.name}.py` in the task's sandbox via `write_file`, skipping the write if a prior call in the SAME `run()` process already did it for this `(task_id, connector.name)` pair (track via a module-level or instance-level set, keyed exactly as Global Constraints specifies). Build the invocation command (feed `json.dumps(arguments)` on stdin via a heredoc, same technique `write_file` already uses for safe content embedding — check `manager.py`'s existing heredoc marker pattern before reinventing one). Parse stdout as JSON; on `json.JSONDecodeError` or non-zero exit, return `{"ok": False, "output": <stderr or raw stdout>, "data": None}`.
- [ ] **Step 8: Run test to verify it passes.**
- [ ] **Step 9: Write the failing test: a connector needing a credential gets it via a provisioned file, the file is removed after the call, and the credential never appears in the returned result even if the script echoes it**

```python
def test_dynamic_connector_tool_provisions_and_shreds_credential(tmp_path, db_session, task):
    source = (
        "import sys, json\n"
        "path = sys.argv[1]\n"
        "secret = open(path).read().strip()\n"
        "print(json.dumps({'ok': True, 'output': f'used token {secret}', 'data': None}))\n"
    )
    connector = Connector(name="github_create_issue", service_name="github", purpose="x",
                           parameters_schema={"type": "object"}, source_code=source,
                           required_hosts=[], status="active")
    db_session.add(connector); db_session.commit()
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=lambda: db_session, cap=2)
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault.set("github", "s3cr3t-value")
    tool = DynamicConnectorTool(connector)
    ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=vault)

    result = tool.run({}, ctx)
    assert result["ok"] is True
    assert "s3cr3t-value" not in result["output"]
    assert "***REDACTED***" in result["output"]
    # the secret file must not survive the call
    volume_dir = tmp_path / manager._volume_name(task.id)
    assert not any(p.name.endswith(".token") for p in volume_dir.glob("secrets/*"))
```

- [ ] **Step 10: Run test to verify it fails.**
- [ ] **Step 11: Implement the credential path.** Before invoking, call `ctx.vault.get(connector.service_name)` if `ctx.vault` is set; if a secret comes back, provision it via `provision_secret_file` (Task 3), pass its path as `sys.argv[1]` to the invocation, and append `; rm -f {secret_path}` (chained after capturing the real exit code, per spec §6 step 3) to the shell command so it's removed unconditionally. After getting the result, run `redact_secret(result["output"], secret)` (Task 3) before returning.
- [ ] **Step 12: Run test to verify it passes.**
- [ ] **Step 13: Write the failing test: two different tasks calling the same connector each get their own cached source file and their own independent, separately-shredded secret file — no crosstalk**

```python
def test_dynamic_connector_tool_isolates_cache_and_secret_per_task(tmp_path, db_session, session_factory):
    task_a = Task(description="a", status="running"); db_session.add(task_a)
    task_b = Task(description="b", status="running"); db_session.add(task_b)
    db_session.commit()
    source = (
        "import sys, json\n"
        "path = sys.argv[1]\n"
        "secret = open(path).read().strip()\n"
        "print(json.dumps({'ok': True, 'output': f'used {secret}', 'data': None}))\n"
    )
    connector = Connector(name="github_create_issue", service_name="github", purpose="x",
                           parameters_schema={"type": "object"}, source_code=source,
                           required_hosts=[], status="active")
    db_session.add(connector); db_session.commit()
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=session_factory, cap=3)
    vault = Vault(Vault.generate_key(), session_factory=session_factory)
    vault.set("github", "secret-for-both-tasks")
    tool = DynamicConnectorTool(connector)

    ctx_a = ToolContext(task_id=task_a.id, session=db_session, sandbox_manager=manager, vault=vault)
    ctx_b = ToolContext(task_id=task_b.id, session=db_session, sandbox_manager=manager, vault=vault)
    result_a = tool.run({}, ctx_a)
    result_b = tool.run({}, ctx_b)
    assert result_a["ok"] is True and result_b["ok"] is True

    # each task's own sandbox volume has its own cached source copy
    assert (tmp_path / manager._volume_name(task_a.id) / "connectors" / "github_create_issue.py").exists()
    assert (tmp_path / manager._volume_name(task_b.id) / "connectors" / "github_create_issue.py").exists()
    # and neither task's volume has a leftover secret file after both calls
    for tid in (task_a.id, task_b.id):
        volume_dir = tmp_path / manager._volume_name(tid)
        assert not any(p.name.endswith(".token") for p in volume_dir.glob("secrets/*"))
```

- [ ] **Step 14: Run test to verify it fails or passes** — if the source-cache key from Step 7 is correctly `(task_id, connector.name)` and each call provisions/shreds its own secret file (Step 11), this should already pass; a failure here means the cache key or secret-file naming isn't actually task-scoped — fix Step 7/Step 11's implementation, not this test.
- [ ] **Step 15: Run the full backend suite** — Expected: all PASS.
- [ ] **Step 16: Commit**

```bash
git add backend/app/connectors/dynamic_tool.py backend/tests/test_dynamic_connector_tool.py
git commit -m "feat: add DynamicConnectorTool (sandboxed execution of forged connectors)"
```

---

## Task 6: Wire into `ToolRegistry`

**Files:**
- Modify: `backend/app/api/tasks.py`, `backend/app/main.py`

**Interfaces:**
- Consumes: `DynamicConnectorTool` (Task 5), `app.connectors.registry.
  list_active` (Task 2), `request_connector_tool`/`register_connector_tool`/
  `vault_provision_test_credential_tool` (Task 4), `get_session`/
  `get_session_factory` (existing `app.db`).
- Produces: `get_registry(session: Session = Depends(get_session)) ->
  ToolRegistry` (modified signature — now takes a session so it can query
  `list_active`), `get_vault(session_factory) -> Vault` (new, mirrors
  `get_sandbox_manager(session_factory)`'s existing lazy-override shape).

- [ ] **Step 1: Write the failing test: `get_registry` includes a `DynamicConnectorTool` for every `active` connector row, by name**

```python
def test_get_registry_loads_active_connectors(db_session, task):
    create_pending(
        db_session, name="github_create_issue", service_name="github", purpose="x",
        parameters_schema={"type": "object"}, source_code="print('hi')",
        required_hosts=[], requested_by_task_id=task.id, test_transcript={},
    )
    approve(db_session, db_session.query(Connector).one().id, approved_by="user")
    registry = get_registry(session=db_session)
    assert registry.get("github_create_issue") is not None
    assert registry.get("github_create_issue").name == "github_create_issue"
```

- [ ] **Step 2: Run test to verify it fails.**
- [ ] **Step 3: Modify `get_registry`** to accept `session: Session = Depends(get_session)`, register the three new tools from Task 4 (`request_connector_tool`, `register_connector_tool`, `vault_provision_test_credential_tool`) alongside the existing built-ins, then `for connector in list_active(session): registry.register(DynamicConnectorTool(connector))`.
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Implement `get_vault(session_factory)` in `backend/app/api/tasks.py`**, mirroring `get_sandbox_manager`'s existing shape: reads `VAULT_ENCRYPTION_KEY` from the environment (raise a clear `RuntimeError` if unset — matching the fail-closed precedent `validate_sandbox_egress_config` already set in sub-project 3, Task 9), constructs `Vault(key, session_factory)`.
- [ ] **Step 6: Update `backend/app/main.py`'s `lifespan`** to build `vault = app.dependency_overrides.get(get_vault, get_vault)(session_factory)` alongside the existing `sandbox_manager`/`browser_service` construction, and update the `registry_factory` lambda to `lambda: app.dependency_overrides.get(get_registry, get_registry)(session_factory())` (so the Scheduler's per-task-fresh registry build — already the existing call pattern in `Scheduler._run_one` — naturally picks up newly approved connectors with no separate reload mechanism needed). Thread `vault=vault` into wherever `ToolContext` is constructed for a live task run (check `run_task_loop`'s signature in `backend/app/agent/loop.py` and the `approve_task` endpoint in `tasks.py` — both need a `vault` param added the same way `sandbox_manager`/`browser_service` were added in earlier sub-projects, and `Scheduler`/`_run_one` needs to forward it into the real `run_fn` call — follow the exact precedent of how `browser_service` was forwarded in sub-project 3, Task 8's fix round, including both call-site branches in `_run_one`, not just one).
- [ ] **Step 7: Run the full backend suite, including the existing test client fixture** — Run: `cd backend && PYTHONPATH=. python3 -m pytest -q` — Expected: all PASS. Pay particular attention to any test that constructs a `TestClient`/triggers `lifespan` without `VAULT_ENCRYPTION_KEY` set — `backend/tests/conftest.py`'s `client` fixture will need a default env var set (mirroring the existing `_default_sandbox_egress_allowlist` autouse fixture) so existing tests don't all break on the new fail-closed vault key check.
- [ ] **Step 8: Commit**

```bash
git add backend/app/api/tasks.py backend/app/main.py backend/app/agent/loop.py backend/app/scheduler.py backend/tests/conftest.py
git commit -m "feat: wire DynamicConnectorTool and Vault into the live ToolRegistry/Scheduler"
```

---

## Task 7: Approval API

**Files:**
- Create: `backend/app/api/connectors.py`
- Modify: `backend/app/main.py` (register the new router)
- Test: `backend/tests/test_api_connectors.py`

**Interfaces:**
- Consumes: `app.connectors.registry.{list_pending, approve, reject}`
  (Task 2), `require_auth` (existing `app.auth`).
- Produces: `GET /connectors/pending` → `[{"id", "name", "service_name",
  "purpose", "source_code", "test_transcript", "required_hosts",
  "missing_egress_hosts": [...]}]` (the last field computed by diffing
  `required_hosts` against `os.environ.get("SANDBOX_EGRESS_ALLOWLIST",
  "").split(",")`, stripped/lowercased); `POST /connectors/{id}/approve` →
  the approved row's `{"id", "name", "status"}` or `400` on a name
  collision (surface `registry.approve`'s `ValueError` as an HTTP 400);
  `POST /connectors/{id}/reject` → the rejected row's `{"id", "status"}`.

- [ ] **Step 1: Write the failing test: `GET /connectors/pending` lists a pending row and flags a missing egress host**

```python
def test_list_pending_connectors_flags_missing_egress_host(client, db_session, task, monkeypatch):
    monkeypatch.setenv("SANDBOX_EGRESS_ALLOWLIST", "api.example.com")
    create_pending(
        db_session, name="github_create_issue", service_name="github", purpose="x",
        parameters_schema={}, source_code="", required_hosts=["api.github.com"],
        requested_by_task_id=task.id, test_transcript={},
    )
    resp = client.get("/connectors/pending")
    assert resp.status_code == 200
    row = resp.json()[0]
    assert row["missing_egress_hosts"] == ["api.github.com"]
```

- [ ] **Step 2: Run test to verify it fails.**
- [ ] **Step 3: Implement `GET /connectors/pending` in `backend/app/api/connectors.py`.**
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the failing test: approve then reject endpoints**

```python
def test_approve_and_reject_endpoints(client, db_session, task):
    row = create_pending(
        db_session, name="github_create_issue", service_name="github", purpose="x",
        parameters_schema={}, source_code="", required_hosts=[],
        requested_by_task_id=task.id, test_transcript={},
    )
    resp = client.post(f"/connectors/{row.id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"

    row2 = create_pending(
        db_session, name="github_list_issues", service_name="github", purpose="y",
        parameters_schema={}, source_code="", required_hosts=[],
        requested_by_task_id=task.id, test_transcript={},
    )
    resp2 = client.post(f"/connectors/{row2.id}/reject")
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "rejected"
```

- [ ] **Step 6: Run test to verify it fails.**
- [ ] **Step 7: Implement `POST /connectors/{id}/approve` and `POST /connectors/{id}/reject`**, catching `registry.approve`'s `ValueError` and returning it as `HTTPException(400, str(exc))`.
- [ ] **Step 8: Run test to verify it passes.**
- [ ] **Step 9: Write the failing test: approving a connector whose name collides returns 400 and leaves the row `pending_review`**

```python
def test_approve_name_collision_returns_400_and_leaves_pending(client, db_session, task):
    row = create_pending(
        db_session, name="run_shell", service_name="evil", purpose="x",
        parameters_schema={}, source_code="", required_hosts=[],
        requested_by_task_id=task.id, test_transcript={},
    )
    resp = client.post(f"/connectors/{row.id}/approve")
    assert resp.status_code == 400
    db_session.expire_all()
    assert db_session.get(Connector, row.id).status == "pending_review"
```

- [ ] **Step 10: Run test to verify it fails, then passes.**
- [ ] **Step 11: Register the router in `backend/app/main.py`** (`app.include_router(connectors_router)`, matching how `dashboard_router`/`tasks_router` are already registered).
- [ ] **Step 12: Run the full backend suite** — Expected: all PASS.
- [ ] **Step 13: Commit**

```bash
git add backend/app/api/connectors.py backend/app/main.py backend/tests/test_api_connectors.py
git commit -m "feat: add connector approval API (list pending, approve, reject)"
```

---

## Task 8: End-to-end integration + leak regression

**Files:**
- Test: `backend/tests/test_connector_forge_e2e.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7.

- [ ] **Step 1: Write the failing test: a full forge-and-call cycle leaves no trace of the credential anywhere in the calling task's `events` table**

```python
def test_full_forge_cycle_never_leaks_credential_into_events(tmp_path, db_session, session_factory, task):
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=session_factory, cap=2)
    manager.get_or_create_container(task.id)
    vault = Vault(Vault.generate_key(), session_factory=session_factory)
    vault.set("github", "s3cr3t-value")

    source = (
        "import sys, json\n"
        "path = sys.argv[1] if len(sys.argv) > 1 else None\n"
        "secret = open(path).read().strip() if path else None\n"
        "print(json.dumps({'ok': True, 'output': f'called github with {secret}', 'data': None}))\n"
    )
    register_ctx = ToolContext(task_id=task.id, session=db_session)
    register_connector_tool.run(
        {
            "name": "github_create_issue", "service_name": "github", "purpose": "create issues",
            "parameters_schema": {"type": "object"}, "source_code": source,
            "required_hosts": ["api.github.com"], "test_transcript": {"ok": True},
        },
        register_ctx,
    )
    connector_row = db_session.query(Connector).one()
    approve(db_session, connector_row.id, approved_by="user")

    registry = ToolRegistry()
    registry.register(DynamicConnectorTool(connector_row))
    tool = registry.get("github_create_issue")
    call_ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=vault)
    result = tool.run({}, call_ctx)
    assert result["ok"] is True
    assert "s3cr3t-value" not in result["output"]

    # Simulate the agent loop persisting this result, same as run_task_loop does
    db_session.add(Event(task_id=task.id, type="tool_result", payload=result))
    db_session.commit()

    all_event_text = " ".join(str(e.payload) for e in db_session.query(Event).filter_by(task_id=task.id).all())
    assert "s3cr3t-value" not in all_event_text
```

- [ ] **Step 2: Run test to verify it fails or passes** — if Tasks 1–7 were each independently verified, this should already PASS; its purpose is as a standing regression test, not to drive new implementation. If it fails, the failure identifies a real gap in an earlier task — fix the earlier task's code, not this test.
- [ ] **Step 3: Write a second failing-or-passing test: the `request_connector` → `register_connector` → approve → call path works through the real `ToolRegistry`/`get_registry` wiring from Task 6, not a hand-assembled registry** (same shape as Step 1 but calling `get_registry(session=db_session)` instead of manually constructing a `ToolRegistry`/`DynamicConnectorTool`, proving Task 6's wiring is what actually exposes a newly approved connector).
- [ ] **Step 4: Run both tests, confirm pass.**
- [ ] **Step 5: Run the full backend suite one final time** — Run: `cd backend && PYTHONPATH=. python3 -m pytest -q` — Expected: all PASS (re-run the known pre-existing flaky test in isolation if it's the only failure).
- [ ] **Step 6: Commit**

```bash
git add backend/tests/test_connector_forge_e2e.py
git commit -m "test: add end-to-end connector-forge cycle and credential-leak regression test"
```
