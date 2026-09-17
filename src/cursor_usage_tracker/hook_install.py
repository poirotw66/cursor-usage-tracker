"""Safe opt-in installation into Cursor's user hook configuration."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

TRACKED_EVENTS = (
    "sessionStart",
    "beforeSubmitPrompt",
    "postToolUse",
    "afterAgentResponse",
    "afterAgentThought",
    "subagentStart",
    "subagentStop",
    "preCompact",
    "stop",
)
COMMAND_MARKER = "cursor-usage hook"


class HookInstallError(ValueError):
    """Raised when an existing hook configuration is unsafe to merge."""


def install_hooks(hooks_path: Path, executable: Path) -> Path | None:
    """Merge tracker hooks, returning the backup path when changed."""
    configuration = _load_configuration(hooks_path)
    hooks = configuration.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise HookInstallError("The hooks property must be a JSON object")

    command = f"{shlex.quote(str(executable.resolve()))} hook"
    changed = False
    for event_name in TRACKED_EVENTS:
        entries = hooks.setdefault(event_name, [])
        if not isinstance(entries, list):
            raise HookInstallError(f"Hook {event_name!r} must be a JSON array")
        if any(_is_tracker_entry(entry) for entry in entries):
            continue
        entries.append({"command": command, "timeout": 10})
        changed = True

    if not changed:
        return None
    configuration["version"] = 1
    backup_path = _backup(hooks_path)
    _atomic_write(hooks_path, configuration)
    return backup_path


def _load_configuration(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"version": 1, "hooks": {}}
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HookInstallError(f"Cannot parse existing hooks file: {path}") from error
    if not isinstance(decoded, dict):
        raise HookInstallError("The hook configuration root must be a JSON object")
    return decoded


def _is_tracker_entry(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    command = value.get("command")
    return isinstance(command, str) and COMMAND_MARKER in command


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_name(f"{path.name}.{timestamp}.bak")
    shutil.copy2(path, backup_path)
    return backup_path


def _atomic_write(path: Path, configuration: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            json.dump(configuration, file_handle, ensure_ascii=False, indent=2)
            file_handle.write("\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        temporary_path.replace(path)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise
