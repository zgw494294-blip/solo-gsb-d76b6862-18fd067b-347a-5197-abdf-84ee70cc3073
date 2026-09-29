"""Standalone correctness checks for planner.py (stdlib only)."""

from app.planner import Snapshot, Segment, plan_recovery, ReplayItem


def plan(snaps, segs):
    p = plan_recovery(list(snaps), list(segs))
    return p


def fmt_replays(p):
    return [(r.id, r.from_seq, r.to_seq) for r in p.replays]


# 1) Basic chain with overlap: cursor 1, [1,3] -> [3,5] overlap skips seq 3.
s1 = Snapshot("snap-a", "2026-09-29T00:00:00Z", 0, "v1")
gA = Segment("a1", "t", "v1", 1, 3)
gB = Segment("b1", "t", "v1", 3, 5)
p = plan([s1], [gA, gB])
assert p.end_seq == 5, p
assert fmt_replays(p) == [("a1", 1, 3), ("b1", 4, 5)], fmt_replays(p)

# 2) Gap stops: [1,2], then [4,5] missing seq 3 -> endpoint 2.
gC = Segment("c1", "t", "v1", 1, 2)
gD = Segment("d1", "t", "v1", 4, 5)
p = plan([s1], [gC, gD])
assert p.end_seq == 2 and fmt_replays(p) == [("c1", 1, 2)], p

# 3) Maximize endpoint: choose farthest-reaching segment at a step.
#    at cursor 1: x:[1,2] then z:[3,10]; y:[1,5] stops at 5 (6 gap).
gx = Segment("x", "t", "v1", 1, 2)
gy = Segment("y", "t", "v1", 1, 5)
gz = Segment("z", "t", "v1", 3, 10)
p = plan([s1], [gx, gy, gz])
assert p.end_seq == 10, fmt_replays(p)
assert [t[0] for t in fmt_replays(p)] == ["x", "z"], fmt_replays(p)

# 4) Same endpoint / same piece count -> lexicographically smallest id chain.
#    cursor 1: m:[1,3] then p:[4,4]; n:[1,3] then p:[4,4]
gm = Segment("m", "t", "v1", 1, 3)
gn = Segment("n", "t", "v1", 1, 3)
gp = Segment("p", "t", "v1", 4, 4)
p = plan([s1], [gm, gn, gp])
assert p.end_seq == 4 and [t[0] for t in fmt_replays(p)] == ["m", "p"]

# 5) Fewer pieces wins on equal endpoint.
#    cursor 1: q:[1,4] ; r:[1,2], s:[3,4]
gq = Segment("q", "t", "v1", 1, 4)
gr = Segment("r", "t", "v1", 1, 2)
gs = Segment("s", "t", "v1", 3, 4)
p = plan([s1], [gq, gr, gs])
assert p.end_seq == 4 and [t[0] for t in fmt_replays(p)] == ["q"]

# 6) Snapshot ranking: same end -> bigger applied_seq wins, then smaller id.
s_hi = Snapshot("snap-z", "t", 10, "v2")  # applied 10, no segs -> end 10
s_lo_a = Snapshot("snap-a", "t", 5, "v3")
g_lo = Segment("w", "t", "v3", 6, 10)
p = plan([s_lo_a, s_hi], [g_lo])  # both end at 10
assert p.snapshot_id == "snap-z", p.snapshot_id

s_hi2 = Snapshot("zzz", "t", 10, "v4")
s_hi1 = Snapshot("aaa", "t", 10, "v5")
p = plan([s_hi2, s_hi1], [])
assert p.snapshot_id == "aaa", p.snapshot_id

# 7) Endpoint dominates applied_seq: snap at applied 0 reaches 100 wins over
#    snap applied 10 reaching only 10.
s_far = Snapshot("snap-far", "t", 0, "v6")
g_far = Segment("big", "t", "v6", 1, 100)
p = plan([s_hi, s_far], [g_far])
assert p.snapshot_id == "snap-far" and p.end_seq == 100

# 8) Different-format segments are never used.
p = plan([s1], [Segment("f-other", "t", "vX", 1, 10)])
assert p.end_seq == 0 and p.replays == ()

# 9) Segment starting before cursor (already applied) can extend from tail.
s5 = Snapshot("s5", "t", 5, "v1")  # cursor 6
ge = Segment("e", "t", "v1", 1, 8)
p = plan([s5], [ge])
assert p.end_seq == 8 and fmt_replays(p) == [("e", 6, 8)], fmt_replays(p)

# 10) No segments -> endpoint = applied_seq.
p = plan([s1], [])
assert p.end_seq == 0 and p.replays == ()

# 11) Longest-reach vs shortest-piece interplay: BFS must still pick min
#     pieces among max-endpoint terminals.
#     cursor1: j:[1,10] (1 piece, end10); k:[1,4], l:[5,10] (2 pieces,end10)
gj = Segment("j", "t", "v1", 1, 10)
gk = Segment("k", "t", "v1", 1, 4)
gl = Segment("l", "t", "v1", 5, 10)
p = plan([s1], [gj, gk, gl])
assert [t[0] for t in fmt_replays(p)] == ["j"], fmt_replays(p)

# 12) No snapshot at all -> None
assert plan([], [Segment("x", "t", "v1", 1, 1)]) is None

# 13) Branching with same-cursor dedup: two ways reach cursor 6 with ids
#     ('aa',) vs ('b',); longer-lex prefix kept smaller.
s0 = Snapshot("s0", "t", 0, "v1")
gaa = Segment("aa", "t", "v1", 1, 5)
gb = Segment("b", "t", "v1", 1, 5)
gtail = Segment("t1", "t", "v1", 6, 7)
p = plan([s0], [gaa, gb, gtail])
assert [t[0] for t in fmt_replays(p)] == ["aa", "t1"], fmt_replays(p)
assert p.end_seq == 7

# 14) Two structurally different chains, same endpoint/piece count -> lex.
#     a2:[1,2] then cursor 3, c2:[3,5] covers it (no overlap) -> 1..5.
#     b2:[1,3] then c2 replays only [4,5] (seq 3 overlap skipped) -> 1..5.
ga2 = Segment("a2", "t", "v1", 1, 2)
gb2 = Segment("b2", "t", "v1", 1, 3)
gc2 = Segment("c2", "t", "v1", 3, 5)
p = plan([s1], [ga2, gb2, gc2])
assert [t[0] for t in fmt_replays(p)] == ["a2", "c2"], fmt_replays(p)
assert p.end_seq == 5

# 15) Genuine stall: short reach forces an extra hop to reach the same gap;
#     a:[1,2]->b:[1,3](plays only 3)->cursor 4, nothing covers 4 -> end 3
#     with 2 pieces; b alone reaches cursor 4 with 1 piece -> b wins.
gh1 = Segment("h1", "t", "v1", 1, 2)
gh2 = Segment("h2", "t", "v1", 1, 3)
gh3 = Segment("h3", "t", "v1", 5, 6)  # unreachable: seq 4 missing
p = plan([s1], [gh1, gh2, gh3])
assert p.end_seq == 3 and [t[0] for t in fmt_replays(p)] == ["h2"], fmt_replays(p)

print("all planner checks passed")
