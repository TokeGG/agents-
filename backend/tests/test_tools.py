import httpx

from app.agent.tools import ToolContext, ToolRegistry, http_get_tool, report_gap_tool
from app.models import Event


def test_registry_lookup():
    registry = ToolRegistry()
    registry.register(http_get_tool)
    assert registry.get("http_get") is http_get_tool
    assert registry.get("nonexistent") is None
    assert any(s["name"] == "http_get" for s in registry.specs())


def test_report_gap_persists_event(db_session, task):
    ctx = ToolContext(task_id=task.id, session=db_session)
    result = report_gap_tool.run(
        {"reason": "no API key for X", "options": ["add key to vault", "use browser instead"]}, ctx
    )
    assert result["ok"] is True
    event = db_session.query(Event).filter_by(task_id=task.id, type="report_gap").one()
    assert event.payload["options"] == ["add key to vault", "use browser instead"]


def test_http_get_tool_returns_status_in_output(db_session, task, monkeypatch):
    ctx = ToolContext(task_id=task.id, session=db_session)

    def fake_get(url):
        return httpx.Response(200, text="hello", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    result = http_get_tool.run({"url": "http://x"}, ctx)

    assert result["ok"] is True
    assert "200" in result["output"]
