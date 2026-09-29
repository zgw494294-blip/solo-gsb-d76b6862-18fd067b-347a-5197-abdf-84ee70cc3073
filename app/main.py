"""FastAPI application: batch registration and recovery planning."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi import status as http_status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import db
from .db import get_conn
from .models import BatchIn
from .planner import Plan, ReplayItem, Snapshot, Segment, plan_recovery


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # SQLite schema is created idempotently before serving traffic.
    db.init_db()
    yield


app = FastAPI(
    title="Recovery Planning HTTP API",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "Register database snapshots and log-segment manifests in atomic "
        "batches (<= 100 items), and compute the best recovery point at a "
        "given UTC instant."
    ),
)


def _to_utc_iso(dt: datetime) -> str:
    """Normalize an aware datetime to a UTC ISO-8601 string ending in 'Z'."""
    as_utc = dt.astimezone(timezone.utc)
    return as_utc.strftime("%Y-%m-%dT%H:%M:%S") + (
        f".{as_utc.microsecond:06d}Z" if as_utc.microsecond else "Z"
    )


def _parse_at(value: str) -> datetime:
    """Parse the `at` query parameter as a timezone-aware UTC instant."""
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "invalid RFC-3339/ISO-8601 timestamp, e.g. 2026-09-29T00:00:00Z"
        ) from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware (use Z or +HH:MM)")
    return dt.astimezone(timezone.utc)


def _get_parsed_at(request: Request) -> datetime:
    raw = request.query_params.get("at")
    if raw is None:
        raise ValueError("query parameter 'at' is required")
    return _parse_at(raw)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/batches", status_code=http_status.HTTP_201_CREATED)
async def register_batch(
    request: Request,
    conn=Depends(get_conn),
) -> Any:
    """Atomically register a batch of snapshots and segments.

    Duplicate ids (inside the batch or against stored data) or illegal fields
    cause the whole batch to be rejected: nothing is written.
    """
    try:
        raw = await request.body()
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error(400, "invalid_json", "request body must be valid JSON")

    if not isinstance(payload, dict):
        return _error(
            422,
            "invalid_batch",
            "request body must be an object with 'snapshots' and 'segments'",
        )

    try:
        batch = BatchIn.model_validate(payload)
    except ValidationError as exc:
        return JSONResponse(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "invalid_batch",
                    "details": jsonable_encoder(exc.errors()),
                }
            },
        )

    snapshot_ids = [s.id for s in batch.snapshots]
    segment_ids = [s.id for s in batch.segments]
    all_ids = snapshot_ids + segment_ids

    if len(set(all_ids)) != len(all_ids):
        return _error(
            409,
            "duplicate_id",
            "duplicate id within batch; ids must be unique across snapshots "
            "and segments; whole batch rejected",
        )

    # Check against already-stored ids (snapshots and segments share one id
    # namespace) inside the same transaction.
    placeholders = ",".join("?" for _ in all_ids)
    row = conn.execute(
        f"""
        SELECT (
            SELECT COUNT(*) FROM snapshots WHERE id IN ({placeholders})
        ) + (
            SELECT COUNT(*) FROM segments WHERE id IN ({placeholders})
        ) AS conflicts
        """,
        all_ids + all_ids,
    ).fetchone()
    if row["conflicts"] > 0:
        return _error(
            409,
            "duplicate_id",
            "one or more ids already exist; whole batch rejected",
        )

    conn.executemany(
        """
        INSERT INTO snapshots (id, created_at, applied_seq, format)
        VALUES (:id, :created_at, :applied_seq, :format)
        """,
        [
            {
                "id": s.id,
                "created_at": _to_utc_iso(s.created_at),
                "applied_seq": s.applied_seq,
                "format": s.format,
            }
            for s in batch.snapshots
        ],
    )
    conn.executemany(
        """
        INSERT INTO segments (id, created_at, format, first_seq, last_seq)
        VALUES (:id, :created_at, :format, :first_seq, :last_seq)
        """,
        [
            {
                "id": s.id,
                "created_at": _to_utc_iso(s.created_at),
                "format": s.format,
                "first_seq": s.first_seq,
                "last_seq": s.last_seq,
            }
            for s in batch.segments
        ],
    )
    conn.commit()

    return {
        "status": "registered",
        "count": {
            "snapshots": len(batch.snapshots),
            "segments": len(batch.segments),
            "total": len(all_ids),
        },
    }


def _replay_to_dict(item: ReplayItem) -> dict[str, Any]:
    return {
        "segment_id": item.id,
        "segment_range": {"first_seq": item.first_seq, "last_seq": item.last_seq},
        "replayed_range": {"from_seq": item.from_seq, "to_seq": item.to_seq},
    }


def _plan_to_dict(plan: Plan) -> dict[str, Any]:
    return {
        "status": "recovery_point",
        "snapshot": {
            "id": plan.snapshot_id,
            "created_at": plan.snapshot_created_at,
            "applied_seq": plan.applied_seq,
            "format": plan.format,
        },
        "replays": [_replay_to_dict(item) for item in plan.replays],
        "segment_count": len(plan.replays),
        "end_seq": plan.end_seq,
    }


@app.get("/v1/recovery-plan")
def recovery_plan(
    request: Request,
    conn=Depends(get_conn),
) -> Any:
    """Return the best recovery point visible at query instant `at` (UTC)."""
    try:
        at = _get_parsed_at(request)
    except ValueError as exc:
        return _error(422, "invalid_at", str(exc))

    at_iso = _to_utc_iso(at)

    snapshot_rows = conn.execute(
        "SELECT id, created_at, applied_seq, format FROM snapshots "
        "WHERE created_at <= ? ORDER BY id",
        (at_iso,),
    ).fetchall()
    segment_rows = conn.execute(
        "SELECT id, created_at, format, first_seq, last_seq FROM segments "
        "WHERE created_at <= ? ORDER BY id",
        (at_iso,),
    ).fetchall()

    snapshots = [
        Snapshot(
            id=r["id"],
            created_at=r["created_at"],
            applied_seq=r["applied_seq"],
            format=r["format"],
        )
        for r in snapshot_rows
    ]
    segments = [
        Segment(
            id=r["id"],
            created_at=r["created_at"],
            format=r["format"],
            first_seq=r["first_seq"],
            last_seq=r["last_seq"],
        )
        for r in segment_rows
    ]

    if not snapshots:
        return {
            "status": "no_recovery_point",
            "at": at_iso,
            "message": "no snapshot is visible at the given instant",
        }

    plan = plan_recovery(snapshots, segments)
    result = _plan_to_dict(plan)
    result["at"] = at_iso
    return result


@app.get("/v1/inventory")
def inventory(
    request: Request,
    conn=Depends(get_conn),
) -> Any:
    """Inspection helper: list all manifest items (optionally as of `at`)."""
    at_iso: str | None = None
    raw = request.query_params.get("at")
    if raw is not None:
        try:
            at_iso = _to_utc_iso(_parse_at(raw))
        except ValueError as exc:
            return _error(422, "invalid_at", str(exc))

    if at_iso is None:
        snapshot_rows = conn.execute(
            "SELECT id, created_at, applied_seq, format FROM snapshots ORDER BY id"
        ).fetchall()
        segment_rows = conn.execute(
            "SELECT id, created_at, format, first_seq, last_seq "
            "FROM segments ORDER BY id"
        ).fetchall()
    else:
        snapshot_rows = conn.execute(
            "SELECT id, created_at, applied_seq, format FROM snapshots "
            "WHERE created_at <= ? ORDER BY id",
            (at_iso,),
        ).fetchall()
        segment_rows = conn.execute(
            "SELECT id, created_at, format, first_seq, last_seq FROM segments "
            "WHERE created_at <= ? ORDER BY id",
            (at_iso,),
        ).fetchall()

    return {
        "at": at_iso,
        "snapshots": [dict(r) for r in snapshot_rows],
        "segments": [dict(r) for r in segment_rows],
    }
