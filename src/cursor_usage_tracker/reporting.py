"""Reporting that keeps observed and inferred quantities distinct."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC

from cursor_usage_tracker.domain import ReportWindow
from cursor_usage_tracker.storage import Storage


def build_report(storage: Storage, window: ReportWindow) -> dict[str, object]:
    """Build a machine-readable report for one UTC interval."""
    starts_at = window.starts_at.astimezone(UTC).isoformat()
    ends_at = window.ends_at.astimezone(UTC).isoformat()
    model_rows = storage.query(
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
                            THEN 1 ELSE 0 END) AS excluded_tool_events,
                   SUM(CASE
                       WHEN category = 'tool_non_text'
                         OR (category = 'tool' AND lower(name) LIKE '%screenshot%')
                            THEN characters ELSE 0 END) AS excluded_tool_characters
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
        SELECT COALESCE(t.attributed_model, 'unresolved') AS model,
               t.effort,
               COUNT(*) AS requests,
               SUM(t.prompt_tokens) AS prompt_tokens,
               SUM(t.attachment_tokens) AS attachment_tokens,
               SUM(COALESCE(e.context_tokens, 0)) AS tool_text_tokens,
               SUM(COALESCE(e.output_tokens, 0)) AS visible_output,
               SUM(COALESCE(e.excluded_tool_events, 0))
                   AS excluded_tool_events,
               SUM(COALESCE(e.excluded_tool_characters, 0))
                   AS excluded_tool_characters,
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
        GROUP BY model, t.effort ORDER BY requests DESC, model
        """,
        (starts_at, ends_at),
    )
    calibrations: dict[str, dict[str, object]] = {
        str(row["model"]): dict(row)
        for row in storage.query("SELECT * FROM calibrations")
    }
    profiles = [
        dict(row) for row in storage.query("SELECT * FROM calibration_profiles")
    ]
    rates = latest_rates(storage, ends_at)
    models: list[dict[str, object]] = []
    total_requests = 0
    cost_point = 0.0
    cost_low = 0.0
    cost_high = 0.0
    costed_requests = 0
    observable_reference_cost = 0.0
    observable_costed_requests = 0
    for row in model_rows:
        row_data = dict(row)
        model = str(row_data["model"])
        is_configured_reference = _as_int(row_data["configured_requests"]) > 0
        requests = _as_int(row_data["requests"])
        total_requests += requests
        calibration = calibrations.get(model) or matching_profile(
            row_data,
            profiles,
        )
        model_report = estimate_model_usage(row_data, calibration)
        model_report["model_source"] = (
            "configured_reference" if is_configured_reference else "cursor_or_hook"
        )
        if is_configured_reference and model_report["confidence"] == "low":
            model_report["confidence"] = "configured_reference"
        rate = rates.get(model)
        if rate is not None and model_report.get("is_calibrated") is True:
            model_cost = calculate_cost(model_report, rate)
            model_report["cost_usd"] = model_cost
            cost_point += model_cost["point"]
            cost_low += model_cost["low"]
            cost_high += model_cost["high"]
            costed_requests += requests
        else:
            model_report["cost_usd"] = None
        if rate is not None:
            reference_cost = calculate_observable_cost(model_report, rate)
            model_report["observable_reference_cost_usd"] = reference_cost
            observable_reference_cost += reference_cost
            observable_costed_requests += requests
        else:
            model_report["observable_reference_cost_usd"] = None
        models.append(model_report)

    repository_rows = storage.query(
        """
        SELECT project_label, project_hash, COUNT(*) AS requests
        FROM turns WHERE created_at >= ? AND created_at < ?
        GROUP BY project_label, project_hash ORDER BY requests DESC
        """,
        (starts_at, ends_at),
    )
    snapshots = storage.query(
        """
        SELECT * FROM dashboard_snapshots
        WHERE captured_at >= ? AND captured_at < ?
        ORDER BY captured_at DESC LIMIT 1
        """,
        (starts_at, ends_at),
    )
    return {
        "window": {"starts_at": starts_at, "ends_at": ends_at},
        "requests": total_requests,
        "models": models,
        "repositories": [dict(row) for row in repository_rows],
        "estimated_cost_usd": (
            {"point": cost_point, "low": cost_low, "high": cost_high}
            if costed_requests
            else None
        ),
        "costed_requests": costed_requests,
        "uncosted_requests": total_requests - costed_requests,
        "observable_reference_cost_usd": (
            observable_reference_cost if observable_costed_requests else None
        ),
        "observable_costed_requests": observable_costed_requests,
        "dashboard_snapshot": dict(snapshots[0]) if snapshots else None,
        "limitations": [
            "Server-side hidden context and cache usage are not observable locally.",
            "Resolved model attribution may be unavailable for Auto requests.",
            (
                "Uncalibrated token totals are observable lower bounds, "
                "not billing totals."
            ),
        ],
    }


def render_text(report: Mapping[str, object]) -> str:
    """Render a concise terminal report."""
    lines = [
        f"Requests: {report['requests']}",
        "Models:",
    ]
    model_values = report.get("models")
    if isinstance(model_values, list) and model_values:
        for value in model_values:
            if not isinstance(value, dict):
                continue
            token_range = value.get("estimated_tokens")
            if isinstance(token_range, dict):
                estimate = f"{token_range.get('point', 0):,}"
                if token_range.get("high") is None:
                    estimate += "+"
                else:
                    estimate += (
                        f" ({token_range.get('low', 0):,}–"
                        f"{token_range.get('high', 0):,})"
                    )
            else:
                observable = _as_int(value.get("observable_text_tokens", 0))
                estimate = (
                    f"{observable:,} observable text tokens; "
                    "estimated total unavailable"
                )
            line = (
                f"  {value.get('model')}: {value.get('requests')} requests, "
                f"{estimate} [{value.get('confidence')}]"
            )
            excluded_events = _as_int(value.get("excluded_tool_events", 0))
            if excluded_events:
                line += f", {excluded_events} non-text tool events excluded"
            lines.append(line)
    else:
        lines.append("  No tracked requests")

    lines.append("Repositories:")
    repositories = report.get("repositories")
    if isinstance(repositories, list) and repositories:
        for repository in repositories:
            if isinstance(repository, dict):
                lines.append(
                    f"  {repository.get('project_label')}: "
                    f"{repository.get('requests')} requests"
                )
    else:
        lines.append("  No tracked repositories")

    cost = report.get("estimated_cost_usd")
    if isinstance(cost, dict):
        lines.append(
            "Estimated cost: "
            f"${cost.get('point', 0):.4f} "
            f"(${cost.get('low', 0):.4f}–${cost.get('high', 0):.4f})"
        )
    else:
        lines.append("Estimated cost: unknown (missing calibration or rate)")
    reference_cost = report.get("observable_reference_cost_usd")
    if isinstance(reference_cost, (int, float)):
        lines.append(
            f"Observable reference cost: ~${reference_cost:.4f} (reference only)"
        )
    uncosted = _as_int(report.get("uncosted_requests", 0))
    if uncosted:
        lines.append(f"Uncosted requests: {uncosted}")
    lines.append(
        "Note: local estimates exclude unobservable server context and cache telemetry."
    )
    return "\n".join(lines)


def render_json(report: Mapping[str, object]) -> str:
    """Serialize a stable JSON report."""
    return json.dumps(report, indent=2, sort_keys=True)


def estimate_model_usage(
    row: Mapping[str, object], calibration: Mapping[str, object] | None
) -> dict[str, object]:
    requests = _as_int(row["requests"])
    prompt_tokens = _as_int(row["prompt_tokens"])
    attachment_tokens = _as_int(row["attachment_tokens"])
    tool_text_tokens = _as_int(row["tool_text_tokens"])
    visible_input = prompt_tokens + attachment_tokens + tool_text_tokens
    visible_output = _as_int(row["visible_output"])
    visible_thinking = _as_int(row.get("visible_thinking_tokens"))
    observable_output = visible_output + visible_thinking
    visible_total = visible_input + observable_output
    loop_estimated_input = max(
        visible_input,
        _as_int(row.get("loop_estimated_input")),
    )
    report: dict[str, object] = {
        "model": str(row["model"]),
        "effort": str(row.get("effort") or "unknown"),
        "requests": requests,
        "resolved_requests": _as_int(row["resolved_requests"]),
        "prompt_tokens": prompt_tokens,
        "attachment_tokens": attachment_tokens,
        "tool_text_tokens": tool_text_tokens,
        "visible_input_tokens": visible_input,
        "visible_output_tokens": visible_output,
        "observable_text_tokens": visible_total,
        "loop_estimated_input_tokens": loop_estimated_input,
        "estimated_model_calls": _as_int(row.get("estimated_model_calls")),
        "compactions": _as_int(row.get("compactions")),
        "thinking_blocks": _as_int(row.get("thinking_blocks")),
        "visible_thinking_tokens": visible_thinking,
        "thinking_duration_ms": _as_int(row.get("thinking_duration_ms")),
        "input_estimators": str(row.get("input_estimators") or "unknown"),
        "output_estimators": str(row.get("output_estimators") or "unknown"),
        "excluded_tool_events": _as_int(row["excluded_tool_events"]),
        "excluded_tool_characters": _as_int(row["excluded_tool_characters"]),
        "attachments": _as_int(row["attachments"]),
    }
    if calibration is None:
        report["confidence"] = "low"
        report["estimated_input_tokens"] = {
            "point": loop_estimated_input,
            "low": visible_input,
            "high": None,
        }
        report["estimated_output_tokens"] = {
            "point": observable_output,
            "low": observable_output,
            "high": None,
        }
        report["estimated_tokens"] = {
            "point": loop_estimated_input + observable_output,
            "low": visible_total,
            "high": None,
        }
        return report

    input_estimate = _calibrated_range(
        loop_estimated_input,
        requests,
        calibration,
        "input",
    )
    output_estimate = _calibrated_range(
        observable_output,
        requests,
        calibration,
        "output",
    )
    sample_size = _as_int(calibration["sample_size"])
    report["is_calibrated"] = True
    report["confidence"] = (
        "high" if sample_size >= 100 else "medium" if sample_size >= 30 else "low"
    )
    report["calibration_sample_size"] = sample_size
    report["estimated_input_tokens"] = input_estimate
    report["estimated_output_tokens"] = output_estimate
    report["estimated_tokens"] = {
        key: int(input_estimate[key]) + int(output_estimate[key])
        for key in ("point", "low", "high")
    }
    return report


def _calibrated_range(
    observed: int,
    requests: int,
    calibration: Mapping[str, object],
    prefix: str,
) -> dict[str, int]:
    return {
        key: max(observed, requests * _as_int(calibration[f"{prefix}_{key}"]))
        for key in ("point", "low", "high")
    }


def matching_profile(
    usage_row: Mapping[str, object],
    profiles: list[dict[str, object]],
) -> dict[str, object] | None:
    requests = max(_as_int(usage_row.get("requests")), 1)
    tool_count = (
        max(
            _as_int(usage_row.get("estimated_model_calls")) - requests,
            0,
        )
        // requests
    )
    attachment_count = _as_int(usage_row.get("attachments")) // requests
    tool_bucket = "0" if tool_count == 0 else "1-5" if tool_count <= 5 else "6+"
    attachment_bucket = (
        "0" if attachment_count == 0 else "1-3" if attachment_count <= 3 else "4+"
    )
    candidates = [
        profile
        for profile in profiles
        if str(profile["model"]) == str(usage_row["model"])
        and str(profile["effort"]) == str(usage_row.get("effort") or "unknown")
    ]
    if not candidates:
        return None
    exact = [
        profile
        for profile in candidates
        if profile["tool_bucket"] == tool_bucket
        and profile["attachment_bucket"] == attachment_bucket
    ]
    selected = max(
        exact or candidates,
        key=lambda profile: _as_int(profile["sample_size"]),
    )
    return {
        "input_low": selected["input_p10"],
        "input_point": selected["input_p50"],
        "input_high": selected["input_p90"],
        "output_low": selected["output_p10"],
        "output_point": selected["output_p50"],
        "output_high": selected["output_p90"],
        "sample_size": selected["sample_size"],
    }


def latest_rates(storage: Storage, ends_at: str) -> dict[str, dict[str, object]]:
    rows = storage.query(
        """
        SELECT rates.* FROM model_rates AS rates
        JOIN (
            SELECT model, MAX(effective_at) AS effective_at
            FROM model_rates WHERE effective_at < ? GROUP BY model
        ) AS latest USING (model, effective_at)
        """,
        (ends_at,),
    )
    return {str(row["model"]): dict(row) for row in rows}


def calculate_cost(
    model_report: Mapping[str, object], rate: Mapping[str, object]
) -> dict[str, float]:
    input_tokens = model_report["estimated_input_tokens"]
    output_tokens = model_report["estimated_output_tokens"]
    if not isinstance(input_tokens, dict) or not isinstance(output_tokens, dict):
        raise TypeError("Estimated token ranges must be dictionaries")
    return {
        key: (
            _as_float(input_tokens[key]) * _as_float(rate["input_per_million"])
            + _as_float(output_tokens[key]) * _as_float(rate["output_per_million"])
        )
        / 1_000_000
        for key in ("point", "low", "high")
    }


def calculate_observable_cost(
    model_report: Mapping[str, object], rate: Mapping[str, object]
) -> float:
    """Price observable text as an uncached reference, not a billing total."""
    return (
        _as_float(model_report["visible_input_tokens"])
        * _as_float(rate["input_per_million"])
        + (
            _as_float(model_report["visible_output_tokens"])
            + _as_float(model_report["visible_thinking_tokens"])
        )
        * _as_float(rate["output_per_million"])
    ) / 1_000_000


def _as_int(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"Expected numeric value, got {type(value).__name__}")
    return int(value)


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"Expected numeric value, got {type(value).__name__}")
    return float(value)
