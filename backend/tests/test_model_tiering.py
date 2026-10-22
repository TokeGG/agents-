from app.agent.model_tiering import ToolFailureTracker


def test_tracker_escalates_after_three_identical_failures():
    tracker = ToolFailureTracker()
    assert tracker.record("run_shell", "ModuleNotFoundError: foo") == 1
    assert tracker.should_escalate() is False
    tracker.record("run_shell", "ModuleNotFoundError: foo")
    assert tracker.record("run_shell", "ModuleNotFoundError: foo") == 3
    assert tracker.should_escalate() is True
    assert tracker.record("run_shell", "SyntaxError: bar") == 1
    assert tracker.should_escalate() is False
