"""Typed domain contracts shared across adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    """Estimated token count derived from observable text."""

    characters: int
    tokens: int
    source: str = "generic_unicode_v1"


@dataclass(frozen=True, slots=True)
class ToolOutputEstimate:
    """Measured tool output with an explicit text-accounting decision."""

    characters: int
    tokens: int
    is_tokenizable_text: bool
    source: str


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """A model attribution with provenance and confidence."""

    conversation_id: str
    generation_id: str
    model: str
    source: str
    confidence: str


@dataclass(frozen=True, slots=True)
class ReportWindow:
    """UTC reporting interval with an exclusive upper bound."""

    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Estimated cost range in US dollars."""

    point: float
    low: float
    high: float


@dataclass(frozen=True, slots=True)
class TelemetryObservation:
    """Known token telemetry imported without request content."""

    observation_id: str
    request_id: str | None
    observed_at: str
    model: str
    effort: str
    tool_count: int
    attachment_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    imported_at: str


@dataclass(frozen=True, slots=True)
class CalibrationProfile:
    """Conditional empirical p10/p50/p90 token distribution."""

    model: str
    effort: str
    tool_bucket: str
    attachment_bucket: str
    sample_size: int
    input_p10: int
    input_p50: int
    input_p90: int
    output_p10: int
    output_p50: int
    output_p90: int
    cache_read_p50: int
    cache_write_p50: int
    updated_at: str
