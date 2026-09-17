"""Accuracy validation against imported request-level telemetry."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta

from cursor_usage_tracker.domain import ReportWindow
from cursor_usage_tracker.storage import Storage


def build_accuracy_report(storage: Storage, window: ReportWindow) -> dict[str, object]:
    """Compare matched estimates and telemetry by local day."""
    rows: list[dict[str, object]] = []
    day_start = window.starts_at
    while day_start < window.ends_at:
        day_end = min(
            datetime.combine(
                day_start.date() + timedelta(days=1),
                time.min,
                day_start.tzinfo,
            ),
            window.ends_at,
        )
        rows.append(_daily_accuracy(storage, day_start, day_end))
        day_start = day_end
    return {
        "window": {
            "starts_at": window.starts_at.astimezone(UTC).isoformat(),
            "ends_at": window.ends_at.astimezone(UTC).isoformat(),
        },
        "rows": rows,
        "definitions": {
            "wape": "sum(abs(estimate-actual)) / sum(actual)",
            "bias": "sum(estimate-actual) / sum(actual)",
            "confidence": "low < 30, medium 30-99, high >= 100 matched samples",
        },
    }


def render_accuracy_table(report: Mapping[str, object]) -> str:
    """Render daily WAPE, bias, sample size, and confidence."""
    raw_rows = report.get("rows")
    rows = raw_rows if isinstance(raw_rows, list) else []
    headers = (
        "Date",
        "Samples",
        "Input WAPE",
        "Input Bias",
        "Output WAPE",
        "Output Bias",
        "Confidence",
    )
    display_rows = [
        (
            str(row.get("date", "")),
            str(row.get("sample_size", 0)),
            _percentage(row.get("input_wape")),
            _percentage(row.get("input_bias")),
            _percentage(row.get("output_wape")),
            _percentage(row.get("output_bias")),
            str(row.get("confidence", "low")),
        )
        for row in rows
        if isinstance(row, dict)
    ]
    if not display_rows:
        return "No accuracy data. Import request-level telemetry first."
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in display_rows))
        for index in range(len(headers))
    ]
    divider = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    numeric = {1, 2, 3, 4, 5}
    lines = [
        "Estimator Accuracy - Daily",
        divider,
        _format_row(headers, widths, numeric),
        divider,
        *(_format_row(row, widths, numeric) for row in display_rows),
        divider,
        "Low confidence means fewer than 30 matched telemetry samples.",
    ]
    return "\n".join(lines)


def _daily_accuracy(
    storage: Storage, starts_at: datetime, ends_at: datetime
) -> dict[str, object]:
    records = storage.query(
        """
        WITH outputs AS (
            SELECT generation_id, SUM(estimated_tokens) AS output_tokens
            FROM event_metrics
            WHERE category = 'assistant'
            GROUP BY generation_id
        ),
        tool_context AS (
            SELECT generation_id, SUM(estimated_tokens) AS context_tokens
            FROM event_metrics
            WHERE category IN ('tool', 'tool_text', 'compaction')
              AND lower(name) NOT LIKE '%screenshot%'
            GROUP BY generation_id
        )
        SELECT COALESCE(
                   runtime.estimated_input_tokens,
                   turns.prompt_tokens + turns.attachment_tokens
                       + COALESCE(tool_context.context_tokens, 0)
               ) AS estimated_input,
               COALESCE(outputs.output_tokens, 0)
                   + COALESCE(runtime.visible_thinking_tokens, 0)
                   AS estimated_output,
               telemetry.input_tokens AS actual_input,
               telemetry.output_tokens AS actual_output,
               telemetry.cache_read_tokens,
               telemetry.cache_write_tokens
        FROM turns
        JOIN telemetry_observations AS telemetry
          ON telemetry.request_id = turns.generation_id
        LEFT JOIN turn_runtime_metrics AS runtime USING (generation_id)
        LEFT JOIN outputs USING (generation_id)
        LEFT JOIN tool_context USING (generation_id)
        WHERE turns.created_at >= ? AND turns.created_at < ?
        """,
        (
            starts_at.astimezone(UTC).isoformat(),
            ends_at.astimezone(UTC).isoformat(),
        ),
    )
    input_pairs = [
        (int(row["estimated_input"]), int(row["actual_input"])) for row in records
    ]
    output_pairs = [
        (int(row["estimated_output"]), int(row["actual_output"])) for row in records
    ]
    sample_size = len(records)
    return {
        "date": starts_at.date().isoformat(),
        "sample_size": sample_size,
        "input_wape": _wape(input_pairs),
        "input_bias": _bias(input_pairs),
        "output_wape": _wape(output_pairs),
        "output_bias": _bias(output_pairs),
        "known_cache_read_tokens": sum(
            int(row["cache_read_tokens"]) for row in records
        ),
        "known_cache_write_tokens": sum(
            int(row["cache_write_tokens"]) for row in records
        ),
        "confidence": _confidence(sample_size),
    }


def _wape(pairs: list[tuple[int, int]]) -> float | None:
    actual_total = sum(actual for _, actual in pairs)
    if actual_total == 0:
        return None
    return sum(abs(estimate - actual) for estimate, actual in pairs) / actual_total


def _bias(pairs: list[tuple[int, int]]) -> float | None:
    actual_total = sum(actual for _, actual in pairs)
    if actual_total == 0:
        return None
    return sum(estimate - actual for estimate, actual in pairs) / actual_total


def _confidence(sample_size: int) -> str:
    if sample_size >= 100:
        return "high"
    return "medium" if sample_size >= 30 else "low"


def _percentage(value: object) -> str:
    return "—" if not isinstance(value, (int, float)) else f"{value:.1%}"


def _format_row(
    cells: tuple[str, ...], widths: list[int], numeric_columns: set[int]
) -> str:
    rendered = [
        cell.rjust(widths[index])
        if index in numeric_columns
        else cell.ljust(widths[index])
        for index, cell in enumerate(cells)
    ]
    return "| " + " | ".join(rendered) + " |"
