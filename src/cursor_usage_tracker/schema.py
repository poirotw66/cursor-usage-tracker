"""Versioned SQLite schema owned by the persistence boundary."""

SCHEMA_VERSION = 3
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS sessions (
    conversation_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    mode TEXT,
    is_background INTEGER NOT NULL,
    project_label TEXT NOT NULL,
    project_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    generation_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    project_label TEXT NOT NULL,
    project_hash TEXT NOT NULL,
    selected_model TEXT,
    selected_model_id TEXT,
    model_params_json TEXT NOT NULL,
    resolved_model TEXT,
    model_source TEXT,
    model_confidence TEXT,
    prompt_characters INTEGER NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    attachment_count INTEGER NOT NULL,
    attachment_characters INTEGER NOT NULL,
    attachment_tokens INTEGER NOT NULL,
    status TEXT
);
CREATE INDEX IF NOT EXISTS turns_created_at_idx ON turns(created_at);
CREATE INDEX IF NOT EXISTS turns_conversation_idx ON turns(conversation_id);
CREATE TABLE IF NOT EXISTS event_metrics (
    event_id TEXT PRIMARY KEY,
    generation_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    characters INTEGER NOT NULL,
    estimated_tokens INTEGER NOT NULL,
    duration_ms INTEGER,
    status TEXT
);
CREATE INDEX IF NOT EXISTS events_generation_idx
    ON event_metrics(generation_id);
CREATE TABLE IF NOT EXISTS subagents (
    subagent_id TEXT PRIMARY KEY,
    parent_conversation_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    subagent_type TEXT NOT NULL,
    model TEXT,
    is_parallel INTEGER NOT NULL,
    status TEXT,
    duration_ms INTEGER,
    message_count INTEGER,
    tool_call_count INTEGER
);
CREATE TABLE IF NOT EXISTS dashboard_snapshots (
    captured_at TEXT PRIMARY KEY,
    total_events INTEGER NOT NULL,
    events_with_token INTEGER NOT NULL,
    events_without_token INTEGER NOT NULL,
    known_input INTEGER NOT NULL,
    known_output INTEGER NOT NULL,
    known_cache_read INTEGER NOT NULL,
    known_cache_write INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS calibrations (
    model TEXT PRIMARY KEY,
    input_point INTEGER NOT NULL,
    input_low INTEGER NOT NULL,
    input_high INTEGER NOT NULL,
    output_point INTEGER NOT NULL,
    output_low INTEGER NOT NULL,
    output_high INTEGER NOT NULL,
    sample_size INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_rates (
    model TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    input_per_million REAL NOT NULL,
    output_per_million REAL NOT NULL,
    cache_read_per_million REAL,
    cache_write_per_million REAL,
    PRIMARY KEY (model, effective_at)
);
CREATE TABLE IF NOT EXISTS tracker_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_assignments (
    assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    scope_type TEXT NOT NULL
        CHECK (scope_type IN ('global', 'repository', 'session')),
    scope_value TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (scope_type, scope_value, effective_from)
);
CREATE INDEX IF NOT EXISTS model_assignments_lookup_idx
    ON model_assignments(scope_type, scope_value, effective_from);
CREATE TABLE IF NOT EXISTS turn_runtime_metrics (
    generation_id TEXT PRIMARY KEY,
    base_input_tokens INTEGER NOT NULL,
    cumulative_context_tokens INTEGER NOT NULL,
    estimated_input_tokens INTEGER NOT NULL,
    estimated_model_calls INTEGER NOT NULL,
    compaction_count INTEGER NOT NULL,
    thinking_blocks INTEGER NOT NULL,
    visible_thinking_tokens INTEGER NOT NULL,
    thinking_duration_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS estimation_metadata (
    generation_id TEXT PRIMARY KEY,
    input_estimator TEXT NOT NULL,
    output_estimator TEXT
);
CREATE TABLE IF NOT EXISTS telemetry_observations (
    observation_id TEXT PRIMARY KEY,
    request_id TEXT,
    observed_at TEXT NOT NULL,
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    tool_count INTEGER NOT NULL,
    attachment_count INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS telemetry_observed_at_idx
    ON telemetry_observations(observed_at);
CREATE TABLE IF NOT EXISTS calibration_profiles (
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    tool_bucket TEXT NOT NULL,
    attachment_bucket TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    input_p10 INTEGER NOT NULL,
    input_p50 INTEGER NOT NULL,
    input_p90 INTEGER NOT NULL,
    output_p10 INTEGER NOT NULL,
    output_p50 INTEGER NOT NULL,
    output_p90 INTEGER NOT NULL,
    cache_read_p50 INTEGER NOT NULL,
    cache_write_p50 INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (model, effort, tool_bucket, attachment_bucket)
);
"""
