"""Full-snapshot-then-incremental supply API ("供数 API").

Design
------
1. An append-only ``supply_events`` log records the downstream effect of every
   business mutation.  Events are inserted in the *same* transaction as the
   mutation, so a correction either commits together with its withdraw event or
   rolls back entirely ("变更与业务提交共同生效，失败均不变").

2. A sync session, at creation time, materialises the full snapshot inside one
   transaction: for every instance it picks the last committed, non-late,
   non-superseded report whose two-side revisions equal the current instance /
   package revisions, and only when that report is ``feasible``.  The event-log
   high-water mark (``tail_after_seq``) is captured in the same transaction, so
   no correction straddling the snapshot/incremental boundary can be lost.

3. Paging first walks the immutable snapshot (its content never changes, even
   while corrections and checks keep being committed), then switches to the
   event tail.  Cursors are opaque, HMAC-signed, bind the token to one session,
   and survive process restarts because every state lives in SQLite.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets

from . import db
from .errors import BadPayload, NotFound

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


# ---------------------------------------------------------------- event log
def append_event(conn, *, instance_id: str, package_id: str, op: str, reason: str,
                 report_id: str | None = None, instance_revision: int | None = None,
                 package_revision: int | None = None, facts: list | None = None,
                 now: str) -> None:
    """Append a supply event inside the caller's open transaction."""
    conn.execute(
        "INSERT INTO supply_events(instance_id, package_id, op, reason, report_id, "
        "instance_revision, package_revision, facts_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (instance_id, package_id, op, reason, report_id, instance_revision,
         package_revision, json.dumps(facts) if facts is not None else None, now),
    )


def facts_from_witness(witness: list | None) -> list[dict]:
    """Extract (concept, reported amount, decimals) rows from a feasible report."""
    if not witness:
        return []
    return [
        {
            "concept": w["concept"],
            "reported_value": w["reported_value"],
            "decimals": w["decimals"],
        }
        for w in witness
    ]


# ---------------------------------------------------------------- eligibility
def _eligible_current_report(conn, inst_row: dict) -> dict | None:
    """The only report allowed to feed the warehouse for one instance.

    Rules: last committed non-late report for the current instance AND package
    revisions; superseded/late reports never qualify; only ``feasible`` reports
    supply data (pending / infeasible do not).
    """
    pkg_row = conn.execute(
        "SELECT latest_rev FROM packages WHERE id=?", (inst_row["package_id"],)
    ).fetchone()
    if pkg_row is None:
        return None
    row = conn.execute(
        "SELECT * FROM reports WHERE instance_id=? AND superseded=0 AND late=0 "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (inst_row["id"],),
    ).fetchone()
    if row is None:
        return None
    if row["status"] != "feasible":
        return None
    if row["instance_revision"] != inst_row["latest_rev"]:
        return None
    if row["package_revision"] != pkg_row["latest_rev"]:
        return None
    return dict(row)


# ---------------------------------------------------------------- sessions
def create_session(page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    if not isinstance(page_size, int) or page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise BadPayload(f"page_size must be an integer between 1 and {MAX_PAGE_SIZE}")
    conn = db.get_conn()
    sync_id = f"sync_{secrets.token_hex(12)}"
    cursor_key = secrets.token_hex(24)
    now = _now()
    with conn:
        # High-water mark and snapshot are taken in one transaction, so every
        # later event has seq > tail_after_seq; nothing at the boundary is lost.
        hwm_row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM supply_events").fetchone()
        tail_after_seq = hwm_row["m"]
        conn.execute(
            "INSERT INTO sync_sessions(id, tail_after_seq, snapshot_total, page_size, "
            "created_at, cursor_key) VALUES (?,?,0,?,?,?)",
            (sync_id, tail_after_seq, page_size, now, cursor_key),
        )
        instances = conn.execute(
            "SELECT * FROM instances ORDER BY created_at, id"
        ).fetchall()
        total = 0
        for inst in instances:
            report = _eligible_current_report(conn, dict(inst))
            if report is None:
                continue  # pending / infeasible / late / no report yet -> not supplied
            facts = facts_from_witness(
                json.loads(report["witness_json"]) if report["witness_json"] else None
            )
            conn.execute(
                "INSERT INTO sync_snapshot(sync_id, instance_id, package_id, report_id, "
                "instance_revision, package_revision, facts_json) VALUES (?,?,?,?,?,?,?)",
                (sync_id, report["instance_id"], report["package_id"], report["id"],
                 report["instance_revision"], report["package_revision"],
                 json.dumps(facts)),
            )
            total += 1
        conn.execute(
            "UPDATE sync_sessions SET snapshot_total=? WHERE id=?", (total, sync_id)
        )
    return get_session(sync_id)


def _load_session(sync_id: str) -> dict:
    row = db.get_conn().execute(
        "SELECT * FROM sync_sessions WHERE id=?", (sync_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"sync session '{sync_id}' not found")
    return dict(row)


def get_session(sync_id: str) -> dict:
    row = _load_session(sync_id)
    return {
        "sync_id": row["id"],
        "created_at": row["created_at"],
        "page_size": row["page_size"],
        "snapshot_total": row["snapshot_total"],
        "tail_after_seq": row["tail_after_seq"],
    }


def list_sessions() -> list[dict]:
    rows = db.get_conn().execute(
        "SELECT id FROM sync_sessions ORDER BY created_at"
    ).fetchall()
    return [get_session(r["id"]) for r in rows]


# ---------------------------------------------------------------- cursors
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encode_cursor(session: dict, payload: dict) -> str:
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(
        session["cursor_key"].encode("ascii"), body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{body}.{sig}"


def decode_cursor(token: str, session: dict) -> dict:
    try:
        body, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        raise BadPayload("malformed cursor")
    expected = hmac.new(
        session["cursor_key"].encode("ascii"), body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise BadPayload("cursor signature does not match this sync session")
    try:
        payload = json.loads(_unb64url(body))
    except Exception:
        raise BadPayload("malformed cursor")
    if payload.get("sync") != session["id"] or payload.get("v") != 1:
        raise BadPayload("cursor does not belong to this sync session")
    if payload.get("phase") not in ("snap", "tail"):
        raise BadPayload("malformed cursor phase")
    return payload


def _tail_cursor(session: dict, after_seq: int) -> str:
    return encode_cursor(
        session, {"v": 1, "sync": session["id"], "phase": "tail",
                  "after_seq": after_seq}
    )


# ---------------------------------------------------------------- paging
def _snapshot_op(row: dict) -> dict:
    return {
        "op": "upsert",
        "op_id": f"snap-{row['id']}",
        "instance_id": row["instance_id"],
        "package_id": row["package_id"],
        "report_id": row["report_id"],
        "instance_revision": row["instance_revision"],
        "package_revision": row["package_revision"],
        "reason": "snapshot",
        "facts": json.loads(row["facts_json"]),
    }


def _event_op(row: dict) -> dict:
    op = {
        "op": row["op"],
        "op_id": f"evt-{row['seq']}",
        "seq": row["seq"],
        "instance_id": row["instance_id"],
        "package_id": row["package_id"],
        "reason": row["reason"],
        "report_id": row["report_id"],
        "instance_revision": row["instance_revision"],
        "package_revision": row["package_revision"],
        "facts": json.loads(row["facts_json"]) if row["facts_json"] else None,
    }
    return op


def fetch_page(sync_id: str, cursor: str | None) -> dict:
    conn = db.get_conn()
    session = _load_session(sync_id)
    page_size = session["page_size"]

    if cursor is None or cursor == "":
        payload = {"phase": "snap", "after_id": 0}
    else:
        payload = decode_cursor(cursor, session)

    operations: list[dict] = []

    if payload["phase"] == "snap":
        after_id = int(payload["after_id"])
        rows = conn.execute(
            "SELECT * FROM sync_snapshot WHERE sync_id=? AND id>? "
            "ORDER BY id LIMIT ?",
            (sync_id, after_id, page_size),
        ).fetchall()
        operations = [_snapshot_op(dict(r)) for r in rows]
        if rows:
            last_id = rows[-1]["id"]
            # Snapshot pages stay strictly snapshot-only, so an old page can
            # never change when corrections are committed later in the log.
            next_cursor = encode_cursor(
                session, {"v": 1, "sync": sync_id, "phase": "snap",
                          "after_id": last_id}
            )
            phase = "snapshot"
            has_more = True
        else:
            # Snapshot exhausted (or empty): read the tail at the boundary
            # watermark within the same request.  Snapshot pages that did
            # contain rows were returned snapshot-only, so they stay immutable.
            rows = conn.execute(
                "SELECT * FROM supply_events WHERE seq>? ORDER BY seq LIMIT ?",
                (session["tail_after_seq"], page_size),
            ).fetchall()
            operations = [_event_op(dict(r)) for r in rows]
            phase = "tail"
            if rows:
                next_cursor = _tail_cursor(session, rows[-1]["seq"])
                has_more = _count_events_after(conn, rows[-1]["seq"]) > 0
            else:
                next_cursor = _tail_cursor(session, session["tail_after_seq"])
                has_more = False
    else:
        phase = "tail"
        after_seq = int(payload["after_seq"])
        rows = conn.execute(
            "SELECT * FROM supply_events WHERE seq>? ORDER BY seq LIMIT ?",
            (after_seq, page_size),
        ).fetchall()
        operations = [_event_op(dict(r)) for r in rows]
        if rows:
            last_seq = rows[-1]["seq"]
            # even when caught up, echo the cursor: it stays valid for polling
            next_cursor = _tail_cursor(session, last_seq)
            has_more = _count_events_after(conn, last_seq) > 0
        else:
            # nothing new since the watermark: same cursor remains reusable
            next_cursor = _tail_cursor(session, after_seq)
            has_more = False

    return {
        "sync_id": sync_id,
        "phase": phase,
        "page_size": page_size,
        "snapshot_total": session["snapshot_total"],
        "has_more": has_more,
        "next_cursor": next_cursor,
        "operations": operations,
    }


def _count_events_after(conn, seq: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM supply_events WHERE seq>?", (seq,)
    ).fetchone()["n"]


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
