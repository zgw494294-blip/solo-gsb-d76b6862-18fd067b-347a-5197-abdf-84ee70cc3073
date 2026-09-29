"""Recovery-point planning.

Only inventory whose ``created_at`` (an instant in UTC) is not later than the
query instant ``at`` is visible.

For a snapshot with format F and ``applied_seq`` = A, replay starts at sequence
A + 1 and may *only* use segments of the same format F. A segment [f, l] is
eligible when it covers the next not-yet-replayed sequence c (``f <= c <= l``);
its already-played prefix [f, c-1] is skipped, replay extends to l, and the
next cursor becomes l + 1. Overlapping content is therefore never replayed.
If no eligible segment covers c, there is a gap and replay stops with endpoint
c - 1.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Snapshot:
    id: str
    created_at: str
    applied_seq: int
    format: str


@dataclass(frozen=True)
class Segment:
    id: str
    created_at: str
    format: str
    first_seq: int
    last_seq: int


@dataclass(frozen=True)
class ReplayItem:
    id: str
    from_seq: int  # first actually replayed sequence
    to_seq: int    # last actually replayed sequence (segment tail)
    first_seq: int  # stored interval of the segment
    last_seq: int   # stored interval of the segment


@dataclass(frozen=True)
class Plan:
    snapshot_id: str
    snapshot_created_at: str
    format: str
    applied_seq: int
    replays: tuple[ReplayItem, ...]
    end_seq: int  # fully recovered closed interval endpoint


def _best_chain_for_snapshot(
    snap: Snapshot, segments: list[Segment]
) -> tuple[int, tuple[str, ...]]:
    """Return (end_seq, chain_of_segment_ids) for one snapshot.

    Breadth-first enumeration over the cursor (= next unreplayed sequence).
    Within one BFS layer all chains have equal length, so for equal cursors
    only the lexicographically smallest chain is kept: any future suffix is
    identical and appends to both chains, so the smaller prefix can never be
    overtaken.
    """
    start_cursor = snap.applied_seq + 1
    # layer: cursor -> lexicographically smallest id chain reaching it
    layer: dict[int, tuple[str, ...]] = {start_cursor: ()}

    best: tuple[int, int, tuple[str, ...]] | None = None
    # key: (-end_seq, chain length, chain) -> minimize

    while layer:
        next_layer: dict[int, tuple[str, ...]] = {}
        for cursor, chain in layer.items():
            eligible = [
                s for s in segments if s.first_seq <= cursor <= s.last_seq
            ]
            if not eligible:
                # Gap (or no segments at all): replay stops here.
                end_seq = cursor - 1
                key = (-end_seq, len(chain), chain)
                if best is None or key < best:
                    best = key
                continue

            for seg in eligible:
                new_cursor = seg.last_seq + 1
                new_chain = chain + (seg.id,)
                old = next_layer.get(new_cursor)
                if old is None or new_chain < old:
                    next_layer[new_cursor] = new_chain
        layer = next_layer

    assert best is not None  # the initial state always yields a terminal
    return -best[0], best[2]


def _build_replays(
    snap: Snapshot, segments: list[Segment], chain: tuple[str, ...]
) -> tuple[ReplayItem, ...]:
    by_id = {s.id: s for s in segments}
    items: list[ReplayItem] = []
    cursor = snap.applied_seq + 1
    for seg_id in chain:
        seg = by_id[seg_id]
        # The overlapping prefix [first_seq, cursor-1] is not replayed.
        items.append(
            ReplayItem(
                id=seg.id,
                from_seq=cursor,
                to_seq=seg.last_seq,
                first_seq=seg.first_seq,
                last_seq=seg.last_seq,
            )
        )
        cursor = seg.last_seq + 1
    return tuple(items)


def plan_recovery(
    snapshots: list[Snapshot], segments: list[Segment]
) -> Plan | None:
    """Choose the best recovery point, or None when no snapshot is visible.

    Ranking (strict lexicographic priority, one snapshot's plan against
    another's):
      1. maximize end_seq
      2. then maximize the snapshot's applied_seq
      3. then minimize snapshot id
      4. then minimize number of replayed pieces
      5. then minimize the replay-id sequence lexicographically
    """
    best_plan: Plan | None = None
    # Components oriented so "smaller key == better plan"; tuple comparison
    # applies the criteria strictly in the documented priority order.
    best_key: tuple | None = None

    for snap in snapshots:
        same_format = [s for s in segments if s.format == snap.format]
        end_seq, chain = _best_chain_for_snapshot(snap, same_format)
        replays = _build_replays(snap, same_format, chain)

        key = (
            -end_seq,
            -snap.applied_seq,
            snap.id,
            len(chain),
            chain,
        )
        if best_key is None or key < best_key:
            best_key = key
            best_plan = Plan(
                snapshot_id=snap.id,
                snapshot_created_at=snap.created_at,
                format=snap.format,
                applied_seq=snap.applied_seq,
                replays=replays,
                end_seq=end_seq,
            )

    return best_plan
