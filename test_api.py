"""End-to-end HTTP checks using FastAPI TestClient (stdlib unittest)."""

import os
import tempfile
import unittest

os.environ["RECOVERY_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.client = TestClient(app)

    def setUp(self):
        conn = db._connect()
        try:
            conn.execute("DELETE FROM snapshots")
            conn.execute("DELETE FROM segments")
            conn.commit()
        finally:
            conn.close()

    # ---- batch validation / atomicity --------------------------------

    def test_empty_batch_rejected(self):
        r = self.client.post("/v1/batches", json={"snapshots": [], "segments": []})
        self.assertEqual(r.status_code, 422)

    def test_batch_over_100_rejected(self):
        body = {
            "snapshots": [
                {"id": f"s{i}", "created_at": "2026-01-01T00:00:00Z",
                 "applied_seq": 0, "format": "f"}
                for i in range(101)
            ],
            "segments": [],
        }
        r = self.client.post("/v1/batches", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.client.get("/v1/inventory").json()["snapshots"], [])

    def test_illegal_fields_whole_batch_dropped(self):
        body = {
            "snapshots": [
                {"id": "good", "created_at": "2026-01-01T00:00:00Z",
                 "applied_seq": 0, "format": "f"},
                {"id": "bad", "created_at": "2026-01-01T00:00:00Z",
                 "applied_seq": -1, "format": "f"},
            ],
            "segments": [],
        }
        r = self.client.post("/v1/batches", json=body)
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.client.get("/v1/inventory").json()["snapshots"], [])

    def test_bad_segment_interval_rejected(self):
        body = {"segments": [
            {"id": "g1", "created_at": "2026-01-01T00:00:00Z", "format": "f",
             "first_seq": 5, "last_seq": 2},
        ]}
        r = self.client.post("/v1/batches", json=body)
        self.assertEqual(r.status_code, 422)

    def test_zero_and_negative_seq_rejected(self):
        body = {"segments": [
            {"id": "g1", "created_at": "2026-01-01T00:00:00Z", "format": "f",
             "first_seq": 0, "last_seq": 2},
        ]}
        self.assertEqual(self.client.post("/v1/batches", json=body).status_code, 422)
        body = {"snapshots": [
            {"id": "s1", "created_at": "2026-01-01T00:00:00Z",
             "applied_seq": -3, "format": "f"},
        ]}
        self.assertEqual(self.client.post("/v1/batches", json=body).status_code, 422)

    def test_naive_timestamp_rejected(self):
        body = {"snapshots": [
            {"id": "s1", "created_at": "2026-01-01T00:00:00",
             "applied_seq": 0, "format": "f"},
        ]}
        self.assertEqual(self.client.post("/v1/batches", json=body).status_code, 422)

    def test_unknown_field_rejected(self):
        body = {"snapshots": [
            {"id": "s1", "created_at": "2026-01-01T00:00:00Z",
             "applied_seq": 0, "format": "f", "extra": 1},
        ]}
        self.assertEqual(self.client.post("/v1/batches", json=body).status_code, 422)

    def test_boolean_seq_rejected(self):
        body = {"snapshots": [
            {"id": "s1", "created_at": "2026-01-01T00:00:00Z",
             "applied_seq": True, "format": "f"},
        ]}
        self.assertEqual(self.client.post("/v1/batches", json=body).status_code, 422)

    def test_blank_id_rejected(self):
        body = {"snapshots": [
            {"id": "   ", "created_at": "2026-01-01T00:00:00Z",
             "applied_seq": 0, "format": "f"},
        ]}
        self.assertEqual(self.client.post("/v1/batches", json=body).status_code, 422)

    def test_duplicate_id_within_batch(self):
        body = {
            "snapshots": [{"id": "x", "created_at": "2026-01-01T00:00:00Z",
                           "applied_seq": 0, "format": "f"}],
            "segments": [{"id": "x", "created_at": "2026-01-01T00:00:00Z",
                          "format": "f", "first_seq": 1, "last_seq": 2}],
        }
        r = self.client.post("/v1/batches", json=body)
        self.assertEqual(r.status_code, 409, r.text)
        inv = self.client.get("/v1/inventory").json()
        self.assertEqual(inv["snapshots"], [])
        self.assertEqual(inv["segments"], [])

    def test_duplicate_id_against_stored(self):
        body = {"snapshots": [{"id": "x", "created_at": "2026-01-01T00:00:00Z",
                               "applied_seq": 0, "format": "f"}]}
        self.assertEqual(self.client.post("/v1/batches", json=body).status_code, 201)
        body2 = {"segments": [{"id": "x", "created_at": "2026-01-01T00:00:00Z",
                               "format": "f", "first_seq": 1, "last_seq": 2}]}
        self.assertEqual(self.client.post("/v1/batches", json=body2).status_code, 409)
        self.assertEqual(self.client.get("/v1/inventory").json()["segments"], [])

    def test_mixed_batch_atomic_on_conflict(self):
        # valid snapshot + valid segment + id clash between two snapshots
        body = {
            "snapshots": [
                {"id": "s1", "created_at": "2026-01-01T00:00:00Z",
                 "applied_seq": 0, "format": "f"},
                {"id": "s1", "created_at": "2026-01-02T00:00:00Z",
                 "applied_seq": 9, "format": "f"},
            ],
            "segments": [
                {"id": "g1", "created_at": "2026-01-01T00:00:00Z",
                 "format": "f", "first_seq": 1, "last_seq": 3},
            ],
        }
        self.assertEqual(self.client.post("/v1/batches", json=body).status_code, 409)
        inv = self.client.get("/v1/inventory").json()
        self.assertEqual(inv["snapshots"], [])
        self.assertEqual(inv["segments"], [])

    def test_valid_batch_accepted_and_persisted(self):
        body = {
            "snapshots": [
                {"id": "s1", "created_at": "2026-01-01T08:00:00+08:00",
                 "applied_seq": 5, "format": "v1"},
            ],
            "segments": [
                {"id": "g1", "created_at": "2026-01-01T01:00:00Z",
                 "format": "v1", "first_seq": 6, "last_seq": 8},
            ],
        }
        r = self.client.post("/v1/batches", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["count"],
                         {"snapshots": 1, "segments": 1, "total": 2})
        inv = self.client.get("/v1/inventory").json()
        self.assertEqual(inv["snapshots"][0]["created_at"], "2026-01-01T00:00:00Z")

    # ---- recovery planning -------------------------------------------

    def _seed(self):
        self.client.post("/v1/batches", json={
            "snapshots": [
                # older snapshot applied 0, v1
                {"id": "snap-old", "created_at": "2026-01-01T00:00:00Z",
                 "applied_seq": 0, "format": "v1"},
                # newer snapshot applied 7, v1
                {"id": "snap-new", "created_at": "2026-01-03T00:00:00Z",
                 "applied_seq": 7, "format": "v1"},
                # v2 snapshot: cannot use v1 segments; its own endpoint (5)
                # must never be boosted by v1 chain segments
                {"id": "snap-v2", "created_at": "2026-01-02T00:00:00Z",
                 "applied_seq": 5, "format": "v2"},
            ],
            "segments": [
                # chain for snap-new (cursor 8):
                {"id": "seg-a", "created_at": "2026-01-04T00:00:00Z",
                 "format": "v1", "first_seq": 1, "last_seq": 9},
                {"id": "seg-b", "created_at": "2026-01-04T01:00:00Z",
                 "format": "v1", "first_seq": 9, "last_seq": 12},
                # gap: no segment covers 13
                {"id": "seg-c", "created_at": "2026-01-04T02:00:00Z",
                 "format": "v1", "first_seq": 14, "last_seq": 20},
                # future segment (not visible until 2026-02-01)
                {"id": "seg-future", "created_at": "2026-02-01T00:00:00Z",
                 "format": "v1", "first_seq": 8, "last_seq": 50},
            ],
        })

    def test_no_recovery_point(self):
        r = self.client.get("/v1/recovery-plan", params={"at": "2025-12-31T00:00:00Z"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "no_recovery_point")

    def test_plan_filters_by_time_and_replays_overlap(self):
        self._seed()
        # At 2026-01-05: both v1 snapshots visible; seg-future not visible.
        r = self.client.get("/v1/recovery-plan",
                            params={"at": "2026-01-05T00:00:00Z"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "recovery_point")
        self.assertEqual(body["snapshot"]["id"], "snap-new")
        self.assertEqual(body["end_seq"], 12)
        ids = [x["segment_id"] for x in body["replays"]]
        self.assertEqual(ids, ["seg-a", "seg-b"])
        # seg-a covers cursor 8: replayed [8,9] (prefix 1..7 not replayed)
        self.assertEqual(body["replays"][0]["replayed_range"],
                         {"from_seq": 8, "to_seq": 9})
        self.assertEqual(body["replays"][0]["segment_range"],
                         {"first_seq": 1, "last_seq": 9})
        # seg-b overlaps at 9: replayed [10,12] only
        self.assertEqual(body["replays"][1]["replayed_range"],
                         {"from_seq": 10, "to_seq": 12})

    def test_plan_includes_future_segment_when_visible(self):
        self._seed()
        r = self.client.get("/v1/recovery-plan",
                            params={"at": "2026-02-02T00:00:00Z"})
        body = r.json()
        self.assertEqual(body["status"], "recovery_point")
        # seg-future single piece takes snap-new cursor 8 -> 50, wins
        self.assertEqual(body["snapshot"]["id"], "snap-new")
        self.assertEqual(body["end_seq"], 50)
        self.assertEqual([x["segment_id"] for x in body["replays"]],
                         ["seg-future"])

    def test_created_exactly_at_query_instant_is_visible(self):
        self._seed()
        r = self.client.get("/v1/recovery-plan",
                            params={"at": "2026-01-03T00:00:00Z"})
        body = r.json()
        # snap-new created exactly at `at` is visible, no segments yet
        self.assertEqual(body["snapshot"]["id"], "snap-new")
        self.assertEqual(body["end_seq"], 7)
        self.assertEqual(body["replays"], [])

    def test_plan_requires_at(self):
        r = self.client.get("/v1/recovery-plan")
        self.assertEqual(r.status_code, 422)

    def test_plan_rejects_naive_at(self):
        r = self.client.get("/v1/recovery-plan", params={"at": "2026-01-01T00:00:00"})
        self.assertEqual(r.status_code, 422)

    def test_timezone_offsets_are_normalized_to_utc(self):
        self._seed()
        # 2026-01-05T08:00+08:00 == 2026-01-05T00:00Z
        r = self.client.get("/v1/recovery-plan",
                            params={"at": "2026-01-05T08:00:00+08:00"})
        self.assertEqual(r.json()["end_seq"], 12)

    def test_snapshot_without_segments_endpoint_is_applied_seq(self):
        self.client.post("/v1/batches", json={"snapshots": [
            {"id": "only", "created_at": "2026-01-01T00:00:00Z",
             "applied_seq": 42, "format": "v9"},
        ]})
        r = self.client.get("/v1/recovery-plan",
                            params={"at": "2026-01-02T00:00:00Z"})
        body = r.json()
        self.assertEqual(body["snapshot"]["id"], "only")
        self.assertEqual(body["end_seq"], 42)
        self.assertEqual(body["replays"], [])
        self.assertEqual(body["segment_count"], 0)

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})

    def test_invalid_json_body(self):
        r = self.client.post(
            "/v1/batches", content=b"{not json",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
