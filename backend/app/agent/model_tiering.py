"""Worker/reviewer model tiering support: tracks consecutive identical tool
failures within a single run_task_loop call so the loop can escalate a
"stuck" task from the cheap worker model to a (presumably stronger) reviewer
model.

A ToolFailureTracker is scoped to one run_task_loop invocation and is not
persisted across pause/resume: a resumed task gets a fresh tracker and starts
clean, rather than replaying history to re-derive a failure count. This keeps
the resume path simple and cheap (no extra event-replay bookkeeping) at the
cost of forgetting an escalation already in progress across a pause/resume
boundary (e.g. an ask_user or approval pause). Since the escalation signal is
itself cheap to re-derive (three more repeats of the same failure), and most
pauses are not themselves tool failures, this is an acceptable trade per the
Global Constraints' additive-cost principle.
"""

STUCK_REPEAT_THRESHOLD = 3


class ToolFailureTracker:
    """Tracks the current run of consecutive identical (tool_name,
    error_signature) failures."""

    def __init__(self) -> None:
        self._last_key: tuple[str, str] | None = None
        self._count = 0

    def record(self, tool_name: str, error_signature: str) -> int:
        key = (tool_name, error_signature)
        if key == self._last_key:
            self._count += 1
        else:
            self._last_key = key
            self._count = 1
        return self._count

    def should_escalate(self) -> bool:
        return self._count >= STUCK_REPEAT_THRESHOLD
