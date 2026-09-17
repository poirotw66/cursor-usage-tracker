"""Cursor hook boundary that discards content after measurement."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from cursor_usage_tracker.conversation_context_storage import (
    ConversationContextRepository,
)
from cursor_usage_tracker.domain import TokenEstimate, ToolOutputEstimate
from cursor_usage_tracker.estimation import (
    estimate_file,
    estimate_text,
    estimate_tool_output,
)
from cursor_usage_tracker.model_assignment_storage import ModelAssignmentRepository
from cursor_usage_tracker.storage import Storage


class InvalidHookPayload(ValueError):
    """Raised when required hook metadata is absent or malformed."""


def process_hook(payload: object, storage: Storage) -> dict[str, object]:
    """Validate and record one hook invocation."""
    hook = _mapping(payload)
    event_name = _required_string(hook, "hook_event_name")
    storage.initialize()
    handlers = {
        "sessionStart": _record_session_start,
        "beforeSubmitPrompt": _record_prompt,
        "postToolUse": _record_content_metric,
        "afterAgentResponse": _record_content_metric,
        "afterAgentThought": _record_content_metric,
        "subagentStart": _record_subagent_start,
        "subagentStop": _record_subagent_stop,
        "preCompact": _record_content_metric,
        "stop": _record_stop,
    }
    handler = handlers.get(event_name)
    if handler is not None:
        handler(hook, storage)
    return _hook_response(event_name)


def safe_hook_response(event_name: str | None) -> dict[str, object]:
    """Return a fail-open response when local analytics fails."""
    return _hook_response(event_name or "")


def _record_session_start(hook: dict[str, object], storage: Storage) -> None:
    conversation_id = _required_string(
        hook, "conversation_id", fallback_key="session_id"
    )
    project_label, project_hash = _project_identity(hook)
    storage.record_session(
        conversation_id=conversation_id,
        started_at=_now(),
        mode=_optional_string(hook, "composer_mode"),
        is_background=_optional_bool(hook, "is_background_agent") or False,
        project_label=project_label,
        project_hash=project_hash,
    )


def _record_prompt(hook: dict[str, object], storage: Storage) -> None:
    prompt = _optional_string(hook, "prompt") or ""
    created_at = _now()
    project_label, project_hash = _project_identity(hook)
    estimation_model = _resolved_estimation_model(
        hook,
        storage,
        project_label=project_label,
        effective_at=created_at,
    )
    prompt_estimate = estimate_text(prompt, estimation_model)
    workspace_roots = _workspace_roots(hook)
    attachment_count = 0
    attachment_characters = 0
    attachment_tokens = 0
    for attachment in _list(hook.get("attachments")):
        attachment_data = _mapping(attachment)
        file_path = _optional_string(attachment_data, "file_path")
        if file_path is None:
            continue
        attachment_count += 1
        try:
            estimate = estimate_file(
                Path(file_path),
                workspace_roots,
                estimation_model,
            )
        except OSError:
            continue
        attachment_characters += estimate.characters
        attachment_tokens += estimate.tokens

    generation_id = _required_string(hook, "generation_id")
    conversation_id = _required_string(hook, "conversation_id")
    inserted = storage.record_turn(
        generation_id=generation_id,
        conversation_id=conversation_id,
        created_at=created_at,
        project_label=project_label,
        project_hash=project_hash,
        selected_model=_optional_string(hook, "model"),
        selected_model_id=_optional_string(hook, "model_id"),
        model_params=_model_params(hook),
        prompt_characters=prompt_estimate.characters,
        prompt_tokens=prompt_estimate.tokens,
        attachment_count=attachment_count,
        attachment_characters=attachment_characters,
        attachment_tokens=attachment_tokens,
    )
    if inserted:
        ConversationContextRepository(storage).initialize_turn(
            generation_id,
            conversation_id,
            prompt_estimate.tokens + attachment_tokens,
            prompt_estimate.source,
            created_at,
        )


def _record_content_metric(hook: dict[str, object], storage: Storage) -> None:
    event_name = _required_string(hook, "hook_event_name")
    content_keys = {
        "postToolUse": "tool_output",
        "afterAgentResponse": "text",
        "afterAgentThought": "text",
        "preCompact": "context",
    }
    content = _optional_string(hook, content_keys[event_name]) or ""
    name = _optional_string(hook, "tool_name") or event_name
    created_at = _now()
    project_label, _ = _project_identity(hook)
    estimation_model = _resolved_estimation_model(
        hook,
        storage,
        project_label=project_label,
        effective_at=created_at,
    )
    estimate: TokenEstimate | ToolOutputEstimate
    if event_name == "postToolUse":
        estimate = estimate_tool_output(name, content, estimation_model)
        category = "tool_text" if estimate.is_tokenizable_text else "tool_non_text"
    else:
        estimate = estimate_text(content, estimation_model)
        category = {
            "afterAgentResponse": "assistant",
            "afterAgentThought": "thinking",
            "preCompact": "compaction",
        }[event_name]
    generation_id = _required_string(hook, "generation_id")
    conversation_id = _required_string(hook, "conversation_id")
    inserted = storage.record_event(
        event_id=_event_id(hook, content),
        generation_id=generation_id,
        conversation_id=conversation_id,
        created_at=created_at,
        category=category,
        name=name,
        characters=estimate.characters,
        estimated_tokens=estimate.tokens,
        duration_ms=_optional_int(hook, "duration", fallback_key="duration_ms"),
        status=None,
    )
    if inserted == 0:
        return
    context_repository = ConversationContextRepository(storage)
    if event_name == "postToolUse":
        context_repository.record_tool_loop(
            generation_id,
            conversation_id,
            estimate.tokens,
            created_at,
        )
    elif event_name == "preCompact":
        context_repository.record_compaction(
            generation_id,
            conversation_id,
            created_at,
        )
    elif event_name == "afterAgentThought":
        context_repository.record_thinking(
            generation_id,
            conversation_id,
            estimate.tokens,
            _optional_int(hook, "duration_ms") or 0,
            created_at,
        )
        storage.record_output_estimator(generation_id, estimate.source)
    elif event_name == "afterAgentResponse":
        context_repository.record_response(
            conversation_id,
            estimate.tokens,
            created_at,
        )
        storage.record_output_estimator(generation_id, estimate.source)


def _record_subagent_start(hook: dict[str, object], storage: Storage) -> None:
    storage.record_subagent_start(
        subagent_id=_required_string(hook, "subagent_id"),
        parent_conversation_id=_required_string(
            hook, "parent_conversation_id", fallback_key="conversation_id"
        ),
        generation_id=_required_string(hook, "generation_id"),
        started_at=_now(),
        subagent_type=_required_string(hook, "subagent_type"),
        model=_optional_string(hook, "subagent_model"),
        is_parallel=_optional_bool(hook, "is_parallel_worker") or False,
    )


def _record_subagent_stop(hook: dict[str, object], storage: Storage) -> None:
    storage.record_subagent_stop(
        subagent_id=_required_string(hook, "subagent_id"),
        status=_optional_string(hook, "status"),
        duration_ms=_optional_int(hook, "duration_ms"),
        message_count=_optional_int(hook, "message_count"),
        tool_call_count=_optional_int(hook, "tool_call_count"),
    )


def _record_stop(hook: dict[str, object], storage: Storage) -> None:
    storage.update_turn_status(
        _required_string(hook, "generation_id"),
        _optional_string(hook, "status"),
    )


def _hook_response(event_name: str) -> dict[str, object]:
    if event_name == "beforeSubmitPrompt":
        return {"continue": True}
    if event_name == "subagentStart":
        return {"permission": "allow"}
    return {}


def _project_identity(hook: dict[str, object]) -> tuple[str, str]:
    roots = _workspace_roots(hook)
    if not roots:
        return "unknown", hashlib.sha256(b"unknown").hexdigest()[:16]
    root_text = str(roots[0])
    return roots[0].name or root_text, hashlib.sha256(root_text.encode()).hexdigest()[
        :16
    ]


def _workspace_roots(hook: dict[str, object]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in _list(hook.get("workspace_roots")):
        if isinstance(value, str):
            roots.append(Path(value).expanduser())
    return tuple(roots)


def _model_params(hook: dict[str, object]) -> list[dict[str, str]]:
    parameters: list[dict[str, str]] = []
    for value in _list(hook.get("model_params")):
        parameter = _mapping(value)
        identifier = _optional_string(parameter, "id")
        parameter_value = _optional_string(parameter, "value")
        if identifier is not None and parameter_value is not None:
            parameters.append({"id": identifier, "value": parameter_value})
    return parameters


def _estimation_model(hook: dict[str, object]) -> str | None:
    for key in ("model_id", "model"):
        model = _optional_string(hook, key)
        if model is not None and model.casefold() not in {"auto", "default"}:
            return model
    return None


def _resolved_estimation_model(
    hook: dict[str, object],
    storage: Storage,
    *,
    project_label: str,
    effective_at: str,
) -> str | None:
    explicit_model = _estimation_model(hook)
    if explicit_model is not None:
        return explicit_model
    return ModelAssignmentRepository(storage).resolve(
        conversation_id=_required_string(hook, "conversation_id"),
        project_label=project_label,
        effective_at=effective_at,
    )


def _event_id(hook: dict[str, object], content: str) -> str:
    stable_parts = {
        "event": hook.get("hook_event_name"),
        "generation": hook.get("generation_id"),
        "tool_use": hook.get("tool_use_id"),
        "subagent": hook.get("subagent_id"),
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
    }
    encoded = json.dumps(stable_parts, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidHookPayload("Expected a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise InvalidHookPayload("JSON object keys must be strings")
    return value


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _required_string(
    mapping: dict[str, object], key: str, *, fallback_key: str | None = None
) -> str:
    value = _optional_string(mapping, key)
    if value is None and fallback_key is not None:
        value = _optional_string(mapping, fallback_key)
    if value is None:
        raise InvalidHookPayload(f"Missing required string field: {key}")
    return value


def _optional_string(mapping: dict[str, object], key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) else None


def _optional_int(
    mapping: dict[str, object], key: str, *, fallback_key: str | None = None
) -> int | None:
    value = mapping.get(key)
    if not isinstance(value, int) and fallback_key is not None:
        value = mapping.get(fallback_key)
    return value if isinstance(value, int) else None


def _optional_bool(mapping: dict[str, object], key: str) -> bool | None:
    value = mapping.get(key)
    return value if isinstance(value, bool) else None


def _now() -> str:
    return datetime.now(UTC).isoformat()
