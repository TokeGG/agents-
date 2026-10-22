from app.browser.service import BrowserService


class _FakePage:
    def __init__(self):
        self.goto_calls: list[str] = []

    def goto(self, url):
        self.goto_calls.append(url)

    def title(self):
        return "Fake Title"

    def inner_text(self, selector):
        return "fake visible text"


class _FakeBrowserContext:
    def __init__(self, name):
        self.name = name
        self.closed = False

    def new_page(self):
        return _FakePage()

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.contexts_created = 0

    def new_context(self):
        self.contexts_created += 1
        return _FakeBrowserContext(f"ctx-{self.contexts_created}")


def test_get_or_create_context_is_per_task(monkeypatch):
    service = BrowserService()
    # Patch the underlying Playwright browser launch to a lightweight fake
    # for this test (do not launch a real browser in unit tests).
    fake_browser = _FakeBrowser()
    monkeypatch.setattr(service, "_ensure_browser", lambda: fake_browser)

    ctx_a1 = service.get_or_create_context(task_id=1)
    ctx_a2 = service.get_or_create_context(task_id=1)
    ctx_b = service.get_or_create_context(task_id=2)

    assert ctx_a1 is ctx_a2
    assert ctx_a1 is not ctx_b


def test_close_context_removes_cached_context(monkeypatch):
    service = BrowserService()
    fake_browser = _FakeBrowser()
    monkeypatch.setattr(service, "_ensure_browser", lambda: fake_browser)

    ctx1 = service.get_or_create_context(task_id=1)
    service.close_context(task_id=1)
    assert ctx1.closed is True

    ctx2 = service.get_or_create_context(task_id=1)
    assert ctx2 is not ctx1
