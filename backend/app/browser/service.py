"""BrowserService: lazily-launched shared Playwright browser with one
isolated BrowserContext per task.

The underlying Playwright `Browser` is expensive to launch, so it is shared
across the whole process and created only on first use. Each task gets its
own `BrowserContext` (cookies/storage isolated from other tasks), cached by
task_id and torn down via `close_context` when the task reaches a terminal
state (wired from `Scheduler._run_one`, mirroring `SandboxManager.teardown`).

Playwright's SYNC API is NOT thread-safe: every object it hands back
(`Browser`, `BrowserContext`, `Page`, ...) is tied to the OS thread that
called `sync_playwright().start()`, and touching it from a different thread
raises ("cannot switch to a different thread") or misbehaves silently. Since
this service is shared across concurrently-running tasks -- each of which
may invoke it from whatever worker thread `asyncio.to_thread` happened to
schedule it on -- every actual Playwright call is marshaled through a single
dedicated worker thread owned by this service. Public methods submit the
real work to that one thread and block for the result, so the public API
stays plain synchronous and callable from any thread, exactly like today.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor


class BrowserService:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._contexts: dict[int, object] = {}
        # All real Playwright work happens on this single worker thread --
        # see the module docstring for why.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser-service")

    def _submit(self, fn, *args, **kwargs):
        return self._executor.submit(fn, *args, **kwargs).result()

    def _ensure_browser(self):
        """Lazily starts Playwright and launches the shared Chromium browser
        on first use. Split out as its own method so tests can monkeypatch
        it with a fake browser instead of launching a real one.

        Must only ever be called on this service's single worker thread."""
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch()
        return self._browser

    def get_or_create_context(self, task_id: int):
        return self._submit(self._get_or_create_context_impl, task_id)

    def _get_or_create_context_impl(self, task_id: int):
        if task_id in self._contexts:
            return self._contexts[task_id]
        browser = self._ensure_browser()
        context = browser.new_context()
        self._contexts[task_id] = context
        return context

    def close_context(self, task_id: int) -> None:
        self._submit(self._close_context_impl, task_id)

    def _close_context_impl(self, task_id: int) -> None:
        context = self._contexts.pop(task_id, None)
        if context is not None:
            context.close()

    def navigate(self, task_id: int, url: str) -> tuple[str, str]:
        """Navigate the task's context to `url` and return (title, visible
        text). All of context creation, page creation and navigation happen
        on the single Playwright thread."""
        return self._submit(self._navigate_impl, task_id, url)

    def _navigate_impl(self, task_id: int, url: str) -> tuple[str, str]:
        context = self._get_or_create_context_impl(task_id)
        page = context.new_page()
        page.goto(url)
        title = page.title()
        text = page.inner_text("body")
        return title, text

    def download(self, task_id: int, url: str) -> bytes:
        """Fetch `url`'s bytes through the task's browser context (so
        cookies/auth state from prior `navigate` calls apply)."""
        return self._submit(self._download_impl, task_id, url)

    def _download_impl(self, task_id: int, url: str) -> bytes:
        context = self._get_or_create_context_impl(task_id)
        response = context.request.get(url)
        return response.body()
