"""Coil thickness batch P95 checker.

Run locally:   python app.py
Docker:        docker compose up --build
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

import qc


BASE_DIR = Path(__file__).resolve().parent


def default_db_path() -> str:
    explicit = os.environ.get("QC_DB")
    if explicit:
        return explicit
    if Path("/data").is_dir() and os.access("/data", os.W_OK):
        return "/data/qc.db"
    return str(BASE_DIR / "data" / "qc.db")


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("QC_SECRET", "coil-p95-dev-secret"),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    DATABASE=default_db_path(),
)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no    TEXT NOT NULL UNIQUE,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coils (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    coil_no     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (batch_id, coil_no)
);

CREATE TABLE IF NOT EXISTS summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    coil_id         INTEGER NOT NULL REFERENCES coils(id) ON DELETE CASCADE,
    point_count     INTEGER NOT NULL,
    min_um          INTEGER NOT NULL,
    p95_um          INTEGER NOT NULL,
    max_um          INTEGER NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    retired_reason  TEXT,
    retired_at      TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_coil ON summaries(coil_id, active);

CREATE TABLE IF NOT EXISTS details (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    coil_id         INTEGER NOT NULL REFERENCES coils(id) ON DELETE CASCADE,
    readings        TEXT NOT NULL,          -- JSON array of integers
    point_count     INTEGER NOT NULL,
    min_um          INTEGER NOT NULL,
    p95_um          INTEGER NOT NULL,
    max_um          INTEGER NOT NULL,
    content_hash    TEXT,                   -- order-insensitive fingerprint of readings
    active          INTEGER NOT NULL DEFAULT 1,
    retired_reason  TEXT,
    retired_at      TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_details_coil ON details(coil_id, active);

CREATE TABLE IF NOT EXISTS determinations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    limit_um        INTEGER NOT NULL,
    total_points    INTEGER NOT NULL,
    p95_index       INTEGER NOT NULL,
    min_possible    INTEGER NOT NULL,
    max_possible    INTEGER NOT NULL,
    verdict         TEXT NOT NULL,
    snapshot_json   TEXT NOT NULL,
    basis_fingerprint TEXT,                 -- NULL for rows written before reuse existed
    reused          INTEGER NOT NULL DEFAULT 0,
    reused_from     INTEGER,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_det_batch ON determinations(batch_id, id);
"""


# Columns added after the first release.  ``CREATE TABLE IF NOT EXISTS`` does
# not touch an existing table, so old databases are upgraded in place here.
_COLUMN_MIGRATIONS = (
    ("details", "content_hash", "TEXT"),
    ("determinations", "basis_fingerprint", "TEXT"),
    ("determinations", "reused", "INTEGER NOT NULL DEFAULT 0"),
    ("determinations", "reused_from", "INTEGER"),
)


def migrate_schema(conn: sqlite3.Connection) -> None:
    for table, column, declaration in _COLUMN_MIGRATIONS:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in columns:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        except sqlite3.OperationalError as exc:
            # A gunicorn worker may have added the column between our check
            # and the ALTER; that is exactly the state we want.
            if "duplicate column" not in str(exc).lower():
                raise


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        path = app.config["DATABASE"]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    """Create/upgrade tables once per process, before the first request."""
    path = app.config["DATABASE"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    own = conn is None
    if own:
        conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    try:
        if own:
            conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        migrate_schema(conn)
        backfill_detail_hashes(conn)
    finally:
        if own:
            conn.close()


def init_db() -> None:
    ensure_schema()


@app.cli.command("init-db")
def init_db_command():
    init_db()
    print("initialised", app.config["DATABASE"])


class InputError(Exception):
    """A user-facing validation problem."""


def rollback_quiet(conn: sqlite3.Connection) -> None:
    """Roll back if a transaction is still open; never mask the real error."""
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        app.logger.exception("rollback failed")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_MAX_STAT = 10_000_000  # 10 m in micrometres, far beyond any foil


def parse_int(value, label: str, minimum: int = 0, maximum: int = _MAX_STAT) -> int:
    if value is None:
        raise InputError(f"请填写{label}")
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        raise InputError(f"请填写{label}")
    if not re.fullmatch(r"[+-]?\d+", text):
        raise InputError(f"{label}必须是整数（微米）")
    number = int(text)
    if number < minimum or number > maximum:
        raise InputError(f"{label}超出合理范围")
    return number


def parse_point_count(value) -> int:
    return parse_int(value, "测点数", minimum=1, maximum=5_000_000)


def normalise_coil_no(value) -> str:
    text = (value or "").strip()
    if not text:
        raise InputError("请填写卷号")
    if len(text) > 64:
        raise InputError("卷号过长")
    if any(ch in text for ch in "\r\n\t"):
        raise InputError("卷号不能包含制表符或换行")
    return text


def normalise_batch_no(value) -> str:
    text = (value or "").strip()
    if not text:
        raise InputError("请填写批号")
    if len(text) > 64:
        raise InputError("批号过长")
    if any(ch in text for ch in "\r\n\t"):
        raise InputError("批号不能包含制表符或换行")
    return text


def summary_problem(n: int, mn: int, p: int, mx: int) -> str | None:
    """Human-readable reason a summary cannot describe any coil, or None."""
    if n <= 0:
        return "测点数必须大于零"
    if not (mn <= p <= mx):
        return "必须满足 最薄值 ≤ P95 ≤ 最厚值"
    k = qc.p95_index(n)
    if k == n and p != mx:
        return (
            f"测点数 {n} 时 P95 是第 {k} 个读数，即最厚值，"
            f"因此 P95 必须等于最厚值（{mx}）"
        )
    if k == 1 and not (mn == p == mx):
        return "测点数为 1 时 P95 就是唯一读数，三项统计必须相同"
    if not qc.summary_feasible(n, mn, p, mx):
        return "测点数与三项统计无法同时成立"
    return None


def _split_fields(line: str) -> list[str]:
    if "," in line:
        parts = line.split(",")
    elif ";" in line:
        parts = line.split(";")
    elif "\t" in line:
        parts = line.split("\t")
    else:
        parts = line.split()
    return [p.strip().strip('"').strip() for p in parts if p.strip() != ""]


def parse_readings_csv(raw: bytes) -> tuple[list[int], tuple[int, int, int] | None]:
    """Parse a complete single-coil readings CSV.

    Each line holds the thickness in micrometres, optionally followed by the
    three summary statistics (``reading,min,p95,max``).  A header line and
    blank lines are tolerated.  Returns the readings and, when present, the
    repeated per-line statistics triple.
    """
    if not raw:
        raise InputError("CSV 文件为空")
    if len(raw) > 8 * 1024 * 1024:
        raise InputError("CSV 文件过大")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError:
            raise InputError("CSV 需为 UTF-8 或 GBK 编码") from None

    readings: list[int] = []
    triple: tuple[int, int, int] | None = None
    seen_content = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        fields = _split_fields(stripped)
        if not fields:
            continue
        if not re.fullmatch(r"[+-]?\d+", fields[0]):
            if not seen_content:
                seen_content = True
                continue  # header
            raise InputError(f"第 {lineno} 行不是有效的整数读数")
        seen_content = True
        try:
            values = [int(f) for f in fields]
        except ValueError:
            raise InputError(f"第 {lineno} 行含非整数内容") from None
        if len(values) == 1:
            reading, stats = values[0], None
        elif len(values) == 4:
            reading, stats = values[0], tuple(values[1:4])
        else:
            raise InputError(
                f"第 {lineno} 行应为「读数」或「读数,最薄值,P95,最厚值」"
            )
        if reading < 0 or reading > _MAX_STAT:
            raise InputError(f"第 {lineno} 行读数超出合理范围")
        if stats is not None:
            for val in stats:
                if val < 0 or val > _MAX_STAT:
                    raise InputError(f"第 {lineno} 行统计值超出合理范围")
            if triple is None:
                triple = stats
            elif triple != stats:
                raise InputError(f"第 {lineno} 行的统计摘要与前面行不一致")
        readings.append(reading)

    if not readings:
        raise InputError("CSV 中没有读数")
    return readings, triple


# ---------------------------------------------------------------------------
# Data access / snapshots
# ---------------------------------------------------------------------------

def fetch_batch(conn, batch_id: int):
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def fetch_coils(conn, batch_id: int):
    return conn.execute(
        "SELECT * FROM coils WHERE batch_id = ? ORDER BY coil_no", (batch_id,)
    ).fetchall()


def active_summary(conn, coil_id: int):
    return conn.execute(
        "SELECT * FROM summaries WHERE coil_id = ? AND active = 1 "
        "ORDER BY id DESC LIMIT 1",
        (coil_id,),
    ).fetchone()


def active_detail(conn, coil_id: int):
    return conn.execute(
        "SELECT * FROM details WHERE coil_id = ? AND active = 1 "
        "ORDER BY id DESC LIMIT 1",
        (coil_id,),
    ).fetchone()


def active_detail_meta(conn, coil_id: int):
    """The active detail without its ``readings`` column.

    Basis resolution must not load per-point readings at all; only a fresh
    computation (``load_snapshots``) does that.
    """
    return conn.execute(
        "SELECT id, coil_id, point_count, min_um, p95_um, max_um, content_hash "
        "FROM details WHERE coil_id = ? AND active = 1 "
        "ORDER BY id DESC LIMIT 1",
        (coil_id,),
    ).fetchone()


def ensure_coil(conn, batch_id: int, coil_no: str):
    row = conn.execute(
        "SELECT * FROM coils WHERE batch_id = ? AND coil_no = ?", (batch_id, coil_no)
    ).fetchone()
    if row is not None:
        return row
    conn.execute(
        "INSERT INTO coils(batch_id, coil_no, created_at) VALUES (?, ?, ?)",
        (batch_id, coil_no, utcnow()),
    )
    return conn.execute(
        "SELECT * FROM coils WHERE batch_id = ? AND coil_no = ?", (batch_id, coil_no)
    ).fetchone()


def retire_detail(conn, detail_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE details SET active = 0, retired_reason = ?, retired_at = ? "
        "WHERE id = ? AND active = 1",
        (reason, utcnow(), detail_id),
    )


def retire_summary(conn, summary_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE summaries SET active = 0, retired_reason = ?, retired_at = ? "
        "WHERE id = ? AND active = 1",
        (reason, utcnow(), summary_id),
    )


def detail_summary_mismatch(detail, summary) -> str | None:
    """Why an active detail no longer agrees with the submitted summary."""
    if summary is None:
        return None
    derived = (detail["point_count"], detail["min_um"], detail["p95_um"], detail["max_um"])
    declared = (summary["point_count"], summary["min_um"], summary["p95_um"], summary["max_um"])
    labels = ("测点数", "最薄值", "P95", "最厚值")
    diffs = [
        f"{label} 明细 {d} / 摘要 {s}"
        for label, d, s in zip(labels, derived, declared)
        if d != s
    ]
    return "；".join(diffs) if diffs else None


def detail_problem(detail) -> tuple[str | None, str | None]:
    """Validate a stored detail's readings; return ``(hash, problem)``.

    The hash covers the *sorted* readings, i.e. the count of each thickness
    value.  It is only produced when the stored statistics really agree with
    the stored readings, so a valid hash is a trustworthy basis fingerprint
    that can be read without loading the readings again later.
    """
    try:
        stored = json.loads(detail["readings"])
        readings = tuple(sorted(int(v) for v in stored))
    except (TypeError, ValueError):
        return None, "读数档案无法解析"
    if not readings or qc.readings_stats(readings) != (
        detail["point_count"],
        detail["min_um"],
        detail["p95_um"],
        detail["max_um"],
    ):
        return None, "档案不完整或统计与读数不符"
    return readings_content_hash(readings), None


def resolve_entries(conn, batch_id: int) -> tuple[list[dict], list[str]]:
    """Resolve every coil to one submitted archive, without reading readings.

    Each entry carries the accounting kind and the numbers that make up the
    basis.  For detail entries ``content_hash`` is the order-insensitive
    readings fingerprint from the detail row itself; readings are only loaded
    later, and only when a fresh computation is actually needed.
    """
    coils = fetch_coils(conn, batch_id)
    if not coils:
        raise InputError("该批次还没有卷")

    entries: list[dict] = []
    notes: list[str] = []
    missing: list[str] = []
    blocked: list[str] = []

    for coil in coils:
        summary = active_summary(conn, coil["id"])
        detail = active_detail_meta(conn, coil["id"])
        if summary is None and detail is None:
            missing.append(coil["coil_no"])
            continue
        problem = None
        if summary is not None:
            problem = summary_problem(
                summary["point_count"],
                summary["min_um"],
                summary["p95_um"],
                summary["max_um"],
            )
        if problem:
            blocked.append(f"卷 {coil['coil_no']} 的摘要无法成立：{problem}")
            continue

        entry = None
        if detail is not None and summary is not None:
            mismatch = detail_summary_mismatch(detail, summary)
            if mismatch is not None:
                notes.append(
                    f"卷 {coil['coil_no']} 的明细与当前摘要不符（{mismatch}），"
                    "本次按摘要核算"
                )
            else:
                digest = detail["content_hash"]
                if digest is None:
                    # No fingerprint yet (corrupt row that backfill skipped, or
                    # a race during upgrade): validate it once and record it.
                    row = conn.execute(
                        "SELECT * FROM details WHERE id = ?", (detail["id"],)
                    ).fetchone()
                    digest, detail_err = detail_problem(row or detail)
                    if digest is not None:
                        conn.execute(
                            "UPDATE details SET content_hash = ? WHERE id = ?",
                            (digest, detail["id"]),
                        )
                    else:
                        notes.append(
                            f"卷 {coil['coil_no']} 的明细{detail_err}，本次退回摘要"
                        )
                if digest is not None:
                    entry = {
                        "coil_id": coil["id"],
                        "coil_no": coil["coil_no"],
                        "kind": "detail",
                        "n": detail["point_count"],
                        "min_um": detail["min_um"],
                        "p95_um": detail["p95_um"],
                        "max_um": detail["max_um"],
                        "summary_id": summary["id"],
                        "detail_id": detail["id"],
                        "content_hash": digest,
                    }
        elif detail is not None:
            notes.append(
                f"卷 {coil['coil_no']} 只有明细、没有已提交摘要，"
                "未满足「与摘要相符」的采用条件，本次不采用明细"
            )

        if entry is None and summary is not None:
            entry = {
                "coil_id": coil["id"],
                "coil_no": coil["coil_no"],
                "kind": "summary",
                "n": summary["point_count"],
                "min_um": summary["min_um"],
                "p95_um": summary["p95_um"],
                "max_um": summary["max_um"],
                "summary_id": summary["id"],
                "detail_id": None,
                "content_hash": None,
            }
        if entry is not None:
            entries.append(entry)
        else:
            # A detail alone is not an accounting basis: it must be checked
            # against a submitted summary first.
            missing.append(coil["coil_no"])

    if blocked:
        raise InputError("；".join(blocked))
    if missing:
        raise InputError(
            "以下卷缺少可核算的依据：" + "、".join(missing)
            + "（明细只有与该卷已提交摘要相符时才可采用；"
            "仅有明细或无任何档案都不能核算）"
        )
    return entries, notes


def entry_fingerprint(entries: list[dict]) -> str:
    payload = [
        {
            "coil_id": e["coil_id"],
            "kind": e["kind"],
            "n": e["n"],
            "min_um": e["min_um"],
            "p95_um": e["p95_um"],
            "max_um": e["max_um"],
            "hash": e["content_hash"],
        }
        for e in entries
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_snapshots(conn, entries: list[dict]) -> list[qc.CoilSnapshot]:
    """Materialise the counted readings for a fresh computation only."""
    snapshots: list[qc.CoilSnapshot] = []
    for entry in entries:
        readings = None
        if entry["kind"] == "detail":
            row = conn.execute(
                "SELECT readings FROM details WHERE id = ?", (entry["detail_id"],)
            ).fetchone()
            if row is None:
                raise InputError(f"卷 {entry['coil_no']} 的明细档案已被删除")
            try:
                readings = tuple(sorted(int(v) for v in json.loads(row["readings"])))
            except (TypeError, ValueError):
                raise InputError(
                    f"卷 {entry['coil_no']} 的明细读数档案无法解析"
                ) from None
            if not readings or qc.readings_stats(readings) != (
                entry["n"], entry["min_um"], entry["p95_um"], entry["max_um"]
            ) or readings_content_hash(readings) != entry["content_hash"]:
                raise InputError(
                    f"卷 {entry['coil_no']} 的明细档案与登记时的统计不符，请重新上传"
                )
        snapshots.append(
            qc.CoilSnapshot(
                coil_id=entry["coil_id"],
                coil_no=entry["coil_no"],
                n=entry["n"],
                mn=entry["min_um"],
                p=entry["p95_um"],
                mx=entry["max_um"],
                summary_id=entry["summary_id"],
                detail_id=entry["detail_id"],
                readings=readings,
            )
        )
    return snapshots


# ---------------------------------------------------------------------------
# Basis fingerprint and interval reuse
# ---------------------------------------------------------------------------
#
# A determination's "basis" is exactly what its bounds were computed from:
# each coil's accounting kind (summary / detail), the four summary numbers,
# and - for detail coils - the readings that were actually counted.  The
# fingerprint captures all of it without keeping another copy of the readings:
#
#   * summary coil: ("summary", n, min, p95, max)
#   * detail coil:  ("detail", n, min, p95, max, content_hash)
#
# ``content_hash`` is computed over the *sorted* readings, so a resubmitted
# detail that differs only in row order reuses, while any change in the
# distribution of counted readings does not.  The four numbers alone are not
# enough: two different reading lists can share them, and the exact counts
# ``<= t`` / ``>= t`` used by ``batch_bounds`` depend on the list itself.

def readings_content_hash(readings) -> str:
    """Order-insensitive fingerprint of a complete readings list."""
    values = sorted(int(v) for v in readings)
    encoded = ",".join(str(v) for v in values).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def backfill_detail_hashes(conn) -> None:
    """Give old detail rows a trustworthy fingerprint, when they deserve one.

    The hash is only recorded when the stored readings really reproduce the
    stored statistics; a row that fails that check keeps NULL so the next
    determination validates it (and falls back to its summary) instead of
    trusting a stale hash.
    """
    rows = conn.execute(
        "SELECT id, readings, point_count, min_um, p95_um, max_um "
        "FROM details WHERE content_hash IS NULL"
    ).fetchall()
    for row in rows:
        try:
            values = tuple(sorted(int(v) for v in json.loads(row["readings"])))
        except (TypeError, ValueError):
            continue  # a corrupt archive must not stop the upgrade
        if not values or qc.readings_stats(values) != (
            row["point_count"], row["min_um"], row["p95_um"], row["max_um"]
        ):
            continue
        conn.execute(
            "UPDATE details SET content_hash = ? WHERE id = ?",
            (readings_content_hash(values), row["id"]),
        )


def find_reusable_determination(
    conn: sqlite3.Connection, batch_id: int, fingerprint: str
) -> sqlite3.Row | None:
    """The last saved determination, if it used the same basis.

    Only the stored interval is reused; the caller still applies *this*
    request's limit and saves a new row.  A row written before fingerprints
    existed carries NULL and therefore never matches, so the first
    determination after an upgrade computes fully.
    """
    last = conn.execute(
        "SELECT * FROM determinations WHERE batch_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (batch_id,),
    ).fetchone()
    if last is None or last["basis_fingerprint"] is None:
        return None
    if last["basis_fingerprint"] != fingerprint:
        return None
    return last


def fetch_coil_views(conn, batch_id: int):
    """Rows used by the batch page: coil plus its active and retired archives."""
    coils = fetch_coils(conn, batch_id)
    views = []
    for coil in coils:
        summary = active_summary(conn, coil["id"])
        detail = active_detail(conn, coil["id"])
        mismatch = detail_summary_mismatch(detail, summary) if detail else None
        retired_details = conn.execute(
            "SELECT * FROM details WHERE coil_id = ? AND active = 0 "
            "ORDER BY id DESC LIMIT 5",
            (coil["id"],),
        ).fetchall()
        retired_views = [
            {
                "row": rd,
                "matches_summary": summary is not None
                and detail_summary_mismatch(rd, summary) is None,
            }
            for rd in retired_details
        ]
        views.append(
            {
                "coil": coil,
                "summary": summary,
                "detail": detail,
                "mismatch": mismatch,
                "retired_details": retired_views,
            }
        )
    return views


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    conn = get_db()
    batches = conn.execute(
        """
        SELECT b.*,
               (SELECT COUNT(*) FROM coils c WHERE c.batch_id = b.id) AS coil_count,
               (SELECT COUNT(*) FROM determinations d WHERE d.batch_id = b.id) AS check_count
        FROM batches b
        ORDER BY b.id DESC
        """
    ).fetchall()
    return render_template("index.html", batches=batches)


@app.post("/batches")
def create_batch():
    batch_no = normalise_batch_no(request.form.get("batch_no"))
    note = (request.form.get("note") or "").strip()[:200]
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id FROM batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if existing:
            raise InputError(f"批号 {batch_no} 已存在")
        cur = conn.execute(
            "INSERT INTO batches(batch_no, note, created_at) VALUES (?, ?, ?)",
            (batch_no, note, utcnow()),
        )
        batch_id = cur.lastrowid
        conn.execute("COMMIT")
    except Exception:
        rollback_quiet(conn)
        raise
    flash(f"批次 {batch_no} 已建立", "ok")
    return redirect(url_for("batch_detail", batch_id=batch_id))


@app.route("/batch/<int:batch_id>")
def batch_detail(batch_id: int):
    conn = get_db()
    batch = fetch_batch(conn, batch_id)
    views = fetch_coil_views(conn, batch_id)
    determinations = conn.execute(
        "SELECT * FROM determinations WHERE batch_id = ? ORDER BY id DESC LIMIT 20",
        (batch_id,),
    ).fetchall()
    history = []
    for d in determinations:
        try:
            snap = json.loads(d["snapshot_json"])
        except (TypeError, ValueError):
            snap = {}
        history.append(
            {
                "row": d,
                "label": qc.VERDICT_LABELS.get(d["verdict"], d["verdict"]),
                "notes": snap.get("notes", []),
                "reused": bool(d["reused"]) or bool(snap.get("reused")),
                "reused_from": (
                    d["reused_from"] if d["reused_from"] is not None
                    else snap.get("reused_from")
                ),
            }
        )
    return render_template(
        "batch.html", batch=batch, views=views, history=history
    )


@app.post("/batch/<int:batch_id>/coils")
def add_coil(batch_id: int):
    coil_no = normalise_coil_no(request.form.get("coil_no"))
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        fetch_batch(conn, batch_id)
        ensure_coil(conn, batch_id, coil_no)
        conn.execute("COMMIT")
    except Exception:
        rollback_quiet(conn)
        raise
    flash(f"卷 {coil_no} 已登记", "ok")
    return redirect(url_for("batch_detail", batch_id=batch_id))


@app.post("/batch/<int:batch_id>/summary")
def register_summary(batch_id: int):
    coil_no = normalise_coil_no(request.form.get("coil_no"))
    n = parse_point_count(request.form.get("point_count"))
    mn = parse_int(request.form.get("min_um"), "最薄值")
    p = parse_int(request.form.get("p95_um"), "P95")
    mx = parse_int(request.form.get("max_um"), "最厚值")
    problem = summary_problem(n, mn, p, mx)
    if problem:
        raise InputError(f"摘要无法登记：{problem}")
    if not qc.summary_feasible(n, mn, p, mx):
        raise InputError("测点数与三项统计无法同时成立，摘要无法登记")

    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        fetch_batch(conn, batch_id)
        coil = ensure_coil(conn, batch_id, coil_no)
        current = active_summary(conn, coil["id"])
        replaced = current is not None
        if current is not None:
            retire_summary(
                conn,
                current["id"],
                f"换交摘要：{current['point_count']} 点 / "
                f"{current['min_um']}-{current['p95_um']}-{current['max_um']} → "
                f"{n} 点 / {mn}-{p}-{mx}",
            )

        detail = active_detail(conn, coil["id"])
        kept = False
        mismatch = None
        if detail is not None:
            mismatch = detail_summary_mismatch(
                detail,
                {
                    "point_count": n,
                    "min_um": mn,
                    "p95_um": p,
                    "max_um": mx,
                },
            )
            if mismatch:
                retire_detail(
                    conn,
                    detail["id"],
                    f"换交摘要后明细与摘要不符：{mismatch}",
                )
            else:
                kept = True

        conn.execute(
            """
            INSERT INTO summaries(coil_id, point_count, min_um, p95_um, max_um,
                                  active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (coil["id"], n, mn, p, mx, utcnow()),
        )
        conn.execute("COMMIT")
    except Exception:
        rollback_quiet(conn)
        raise

    if mismatch:
        flash(
            f"卷 {coil_no} 摘要已换交；原明细与摘要不符（{mismatch}），已停用，改按新摘要核算",
            "warn",
        )
    elif kept:
        flash(f"卷 {coil_no} 摘要已换交；现有明细与摘要相符，明细继续有效", "ok")
    elif replaced:
        flash(f"卷 {coil_no} 摘要已换交，旧摘要已作废", "ok")
    else:
        flash(f"卷 {coil_no} 摘要已登记", "ok")
    return redirect(url_for("batch_detail", batch_id=batch_id))


@app.post("/batch/<int:batch_id>/detail")
def upload_detail(batch_id: int):
    coil_no = normalise_coil_no(request.form.get("coil_no"))
    upload = request.files.get("readings")
    if upload is None or not upload.filename:
        raise InputError("请选择读数 CSV 文件")
    raw = upload.read()
    readings, triple = parse_readings_csv(raw)

    derived = qc.readings_stats(readings)
    if triple is not None and tuple(derived[1:]) != tuple(triple):
        raise InputError(
            "CSV 中携带的统计与读数不一致："
            f"读数实算 最薄 {derived[1]} / P95 {derived[2]} / 最厚 {derived[3]}，"
            f"文件声明 最薄 {triple[0]} / P95 {triple[1]} / 最厚 {triple[2]}"
        )

    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        fetch_batch(conn, batch_id)
        coil = ensure_coil(conn, batch_id, coil_no)
        if active_detail(conn, coil["id"]) is not None:
            raise InputError(
                f"卷 {coil_no} 已有有效明细；如需重传，请先停用原明细"
            )
        summary = active_summary(conn, coil["id"])
        if summary is None:
            raise InputError(
                f"卷 {coil_no} 还没有已提交摘要；明细须与摘要的点数和统计相符后才能核算，"
                "请先登记摘要"
            )
        mismatch = detail_summary_mismatch(
            {
                "point_count": derived[0],
                "min_um": derived[1],
                "p95_um": derived[2],
                "max_um": derived[3],
            },
            summary,
        )
        # "点数和三项统计都与摘要相符才按明细核算": an unmatched detail is
        # archived but immediately inactive, so accounting stays on the summary.
        active = 1 if mismatch is None else 0
        reason = None if mismatch is None else f"与摘要不符：{mismatch}"
        conn.execute(
            """
            INSERT INTO details(coil_id, readings, point_count, min_um, p95_um,
                                max_um, content_hash, active, retired_reason,
                                retired_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                coil["id"],
                json.dumps(readings),
                derived[0],
                derived[1],
                derived[2],
                derived[3],
                readings_content_hash(readings),
                active,
                reason,
                None if active else utcnow(),
                utcnow(),
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        rollback_quiet(conn)
        raise

    if mismatch:
        flash(
            f"卷 {coil_no} 明细已存档但不与摘要相符（{mismatch}）："
            f"{derived[0]} 个读数，最薄 {derived[1]} / P95 {derived[2]} / "
            f"最厚 {derived[3]} μm。明细已停用，核算退回按摘要进行",
            "warn",
        )
    else:
        flash(
            f"卷 {coil_no} 明细已接收并启用：{derived[0]} 个读数，"
            f"最薄 {derived[1]} μm、P95 {derived[2]} μm、最厚 {derived[3]} μm",
            "ok",
        )
    return redirect(url_for("batch_detail", batch_id=batch_id))


@app.post("/batch/<int:batch_id>/detail/<int:detail_id>/retire")
def retire_detail_route(batch_id: int, detail_id: int):
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        fetch_batch(conn, batch_id)
        row = conn.execute(
            "SELECT d.id FROM details d JOIN coils c ON c.id = d.coil_id "
            "WHERE d.id = ? AND c.batch_id = ?",
            (detail_id, batch_id),
        ).fetchone()
        if row is None:
            raise InputError("明细不存在")
        retire_detail(conn, detail_id, "人工停用")
        conn.execute("COMMIT")
    except Exception:
        rollback_quiet(conn)
        raise
    flash("明细已停用", "ok")
    return redirect(url_for("batch_detail", batch_id=batch_id))


@app.post("/batch/<int:batch_id>/detail/<int:detail_id>/activate")
def activate_detail_route(batch_id: int, detail_id: int):
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        fetch_batch(conn, batch_id)
        row = conn.execute(
            "SELECT d.* FROM details d JOIN coils c ON c.id = d.coil_id "
            "WHERE d.id = ? AND c.batch_id = ?",
            (detail_id, batch_id),
        ).fetchone()
        if row is None:
            raise InputError("明细不存在")
        if active_detail(conn, row["coil_id"]) is not None:
            raise InputError("该卷已有有效明细")
        summary = active_summary(conn, row["coil_id"])
        if summary is None:
            raise InputError("该卷没有已提交摘要；明细须与摘要相符才可用于核算，不能单独启用")
        mismatch = detail_summary_mismatch(row, summary)
        if mismatch:
            raise InputError(
                f"该明细与当前摘要不符（{mismatch}），不能启用；可先作废摘要"
            )
        readings = tuple(sorted(int(v) for v in json.loads(row["readings"])))
        if (
            not readings
            or qc.readings_stats(readings)
            != (row["point_count"], row["min_um"], row["p95_um"], row["max_um"])
        ):
            raise InputError("该明细档案不完整或统计与读数不符，不能启用")
        conn.execute(
            "UPDATE details SET active = 1, retired_reason = NULL, "
            "retired_at = NULL WHERE id = ?",
            (detail_id,),
        )
        conn.execute("COMMIT")
    except Exception:
        rollback_quiet(conn)
        raise
    flash("明细已启用", "ok")
    return redirect(url_for("batch_detail", batch_id=batch_id))


@app.post("/batch/<int:batch_id>/summary/<int:summary_id>/retire")
def retire_summary_route(batch_id: int, summary_id: int):
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        fetch_batch(conn, batch_id)
        row = conn.execute(
            "SELECT s.id, s.coil_id FROM summaries s JOIN coils c ON c.id = s.coil_id "
            "WHERE s.id = ? AND c.batch_id = ?",
            (summary_id, batch_id),
        ).fetchone()
        if row is None:
            raise InputError("摘要不存在")
        retire_summary(conn, summary_id, "人工作废")
        conn.execute("COMMIT")
    except Exception:
        rollback_quiet(conn)
        raise
    flash("摘要已作废", "ok")
    return redirect(url_for("batch_detail", batch_id=batch_id))


@app.post("/batch/<int:batch_id>/determine")
def determine(batch_id: int):
    limit = parse_int(request.form.get("limit_um"), "厚度上限")
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        fetch_batch(conn, batch_id)
        try:
            # Resolve the submitted archives and their fingerprint first; this
            # reads only archive metadata, never the per-point readings.
            entries, notes = resolve_entries(conn, batch_id)
            fingerprint = entry_fingerprint(entries)
            previous = find_reusable_determination(conn, batch_id, fingerprint)
            if previous is not None:
                # Same submitted basis as an earlier determination: reuse the
                # whole-batch P95 interval, never the old verdict - this
                # request's limit decides that, and it is saved as a new row.
                total_points = previous["total_points"]
                p95_index = previous["p95_index"]
                min_possible = previous["min_possible"]
                max_possible = previous["max_possible"]
                reused_from = previous["id"]
            else:
                snapshots = load_snapshots(conn, entries)
                bounds = qc.batch_bounds(snapshots)
                total_points = bounds.total_points
                p95_index = bounds.p95_index
                min_possible = bounds.min_possible
                max_possible = bounds.max_possible
                reused_from = None
        except InputError:
            raise
        except ValueError as exc:
            raise InputError(str(exc)) from exc

        verdict = qc.verdict_for(min_possible, max_possible, limit)
        method = (
            "逐卷按已提交摘要的闭式计数求整批 P95 的可行区间，"
            "明细卷按完整读数计数；未把摘要展开为逐点读数"
        )
        if reused_from is not None:
            method = (
                f"复用判定 #{reused_from} 已保存的整批 P95 区间：本次各卷依据类型、"
                "摘要点数与三项统计、明细读数分布均与上次成功判定相同，"
                "未重新逐点核算；只按本次上限重新判定"
            )
        snapshot = {
            "coils": [
                {
                    "coil_id": e["coil_id"],
                    "coil_no": e["coil_no"],
                    "kind": e["kind"],
                    "n": e["n"],
                    "min_um": e["min_um"],
                    "p95_um": e["p95_um"],
                    "max_um": e["max_um"],
                    "summary_id": e["summary_id"],
                    "detail_id": e["detail_id"],
                }
                for e in entries
            ],
            "notes": notes,
            "limit_um": limit,
            "computed_at": utcnow(),
            "method": method,
            "reused": reused_from is not None,
            "reused_from": reused_from,
        }
        cur = conn.execute(
            """
            INSERT INTO determinations(batch_id, limit_um, total_points,
                p95_index, min_possible, max_possible, verdict, snapshot_json,
                basis_fingerprint, reused, reused_from, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                limit,
                total_points,
                p95_index,
                min_possible,
                max_possible,
                verdict,
                json.dumps(snapshot, ensure_ascii=False),
                fingerprint,
                1 if reused_from is not None else 0,
                reused_from,
                utcnow(),
            ),
        )
        determination_id = cur.lastrowid
        conn.execute("COMMIT")
    except Exception:
        rollback_quiet(conn)
        raise
    return redirect(
        url_for("determination_detail", batch_id=batch_id, det_id=determination_id)
    )


@app.route("/batch/<int:batch_id>/determination/<int:det_id>")
def determination_detail(batch_id: int, det_id: int):
    conn = get_db()
    batch = fetch_batch(conn, batch_id)
    row = conn.execute(
        "SELECT * FROM determinations WHERE id = ? AND batch_id = ?",
        (det_id, batch_id),
    ).fetchone()
    if row is None:
        abort(404)
    snap = json.loads(row["snapshot_json"])
    detail = {
        "row": row,
        "label": qc.VERDICT_LABELS.get(row["verdict"], row["verdict"]),
        "coils": snap.get("coils", []),
        "notes": snap.get("notes", []),
        "method": snap.get("method", ""),
        "computed_at": snap.get("computed_at", row["created_at"]),
        # Column first so rows written before the snapshot gained the field
        # still report truthfully.
        "reused": bool(row["reused"]) or bool(snap.get("reused")),
        "reused_from": (
            row["reused_from"] if row["reused_from"] is not None
            else snap.get("reused_from")
        ),
    }
    return render_template(
        "result.html", batch=batch, det=detail, limit=row["limit_um"]
    )


@app.errorhandler(InputError)
def handle_input_error(exc):
    flash(str(exc), "error")
    target = request.referrer or url_for("index")
    return redirect(target)


@app.errorhandler(413)
def too_large(exc):
    flash("上传文件超过大小限制", "error")
    target = request.referrer or url_for("index")
    return redirect(target)


@app.errorhandler(sqlite3.Error)
def database_error(exc):
    app.logger.exception("database error")
    flash("写入失败：数据库未保存本次改动，原档案保持不变", "error")
    target = request.referrer or url_for("index")
    return redirect(target)


@app.template_filter("um")
def format_um(value):
    return f"{int(value):,} μm"


if __name__ == "__main__":
    ensure_schema()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
else:
    ensure_schema()
