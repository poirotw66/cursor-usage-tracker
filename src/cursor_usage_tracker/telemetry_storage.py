"""Persistence operations owned by telemetry calibration."""

from __future__ import annotations

from collections.abc import Sequence

from cursor_usage_tracker.domain import CalibrationProfile, TelemetryObservation
from cursor_usage_tracker.storage import Storage


class TelemetryRepository:
    """Persist imported observations and their derived profiles."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def import_observations(self, observations: Sequence[TelemetryObservation]) -> int:
        """Upsert validated telemetry observations in one transaction."""
        with self.storage.connect(write=True) as connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO telemetry_observations VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(observation_id) DO UPDATE SET
                    request_id = excluded.request_id,
                    observed_at = excluded.observed_at,
                    model = excluded.model,
                    effort = excluded.effort,
                    tool_count = excluded.tool_count,
                    attachment_count = excluded.attachment_count,
                    input_tokens = excluded.input_tokens,
                    output_tokens = excluded.output_tokens,
                    cache_read_tokens = excluded.cache_read_tokens,
                    cache_write_tokens = excluded.cache_write_tokens,
                    imported_at = excluded.imported_at
                """,
                [
                    (
                        item.observation_id,
                        item.request_id,
                        item.observed_at,
                        item.model,
                        item.effort,
                        item.tool_count,
                        item.attachment_count,
                        item.input_tokens,
                        item.output_tokens,
                        item.cache_read_tokens,
                        item.cache_write_tokens,
                        item.imported_at,
                    )
                    for item in observations
                ],
            )
            return connection.total_changes - before

    def replace_profiles(self, profiles: Sequence[CalibrationProfile]) -> None:
        """Atomically replace all derived calibration profiles."""
        with self.storage.connect(write=True) as connection:
            connection.execute("DELETE FROM calibration_profiles")
            connection.executemany(
                """
                INSERT INTO calibration_profiles VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    (
                        profile.model,
                        profile.effort,
                        profile.tool_bucket,
                        profile.attachment_bucket,
                        profile.sample_size,
                        profile.input_p10,
                        profile.input_p50,
                        profile.input_p90,
                        profile.output_p10,
                        profile.output_p50,
                        profile.output_p90,
                        profile.cache_read_p50,
                        profile.cache_write_p50,
                        profile.updated_at,
                    )
                    for profile in profiles
                ],
            )
