from app.models import Task

IRREVERSIBLE_TOOL_NAMES: set[str] = {"send_email"}


def is_irreversible(tool_name: str) -> bool:
    return tool_name in IRREVERSIBLE_TOOL_NAMES


def needs_approval(tool_name: str, task: Task) -> bool:
    return is_irreversible(tool_name) and not task.full_auto
