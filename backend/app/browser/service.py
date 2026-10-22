"""BrowserService: lazily-launched shared Playwright browser with one
isolated BrowserContext per task.

The underlying Playwright `Browser` is expensive to launch, so it is shared
across the whole process and created only on first use. Each task gets its
own `BrowserContext` (cookies/storage isolated from other tasks), cached by
task_id and torn down via `close_context` when the task reaches a terminal
state (wired from `Scheduler._run_one`, mirroring `SandboxManager.teardown`).
"""

from __future__ import annotations


class BrowserService:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._contexts: dict[int, object] = {}

    def _ensure_browser(self):
        """Lazily starts Playwright and launches the shared Chromium browser
        on first use. Split out as its own method so tests can monkeypatch
        it with a fake browser instead of launching a real one."""
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch()
        return self._browser

    def get_or_create_context(self, task_id: int):
        if task_id in self._contexts:
            return self._contexts[task_id]
        browser = self._ensure_browser()
        context = browser.new_context()
        self._contexts[task_id] = context
        return context

    def close_context(self, task_id: int) -> None:
        context = self._contexts.pop(task_id, None)
        if context is not None:
            context.close()
