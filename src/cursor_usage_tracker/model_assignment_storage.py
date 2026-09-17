"""Persistence boundary for effective-dated model assumptions."""

from __future__ import annotations

import sqlite3

from cursor_usage_tracker.storage import Storage


class ModelAssignmentRepository:
    """Store and resolve scoped model assumptions."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def add(
        self,
        *,
        model: str,
        scope_type: str,
        scope_value: str,
        effective_from: str,
        created_at: str,
    ) -> None:
        """Store an effective-dated model assumption."""
        self.storage.execute(
            """
            INSERT INTO model_assignments (
                model, scope_type, scope_value, effective_from, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope_type, scope_value, effective_from)
            DO UPDATE SET model = excluded.model, created_at = excluded.created_at
            """,
            (model, scope_type, scope_value, effective_from, created_at),
        )

    def latest(self, *, scope_type: str, scope_value: str) -> sqlite3.Row | None:
        """Return the newest assignment for one exact scope."""
        rows = self.storage.query(
            """
            SELECT * FROM model_assignments
            WHERE scope_type = ? AND scope_value = ?
            ORDER BY effective_from DESC LIMIT 1
            """,
            (scope_type, scope_value),
        )
        return rows[0] if rows else None

    def resolve(
        self,
        *,
        conversation_id: str,
        project_label: str,
        effective_at: str,
    ) -> str | None:
        """Resolve session, repository, then global assignment precedence."""
        rows = self.storage.query(
            """
            SELECT model FROM model_assignments
            WHERE effective_from <= ?
              AND (
                  (scope_type = 'session' AND scope_value = ?)
                  OR (scope_type = 'repository' AND scope_value = ?)
                  OR (scope_type = 'global' AND scope_value = '*')
              )
            ORDER BY CASE scope_type
                         WHEN 'session' THEN 3
                         WHEN 'repository' THEN 2
                         ELSE 1
                     END DESC,
                     effective_from DESC
            LIMIT 1
            """,
            (effective_at, conversation_id, project_label),
        )
        return str(rows[0]["model"]) if rows else None
