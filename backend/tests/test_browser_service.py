import threading
from concurrent.futures import ThreadPoolExecutor

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


def test_all_playwright_calls_run_on_single_internal_thread(monkeypatch):
    """Calls arriving from different Python threads must all actually be
    executed on BrowserService's one dedicated worker thread -- Playwright's
    sync API is not thread-safe and breaks if its objects are touched from
    more than one thread."""
    service = BrowserService()
    fake_browser = _FakeBrowser()
    monkeypatch.setattr(service, "_ensure_browser", lambda: fake_browser)

    seen_thread_ids: list[int] = []
    lock = threading.Lock()

    # Wrap the real impl to record the thread id it actually executes on,
    # while still doing real work.
    original_impl = service._get_or_create_context_impl

    def recording_impl(task_id):
        with lock:
            seen_thread_ids.append(threading.get_ident())
        return original_impl(task_id)

    monkeypatch.setattr(service, "_get_or_create_context_impl", recording_impl)

    callers = ThreadPoolExecutor(max_workers=8)
    futures = [
        callers.submit(service.get_or_create_context, task_id=i % 3) for i in range(20)
    ]
    for f in futures:
        f.result()
    callers.shutdown(wait=True)

    assert len(seen_thread_ids) == 20
    assert len({*seen_thread_ids}) == 1
