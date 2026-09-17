from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cursor_usage_tracker.accuracy import build_accuracy_report, render_accuracy_table
from cursor_usage_tracker.cursor_db import (
    CursorDatabaseError,
    sync_cursor_database,
)
from cursor_usage_tracker.daily_reporting import build_daily_report, render_daily_table
from cursor_usage_tracker.domain import ReportWindow
from cursor_usage_tracker.estimation import estimate_file, estimate_text
from cursor_usage_tracker.hook_install import install_hooks
from cursor_usage_tracker.hooks import InvalidHookPayload, process_hook
from cursor_usage_tracker.model_configuration import (
    configure_default_model,
    get_configured_model,
    render_model_presets,
)
from cursor_usage_tracker.model_presets import MODEL_PRESETS
from cursor_usage_tracker.reporting import build_report
from cursor_usage_tracker.storage import Storage
from cursor_usage_tracker.telemetry import import_telemetry


def test_estimate_text_accounts_for_non_ascii() -> None:
    assert estimate_text("abcd").tokens == 1
    assert estimate_text("測試").tokens == 2
    assert estimate_text("").tokens == 0
    assert estimate_text("abcdefgh", "claude-opus-5").tokens == 3
    assert estimate_text("abcdefgh", "gemini-3.8-flash").tokens == 2


def test_estimate_file_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    included = workspace / "included.txt"
    included.write_text("abcdefgh", encoding="utf-8")
    excluded = tmp_path / "excluded.txt"
    excluded.write_text("secret", encoding="utf-8")

    assert estimate_file(included, (workspace,)).tokens == 2
    assert estimate_file(excluded, (workspace,)).tokens == 0


def test_hook_stores_metrics_without_content(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "usage.db")
    secret = "do not persist this prompt"
    payload = _prompt_payload(tmp_path, generation_id="generation-1", prompt=secret)

    assert process_hook(payload, storage) == {"continue": True}
    rows = storage.query("SELECT * FROM turns")
    assert len(rows) == 1
    assert rows[0]["prompt_characters"] == len(secret)
    assert secret not in _database_text(storage.path)

    response_payload = {
        **_common_payload("generation-1"),
        "hook_event_name": "afterAgentResponse",
        "text": "private response text",
    }
    process_hook(response_payload, storage)
    assert "private response text" not in _database_text(storage.path)


def test_duplicate_hook_event_is_idempotent(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "usage.db")
    process_hook(_prompt_payload(tmp_path), storage)
    event = {
        **_common_payload(),
        "hook_event_name": "postToolUse",
        "tool_name": "Read",
        "tool_use_id": "tool-1",
        "tool_output": "contents",
    }
    process_hook(event, storage)
    process_hook(event, storage)
    count = storage.query("SELECT COUNT(*) AS count FROM event_metrics")[0]["count"]
    assert count == 1


def test_parallel_hook_writes_are_safe(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "usage.db")
    storage.initialize()

    def record(index: int) -> None:
        process_hook(
            _prompt_payload(tmp_path, generation_id=f"generation-{index}"),
            storage,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(record, range(12)))
    count = storage.query("SELECT COUNT(*) AS count FROM turns")[0]["count"]
    assert count == 12


def test_invalid_hook_payload_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidHookPayload):
        process_hook(
            {"hook_event_name": "beforeSubmitPrompt"}, Storage(tmp_path / "x.db")
        )


def test_hook_installer_preserves_existing_entries_and_is_idempotent(
    tmp_path: Path,
) -> None:
    hooks_path = tmp_path / ".cursor" / "hooks.json"
    hooks_path.parent.mkdir()
    hooks_path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "beforeSubmitPrompt": [{"command": "/opt/orca-hook", "timeout": 10}]
                },
            }
        ),
        encoding="utf-8",
    )

    backup = install_hooks(hooks_path, Path("/usr/local/bin/cursor-usage"))
    configuration = json.loads(hooks_path.read_text(encoding="utf-8"))
    entries = configuration["hooks"]["beforeSubmitPrompt"]
    assert backup is not None and backup.exists()
    assert entries[0]["command"] == "/opt/orca-hook"
    assert len(entries) == 2
    assert len(configuration["hooks"]["afterAgentThought"]) == 1
    assert install_hooks(hooks_path, Path("/usr/local/bin/cursor-usage")) is None


def test_sync_ignores_default_and_enriches_explicit_models(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "usage.db")
    process_hook(_prompt_payload(tmp_path, generation_id="resolved"), storage)
    process_hook(_prompt_payload(tmp_path, generation_id="default"), storage)
    cursor_path = tmp_path / "state.vscdb"
    _create_cursor_database(cursor_path)
    with sqlite3.connect(cursor_path) as connection:
        _insert_cursor_value(
            connection,
            "bubbleId:conversation-1:bubble-1",
            {
                "requestId": "resolved",
                "modelInfo": {"modelName": "grok-4.6"},
            },
        )
        _insert_cursor_value(
            connection,
            "bubbleId:conversation-1:bubble-2",
            {
                "requestId": "default",
                "modelInfo": {"modelName": "default"},
            },
        )

    result = sync_cursor_database(cursor_path, storage)
    rows = storage.query(
        "SELECT generation_id, resolved_model FROM turns ORDER BY generation_id"
    )
    assert result.resolved_models == 1
    assert result.ignored_models == 1
    assert [tuple(row) for row in rows] == [
        ("default", None),
        ("resolved", "grok-4.6"),
    ]


def test_report_normalizes_default_and_excludes_screenshot_payload(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "usage.db")
    payload = _prompt_payload(tmp_path, prompt="abcd")
    payload["model"] = "default"
    payload["model_id"] = "default"
    process_hook(payload, storage)
    process_hook(
        {
            **_common_payload(),
            "hook_event_name": "postToolUse",
            "tool_name": "MCP:browser_take_screenshot",
            "tool_use_id": "screenshot-1",
            "tool_output": "A" * 100_000,
        },
        storage,
    )
    now = datetime.now(UTC)

    report = build_report(
        storage,
        ReportWindow(
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(minutes=1),
        ),
    )
    models = report["models"]
    assert isinstance(models, list)
    model = models[0]
    assert isinstance(model, dict)
    assert model["model"] == "unresolved"
    assert model["observable_text_tokens"] == 1
    estimated_tokens = model["estimated_tokens"]
    assert isinstance(estimated_tokens, dict)
    assert estimated_tokens["point"] == 2
    assert model["estimated_model_calls"] == 2
    assert model["excluded_tool_events"] == 1


def test_daily_table_groups_repository_and_marks_unknown_totals(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "usage.db")
    payload = _prompt_payload(tmp_path, prompt="abcdefgh")
    payload["model"] = "default"
    payload["model_id"] = "default"
    process_hook(payload, storage)
    now = datetime.now(UTC)
    report = build_daily_report(
        storage,
        ReportWindow(
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(minutes=1),
        ),
    )

    rows = report["rows"]
    assert isinstance(rows, list)
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, dict)
    assert row["repository"] == tmp_path.name
    assert row["model"] == "unresolved"
    table = render_daily_table(report)
    assert "Cursor Usage Report - Daily" in table
    assert "Input Est.*" in table
    assert "unresolved" in table
    assert "—" in table


def test_configured_model_applies_reference_rate_to_daily_report(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "usage.db")
    storage.initialize()
    configured = configure_default_model(storage, "cursor-grok-4.6-high-fast")
    payload = _prompt_payload(tmp_path, prompt="abcdefgh")
    payload["model"] = "default"
    payload["model_id"] = "default"
    process_hook(payload, storage)
    now = datetime.now(UTC)
    report = build_daily_report(
        storage,
        ReportWindow(
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(minutes=1),
        ),
    )

    assert get_configured_model(storage) == configured
    assert len(MODEL_PRESETS) == 5
    rows = report["rows"]
    assert isinstance(rows, list)
    row = rows[0]
    assert isinstance(row, dict)
    assert row["model"] == "cursor-grok-4.6-high-fast"
    assert row["model_source"] == "configured_reference"
    assert row["observable_reference_cost_usd"] == pytest.approx(0.000012)
    assert "Pricing reference only" in render_model_presets(configured.slug)
    assert "~$0.0000" in render_daily_table(report)


def test_model_assignments_are_effective_dated_and_scope_specific(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "usage.db")
    old_payload = _prompt_payload(
        tmp_path,
        generation_id="old-generation",
    )
    old_payload["model"] = "default"
    old_payload["model_id"] = "default"
    process_hook(old_payload, storage)
    configure_default_model(storage, "cursor-grok-4.6-high-fast")
    configure_default_model(
        storage,
        "claude-opus-5-thinking-high",
        scope_type="repository",
        scope_value=tmp_path.name,
    )
    configure_default_model(
        storage,
        "gpt-5.6-sol-medium",
        scope_type="session",
        scope_value="conversation-1",
    )
    new_payload = _prompt_payload(
        tmp_path,
        generation_id="new-generation",
    )
    new_payload["model"] = "default"
    new_payload["model_id"] = "default"
    process_hook(new_payload, storage)
    now = datetime.now(UTC)

    report = build_report(
        storage,
        ReportWindow(
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(minutes=1),
        ),
    )
    models = report["models"]
    assert isinstance(models, list)
    model_names = {str(model["model"]) for model in models if isinstance(model, dict)}
    assert model_names == {"unresolved", "gpt-5.6-sol-medium"}
    estimator = storage.query(
        """
        SELECT input_estimator FROM estimation_metadata
        WHERE generation_id = 'new-generation'
        """
    )[0]["input_estimator"]
    assert estimator == "gpt_heuristic_v1"


def test_visible_thinking_is_aggregated_without_content(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "usage.db")
    process_hook(_prompt_payload(tmp_path), storage)
    thought = {
        **_common_payload(),
        "hook_event_name": "afterAgentThought",
        "text": "private visible reasoning",
        "duration_ms": 1250,
    }
    process_hook(thought, storage)
    process_hook(thought, storage)

    metrics = storage.query("SELECT * FROM turn_runtime_metrics")[0]
    assert metrics["thinking_blocks"] == 1
    assert metrics["visible_thinking_tokens"] > 0
    assert metrics["thinking_duration_ms"] == 1250
    assert "private visible reasoning" not in _database_text(storage.path)


def test_new_turn_inherits_observable_conversation_context(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "usage.db")
    process_hook(_prompt_payload(tmp_path, prompt="abcd"), storage)
    process_hook(
        {
            **_common_payload(),
            "hook_event_name": "afterAgentResponse",
            "text": "abcd",
        },
        storage,
    )
    process_hook(
        _prompt_payload(
            tmp_path,
            generation_id="generation-2",
            prompt="abcd",
        ),
        storage,
    )

    runtime = storage.query(
        """
        SELECT * FROM turn_runtime_metrics
        WHERE generation_id = 'generation-2'
        """
    )[0]
    context = storage.query("SELECT * FROM conversation_context_metrics")[0]
    assert runtime["base_input_tokens"] == 6
    assert runtime["estimated_input_tokens"] == 6
    assert context["cumulative_context_tokens"] == 6


def test_loop_estimator_accumulates_and_resets_after_compaction(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "usage.db")
    process_hook(_prompt_payload(tmp_path, prompt="abcd"), storage)
    for tool_use_id in ("tool-before-compact",):
        process_hook(
            {
                **_common_payload(),
                "hook_event_name": "postToolUse",
                "tool_name": "Read",
                "tool_use_id": tool_use_id,
                "tool_output": "abcd",
            },
            storage,
        )
    process_hook(
        {
            **_common_payload(),
            "hook_event_name": "preCompact",
            "context": "",
        },
        storage,
    )
    process_hook(
        {
            **_common_payload(),
            "hook_event_name": "postToolUse",
            "tool_name": "Read",
            "tool_use_id": "tool-after-compact",
            "tool_output": "abcd",
        },
        storage,
    )

    metrics = storage.query("SELECT * FROM turn_runtime_metrics")[0]
    assert metrics["estimated_model_calls"] == 3
    assert metrics["estimated_input_tokens"] == 8
    assert metrics["cumulative_context_tokens"] == 2
    assert metrics["compaction_count"] == 1
    context = storage.query("SELECT * FROM conversation_context_metrics")[0]
    assert context["cumulative_context_tokens"] == 2
    assert context["compaction_count"] == 1


def test_telemetry_import_builds_profiles_and_accuracy_report(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "usage.db")
    process_hook(_prompt_payload(tmp_path, prompt="abcd"), storage)
    process_hook(
        {
            **_common_payload(),
            "hook_event_name": "afterAgentResponse",
            "text": "abcd",
        },
        storage,
    )
    telemetry_path = tmp_path / "telemetry.jsonl"
    telemetry_path.write_text(
        json.dumps(
            {
                "request_id": "generation-1",
                "timestamp": datetime.now(UTC).isoformat(),
                "model": "grok-4.6",
                "effort": "high",
                "tool_count": 0,
                "attachment_count": 0,
                "input_tokens": 10,
                "output_tokens": 4,
                "cache_read_tokens": 2,
                "cache_write_tokens": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = import_telemetry(telemetry_path, storage)
    assert result.observations == 1
    assert result.profiles == 1
    profile = storage.query("SELECT * FROM calibration_profiles")[0]
    assert profile["input_p10"] == 10
    assert profile["input_p50"] == 10
    assert profile["input_p90"] == 10
    now = datetime.now(UTC)
    accuracy = build_accuracy_report(
        storage,
        ReportWindow(
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(minutes=1),
        ),
    )
    rows = accuracy["rows"]
    assert isinstance(rows, list)
    row = rows[0]
    assert isinstance(row, dict)
    assert row["sample_size"] == 1
    assert row["input_wape"] == pytest.approx(0.8)
    assert row["input_bias"] == pytest.approx(-0.8)
    assert row["confidence"] == "low"
    assert "Input WAPE" in render_accuracy_table(accuracy)


def test_sync_detects_schema_drift(tmp_path: Path) -> None:
    cursor_path = tmp_path / "state.vscdb"
    sqlite3.connect(cursor_path).close()
    storage = Storage(tmp_path / "usage.db")
    storage.initialize()
    with pytest.raises(CursorDatabaseError, match="cursorDiskKV"):
        sync_cursor_database(cursor_path, storage)


def test_calibrated_report_and_cost(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "usage.db")
    process_hook(_prompt_payload(tmp_path, prompt="abcd"), storage)
    storage.add_calibration(
        model="grok-4.6",
        input_point=100,
        input_low=80,
        input_high=120,
        output_point=20,
        output_low=10,
        output_high=30,
        sample_size=50,
        updated_at=datetime.now(UTC).isoformat(),
    )
    storage.add_rate(
        model="grok-4.6",
        effective_at="2026-01-01T00:00:00+00:00",
        input_per_million=1.0,
        output_per_million=2.0,
        cache_read_per_million=None,
        cache_write_per_million=None,
    )
    now = datetime.now(UTC)
    report = build_report(
        storage,
        ReportWindow(
            starts_at=now - timedelta(minutes=1), ends_at=now + timedelta(minutes=1)
        ),
    )
    models = report["models"]
    assert isinstance(models, list)
    model = models[0]
    assert isinstance(model, dict)
    assert model["confidence"] == "medium"
    assert model["is_calibrated"] is True
    estimated_tokens = model["estimated_tokens"]
    assert isinstance(estimated_tokens, dict)
    assert estimated_tokens["point"] == 120
    assert report["estimated_cost_usd"] is not None


def _common_payload(generation_id: str = "generation-1") -> dict[str, object]:
    return {
        "conversation_id": "conversation-1",
        "generation_id": generation_id,
        "model": "auto",
        "model_id": "grok-4.6",
        "model_params": [{"id": "effort", "value": "high"}],
        "workspace_roots": [],
    }


def _prompt_payload(
    workspace: Path,
    *,
    generation_id: str = "generation-1",
    prompt: str = "hello",
) -> dict[str, object]:
    return {
        **_common_payload(generation_id),
        "hook_event_name": "beforeSubmitPrompt",
        "workspace_roots": [str(workspace)],
        "prompt": prompt,
        "attachments": [],
    }


def _database_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="ignore")


def _create_cursor_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )


def _insert_cursor_value(
    connection: sqlite3.Connection, key: str, value: dict[str, object]
) -> None:
    connection.execute(
        "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )
