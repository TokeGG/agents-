"""End-to-end connector-forge integration test + standing credential-leak
regression test.

This is intentionally a regression test, not a driver for new
implementation: if it fails, the failure identifies a real gap in an
earlier task's (1-7) code, which should be fixed there rather than
weakening this test.

The secret used throughout (`SECRET`) deliberately contains a double-quote
and a backslash: a plain alphanumeric secret is caught by the simple
raw-text redaction pass alone and would never actually exercise the
recursive `_redact_structure` JSON-unescaping fix in dynamic_tool.py. The
fixture connector script also echoes the secret into `data` (a nested dict,
a list, and a dict key), not just `output`, since a regression of "redact
`output` but forget `data`" would otherwise go undetected.
"""

from app.agent.loop import run_task_loop
from app.agent.model_adapter import FakeModelAdapter
from app.agent.tools import ToolContext, ToolRegistry, register_connector_tool
from app.api.tasks import get_registry
from app.connectors.dynamic_tool import DynamicConnectorTool
from app.connectors.registry import Connector, approve
from app.models import Event
from app.sandbox.manager import SandboxManager
from app.sandbox.runtime import FakeContainerRuntime
from app.vault import Vault

# Contains a double-quote and a backslash so that json.dumps() in the
# connector script escapes it in the raw JSON text (as \" and \\), and only
# un-escapes back to this literal form after json.loads() parses it. This
# means the raw-text redaction pass alone cannot catch it; only the
# recursive, post-parse `_redact_structure` walk can.
SECRET = 's3c"r3t\\val'

CONNECTOR_SOURCE = (
    "import sys, json\n"
    "path = sys.argv[1] if len(sys.argv) > 1 else None\n"
    "secret = open(path).read().strip() if path else None\n"
    "print(json.dumps({\n"
    "    'ok': True,\n"
    "    'output': f'called github with {secret}',\n"
    "    'data': {\n"
    "        'nested': {'token': secret},\n"
    "        'items': ['prefix', secret, 'suffix'],\n"
    "        secret: 'value-under-secret-key',\n"
    "    },\n"
    "}))\n"
)


def _find_all_strings(value):
    """Recursively collect every string appearing anywhere in a decoded JSON
    structure (dict keys, dict values, list items), so the leak assertion
    does not rely on a single `in` check against `str(payload)`, which could
    itself hide an escaped form."""
    strings = []
    if isinstance(value, str):
        strings.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            strings.extend(_find_all_strings(k))
            strings.extend(_find_all_strings(v))
    elif isinstance(value, list):
        for item in value:
            strings.extend(_find_all_strings(item))
    return strings


def _assert_secret_absent_everywhere(value, secret=SECRET):
    for s in _find_all_strings(value):
        assert secret not in s, f"secret leaked into: {s!r}"


def _setup_approved_github_connector(db_session, task_id):
    """Shared setup: register + approve a github connector whose source
    echoes SECRET into output and multiple places inside data. Returns the
    approved Connector row."""
    register_ctx = ToolContext(task_id=task_id, session=db_session)
    register_connector_tool.run(
        {
            "name": "github_create_issue", "service_name": "github", "purpose": "create issues",
            "parameters_schema": {"type": "object"}, "source_code": CONNECTOR_SOURCE,
            "required_hosts": ["api.github.com"], "test_transcript": {"ok": True},
        },
        register_ctx,
    )
    connector_row = db_session.query(Connector).filter_by(name="github_create_issue").one()
    approve(db_session, connector_row.id, approved_by="user")
    return connector_row


def test_full_forge_cycle_never_leaks_credential_into_events(tmp_path, db_session, session_factory, task):
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=session_factory, cap=2)
    manager.get_or_create_container(task.id)
    vault = Vault(Vault.generate_key(), session_factory=session_factory)
    vault.set("github", SECRET)

    connector_row = _setup_approved_github_connector(db_session, task.id)

    registry = ToolRegistry()
    registry.register(DynamicConnectorTool(connector_row))
    tool = registry.get("github_create_issue")
    call_ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=vault)
    result = tool.run({}, call_ctx)
    assert result["ok"] is True
    _assert_secret_absent_everywhere(result)

    # Simulate the agent loop persisting this result, same as run_task_loop does
    db_session.add(Event(task_id=task.id, type="tool_result", payload=result))
    db_session.commit()

    events = db_session.query(Event).filter_by(task_id=task.id).all()
    for event in events:
        _assert_secret_absent_everywhere(event.payload)


def test_full_forge_cycle_through_get_registry_wiring(tmp_path, db_session, session_factory, task):
    """Same shape as above, but goes through the real get_registry() wiring
    (Task 6) instead of a hand-assembled ToolRegistry/DynamicConnectorTool,
    proving a newly approved connector is actually exposed through the real
    FastAPI dependency wiring -- and that it is NOT exposed before approval."""
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=session_factory, cap=2)
    manager.get_or_create_container(task.id)
    vault = Vault(Vault.generate_key(), session_factory=session_factory)
    vault.set("github", SECRET)

    register_ctx = ToolContext(task_id=task.id, session=db_session)
    register_connector_tool.run(
        {
            "name": "github_create_issue", "service_name": "github", "purpose": "create issues",
            "parameters_schema": {"type": "object"}, "source_code": CONNECTOR_SOURCE,
            "required_hosts": ["api.github.com"], "test_transcript": {"ok": True},
        },
        register_ctx,
    )
    connector_row = db_session.query(Connector).filter_by(name="github_create_issue").one()

    # Gate check: a pending (not yet approved) connector must not be exposed
    # by get_registry.
    pending_registry = get_registry(session=db_session)
    assert pending_registry.get("github_create_issue") is None

    approve(db_session, connector_row.id, approved_by="user")

    # Real wiring: get_registry() builds the ToolRegistry from scratch and
    # registers every active connector, exactly as the FastAPI dependency
    # does per-request.
    registry = get_registry(session=db_session)
    tool = registry.get("github_create_issue")
    assert tool is not None

    call_ctx = ToolContext(task_id=task.id, session=db_session, sandbox_manager=manager, vault=vault)
    result = tool.run({}, call_ctx)
    assert result["ok"] is True
    _assert_secret_absent_everywhere(result)

    db_session.add(Event(task_id=task.id, type="tool_result", payload=result))
    db_session.commit()

    events = db_session.query(Event).filter_by(task_id=task.id).all()
    for event in events:
        _assert_secret_absent_everywhere(event.payload)


def test_full_forge_cycle_through_run_task_loop_never_leaks_credential(
    tmp_path, db_session, session_factory, task
):
    """Drives the connector call through the REAL run_task_loop (app/agent/loop.py),
    with a real ToolRegistry (via get_registry), a real SandboxManager, and a real
    Vault holding the credential. Every Event row asserted against here is written
    by run_task_loop itself (its own _add_event calls for tool_call/tool_result),
    not by this test -- proving the full production write path (loop -> ToolContext
    -> DynamicConnectorTool -> redaction -> Event persistence) never leaks the
    credential, end to end."""
    manager = SandboxManager(FakeContainerRuntime(tmp_path), session_factory=session_factory, cap=2)
    manager.get_or_create_container(task.id)
    vault = Vault(Vault.generate_key(), session_factory=session_factory)
    vault.set("github", SECRET)

    _setup_approved_github_connector(db_session, task.id)

    registry = get_registry(session=db_session)
    assert registry.get("github_create_issue") is not None

    adapter = FakeModelAdapter(script=[
        {"type": "tool_call", "tool_name": "github_create_issue", "arguments": {}},
        {"type": "final_answer", "content": "done"},
    ])

    run_task_loop(
        task.id,
        db_session,
        adapter,
        registry,
        sandbox_manager=manager,
        vault=vault,
    )

    events = db_session.query(Event).filter_by(task_id=task.id).order_by(Event.id).all()
    assert any(e.type == "tool_result" for e in events)
    tool_result_events = [e for e in events if e.type == "tool_result"]
    assert any(e.payload.get("ok") is True for e in tool_result_events)
    for event in events:
        _assert_secret_absent_everywhere(event.payload)
