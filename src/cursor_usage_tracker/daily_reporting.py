"""Daily terminal table grouped by repository and model."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta

from cursor_usage_tracker.domain import ReportWindow
from cursor_usage_tracker.reporting import (
    calculate_cost,
    calculate_observable_cost,
    estimate_model_usage,
    latest_rates,
    matching_profile,
)
from cursor_usage_tracker.storage import Storage


def build_daily_report(storage: Storage, window: ReportWindow) -> dict[str, object]:
    """Build daily repository/model rows in the report's local timezone."""
    calibrations = {
        str(row["model"]): dict(row)
        for row in storage.query("SELECT * FROM calibrations")
    }
    profiles = [
        dict(row) for row in storage.query("SELECT * FROM calibration_profiles")
    ]
    rows: list[dict[str, object]] = []
    day_start = window.starts_at
    while day_start < window.ends_at:
        next_date = day_start.date() + timedelta(days=1)
        day_end = min(
            datetime.combine(next_date, time.min, day_start.tzinfo),
            window.ends_at,
        )
        rows.extend(
            _build_day_rows(
                storage,
                starts_at=day_start,
                ends_at=day_end,
                calibrations=calibrations,
                profiles=profiles,
            )
        )
        day_start = day_end
    return {
        "window": {
            "starts_at": window.starts_at.astimezone(UTC).isoformat(),
            "ends_at": window.ends_at.astimezone(UTC).isoformat(),
        },
        "rows": rows,
        "limitations": [
            "Input and output columns contain observable text estimates only.",
            "A bounded total range requires calibration.",
            "Configured models and prices are reference-only assumptions.",
            "Cache read and cache write usage are not observable locally.",
        ],
    }


def render_daily_table(report: Mapping[str, object]) -> str:
    """Render a dependency-free terminal table."""
    raw_rows = report.get("rows")
    rows = raw_rows if isinstance(raw_rows, list) else []
    headers = (
        "Date",
        "Repository",
        "Model",
        "Requests",
        "Calls",
        "Input Est.*",
        "Output*",
        "Think*",
        "Excluded",
        "Total Est.",
        "Ref Cost",
    )
    display_rows = [_display_row(row) for row in rows if isinstance(row, dict)]
    display_rows.append(_total_row(rows))
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in display_rows))
        for index in range(len(headers))
    ]
    numeric_columns = set(range(3, len(headers)))
    divider = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    lines = [
        "Cursor Usage Report - Daily",
        divider,
        _format_row(headers, widths, numeric_columns),
        divider,
    ]
    lines.extend(_format_row(row, widths, numeric_columns) for row in display_rows[:-1])
    lines.extend(
        [
            divider,
            _format_row(display_rows[-1], widths, numeric_columns),
            divider,
            "* Input is loop-aware; Output/Think cover visible text only.",
            "  A bounded total range requires calibration.",
            "  Ref Cost uses calibration when available, otherwise observable text.",
            "  Configured models and prices are reference-only assumptions.",
            "  Cache create/read are not observable locally.",
        ]
    )
    return "\n".join(lines)


def _build_day_rows(
    storage: Storage,
    *,
    starts_at: datetime,
    ends_at: datetime,
    calibrations: dict[str, dict[str, object]],
    profiles: list[dict[str, object]],
) -> list[dict[str, object]]:
    start_utc = starts_at.astimezone(UTC).isoformat()
    end_utc = ends_at.astimezone(UTC).isoformat()
    rates = latest_rates(storage, end_utc)
    database_rows = storage.query(
        """
        WITH event_totals AS (
            SELECT generation_id,
                   SUM(CASE
                       WHEN category IN ('tool', 'tool_text', 'compaction')
                        AND lower(name) NOT LIKE '%screenshot%'
                            THEN estimated_tokens ELSE 0 END) AS context_tokens,
                   SUM(CASE WHEN category = 'assistant'
                            THEN estimated_tokens ELSE 0 END) AS output_tokens,
                   SUM(CASE
                       WHEN category = 'tool_non_text'
                         OR (category = 'tool' AND lower(name) LIKE '%screenshot%')
                            THEN 1 ELSE 0 END) AS excluded_tool_events
            FROM event_metrics GROUP BY generation_id
        ),
        turn_models AS (
            SELECT t.*,
                   CASE
                       WHEN lower(COALESCE(t.resolved_model, ''))
                            NOT IN ('', 'auto', 'default') THEN t.resolved_model
                       WHEN lower(COALESCE(t.selected_model_id, ''))
                            NOT IN ('', 'auto', 'default') THEN t.selected_model_id
                       WHEN lower(COALESCE(t.selected_model, ''))
                            NOT IN ('', 'auto', 'default') THEN t.selected_model
                       ELSE (
                           SELECT assignment.model
                           FROM model_assignments AS assignment
                           WHERE assignment.effective_from <= t.created_at
                             AND (
                                 (assignment.scope_type = 'session'
                                  AND assignment.scope_value = t.conversation_id)
                                 OR
                                 (assignment.scope_type = 'repository'
                                  AND assignment.scope_value = t.project_label)
                                 OR
                                 (assignment.scope_type = 'global'
                                  AND assignment.scope_value = '*')
                             )
                           ORDER BY CASE assignment.scope_type
                                        WHEN 'session' THEN 3
                                        WHEN 'repository' THEN 2
                                        ELSE 1
                                    END DESC,
                                    assignment.effective_from DESC
                           LIMIT 1
                       )
                   END AS attributed_model,
                   COALESCE(
                       (
                           SELECT json_extract(parameter.value, '$.value')
                           FROM json_each(t.model_params_json) AS parameter
                           WHERE json_extract(parameter.value, '$.id') = 'effort'
                           LIMIT 1
                       ),
                       'unknown'
                   ) AS effort
            FROM turns AS t
        )
        SELECT t.project_label,
               COALESCE(t.attributed_model, 'unresolved') AS model,
               t.effort,
               COUNT(*) AS requests,
               SUM(t.prompt_tokens) AS prompt_tokens,
               SUM(t.attachment_tokens) AS attachment_tokens,
               SUM(COALESCE(e.context_tokens, 0)) AS tool_text_tokens,
               SUM(COALESCE(e.output_tokens, 0)) AS visible_output,
               SUM(COALESCE(e.excluded_tool_events, 0))
                   AS excluded_tool_events,
               0 AS excluded_tool_characters,
               SUM(t.attachment_count) AS attachments,
               SUM(COALESCE(
                   runtime.estimated_input_tokens,
                   t.prompt_tokens + t.attachment_tokens
                       + COALESCE(e.context_tokens, 0)
               )) AS loop_estimated_input,
               SUM(COALESCE(runtime.estimated_model_calls, 1))
                   AS estimated_model_calls,
               SUM(COALESCE(runtime.compaction_count, 0)) AS compactions,
               SUM(COALESCE(runtime.thinking_blocks, 0)) AS thinking_blocks,
               SUM(COALESCE(runtime.visible_thinking_tokens, 0))
                   AS visible_thinking_tokens,
               SUM(COALESCE(runtime.thinking_duration_ms, 0))
                   AS thinking_duration_ms,
               GROUP_CONCAT(DISTINCT metadata.input_estimator)
                   AS input_estimators,
               GROUP_CONCAT(DISTINCT metadata.output_estimator)
                   AS output_estimators,
               SUM(CASE
                   WHEN lower(COALESCE(t.resolved_model, ''))
                        NOT IN ('', 'auto', 'default') THEN 1 ELSE 0 END)
                   AS resolved_requests,
               SUM(CASE
                   WHEN t.attributed_model IS NOT NULL
                    AND lower(COALESCE(t.resolved_model, ''))
                        IN ('', 'auto', 'default')
                    AND lower(COALESCE(t.selected_model_id, ''))
                        IN ('', 'auto', 'default')
                    AND lower(COALESCE(t.selected_model, ''))
                        IN ('', 'auto', 'default')
                   THEN 1 ELSE 0 END) AS configured_requests
        FROM turn_models AS t
        LEFT JOIN event_totals AS e USING (generation_id)
        LEFT JOIN turn_runtime_metrics AS runtime USING (generation_id)
        LEFT JOIN estimation_metadata AS metadata USING (generation_id)
        WHERE t.created_at >= ? AND t.created_at < ?
        GROUP BY t.project_label, model, t.effort
        ORDER BY t.project_label, requests DESC, model
        """,
        (start_utc, end_utc),
    )
    rows: list[dict[str, object]] = []
    for database_row in database_rows:
        row_data = dict(database_row)
        model = str(row_data["model"])
        is_configured_reference = int(row_data["configured_requests"]) > 0
        calibration = calibrations.get(model) or matching_profile(row_data, profiles)
        usage = estimate_model_usage(row_data, calibration)
        usage["model_source"] = (
            "configured_reference" if is_configured_reference else "cursor_or_hook"
        )
        if is_configured_reference and usage["confidence"] == "low":
            usage["confidence"] = "configured_reference"
        rate = rates.get(model)
        cost = (
            calculate_cost(usage, rate)
            if rate is not None and usage.get("is_calibrated") is True
            else None
        )
        observable_reference_cost = (
            calculate_observable_cost(usage, rate) if rate is not None else None
        )
        rows.append(
            {
                "date": starts_at.date().isoformat(),
                "repository": str(row_data["project_label"]),
                **usage,
                "cost_usd": cost,
                "observable_reference_cost_usd": observable_reference_cost,
            }
        )
    return rows


def _display_row(row: Mapping[str, object]) -> tuple[str, ...]:
    estimated_tokens = row.get("estimated_tokens")
    cost = row.get("cost_usd")
    reference_cost = row.get("observable_reference_cost_usd")
    return (
        str(row.get("date", "")),
        _shorten(str(row.get("repository", "")), 24),
        _shorten(str(row.get("model", "")), 28),
        f"{_integer(row.get('requests')):,}",
        f"{_integer(row.get('estimated_model_calls')):,}",
        f"{_integer(row.get('loop_estimated_input_tokens')):,}",
        f"{_integer(row.get('visible_output_tokens')):,}",
        f"{_integer(row.get('visible_thinking_tokens')):,}",
        f"{_integer(row.get('excluded_tool_events')):,}",
        (
            f"{_integer(estimated_tokens.get('point')):,}"
            if isinstance(estimated_tokens, dict)
            else "—"
        ),
        (
            f"${_number(cost.get('point')):.4f}"
            if isinstance(cost, dict)
            else (
                f"~${_number(reference_cost):.4f}"
                if reference_cost is not None
                else "—"
            )
        ),
    )


def _total_row(rows: list[object]) -> tuple[str, ...]:
    mappings = [row for row in rows if isinstance(row, dict)]
    has_unknown_total = any(row.get("estimated_tokens") is None for row in mappings)
    has_unknown_cost = any(
        row.get("cost_usd") is None and row.get("observable_reference_cost_usd") is None
        for row in mappings
    )
    return (
        "Total",
        "",
        "",
        f"{sum(_integer(row.get('requests')) for row in mappings):,}",
        f"{_sum_field(mappings, 'estimated_model_calls'):,}",
        f"{_sum_field(mappings, 'loop_estimated_input_tokens'):,}",
        f"{sum(_integer(row.get('visible_output_tokens')) for row in mappings):,}",
        f"{sum(_integer(row.get('visible_thinking_tokens')) for row in mappings):,}",
        f"{sum(_integer(row.get('excluded_tool_events')) for row in mappings):,}",
        (
            "—"
            if has_unknown_total
            else f"{sum(_estimate_point(row) for row in mappings):,}"
        ),
        (
            "—"
            if has_unknown_cost
            else f"~${sum(_cost_point(row) for row in mappings):.4f}"
        ),
    )


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


def _estimate_point(row: Mapping[str, object]) -> int:
    estimate = row.get("estimated_tokens")
    return _integer(estimate.get("point")) if isinstance(estimate, dict) else 0


def _sum_field(rows: list[dict[object, object]], field: str) -> int:
    return sum(_integer(row.get(field)) for row in rows)


def _cost_point(row: Mapping[str, object]) -> float:
    cost = row.get("cost_usd")
    if isinstance(cost, dict):
        return _number(cost.get("point"))
    return _number(row.get("observable_reference_cost_usd"))


def _integer(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"Expected numeric value, got {type(value).__name__}")
    return int(value)


def _number(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"Expected numeric value, got {type(value).__name__}")
    return float(value)


def _shorten(value: str, width: int) -> str:
    return value if len(value) <= width else f"{value[: width - 1]}…"
