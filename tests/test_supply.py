"""Supply API: full snapshot, incremental tail, withdraw/replace, cursors."""
from __future__ import annotations

import uuid

from conftest import check, create_instance, create_package


def _create_sync(client, page_size=None):
    url = "/syncs" if page_size is None else f"/syncs?page_size={page_size}"
    r = client.post(url)
    assert r.status_code == 201, r.text
    return r.json()


def _drain(client, sync_id, start_cursor=None):
    """Walk every page (snapshot + tail until caught up) and collect ops."""
    ops, cursor = [], start_cursor
    for _ in range(100):
        url = f"/syncs/{sync_id}/pages"
        if cursor is not None:
            url += f"?cursor={cursor}"
        r = client.get(url)
        assert r.status_code == 200, r.text
        page = r.json()
        ops.extend(page["operations"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            return ops, page, cursor
    raise AssertionError("paging did not converge")


def _feasible_setup(client, samples, *scenarios):
    pkg = create_package(client, samples)
    ids = {}
    for scenario in scenarios:
        inst = create_instance(client, pkg["package_id"], samples[scenario])
        r = check(client, inst["instance_id"], 1, 1)
        assert r.status_code == 200
        ids[scenario] = (inst["instance_id"], r.json()["report"]["report_id"])
    return pkg, ids


# ------------------------------------------------------------ eligibility
def test_snapshot_only_contains_feasible_current_reports(client, samples):
    pkg, ids = _feasible_setup(
        client, samples, "feasible", "rounding", "infeasible", "pending"
    )
    sync = _create_sync(client)
    assert sync["snapshot_total"] == 2  # only feasible + rounding

    ops, page, _ = _drain(client, sync["sync_id"])
    assert page["phase"] == "tail"
    supplied_instances = {op["instance_id"] for op in ops}
    assert supplied_instances == {ids["feasible"][0], ids["rounding"][0]}
    assert all(op["op"] == "upsert" for op in ops)

    # payload shape: concept, reported amount, decimals + two-side revisions
    feasible_op = next(op for op in ops if op["instance_id"] == ids["feasible"][0])
    assert feasible_op["report_id"] == ids["feasible"][1]
    assert feasible_op["instance_revision"] == 1
    assert feasible_op["package_revision"] == 1
    concepts = {f["concept"]: f for f in feasible_op["facts"]}
    assert "acct:Assets" in concepts
    assert concepts["acct:Assets"]["reported_value"] == 500.0
    assert concepts["acct:Assets"]["decimals"] == 0


def test_instance_without_report_is_absent_from_snapshot(client, samples):
    pkg = create_package(client, samples)
    create_instance(client, pkg["package_id"], samples["feasible"])  # no check
    sync = _create_sync(client)
    assert sync["snapshot_total"] == 0
    ops, page, _ = _drain(client, sync["sync_id"])
    assert ops == [] and page["phase"] == "tail"


def test_late_report_never_supplied(client, samples):
    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], samples["infeasible"])
    check(client, inst["instance_id"], 1, 1)
    client.post(
        f"/instances/{inst['instance_id']}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["feasible"], "application/xml")},
    )
    check(client, inst["instance_id"], 2, 1)  # current feasible report
    # late historical report on the old revision must not influence supply
    check(client, inst["instance_id"], 1, 1, late=True)

    sync = _create_sync(client)
    ops, _, _ = _drain(client, sync["sync_id"])
    assert len(ops) == 1 and ops[0]["op"] == "upsert"
    assert ops[0]["instance_revision"] == 2


# ------------------------------------------------------------ paging
def test_snapshot_pagination_is_stable_and_deterministic(client, samples):
    _feasible_setup(client, samples, "feasible", "rounding")
    sync = _create_sync(client, page_size=1)
    assert sync["snapshot_total"] == 2

    first = client.get(f"/syncs/{sync['sync_id']}/pages").json()
    assert first["phase"] == "snapshot" and len(first["operations"]) == 1
    assert first["has_more"] is True

    # retry with the same cursor returns exactly the same page and cursor
    again = client.get(
        f"/syncs/{sync['sync_id']}/pages?cursor={first['next_cursor']}"
    ).json()
    retried = client.get(
        f"/syncs/{sync['sync_id']}/pages?cursor={first['next_cursor']}"
    ).json()
    assert again == retried
    assert [op["op_id"] for op in again["operations"]] == \
           [op["op_id"] for op in retried["operations"]]

    ops, page, _ = _drain(client, sync["sync_id"])
    assert page["phase"] == "tail"
    assert len(ops) == 2
    assert len({op["op_id"] for op in ops}) == 2  # ids are unique for dedup


def test_old_snapshot_pages_do_not_change_when_corrections_happen(client, samples):
    pkg, ids = _feasible_setup(client, samples, "feasible")
    sync = _create_sync(client, page_size=1)
    first = client.get(f"/syncs/{sync['sync_id']}/pages").json()
    frozen = first["operations"][0]
    assert frozen["instance_revision"] == 1

    # corrections keep being accepted while the client is still paging
    client.post(
        f"/instances/{ids['feasible'][0]}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["rounding"], "application/xml")},
    )
    check(client, ids["feasible"][0], 2, 1)

    # the already-served snapshot page is immutable ...
    same = client.get(f"/syncs/{sync['sync_id']}/pages").json()
    assert same["operations"] == first["operations"]

    # ... while the change shows up in the incremental tail
    ops, _, _ = _drain(client, sync["sync_id"])
    tail = [op for op in ops if op["seq"] is not None]
    reasons = [op["reason"] for op in tail]
    assert "instance_revised" in reasons
    assert "report_feasible" in reasons
    upserts = [op for op in tail if op["op"] == "upsert"]
    assert upserts[-1]["instance_revision"] == 2


# ------------------------------------------------------------ boundary
def test_no_event_lost_at_snapshot_tail_boundary(client, samples):
    pkg, ids = _feasible_setup(client, samples, "infeasible")
    sync = _create_sync(client)  # no eligible report -> empty snapshot
    assert sync["snapshot_total"] == 0

    # correction + feasible re-check happen entirely after session creation
    client.post(
        f"/instances/{ids['infeasible'][0]}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["feasible"], "application/xml")},
    )
    check(client, ids["infeasible"][0], 2, 1)

    ops, page, _ = _drain(client, sync["sync_id"])
    assert [op["op"] for op in ops] == ["withdraw", "upsert"]
    assert ops[-1]["facts"] and ops[-1]["instance_revision"] == 2
    assert page["phase"] == "tail"


def test_snapshot_boundary_includes_everything_before_and_nothing_after(client, samples):
    pkg, ids = _feasible_setup(client, samples, "feasible")
    sync = _create_sync(client)
    hwm = sync["tail_after_seq"]
    # one event was already produced by the initial feasible check
    assert hwm >= 1
    check(client, ids["feasible"][0], 1, 1)  # duplicate check -> new event
    ops, _, _ = _drain(client, sync["sync_id"])
    snapshot_ops = [op for op in ops if op["seq"] is None]
    tail_ops = [op for op in ops if op["seq"] is not None]
    assert len(snapshot_ops) == 1 and all(s["seq"] > hwm for s in tail_ops)


# ------------------------------------------------------------ tail semantics
def test_correction_withdraws_then_recheck_replaces_whole_group(client, samples):
    pkg, ids = _feasible_setup(client, samples, "feasible")
    sync = _create_sync(client)
    _drain(client, sync["sync_id"])

    client.post(
        f"/instances/{ids['feasible'][0]}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["infeasible"], "application/xml")},
    )
    check(client, ids["feasible"][0], 2, 1)  # infeasible now -> withdraw
    client.post(
        f"/instances/{ids['feasible'][0]}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["feasible"], "application/xml")},
    )
    check(client, ids["feasible"][0], 3, 1)  # feasible again -> whole-group replace

    ops, page, cursor = _drain(client, sync["sync_id"])
    assert page["has_more"] is False
    kinds = [(op["op"], op["reason"]) for op in ops]
    assert ("withdraw", "instance_revised") in kinds
    assert ("withdraw", "report_not_suppliable") in kinds
    assert kinds[-1] == ("upsert", "report_feasible")
    final = ops[-1]
    assert final["instance_revision"] == 3
    assert len(final["facts"]) > 1  # whole instance group, not a single fact

    # polling again after catching up returns the same cursor and no new ops
    again = client.get(
        f"/syncs/{sync['sync_id']}/pages?cursor={cursor}"
    ).json()
    assert again["has_more"] is False and again["operations"] == []
    assert again["next_cursor"] == cursor


def test_package_revision_withdraws_all_its_instances(client, samples):
    pkg, ids = _feasible_setup(client, samples, "feasible", "rounding")
    sync = _create_sync(client)
    _drain(client, sync["sync_id"])

    r = client.post(
        f"/packages/{pkg['package_id']}/revisions",
        headers={"Expected-Revision": "1"},
        files={
            "xsd": ("acct.xsd", samples["xsd"], "application/xml"),
            "calculation": ("acct-calc.xml", samples["calc"], "application/xml"),
        },
    )
    assert r.status_code == 200

    ops, _, _ = _drain(client, sync["sync_id"])
    tail = [op for op in ops if op["seq"] is not None]
    assert len(tail) == 2
    assert {op["op"] for op in tail} == {"withdraw"}
    assert {op["instance_id"] for op in tail} == set(
        i for i, _ in ids.values()
    )


def test_pending_and_infeasible_reports_emit_withdraw_not_upsert(client, samples):
    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], samples["feasible"])
    check(client, inst["instance_id"], 1, 1)
    sync = _create_sync(client)
    _drain(client, sync["sync_id"])

    for bad in ("pending", "infeasible"):
        client.post(
            f"/instances/{inst['instance_id']}/revisions",
            headers={"Expected-Package-Revision": "1"},
            files={"instance": ("i.xml", samples[bad], "application/xml")},
        )
        r = check(client, inst["instance_id"],
                  client.get(f"/instances/{inst['instance_id']}").json()
                  ["latest_revision"], 1)
        assert r.json()["report"]["status"] == bad

    ops, _, _ = _drain(client, sync["sync_id"])
    tail = [op for op in ops if op["seq"] is not None]
    assert all(op["op"] == "withdraw" for op in tail)
    assert sum(1 for op in tail if op["reason"] == "report_not_suppliable") == 2


def test_late_check_emits_no_supply_event(client, samples):
    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], samples["infeasible"])
    check(client, inst["instance_id"], 1, 1)
    client.post(
        f"/instances/{inst['instance_id']}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["feasible"], "application/xml")},
    )
    check(client, inst["instance_id"], 2, 1)
    events_before = client.post("/syncs").json()["tail_after_seq"]
    check(client, inst["instance_id"], 1, 1, late=True)  # late: history only
    sync2 = client.post("/syncs").json()
    assert sync2["tail_after_seq"] == events_before  # no event appended


# ------------------------------------------------------------ atomicity
def test_failed_mutation_appends_no_event(client, samples):
    pkg, ids = _feasible_setup(client, samples, "feasible")
    sync = _create_sync(client)
    hwm = sync["tail_after_seq"]

    # stale correction is rejected -> neither revision nor withdraw event
    r = client.post(
        f"/instances/{ids['feasible'][0]}/revisions",
        headers={"Expected-Package-Revision": "9"},
        files={"instance": ("i.xml", samples["feasible"], "application/xml")},
    )
    assert r.status_code == 412
    after = client.post("/syncs").json()
    assert after["tail_after_seq"] == hwm
    # state unchanged too
    assert client.get(
        f"/instances/{ids['feasible'][0]}"
    ).json()["latest_revision"] == 1


# ------------------------------------------------------------ cursor safety
def test_cursor_is_bound_to_its_session_and_tamper_proof(client, samples):
    _feasible_setup(client, samples, "feasible")
    s1 = _create_sync(client)
    s2 = _create_sync(client)
    page = client.get(f"/syncs/{s1['sync_id']}/pages").json()
    cursor = page["next_cursor"]

    # using another session's cursor is rejected
    r = client.get(f"/syncs/{s2['sync_id']}/pages?cursor={cursor}")
    assert r.status_code == 422
    # tampered signature / garbage cursors are rejected
    r = client.get(f"/syncs/{s1['sync_id']}/pages?cursor=not-a-cursor")
    assert r.status_code == 422
    body, sig = cursor.split(".")
    r = client.get(
        f"/syncs/{s1['sync_id']}/pages?cursor={body}.{sig[:-1]}x"
    )
    assert r.status_code == 422
    # unknown session
    assert client.get(f"/syncs/sync_{uuid.uuid4().hex}/pages").status_code == 404


def test_interleaved_corrections_during_paging_are_all_delivered_once(client, samples):
    pkg = create_package(client, samples)
    # three feasible instances to force several snapshot pages
    iids = []
    for _ in range(3):
        inst = create_instance(client, pkg["package_id"], samples["feasible"])
        check(client, inst["instance_id"], 1, 1)
        iids.append(inst["instance_id"])

    sync = _create_sync(client, page_size=1)
    # consume exactly one snapshot page, then mutate while paging is in flight
    first = client.get(f"/syncs/{sync['sync_id']}/pages").json()
    assert first["phase"] == "snapshot"
    client.post(
        f"/instances/{iids[0]}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["infeasible"], "application/xml")},
    )
    check(client, iids[0], 2, 1)
    # another correction before the snapshot is fully drained
    client.post(
        f"/instances/{iids[1]}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["rounding"], "application/xml")},
    )
    check(client, iids[1], 2, 1)

    ops, page, _ = _drain(client, sync["sync_id"],
                          start_cursor=first["next_cursor"])
    all_ops = first["operations"] + ops

    # snapshot contributes exactly 3 immutable upserts, each once
    snap = [op for op in all_ops if op["seq"] is None]
    assert len(snap) == 3 and len({op["op_id"] for op in snap}) == 3

    # every interleaved event is delivered in the tail, and no op_id repeats
    tail = [op for op in all_ops if op["seq"] is not None]
    op_ids = [op["op_id"] for op in all_ops]
    assert len(op_ids) == len(set(op_ids))
    by_instance = {}
    for op in tail:
        by_instance.setdefault(op["instance_id"], []).append(op)
    assert [op["op"] for op in by_instance[iids[0]]] == ["withdraw", "withdraw"]
    assert [op["op"] for op in by_instance[iids[1]]] == ["withdraw", "upsert"]
    assert by_instance[iids[1]][-1]["instance_revision"] == 2
    assert page["phase"] == "tail" and page["has_more"] is False


# ------------------------------------------------------------ restart
def test_sessions_and_cursors_survive_restart(client, samples, monkeypatch):
    from app import db

    pkg, ids = _feasible_setup(client, samples, "feasible", "rounding")
    sync = _create_sync(client, page_size=1)
    page = client.get(f"/syncs/{sync['sync_id']}/pages").json()
    cursor = page["next_cursor"]
    first_op_id = page["operations"][0]["op_id"]

    db.reset_conn()  # simulate container restart; everything lives in SQLite
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c2:
        r = c2.get(f"/syncs/{sync['sync_id']}")
        assert r.status_code == 200
        assert r.json()["snapshot_total"] == 2
        same = c2.get(
            f"/syncs/{sync['sync_id']}/pages?cursor={cursor}"
        ).json()
        assert same["operations"][0]["op_id"] != first_op_id  # next page
        # resume exactly where the pre-restart cursor pointed
        ops, _, _ = _drain(c2, sync["sync_id"], start_cursor=cursor)
        assert len(ops) == 1  # only the remaining snapshot row
        assert ops[0]["op_id"] != first_op_id
