"""Application paths and runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_DIRECTORY_NAME = "cursor-usage-tracker"
DATABASE_ENVIRONMENT_VARIABLE = "CURSOR_USAGE_DB"
CURSOR_DATABASE_ENVIRONMENT_VARIABLE = "CURSOR_STATE_DB"


def default_data_directory() -> Path:
    """Return the platform-appropriate local application data directory."""
    if os.name == "posix" and Path.home().joinpath("Library").is_dir():
        return Path.home() / "Library" / "Application Support" / APP_DIRECTORY_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / (
        APP_DIRECTORY_NAME
    )


def default_cursor_database() -> Path:
    """Return the default Cursor global-state database path."""
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Cursor"
        / "User"
        / "globalStorage"
        / "state.vscdb"
    )


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated filesystem settings for one command invocation."""

    database_path: Path
    cursor_database_path: Path
    hooks_path: Path

    @classmethod
    def from_environment(cls) -> Settings:
        """Build settings from environment variables and safe local defaults."""
        database_path = Path(
            os.environ.get(
                DATABASE_ENVIRONMENT_VARIABLE,
                default_data_directory() / "usage.db",
            )
        ).expanduser()
        cursor_database_path = Path(
            os.environ.get(
                CURSOR_DATABASE_ENVIRONMENT_VARIABLE,
                default_cursor_database(),
            )
        ).expanduser()
        return cls(
            database_path=database_path,
            cursor_database_path=cursor_database_path,
            hooks_path=Path.home() / ".cursor" / "hooks.json",
        )
