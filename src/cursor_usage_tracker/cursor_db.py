"""Read-only adapter for Cursor's undocumented local state database."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cursor_usage_tracker.domain import ResolvedModel
from cursor_usage_tracker.storage import Storage

UNRESOLVED_MODELS = {"", "auto", "default"}


class CursorDatabaseError(RuntimeError):
    """Raised when Cursor's local schema is missing or incompatible."""


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Counts produced by one enrichment pass."""

    resolved_models: int
    selected_models: int
    ignored_models: int


def sync_cursor_database(cursor_path: Path, storage: Storage) -> SyncResult:
    """Enrich tracker rows from Cursor data without writing to Cursor's DB."""
    if not cursor_path.is_file():
        raise CursorDatabaseError(f"Cursor database not found: {cursor_path}")
    connection = _connect_read_only(cursor_path)
    try:
        _validate_schema(connection)
        resolved = _resolved_models(connection)
        selected = _selected_models(connection)
    except sqlite3.Error as error:
        raise CursorDatabaseError(
            "Cannot read Cursor's local state database"
        ) from error
    finally:
        connection.close()

    resolved_count = 0
    ignored_count = 0
    for resolved_model in resolved:
        if resolved_model.model.casefold() in UNRESOLVED_MODELS:
            ignored_count += 1
            continue
        updated = storage.apply_resolved_model(resolved_model)
        resolved_count += updated
        ignored_count += int(updated == 0)
    selected_count = 0
    for conversation_id, selected_model in selected:
        if selected_model.casefold() in UNRESOLVED_MODELS:
            ignored_count += 1
            continue
        updated = storage.apply_selected_model(
            conversation_id,
            selected_model,
            "cursor_composer_config",
        )
        selected_count += updated
        ignored_count += int(updated == 0)
    return SyncResult(
        resolved_models=resolved_count,
        selected_models=selected_count,
        ignored_models=ignored_count,
    )


def cursor_database_health(cursor_path: Path) -> tuple[bool, str]:
    """Check availability and expected tables without mutating the DB."""
    try:
        connection = _connect_read_only(cursor_path)
        try:
            _validate_schema(connection)
            count = connection.execute(
                "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
            ).fetchone()
        finally:
            connection.close()
    except (CursorDatabaseError, sqlite3.Error) as error:
        return False, str(error)
    bubble_count = int(count[0]) if count is not None else 0
    return True, f"readable, {bubble_count} bubble records"


def _connect_read_only(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5
        )
    except sqlite3.Error as error:
        raise CursorDatabaseError(f"Cannot open Cursor database: {path}") from error
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _validate_schema(connection: sqlite3.Connection) -> None:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cursorDiskKV'"
    ).fetchone()
    if table is None:
        raise CursorDatabaseError("Cursor database has no cursorDiskKV table")


def _resolved_models(connection: sqlite3.Connection) -> list[ResolvedModel]:
    rows = connection.execute(
        """
        SELECT key,
               json_extract(value, '$.requestId') AS generation_id,
               json_extract(value, '$.modelInfo.modelName') AS model
        FROM cursorDiskKV
        WHERE key LIKE 'bubbleId:%'
          AND json_type(value, '$.requestId') = 'text'
          AND json_type(value, '$.modelInfo.modelName') = 'text'
        """
    )
    models: list[ResolvedModel] = []
    for row in rows:
        parts = str(row["key"]).split(":", 2)
        if len(parts) != 3:
            continue
        models.append(
            ResolvedModel(
                conversation_id=parts[1],
                generation_id=str(row["generation_id"]),
                model=str(row["model"]),
                source="cursor_bubble_model_info",
                confidence="high",
            )
        )
    return models


def _selected_models(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = connection.execute(
        """
        SELECT substr(key, length('composerData:') + 1) AS conversation_id,
               COALESCE(
                   json_extract(value, '$.modelConfig.selectedModels[0].modelId'),
                   json_extract(value, '$.modelConfig.modelName')
               ) AS model
        FROM cursorDiskKV
        WHERE key LIKE 'composerData:%'
        """
    )
    return [
        (str(row["conversation_id"]), str(row["model"]))
        for row in rows
        if row["model"] is not None
    ]
