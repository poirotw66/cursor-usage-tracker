"""Persistence for privacy-preserving conversation context aggregates."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cursor_usage_tracker.storage import Storage


class ConversationContextRepository:
    """Maintain observable token context across turns and model calls."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def initialize_turn(
        self,
        generation_id: str,
        conversation_id: str,
        new_context_tokens: int,
        input_estimator: str,
        updated_at: str,
    ) -> int:
        """Initialize a request with its inherited observable context."""
        with self._storage.connect(write=True) as connection:
            inherited_context = self._load_or_reconstruct(
                connection,
                conversation_id,
                generation_id,
                updated_at,
            )
            base_input_tokens = inherited_context + new_context_tokens
            cursor = connection.execute(
                """
                INSERT INTO turn_runtime_metrics
                    VALUES (?, ?, 0, ?, 1, 0, 0, 0, 0)
                ON CONFLICT(generation_id) DO NOTHING
                """,
                (generation_id, base_input_tokens, base_input_tokens),
            )
            if cursor.rowcount == 0:
                return inherited_context
            connection.execute(
                """
                UPDATE conversation_context_metrics
                SET cumulative_context_tokens =
                        cumulative_context_tokens + ?,
                    updated_at = ?
                WHERE conversation_id = ?
                """,
                (new_context_tokens, updated_at, conversation_id),
            )
            connection.execute(
                """
                INSERT INTO estimation_metadata (
                    generation_id, input_estimator, output_estimator
                ) VALUES (?, ?, NULL)
                ON CONFLICT(generation_id) DO NOTHING
                """,
                (generation_id, input_estimator),
            )
            return inherited_context

    def record_tool_loop(
        self,
        generation_id: str,
        conversation_id: str,
        context_tokens: int,
        updated_at: str,
    ) -> None:
        """Account for the next model call after one tool result."""
        with self._storage.connect(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE turn_runtime_metrics
                SET estimated_input_tokens = estimated_input_tokens
                        + base_input_tokens + cumulative_context_tokens + ?,
                    cumulative_context_tokens = cumulative_context_tokens + ?,
                    estimated_model_calls = estimated_model_calls + 1
                WHERE generation_id = ?
                """,
                (context_tokens, context_tokens, generation_id),
            )
            if cursor.rowcount:
                self._add_context(
                    connection,
                    conversation_id,
                    context_tokens,
                    updated_at,
                )

    def record_compaction(
        self, generation_id: str, conversation_id: str, updated_at: str
    ) -> None:
        """Reset accumulated context when Cursor compacts a conversation."""
        with self._storage.connect(write=True) as connection:
            connection.execute(
                """
                UPDATE turn_runtime_metrics
                SET base_input_tokens = 0,
                    cumulative_context_tokens = 0,
                    compaction_count = compaction_count + 1
                WHERE generation_id = ?
                """,
                (generation_id,),
            )
            connection.execute(
                """
                INSERT INTO conversation_context_metrics
                    VALUES (?, 0, 1, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    cumulative_context_tokens = 0,
                    compaction_count = compaction_count + 1,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, updated_at),
            )

    def record_thinking(
        self,
        generation_id: str,
        conversation_id: str,
        tokens: int,
        duration_ms: int,
        updated_at: str,
    ) -> None:
        """Store visible reasoning aggregates without thought content."""
        with self._storage.connect(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE turn_runtime_metrics
                SET cumulative_context_tokens = cumulative_context_tokens + ?,
                    thinking_blocks = thinking_blocks + 1,
                    visible_thinking_tokens = visible_thinking_tokens + ?,
                    thinking_duration_ms = thinking_duration_ms + ?
                WHERE generation_id = ?
                """,
                (tokens, tokens, duration_ms, generation_id),
            )
            if cursor.rowcount:
                self._add_context(
                    connection,
                    conversation_id,
                    tokens,
                    updated_at,
                )

    def record_response(
        self, conversation_id: str, tokens: int, updated_at: str
    ) -> None:
        """Carry visible assistant output into the conversation's next turn."""
        with self._storage.connect(write=True) as connection:
            self._add_context(
                connection,
                conversation_id,
                tokens,
                updated_at,
            )

    def _load_or_reconstruct(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        generation_id: str,
        updated_at: str,
    ) -> int:
        context_row = connection.execute(
            """
            SELECT cumulative_context_tokens
            FROM conversation_context_metrics
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        if context_row is not None:
            return int(context_row[0])
        inherited_context, compaction_count = self._reconstruct(
            connection,
            conversation_id,
            generation_id,
            updated_at,
        )
        connection.execute(
            """
            INSERT INTO conversation_context_metrics VALUES (?, ?, ?, ?)
            """,
            (
                conversation_id,
                inherited_context,
                compaction_count,
                updated_at,
            ),
        )
        return inherited_context

    @staticmethod
    def _reconstruct(
        connection: sqlite3.Connection,
        conversation_id: str,
        generation_id: str,
        updated_at: str,
    ) -> tuple[int, int]:
        """Rebuild observable context for an existing conversation after upgrade."""
        compaction = connection.execute(
            """
            SELECT MAX(created_at), COUNT(*)
            FROM event_metrics
            WHERE conversation_id = ? AND category = 'compaction'
              AND created_at <= ?
            """,
            (conversation_id, updated_at),
        ).fetchone()
        boundary = str(compaction[0]) if compaction[0] is not None else ""
        compaction_count = int(compaction[1])
        turn_tokens = connection.execute(
            """
            SELECT COALESCE(SUM(prompt_tokens + attachment_tokens), 0)
            FROM turns
            WHERE conversation_id = ? AND generation_id != ?
              AND created_at > ? AND created_at <= ?
            """,
            (conversation_id, generation_id, boundary, updated_at),
        ).fetchone()
        event_tokens = connection.execute(
            """
            SELECT COALESCE(SUM(estimated_tokens), 0)
            FROM event_metrics
            WHERE conversation_id = ? AND created_at > ? AND created_at <= ?
              AND (
                  category IN ('tool_text', 'assistant', 'thinking')
                  OR (category = 'tool' AND lower(name) NOT LIKE '%screenshot%')
              )
            """,
            (conversation_id, boundary, updated_at),
        ).fetchone()
        return int(turn_tokens[0]) + int(event_tokens[0]), compaction_count

    @staticmethod
    def _add_context(
        connection: sqlite3.Connection,
        conversation_id: str,
        tokens: int,
        updated_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO conversation_context_metrics VALUES (?, ?, 0, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                cumulative_context_tokens =
                    cumulative_context_tokens + excluded.cumulative_context_tokens,
                updated_at = excluded.updated_at
            """,
            (conversation_id, tokens, updated_at),
        )
