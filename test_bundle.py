"""End-to-end checks for the session bundle export/import feature."""

import os
import sqlite3
import threading

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

P = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 1},
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 1},
    ],
}

# Self-looping pair: the same ids can repeat in one trace ([t1, t4, t1, ...]).
PLOOP = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 0},
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 0},
        {"id": "t9", "from": "A", "to": "B", "action": "send", "message": "f",
         "source": 0, "target": 1},
        {"id": "t8", "from": "A", "to": "B", "action": "receive", "message": "f",
         "source": 0, "target": 1},
    ],
}

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def new_session(protocol=P):
    r = client.post("/v1/sessions", json=protocol)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def run(sid, tid, ver):
    return client.post(f"/v1/sessions/{sid}/transitions",
                       json={"transition_id": tid, "expected_version": ver})


def lock(sid, ver, locked=True):
    return client.put(f"/v1/sessions/{sid}/lock",
                      json={"expected_version": ver, "locked": locked})


def export(items):
    return client.post("/v1/session-exports", json={"sessions": items})


def item(sid, ver):
    return {"session_id": sid, "expected_version": ver}


def import_bundle(bundle):
    return client.post("/v1/session-imports", json=bundle)


def stored_session_count():
    path = os.environ.get("SESSIONS_DB", "sessions.db")
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

# two source sessions: s1 walked [t1] (v1), s2 walked [t1, t4] then locked (v3)
s1 = new_session()
run(s1, "t1", 0)
s2 = new_session()
run(s2, "t1", 0)
run(s2, "t4", 1)
check("lock s2", lock(s2, 2).status_code == 200)

r = export([item(s2, 3), item(s1, 1)])
check("export 200", r.status_code == 200, (r.status_code, r.text))
bundle = r.json()
check("export schema_version 1", bundle.get("schema_version") == 1, bundle)
entries = bundle.get("sessions", [])
check("export entry count", len(entries) == 2, entries)
check("export input order", [e["source_id"] for e in entries] == [s2, s1], entries)
check("export versions", [e["source_version"] for e in entries] == [3, 1], entries)
check("export locked flags", [e["locked"] for e in entries] == [True, False], entries)
check("export trace ids", [e["trace_ids"] for e in entries] == [["t1", "t4"], ["t1"]],
      entries)
check("export protocol unchanged", entries[0]["protocol"] == P
      and entries[1]["protocol"] == P, entries[0].get("protocol"))
check("export entry keys", all(set(e) == {"source_id", "source_version", "protocol",
                                          "trace_ids", "locked"} for e in entries),
      entries)

# the export is read-only: versions and traces are untouched
check("export keeps s1 version", client.get(f"/v1/sessions/{s1}").json()["version"] == 1)
check("export keeps s2 version", client.get(f"/v1/sessions/{s2}").json()["version"] == 3)

# a repeated-id trace exports in order
s3 = new_session(PLOOP)
for tid, ver in (("t1", 0), ("t4", 1), ("t1", 2), ("t4", 3)):
    check(f"loop step {tid} v{ver}", run(s3, tid, ver).status_code == 200)
r = export([item(s3, 4)])
check("export repeated ids", r.status_code == 200
      and r.json()["sessions"][0]["trace_ids"] == ["t1", "t4", "t1", "t4"], r.text)

# export errors
r = export([item("does-not-exist", 0)])
check("export unknown 404", r.status_code == 404, r.status_code)
check("export unknown body", r.json() == {"detail": "unknown session"}, r.json())

r = export([item(s1, 1), item(s2, 1)])
check("export stale 409", r.status_code == 409, (r.status_code, r.text))
check("export stale detail", r.json()["detail"] == {
    "error": "version_conflict", "current_version": 3, "session_id": s2}, r.json())
check("export stale names first stale in input order",
      export([item(s2, 0), item(s1, 0)]).json()["detail"]["session_id"] == s2)

for bad, name in [
    ({"sessions": [item(s1, 1), item(s1, 1)]}, "duplicate ids"),
    ({"sessions": []}, "empty list"),
    ({"sessions": [item(new_session(), 0) for _ in range(9)]}, "nine entries"),
    ({"sessions": [{"session_id": s1}]}, "missing expected_version"),
    ({"sessions": [{"session_id": s1, "expected_version": -1}]}, "negative version"),
    ({"sessions": [{"session_id": "", "expected_version": 0}]}, "empty id"),
    ({"sessions": [{"session_id": s1, "expected_version": 1, "x": 1}]}, "extra item field"),
    ({"sessions": [item(s1, 1)], "x": 1}, "extra top field"),
    ({}, "missing sessions"),
]:
    r = client.post("/v1/session-exports", json=bad)
    check(f"export {name} 422", r.status_code == 422, (r.status_code, r.text))

# ---------------------------------------------------------------------------
# Import: round trip and continuation
# ---------------------------------------------------------------------------

before = stored_session_count()
r = import_bundle(bundle)
check("import 201", r.status_code == 201, (r.status_code, r.text))
created = r.json().get("sessions", [])
check("import entry count", len(created) == 2, created)
check("import order", [c["source_id"] for c in created] == [s2, s1], created)
new2, new1 = (c["session"] for c in created)
check("import fresh ids", new2["id"] not in (s1, s2) and new1["id"] not in (s1, s2)
      and new1["id"] != new2["id"], (new1["id"], new2["id"]))
check("import version 0", new1["version"] == 0 and new2["version"] == 0)
check("import locked preserved", new2["locked"] is True and new1["locked"] is False)
check("import protocol preserved", new1["protocol"] == P and new2["protocol"] == P)
check("import trace recomputed",
      [s["transition_id"] for s in new2["trace"]] == ["t1", "t4"]
      and [s["index"] for s in new2["trace"]] == [1, 2], new2["trace"])
check("import completed snapshot", new2["configuration"] == {
    "state": {"A": 1, "B": 1}, "queues": {"A_to_B": None, "B_to_A": None}}
      and new2["enabled_transition_ids"] == [], new2["configuration"])
check("import mid-drill snapshot", new1["configuration"] == {
    "state": {"A": 1, "B": 0}, "queues": {"A_to_B": "m", "B_to_A": None}}
      and new1["enabled_transition_ids"] == ["t4"], new1["configuration"])
check("import created both rows", stored_session_count() == before + 2)

# the imported mid-drill session continues from version 0
nid1 = new1["id"]
r = run(nid1, "t4", 0)
check("continue imported 200", r.status_code == 200, (r.status_code, r.text))
check("continue imported version 1", r.json()["version"] == 1)
check("continue imported completed", r.json()["enabled_transition_ids"] == [])
r = run(nid1, "t4", 0)
check("continue imported stale 409", r.status_code == 409, r.status_code)

# the imported locked session rejects execution until unlocked
nid2 = new2["id"]
r = run(nid2, "t1", 0)
check("imported locked execute 409", r.status_code == 409, r.status_code)
check("imported locked error", r.json()["detail"]["error"] == "session_locked", r.json())
check("unlock imported", lock(nid2, 0, False).status_code == 200)
r = client.put(f"/v1/sessions/{nid2}/protocol",
               json={"protocol": PLOOP, "expected_version": 1})
check("unlocked imported replace 200", r.status_code == 200, (r.status_code, r.text))

# importing the same bundle again creates another disjoint set of sessions;
# nothing is overwritten
r = import_bundle(bundle)
check("re-import 201", r.status_code == 201, (r.status_code, r.text))
again = [c["session"]["id"] for c in r.json()["sessions"]]
check("re-import disjoint ids", len(set(again + [nid1, nid2])) == 4, again)
check("first import intact", client.get(f"/v1/sessions/{nid1}").json()["version"] == 1)

# the source sessions were never modified by export or import
check("source s1 untouched", client.get(f"/v1/sessions/{s1}").json()["version"] == 1)
check("source s2 untouched", client.get(f"/v1/sessions/{s2}").json()["version"] == 3)

# an empty trace imports as a fresh drill of the same protocol
r = import_bundle({"schema_version": 1, "sessions": [
    {"source_id": "elsewhere", "source_version": 12, "protocol": P,
     "trace_ids": [], "locked": False}]})
check("import empty trace 201", r.status_code == 201, (r.status_code, r.text))
e = r.json()["sessions"][0]
check("import empty trace snapshot", e["session"]["trace"] == []
      and e["session"]["enabled_transition_ids"] == ["t1"]
      and e["session"]["version"] == 0, e)

# a trace with repeated ids replays in order
r = import_bundle({"schema_version": 1, "sessions": [
    {"source_id": s3, "source_version": 4, "protocol": PLOOP,
     "trace_ids": ["t1", "t4", "t1", "t4"], "locked": False}]})
check("import repeated ids 201", r.status_code == 201, (r.status_code, r.text))
check("import repeated ids trace",
      [s["transition_id"] for s in r.json()["sessions"][0]["session"]["trace"]]
      == ["t1", "t4", "t1", "t4"], r.text)

# ---------------------------------------------------------------------------
# Import: whole-bundle rejection
# ---------------------------------------------------------------------------

before = stored_session_count()

# unsupported format versions
r = import_bundle({"schema_version": 2, "sessions": [
    {"source_id": "x", "source_version": 0, "protocol": P,
     "trace_ids": [], "locked": False}]})
check("import schema 2 -> 422", r.status_code == 422, (r.status_code, r.text))
check("import schema detail", r.json()["detail"] == {
    "error": "unsupported_schema_version", "schema_version": 2}, r.json())
for bad_version in ("1", 1.0, True, None, 0, -1):
    r = import_bundle({"schema_version": bad_version, "sessions": [
        {"source_id": "x", "source_version": 0, "protocol": P,
         "trace_ids": [], "locked": False}]})
    check(f"import schema {bad_version!r} -> 422", r.status_code == 422,
          (bad_version, r.status_code, r.text))

# duplicate source_id
r = import_bundle({"schema_version": 1, "sessions": [
    {"source_id": "dup", "source_version": 0, "protocol": P,
     "trace_ids": [], "locked": False},
    {"source_id": "dup", "source_version": 1, "protocol": P,
     "trace_ids": ["t1"], "locked": False}]})
check("import duplicate source_id 422", r.status_code == 422, (r.status_code, r.text))

# replay failures name the entry (from 1) and the failed step
r = import_bundle({"schema_version": 1, "sessions": [
    {"source_id": "ok", "source_version": 0, "protocol": P,
     "trace_ids": ["t1"], "locked": False},
    {"source_id": "bad", "source_version": 0, "protocol": P,
     "trace_ids": ["t1", "nope"], "locked": False}]})
check("import unknown id 422", r.status_code == 422, (r.status_code, r.text))
check("import unknown id detail", r.json()["detail"] == {
    "error": "replay_failed", "entry_index": 2, "first_failed_step": 2,
    "reason": "unknown transition id 'nope'"}, r.json())

r = import_bundle({"schema_version": 1, "sessions": [
    {"source_id": "bad", "source_version": 0, "protocol": P,
     "trace_ids": ["t4"], "locked": False}]})
check("import disabled step 422", r.status_code == 422, (r.status_code, r.text))
check("import disabled step detail", r.json()["detail"] == {
    "error": "replay_failed", "entry_index": 1, "first_failed_step": 1,
    "reason": "transition 't4' is not enabled"}, r.json())

# a trace that completes before its last step is rejected
r = import_bundle({"schema_version": 1, "sessions": [
    {"source_id": "bad", "source_version": 0, "protocol": P,
     "trace_ids": ["t1", "t4", "t1"], "locked": False}]})
check("import completed mid-trace 422", r.status_code == 422, (r.status_code, r.text))
check("import completed mid-trace detail", r.json()["detail"] == {
    "error": "replay_failed", "entry_index": 1, "first_failed_step": 3,
    "reason": "configuration already completed"}, r.json())

# invalid protocol inside an otherwise fine bundle -> 422
bad_protocol = {**P, "transitions": P["transitions"] + [
    {"id": "t1", "from": "B", "to": "A", "action": "send", "message": "q",
     "source": 0, "target": 1}]}
r = import_bundle({"schema_version": 1, "sessions": [
    {"source_id": "bad", "source_version": 0, "protocol": bad_protocol,
     "trace_ids": [], "locked": False}]})
check("import invalid protocol 422", r.status_code == 422, (r.status_code, r.text))

# malformed bundles -> 422
for bad, name in [
    ({"schema_version": 1}, "missing sessions"),
    ({"sessions": []}, "missing schema_version"),
    ({"schema_version": 1, "sessions": []}, "empty sessions"),
    ({"schema_version": 1, "sessions": [
        {"source_id": str(i), "source_version": 0, "protocol": P,
         "trace_ids": [], "locked": False} for i in range(9)]}, "nine entries"),
    ({"schema_version": 1, "sessions": [
        {"source_id": "x", "source_version": 0, "protocol": P, "trace_ids": []}]},
     "missing locked"),
    ({"schema_version": 1, "sessions": [
        {"source_id": "x", "source_version": 0, "protocol": P,
         "trace_ids": [], "locked": "true"}]}, "string locked"),
    ({"schema_version": 1, "sessions": [
        {"source_id": "x", "source_version": -1, "protocol": P,
         "trace_ids": [], "locked": False}]}, "negative source_version"),
    ({"schema_version": 1, "sessions": [
        {"source_id": "x", "source_version": 0, "protocol": P,
         "trace_ids": [], "locked": False, "x": 1}]}, "extra entry field"),
    ({"schema_version": 1, "sessions": [], "x": 1}, "extra top field"),
]:
    r = client.post("/v1/session-imports", json=bad)
    check(f"import {name} 422", r.status_code == 422, (r.status_code, r.text))

# no partial sessions: every rejection above left the database untouched
check("failed imports created nothing", stored_session_count() == before,
      (before, stored_session_count()))

# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

# parallel imports of one bundle: every import commits its own complete set
results = []
barrier = threading.Barrier(5)


def import_worker():
    barrier.wait()
    rr = import_bundle(bundle)
    ids = [c["session"]["id"] for c in rr.json()["sessions"]] \
        if rr.status_code == 201 else []
    results.append((rr.status_code, ids))


threads = [threading.Thread(target=import_worker) for _ in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("parallel imports all 201", all(code == 201 for code, _ in results), results)
all_ids = [sid for _code, ids in results for sid in ids]
check("parallel imports disjoint ids", len(set(all_ids)) == 10, all_ids)

# export vs concurrent modification: the export either sees the named version
# whole or answers 409, never a cross-revision mix
cs = new_session()
results = []
barrier = threading.Barrier(6)


def export_worker():
    barrier.wait()
    rr = export([item(cs, 0)])
    body = rr.json()
    results.append((rr.status_code,
                    body.get("sessions", [{}])[0].get("trace_ids")
                    if rr.status_code == 200 else None))


def execute_worker():
    barrier.wait()
    rr = run(cs, "t1", 0)
    results.append(("execute", rr.status_code))


threads = [threading.Thread(target=export_worker) for _ in range(5)]
threads.append(threading.Thread(target=execute_worker))
for t in threads:
    t.start()
for t in threads:
    t.join()
check("concurrent execute 200", ("execute", 200) in results, results)
for code, trace_ids in results:
    if code == "execute":
        continue
    check("export whole snapshot or 409",
          (code == 200 and trace_ids == []) or code == 409, results)
check("concurrent export source v1",
      client.get(f"/v1/sessions/{cs}").json()["version"] == 1)

# existing endpoints still work
r = client.get("/healthz")
check("healthz", r.status_code == 200 and r.json() == {"status": "ok"})
r = client.post("/v1/deadlocks", json=P)
check("deadlocks endpoint intact", r.status_code == 200)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    raise SystemExit(1)
print("ALL BUNDLE CHECKS PASSED")
