"""Flask-flow tests: archives, fallback, retirement and failure preservation."""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="qc-test-")
os.environ["QC_DB"] = os.path.join(_TMP, "test.db")

import app as appmod  # noqa: E402


class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        appmod.app.config.update(TESTING=True, DATABASE=os.environ["QC_DB"])
        appmod.init_db()

    def setUp(self):
        conn = self.db()
        conn.executescript(
            "DROP TRIGGER IF EXISTS fail_sum; DROP TRIGGER IF EXISTS fail_det;"
            "DELETE FROM determinations; DELETE FROM details; "
            "DELETE FROM summaries; DELETE FROM coils; DELETE FROM batches;"
        )
        conn.commit()
        conn.close()
        self.client = appmod.app.test_client()
        self.client.post("/batches", data={"batch_no": "B1"})
        self.bid = self.db().execute("SELECT id FROM batches").fetchone()["id"]

    def db(self):
        conn = sqlite3.connect(os.environ["QC_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def summary(self, coil, n, mn, p, mx):
        return self.client.post(
            f"/batch/{self.bid}/summary",
            data={"coil_no": coil, "point_count": n, "min_um": mn,
                  "p95_um": p, "max_um": mx},
            follow_redirects=True,
        )

    def detail(self, coil, raw):
        return self.client.post(
            f"/batch/{self.bid}/detail",
            content_type="multipart/form-data",
            data={"coil_no": coil, "readings": (io.BytesIO(raw), "c.csv")},
            follow_redirects=True,
        )

    def determine(self, limit):
        return self.client.post(
            f"/batch/{self.bid}/determine",
            data={"limit_um": limit}, follow_redirects=True,
        )

    def last_det(self):
        return self.db().execute(
            "SELECT * FROM determinations ORDER BY id DESC LIMIT 1"
        ).fetchone()

    # ------------------------------------------------------------------

    def test_infeasible_summary_rejected(self):
        text = self.summary("C1", 2, 100, 200, 300).get_data(as_text=True)
        self.assertIn("无法", text)
        count = self.db().execute("SELECT COUNT(*) c FROM summaries").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_bounds_and_verdicts(self):
        self.summary("C1", 2, 100, 300, 300)
        self.summary("C2", 2, 200, 300, 300)
        self.determine(300)
        d = self.last_det()
        # Total 4 readings, k=4: P95 is the batch maximum, forced to 300.
        self.assertEqual((d["min_possible"], d["max_possible"]), (300, 300))
        self.assertEqual(d["verdict"], "must_not_exceed")

        self.determine(250)
        self.assertEqual(self.last_det()["verdict"], "must_exceed")

    def test_needs_detail_band(self):
        # Total 40 readings, k=38.  C1 (20 points) never exceeds 300, so the
        # low end is 300; C2's P95=500 pushes the high end to 500.
        self.summary("C1", 20, 100, 300, 300)
        self.summary("C2", 20, 100, 500, 500)
        self.determine(400)
        d = self.last_det()
        self.assertEqual((d["min_possible"], d["max_possible"]), (300, 500), dict(d))
        self.assertEqual(d["verdict"], "needs_detail", dict(d))
        self.determine(299)
        self.assertEqual(self.last_det()["verdict"], "must_exceed")
        self.determine(500)
        self.assertEqual(self.last_det()["verdict"], "must_not_exceed")

    def test_matching_detail_is_used(self):
        self.summary("C1", 2, 100, 300, 300)
        self.detail("C1", b"reading,min,p95,max\n100,100,300,300\n300,100,300,300\n")
        row = self.db().execute("SELECT * FROM details").fetchone()
        self.assertEqual(row["active"], 1)
        self.determine(300)
        snap = json.loads(self.last_det()["snapshot_json"])
        self.assertEqual({c["coil_no"]: c["kind"] for c in snap["coils"]},
                         {"C1": "detail"})

    def test_mismatched_detail_is_archived_inactive(self):
        self.summary("C1", 2, 100, 300, 300)
        text = self.detail("C1", b"100\n200\n300\n").get_data(as_text=True)
        self.assertIn("已停用", text)
        row = self.db().execute("SELECT * FROM details").fetchone()
        self.assertEqual(row["active"], 0)
        self.assertIn("与摘要不符", row["retired_reason"])
        self.determine(300)
        snap = json.loads(self.last_det()["snapshot_json"])
        self.assertEqual(snap["coils"][0]["kind"], "summary")

    def test_summary_change_retires_unmatched_detail(self):
        self.summary("C1", 2, 100, 300, 300)
        self.detail("C1", b"100\n300\n")
        text = self.summary("C1", 3, 100, 300, 300).get_data(as_text=True)
        self.assertIn("已停用", text)
        self.assertIn("改按新摘要核算", text)
        detail = self.db().execute("SELECT * FROM details").fetchone()
        self.assertEqual(detail["active"], 0)
        active = self.db().execute(
            "SELECT * FROM summaries WHERE active = 1"
        ).fetchall()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["point_count"], 3)

    def test_summary_change_keeps_matching_detail(self):
        self.summary("C1", 2, 100, 300, 300)
        self.detail("C1", b"100\n300\n")
        text = self.summary("C1", 2, 100, 300, 300).get_data(as_text=True)
        self.assertIn("明细继续有效", text)
        detail = self.db().execute("SELECT * FROM details").fetchone()
        self.assertEqual(detail["active"], 1)

    def test_detail_without_summary_refused(self):
        text = self.detail("C1", b"100\n300\n").get_data(as_text=True)
        self.assertIn("请先登记摘要", text)
        count = self.db().execute("SELECT COUNT(*) c FROM details").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_history_snapshot_is_immutable(self):
        self.summary("C1", 2, 100, 300, 300)
        self.determine(300)
        first = self.last_det()
        self.summary("C1", 2, 100, 500, 500)   # change the archive afterwards
        again = self.db().execute(
            "SELECT * FROM determinations WHERE id = ?", (first["id"],)
        ).fetchone()
        self.assertEqual(again["snapshot_json"], first["snapshot_json"])
        self.assertEqual(again["min_possible"], 300)

    def test_invalid_limit_writes_nothing(self):
        self.summary("C1", 2, 100, 300, 300)
        self.determine(300)
        before = self.db().execute("SELECT COUNT(*) c FROM determinations").fetchone()["c"]
        self.determine("abc")
        after = self.db().execute("SELECT COUNT(*) c FROM determinations").fetchone()["c"]
        self.assertEqual(before, after)

    def test_write_failure_rolls_back(self):
        self.summary("C1", 2, 100, 300, 300)
        self.detail("C1", b"100\n300\n")
        conn = self.db()
        conn.execute(
            "CREATE TRIGGER fail_sum BEFORE INSERT ON summaries "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        conn.commit()
        conn.close()
        self.summary("C1", 3, 100, 300, 300)
        conn = self.db()
        rows = conn.execute(
            "SELECT point_count, active FROM summaries ORDER BY id"
        ).fetchall()
        detail = conn.execute("SELECT active FROM details").fetchone()
        conn.close()
        self.assertEqual([tuple(r) for r in rows], [(2, 1)])
        self.assertEqual(detail["active"], 1)

    def test_determination_failure_keeps_previous(self):
        self.summary("C1", 2, 100, 300, 300)
        self.determine(300)
        conn = self.db()
        conn.execute(
            "CREATE TRIGGER fail_det BEFORE INSERT ON determinations "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        conn.commit()
        conn.close()
        text = self.determine(999).get_data(as_text=True)
        self.assertIn("原档案保持不变", text)
        count = self.db().execute("SELECT COUNT(*) c FROM determinations").fetchone()["c"]
        self.assertEqual(count, 1)


class ReuseTests(unittest.TestCase):
    """The saved whole-batch P95 interval is reused only for the same basis."""

    def setUp(self):
        appmod.app.config.update(TESTING=True, DATABASE=os.environ["QC_DB"])
        appmod.ensure_schema()
        conn = sqlite3.connect(os.environ["QC_DB"])
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "DROP TRIGGER IF EXISTS fail_sum; DROP TRIGGER IF EXISTS fail_det;"
            "DELETE FROM determinations; DELETE FROM details; "
            "DELETE FROM summaries; DELETE FROM coils; DELETE FROM batches;"
        )
        conn.commit()
        conn.close()
        self.client = appmod.app.test_client()
        self.client.post("/batches", data={"batch_no": "R1"})
        self.bid = self.db().execute("SELECT id FROM batches").fetchone()["id"]

    def db(self):
        conn = sqlite3.connect(os.environ["QC_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def summary(self, coil, n, mn, p, mx):
        return self.client.post(
            f"/batch/{self.bid}/summary",
            data={"coil_no": coil, "point_count": n, "min_um": mn,
                  "p95_um": p, "max_um": mx},
            follow_redirects=True,
        )

    def detail(self, coil, raw):
        return self.client.post(
            f"/batch/{self.bid}/detail",
            content_type="multipart/form-data",
            data={"coil_no": coil, "readings": (io.BytesIO(raw), "c.csv")},
            follow_redirects=True,
        )

    def determine(self, limit):
        return self.client.post(
            f"/batch/{self.bid}/determine",
            data={"limit_um": limit}, follow_redirects=True,
        )

    def rows(self):
        conn = self.db()
        rows = conn.execute(
            "SELECT * FROM determinations ORDER BY id"
        ).fetchall()
        conn.close()
        return rows

    def last(self):
        return self.rows()[-1]

    # ------------------------------------------------------------------

    def test_same_basis_reuses_interval_across_limits(self):
        self.summary("C1", 20, 100, 300, 300)
        self.summary("C2", 20, 100, 500, 500)
        self.determine(400)
        self.determine(299)
        self.determine(500)
        first, second, third = self.rows()
        self.assertEqual(first["reused"], 0)
        self.assertEqual(second["reused"], 1)
        self.assertEqual(second["reused_from"], first["id"])
        self.assertEqual(third["reused"], 1)
        # The reused interval is the one from the last saved determination.
        self.assertEqual(third["reused_from"], second["id"])
        for row in (first, second, third):
            self.assertEqual(
                (row["total_points"], row["p95_index"],
                 row["min_possible"], row["max_possible"]),
                (40, 38, 300, 500),
            )
        # The verdict follows each request's own limit.
        self.assertEqual([r["verdict"] for r in (first, second, third)],
                         ["needs_detail", "must_exceed", "must_not_exceed"])
        # Every determination is saved with its own archive number.
        self.assertEqual(len({r["id"] for r in (first, second, third)}), 3)

    def test_reuse_requires_the_last_determination_to_match(self):
        self.summary("C1", 2, 100, 300, 300)
        self.determine(300)
        first = self.last()
        self.summary("C1", 2, 100, 500, 500)
        self.determine(300)
        changed = self.last()
        self.assertEqual(changed["reused"], 0)
        self.summary("C1", 2, 100, 300, 300)
        self.determine(300)
        # The basis matches #first, but the last saved determination does not.
        self.assertEqual(self.last()["reused"], 0)
        self.assertEqual(self.last()["reused_from"], None)

    def test_result_page_marks_reuse_and_number(self):
        self.summary("C1", 2, 100, 300, 300)
        self.determine(300)
        text = self.determine(300).get_data(as_text=True)
        first = self.rows()[0]
        self.assertIn("复用区间", text)
        self.assertIn(f"#{first['id']}", text)
        self.assertIn(f"#{self.last()['id']}", text)

    def test_resubmitted_identical_summary_reuses(self):
        self.summary("C1", 20, 100, 300, 300)
        self.summary("C2", 20, 100, 500, 500)
        self.determine(400)
        self.summary("C1", 20, 100, 300, 300)   # same numbers, new archive row
        self.determine(400)
        self.assertEqual(self.last()["reused"], 1)

    def test_changing_summary_recomputes(self):
        self.summary("C1", 2, 100, 300, 300)
        self.determine(300)
        self.summary("C1", 2, 100, 500, 500)
        self.determine(300)
        self.assertEqual(self.last()["reused"], 0)
        self.assertEqual(self.last()["max_possible"], 500)

    def test_switching_basis_kind_recomputes_even_with_equal_stats(self):
        self.summary("C1", 20, 100, 300, 300)
        self.summary("C2", 20, 100, 400, 400)
        self.determine(300)
        first = self.last()
        self.assertEqual(first["reused"], 0)
        # Same four numbers, but now the coil is accounted by its readings.
        self.detail("C1", b"100\n" + b"300\n" * 19)
        self.determine(300)
        snap = json.loads(self.last()["snapshot_json"])
        self.assertEqual({c["coil_no"]: c["kind"] for c in snap["coils"]},
                         {"C1": "detail", "C2": "summary"})
        self.assertEqual(self.last()["reused"], 0)

    def retire_detail(self, coil):
        conn = self.db()
        row = conn.execute(
            "SELECT d.id FROM details d JOIN coils c ON c.id = d.coil_id "
            "WHERE c.coil_no = ? AND d.active = 1", (coil,)
        ).fetchone()
        conn.close()
        self.client.post(
            f"/batch/{self.bid}/detail/{row['id']}/retire", follow_redirects=True
        )

    def test_detail_reorder_reuses_but_changed_distribution_does_not(self):
        self.summary("C1", 20, 100, 200, 300)
        self.detail("C1", b"100\n" * 18 + b"200\n300\n")
        self.summary("C2", 20, 100, 400, 400)
        self.determine(300)
        first = self.last()
        self.assertEqual(first["reused"], 0)

        # Same readings, different row order -> reuse.
        self.retire_detail("C1")
        self.detail("C1", b"300\n200\n" + b"100\n" * 18)
        self.determine(300)
        self.assertEqual(self.last()["reused"], 1)
        self.assertEqual(self.last()["reused_from"], first["id"])

        # Different distribution with the same four statistics -> recompute.
        self.retire_detail("C1")
        self.detail("C1", b"100\n" * 17 + b"200\n200\n300\n")
        self.determine(300)
        self.assertEqual(self.last()["reused"], 0)

    def test_empty_detail_not_used(self):
        self.summary("C1", 2, 100, 300, 300)
        # An active detail whose readings are blank cannot be the basis.
        conn = self.db()
        coil_id = conn.execute("SELECT id FROM coils WHERE coil_no='C1'").fetchone()["id"]
        conn.execute(
            "INSERT INTO details(coil_id, readings, point_count, min_um, p95_um, "
            "max_um, content_hash, active, created_at) VALUES (?, '[]', 2, 100, 300, 300, NULL, 1, 'x')",
            (coil_id,),
        )
        conn.commit()
        conn.close()
        text = self.determine(300).get_data(as_text=True)
        self.assertIn("退回摘要", text)
        self.assertEqual(
            json.loads(self.last()["snapshot_json"])["coils"][0]["kind"], "summary"
        )

    def test_reuse_does_not_read_per_point_readings(self):
        self.summary("C1", 20, 100, 200, 300)
        self.detail("C1", b"100\n" * 18 + b"200\n300\n")
        self.determine(300)
        with mock.patch.object(
            appmod, "load_snapshots",
            side_effect=AssertionError("per-point readings must not be read"),
        ):
            text = self.determine(150).get_data(as_text=True)
        self.assertIn("复用区间", text)
        self.assertEqual(self.last()["reused"], 1)
        self.assertEqual(self.last()["verdict"], "must_exceed")

    def test_failed_save_can_be_retried_and_history_stays_readable(self):
        self.summary("C1", 2, 100, 300, 300)
        self.determine(300)
        conn = self.db()
        conn.execute(
            "CREATE TRIGGER fail_det BEFORE INSERT ON determinations "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        conn.commit()
        conn.close()
        self.determine(250)
        self.assertEqual(len(self.rows()), 1)
        conn = self.db()
        conn.execute("DROP TRIGGER fail_det")
        conn.commit()
        conn.close()
        text = self.determine(250).get_data(as_text=True)
        self.assertIn("必超限", text)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["reused"], 1)
        self.assertEqual(rows[1]["reused_from"], rows[0]["id"])
        old = self.client.get(f"/batch/{self.bid}/determination/{rows[0]['id']}")
        self.assertEqual(old.status_code, 200)

    def test_old_schema_upgrades_and_reuses_after_restart(self):
        # Simulate a database written by the first release: no reuse columns.
        path = os.path.join(_TMP, "legacy.db")
        if os.path.exists(path):
            os.remove(path)
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE batches (id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_no TEXT NOT NULL UNIQUE, note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL);
            CREATE TABLE coils (id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              coil_no TEXT NOT NULL, created_at TEXT NOT NULL,
              UNIQUE (batch_id, coil_no));
            CREATE TABLE summaries (id INTEGER PRIMARY KEY AUTOINCREMENT,
              coil_id INTEGER NOT NULL REFERENCES coils(id) ON DELETE CASCADE,
              point_count INTEGER NOT NULL, min_um INTEGER NOT NULL,
              p95_um INTEGER NOT NULL, max_um INTEGER NOT NULL,
              active INTEGER NOT NULL DEFAULT 1, retired_reason TEXT,
              retired_at TEXT, created_at TEXT NOT NULL);
            CREATE TABLE details (id INTEGER PRIMARY KEY AUTOINCREMENT,
              coil_id INTEGER NOT NULL REFERENCES coils(id) ON DELETE CASCADE,
              readings TEXT NOT NULL, point_count INTEGER NOT NULL,
              min_um INTEGER NOT NULL, p95_um INTEGER NOT NULL,
              max_um INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
              retired_reason TEXT, retired_at TEXT, created_at TEXT NOT NULL);
            CREATE TABLE determinations (id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              limit_um INTEGER NOT NULL, total_points INTEGER NOT NULL,
              p95_index INTEGER NOT NULL, min_possible INTEGER NOT NULL,
              max_possible INTEGER NOT NULL, verdict TEXT NOT NULL,
              snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL);
            INSERT INTO batches(batch_no, note, created_at) VALUES ('OLD', '', 'x');
            INSERT INTO coils(batch_id, coil_no, created_at) VALUES (1, 'C1', 'x');
            INSERT INTO summaries(coil_id, point_count, min_um, p95_um, max_um,
              active, created_at) VALUES (1, 2, 100, 300, 300, 1, 'x');
            INSERT INTO details(coil_id, readings, point_count, min_um, p95_um,
              max_um, active, created_at)
              VALUES (1, '[100,300]', 2, 100, 300, 300, 1, 'x');
            """
        )
        conn.commit()
        conn.close()

        original = appmod.app.config["DATABASE"]
        appmod.app.config["DATABASE"] = path
        try:
            appmod.ensure_schema()  # first start after upgrade
            client = appmod.app.test_client()
            client.post("/batch/1/determine", data={"limit_um": 300},
                        follow_redirects=True)
            # Restart: schema is applied again, the interval is reusable.
            appmod.ensure_schema()
            client.post("/batch/1/determine", data={"limit_um": 200},
                        follow_redirects=True)
        finally:
            appmod.app.config["DATABASE"] = original

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM determinations ORDER BY id").fetchall()
        detail = conn.execute("SELECT content_hash FROM details").fetchone()
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["reused"], 0)
        self.assertEqual(rows[1]["reused"], 1)
        self.assertEqual(rows[1]["reused_from"], rows[0]["id"])
        self.assertEqual((rows[0]["min_possible"], rows[0]["max_possible"]),
                         (300, 300))
        self.assertIsNotNone(detail["content_hash"])


if __name__ == "__main__":
    unittest.main()
