"""Request models with strict validation.

Any validation failure makes FastAPI return 422 *before* the endpoint runs,
so an illegal batch never touches the database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def _non_blank(value: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError("must be a non-empty, non-blank string")
    return value


NonBlankStr = Annotated[str, AfterValidator(_non_blank)]


def _require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            "must be timezone-aware, e.g. 2026-01-01T00:00:00Z or "
            "2026-01-01T08:00:00+08:00"
        )
    return dt


class SnapshotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NonBlankStr
    created_at: datetime
    applied_seq: int = Field(ge=0)
    format: NonBlankStr

    @field_validator("created_at")
    @classmethod
    def _created_at_aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("applied_seq", mode="before")
    @classmethod
    def _applied_seq_strict_int(cls, v):
        # bool is a subclass of int; reject True/False before coercion.
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("must be an integer (booleans are not allowed)")
        return v


class SegmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NonBlankStr
    created_at: datetime
    format: NonBlankStr
    first_seq: int
    last_seq: int

    @field_validator("created_at")
    @classmethod
    def _created_at_aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("first_seq", "last_seq", mode="before")
    @classmethod
    def _seqs_strict_positive_int(cls, v):
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("must be an integer (booleans are not allowed)")
        if v < 1:
            raise ValueError("must be a positive integer (>= 1)")
        return v

    @model_validator(mode="after")
    def _closed_interval(self) -> "SegmentIn":
        if self.first_seq > self.last_seq:
            raise ValueError("first_seq must be <= last_seq (closed interval)")
        return self


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshots: list[SnapshotIn] = Field(default_factory=list)
    segments: list[SegmentIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _batch_size(self) -> "BatchIn":
        total = len(self.snapshots) + len(self.segments)
        if total == 0:
            raise ValueError(
                "batch must contain at least one snapshot or segment"
            )
        if total > 100:
            raise ValueError(
                "batch total must not exceed 100 (snapshots + segments), "
                f"got {total}"
            )
        return self
