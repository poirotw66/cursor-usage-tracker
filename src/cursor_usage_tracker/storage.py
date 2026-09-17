"""SQLite persistence with short, idempotent writes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from cursor_usage_tracker.domain import ResolvedModel
from cursor_usage_tracker.schema import SCHEMA, SCHEMA_VERSION


class Storage:
    """Own application persistence and transaction boundaries."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Open one bounded database connection."""
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5)
        else:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        if write:
            connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            if write:
                connection.commit()
        except sqlite3.Error:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create or update the local schema."""
        with self.connect(write=True) as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO model_assignments (
                    model, scope_type, scope_value, effective_from, created_at
                )
                SELECT value, 'global', '*', updated_at, updated_at
                FROM tracker_settings WHERE key = 'default_model'
                """
            )

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> int:
        """Execute one parameterized write."""
        with self.connect(write=True) as connection:
            cursor = connection.execute(sql, parameters)
            return cursor.rowcount

    def query(self, sql: str, parameters: Sequence[object] = ()) -> list[sqlite3.Row]:
        """Run a read-only parameterized query."""
        with self.connect() as connection:
            return list(connection.execute(sql, parameters))

    def record_session(
        self,
        *,
        conversation_id: str,
        started_at: str,
        mode: str | None,
        is_background: bool,
        project_label: str,
        project_hash: str,
    ) -> None:
        """Record session metadata without prompt content."""
        self.execute(
            """
            INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                mode = COALESCE(excluded.mode, sessions.mode),
                is_background = excluded.is_background
            """,
            (
                conversation_id,
                started_at,
                mode,
                is_background,
                project_label,
                project_hash,
            ),
        )

    def record_turn(
        self,
        *,
        generation_id: str,
        conversation_id: str,
        created_at: str,
        project_label: str,
        project_hash: str,
        selected_model: str | None,
        selected_model_id: str | None,
        model_params: list[dict[str, str]],
        prompt_characters: int,
        prompt_tokens: int,
        attachment_count: int,
        attachment_characters: int,
        attachment_tokens: int,
    ) -> None:
        """Insert a request once, keyed by Cursor generation ID."""
        self.execute(
            """
            INSERT INTO turns (
                generation_id, conversation_id, created_at, project_label,
                project_hash, selected_model, selected_model_id,
                model_params_json, prompt_characters, prompt_tokens,
                attachment_count, attachment_characters, attachment_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(generation_id) DO NOTHING
            """,
            (
                generation_id,
                conversation_id,
                created_at,
                project_label,
                project_hash,
                selected_model,
                selected_model_id,
                json.dumps(model_params, separators=(",", ":"), sort_keys=True),
                prompt_characters,
                prompt_tokens,
                attachment_count,
                attachment_characters,
                attachment_tokens,
            ),
        )

    def apply_resolved_model(self, resolved: ResolvedModel) -> int:
        """Apply model enrichment only when it is usable."""
        return self.execute(
            """
            UPDATE turns
            SET resolved_model = ?, model_source = ?, model_confidence = ?
            WHERE generation_id = ? AND conversation_id = ?
            """,
            (
                resolved.model,
                resolved.source,
                resolved.confidence,
                resolved.generation_id,
                resolved.conversation_id,
            ),
        )

    def apply_selected_model(
        self, conversation_id: str, model: str, source: str
    ) -> int:
        """Backfill a selected model without presenting it as resolved."""
        return self.execute(
            """
            UPDATE turns
            SET selected_model = ?, model_source = COALESCE(model_source, ?)
            WHERE conversation_id = ?
              AND (selected_model IS NULL OR selected_model = 'default')
            """,
            (model, source, conversation_id),
        )

    def record_event(
        self,
        *,
        event_id: str,
        generation_id: str,
        conversation_id: str,
        created_at: str,
        category: str,
        name: str,
        characters: int,
        estimated_tokens: int,
        duration_ms: int | None,
        status: str | None,
    ) -> int:
        """Store one deduplicated aggregate event."""
        return self.execute(
            """
            INSERT INTO event_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (
                event_id,
                generation_id,
                conversation_id,
                created_at,
                category,
                name,
                characters,
                estimated_tokens,
                duration_ms,
                status,
            ),
        )

    def initialize_turn_runtime(
        self,
        generation_id: str,
        base_input_tokens: int,
        input_estimator: str,
    ) -> None:
        """Initialize loop-aware metrics for a request."""
        self.execute(
            """
            INSERT INTO turn_runtime_metrics VALUES (?, ?, 0, ?, 1, 0, 0, 0, 0)
            ON CONFLICT(generation_id) DO NOTHING
            """,
            (generation_id, base_input_tokens, base_input_tokens),
        )
        self.execute(
            """
            INSERT INTO estimation_metadata (
                generation_id, input_estimator, output_estimator
            ) VALUES (?, ?, NULL)
            ON CONFLICT(generation_id) DO NOTHING
            """,
            (generation_id, input_estimator),
        )

    def record_output_estimator(self, generation_id: str, estimator: str) -> None:
        """Record the heuristic used for visible model output."""
        self.execute(
            """
            UPDATE estimation_metadata SET output_estimator = ?
            WHERE generation_id = ?
            """,
            (estimator, generation_id),
        )

    def record_tool_loop(self, generation_id: str, context_tokens: int) -> None:
        """Account for the next model call after one tool result."""
        self.execute(
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

    def record_compaction(self, generation_id: str) -> None:
        """Reset observable accumulated context after compaction."""
        self.execute(
            """
            UPDATE turn_runtime_metrics
            SET cumulative_context_tokens = 0,
                compaction_count = compaction_count + 1
            WHERE generation_id = ?
            """,
            (generation_id,),
        )

    def record_thinking(
        self, generation_id: str, tokens: int, duration_ms: int
    ) -> None:
        """Store visible reasoning aggregates without thought content."""
        self.execute(
            """
            UPDATE turn_runtime_metrics
            SET thinking_blocks = thinking_blocks + 1,
                visible_thinking_tokens = visible_thinking_tokens + ?,
                thinking_duration_ms = thinking_duration_ms + ?
            WHERE generation_id = ?
            """,
            (tokens, duration_ms, generation_id),
        )

    def record_subagent_start(
        self,
        *,
        subagent_id: str,
        parent_conversation_id: str,
        generation_id: str,
        started_at: str,
        subagent_type: str,
        model: str | None,
        is_parallel: bool,
    ) -> None:
        """Store non-content subagent attribution."""
        self.execute(
            """
            INSERT INTO subagents (
                subagent_id, parent_conversation_id, generation_id, started_at,
                subagent_type, model, is_parallel
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subagent_id) DO NOTHING
            """,
            (
                subagent_id,
                parent_conversation_id,
                generation_id,
                started_at,
                subagent_type,
                model,
                is_parallel,
            ),
        )

    def record_subagent_stop(
        self,
        *,
        subagent_id: str,
        status: str | None,
        duration_ms: int | None,
        message_count: int | None,
        tool_call_count: int | None,
    ) -> None:
        """Finalize an existing subagent without retaining its summary."""
        self.execute(
            """
            UPDATE subagents SET status = ?, duration_ms = ?,
                message_count = ?, tool_call_count = ?
            WHERE subagent_id = ?
            """,
            (status, duration_ms, message_count, tool_call_count, subagent_id),
        )

    def update_turn_status(self, generation_id: str, status: str | None) -> None:
        """Record the final state of a generation."""
        self.execute(
            "UPDATE turns SET status = ? WHERE generation_id = ?",
            (status, generation_id),
        )

    def add_dashboard_snapshot(
        self,
        *,
        captured_at: str,
        total_events: int,
        events_with_token: int,
        events_without_token: int,
        known_input: int,
        known_output: int,
        known_cache_read: int,
        known_cache_write: int,
    ) -> None:
        """Store an explicit dashboard observation."""
        self.execute(
            """
            INSERT INTO dashboard_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(captured_at) DO UPDATE SET
                total_events = excluded.total_events,
                events_with_token = excluded.events_with_token,
                events_without_token = excluded.events_without_token,
                known_input = excluded.known_input,
                known_output = excluded.known_output,
                known_cache_read = excluded.known_cache_read,
                known_cache_write = excluded.known_cache_write
            """,
            (
                captured_at,
                total_events,
                events_with_token,
                events_without_token,
                known_input,
                known_output,
                known_cache_read,
                known_cache_write,
            ),
        )

    def add_calibration(
        self,
        *,
        model: str,
        input_point: int,
        input_low: int,
        input_high: int,
        output_point: int,
        output_low: int,
        output_high: int,
        sample_size: int,
        updated_at: str,
    ) -> None:
        """Set an empirical per-request token distribution."""
        self.execute(
            """
            INSERT INTO calibrations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model) DO UPDATE SET
                input_point = excluded.input_point,
                input_low = excluded.input_low,
                input_high = excluded.input_high,
                output_point = excluded.output_point,
                output_low = excluded.output_low,
                output_high = excluded.output_high,
                sample_size = excluded.sample_size,
                updated_at = excluded.updated_at
            """,
            (
                model,
                input_point,
                input_low,
                input_high,
                output_point,
                output_low,
                output_high,
                sample_size,
                updated_at,
            ),
        )

    def add_rate(
        self,
        *,
        model: str,
        effective_at: str,
        input_per_million: float,
        output_per_million: float,
        cache_read_per_million: float | None,
        cache_write_per_million: float | None,
    ) -> None:
        """Store a user-supplied, effective-dated model rate."""
        self.execute(
            """
            INSERT INTO model_rates VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(model, effective_at) DO UPDATE SET
                input_per_million = excluded.input_per_million,
                output_per_million = excluded.output_per_million,
                cache_read_per_million = excluded.cache_read_per_million,
                cache_write_per_million = excluded.cache_write_per_million
            """,
            (
                model,
                effective_at,
                input_per_million,
                output_per_million,
                cache_read_per_million,
                cache_write_per_million,
            ),
        )
