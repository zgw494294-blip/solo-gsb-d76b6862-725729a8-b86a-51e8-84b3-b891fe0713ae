"""End-to-end checks for the cross-database session package export/import."""

import json
import os
import sqlite3
import tempfile

_db1 = tempfile.NamedTemporaryFile(prefix="sessions-exp-a-", suffix=".db", delete=False)
_db1.close()
os.environ["SESSIONS_DB"] = _db1.name

from fastapi.testclient import TestClient

from app import sessions
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

P_COMPLETED_INITIAL = {
    "A": {"states": [0], "initial": 0, "finals": [0]},
    "B": {"states": [0], "initial": 0, "finals": [0]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 0},
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 0},
    ],
}

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def new_session(protocol=P):
    return client.post("/v1/sessions", json=protocol).json()["id"]


def run(sid, tid, ver):
    return client.post(f"/v1/sessions/{sid}/transitions",
                       json={"transition_id": tid, "expected_version": ver})


def export_pairs(pairs):
    return client.post("/v1/session-exports",
                       json={"sessions": [
                           {"session_id": sid, "expected_version": ver}
                           for sid, ver in pairs]})


# ---------------------------------------------------------------- setup ----
# s1: trace [t1, t4] v2 (completed, unlocked); s2: trace [t1] v1 locked;
# s3: empty v0 unlocked.
s1 = new_session()
check("setup s1 t1", run(s1, "t1", 0).status_code == 200)
check("setup s1 t4", run(s1, "t4", 1).status_code == 200)
s2 = new_session()
check("setup s2 t1", run(s2, "t1", 0).status_code == 200)
check("setup s2 lock", client.put(f"/v1/sessions/{s2}/lock",
                                  json={"expected_version": 1, "locked": True}
                                  ).status_code == 200)
s3 = new_session()
empty_ids = [new_session() for _ in range(6)]  # pads a request up to 8 entries

# ------------------------------------------------------------- export ------
r = export_pairs([(s3, 0), (s1, 2), (s2, 2)])
check("export 200", r.status_code == 200, (r.status_code, r.text))
pkg = r.json()
check("export schema_version 1", pkg["schema_version"] == 1)
check("export entry count", len(pkg["sessions"]) == 3)
check("export input order",
      [e["source_id"] for e in pkg["sessions"]] == [s3, s1, s2],
      [e["source_id"] for e in pkg["sessions"]])
e0, e1, e2 = pkg["sessions"]
check("export entry fields",
      set(e0) == {"source_id", "source_version", "protocol", "trace_ids", "locked"},
      set(e0))
check("export s3 empty v0", e0["source_version"] == 0 and e0["trace_ids"] == []
      and e0["locked"] is False)
check("export s1 trace v2", e1["source_version"] == 2
      and e1["trace_ids"] == ["t1", "t4"] and e1["locked"] is False)
check("export s1 protocol roundtrip",
      e1["protocol"]["transitions"][0]["from"] == "A"
      and e1["protocol"]["transitions"][0]["id"] == "t1")
check("export list order preserved",
      [t["id"] for t in e1["protocol"]["transitions"]] == ["t1", "t4"])
check("export s2 locked", e2["source_version"] == 2 and e2["locked"] is True
      and e2["trace_ids"] == ["t1"])

# export is read-only: versions/locks unchanged
check("export does not write s1", client.get(f"/v1/sessions/{s1}").json()["version"] == 2)
check("export does not write s2",
      client.get(f"/v1/sessions/{s2}").json()["version"] == 2
      and client.get(f"/v1/sessions/{s2}").json()["locked"] is True)

# 1..8 bounds and duplicate ids
r = client.post("/v1/session-exports", json={"sessions": []})
check("export zero entries 422", r.status_code == 422, r.status_code)
nine = [(sid, 0) for sid in [s3] + empty_ids] + [(s1, 2), (s2, 2)]
check("nine entries prepared", len(nine) == 9)
r = export_pairs(nine)
check("export nine entries 422", r.status_code == 422, r.status_code)
eight = nine[:8]
r = export_pairs(eight)
check("export eight entries 200", r.status_code == 200, (r.status_code, r.text))
r = export_pairs([(s1, 2), (s1, 2)])
check("export duplicate id 422", r.status_code == 422, r.status_code)
r = client.post("/v1/session-exports", json={"sessions": [
    {"session_id": s1, "expected_version": -1}]})
check("export negative version 422", r.status_code == 422)
r = client.post("/v1/session-exports", json={"sessions": [
    {"session_id": s1, "expected_version": 0}], "extra": 1})
check("export extra field 422", r.status_code == 422)
r = client.post("/v1/session-exports", json={"sessions": [
    {"session_id": s1}]})
check("export missing version 422", r.status_code == 422)
r = client.post("/v1/session-exports", json={"sessions": [
    {"session_id": "", "expected_version": 0}]})
check("export empty id 422", r.status_code == 422)
r = client.post("/v1/session-exports", json={"sessions": [
    {"session_id": "unknown-id", "expected_version": 0}]})
check("export plain unknown 404", r.status_code == 404)

# unknown session -> 404 naming the id
r = export_pairs([(s1, 2), ("deadbeef", 0), (s3, 0)])
check("export unknown 404", r.status_code == 404, r.status_code)
check("export unknown names id",
      r.json()["detail"] == {"error": "unknown_session", "session_id": "deadbeef"},
      r.json()["detail"])

# unknown precedence: unknown reported even when another entry is stale
r = export_pairs([(s1, 0), ("deadbeef", 0)])
check("export unknown beats stale", r.status_code == 404, r.status_code)

# stale version -> 409 naming the id and current_version, first stale in order
r = export_pairs([(s3, 0), (s1, 1), (s2, 2)])
check("export stale 409", r.status_code == 409, r.status_code)
check("export stale detail",
      r.json()["detail"] == {"error": "version_conflict", "current_version": 2,
                             "session_id": s1},
      r.json()["detail"])
r = export_pairs([(s1, 2), (s2, 1)])
check("export stale second entry 409", r.status_code == 409)
check("export stale second entry detail",
      r.json()["detail"]["session_id"] == s2
      and r.json()["detail"]["current_version"] == 2,
      r.json()["detail"])
check("export stale no writes",
      client.get(f"/v1/sessions/{s1}").json()["version"] == 2)

# ------------------------------------------------------------- import ------
# Switch the storage to a second, unrelated database: the import must work
# without consulting the source database at all.
_db2 = tempfile.NamedTemporaryFile(prefix="sessions-exp-b-", suffix=".db", delete=False)
_db2.close()
sessions._DB_PATH = _db2.name
sessions._initialized = False


def db_count(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        connection.close()


def import_package(package):
    return client.post("/v1/session-imports", json=package)


# source ids do not exist on this database -> import still succeeds
check("source absent on target db", client.get(f"/v1/sessions/{s1}").status_code == 404)
check("target db empty before import", db_count(_db2.name) == 0)

r = import_package(pkg)
check("import 201", r.status_code == 201, (r.status_code, r.text))
out = r.json()
check("import schema_version 1", out["schema_version"] == 1)
check("import count", len(out["imported"]) == 3)

mapping = [(row["source_id"], row["id"]) for row in out["imported"]]
check("import order is package order",
      [source for source, _ in mapping] == [s3, s1, s2], mapping)
check("import ids fresh and distinct",
      all(new != source for source, new in mapping)
      and len({new for _, new in mapping}) == 3, mapping)

n3, n1, n2 = (new for _, new in mapping)

v3 = client.get(f"/v1/sessions/{n3}").json()
check("imported n3 version 0", v3["version"] == 0 and v3["trace"] == []
      and v3["locked"] is False)
check("imported n3 source_id", out["imported"][0]["source_id"] == s3)
check("imported n3 initial snapshot",
      v3["configuration"] == {"state": {"A": 0, "B": 0},
                              "queues": {"A_to_B": None, "B_to_A": None}}
      and v3["enabled_transition_ids"] == ["t1"], v3["configuration"])

v1 = client.get(f"/v1/sessions/{n1}").json()
check("imported n1 version 0", v1["version"] == 0)
check("imported n1 full replayed trace",
      [(s["index"], s["transition_id"]) for s in v1["trace"]]
      == [(1, "t1"), (2, "t4")], v1["trace"])
check("imported n1 snapshot recomputed",
      v1["configuration"] == {"state": {"A": 1, "B": 1},
                              "queues": {"A_to_B": None, "B_to_A": None}}
      and v1["enabled_transition_ids"] == [], v1["configuration"])
check("imported n1 unlocked", v1["locked"] is False)

v2 = client.get(f"/v1/sessions/{n2}").json()
check("imported n2 locked kept", v2["locked"] is True and v2["version"] == 0)
check("imported n2 trace", [s["transition_id"] for s in v2["trace"]] == ["t1"])
# a locked import rejects writes with 409 and can be unlocked to continue
r = run(n2, "t4", 0)
check("imported locked rejects execute", r.status_code == 409, r.status_code)
r = client.put(f"/v1/sessions/{n2}/lock",
               json={"expected_version": 0, "locked": False})
check("imported locked can unlock", r.status_code == 200)
r = run(n2, "t4", 1)
check("imported session continues drill", r.status_code == 200
      and r.json()["version"] == 2, (r.status_code, r.text))

# re-importing the same package creates further fresh ids; nothing overwritten
before = db_count(_db2.name)
r = import_package(pkg)
check("re-import 201", r.status_code == 201)
mapping2 = [(row["source_id"], row["id"]) for row in r.json()["imported"]]
check("re-import fresh ids",
      {new for _, new in mapping2}.isdisjoint({new for _, new in mapping}))
check("re-import adds rows", db_count(_db2.name) == before + 3)
check("old imported sessions intact",
      client.get(f"/v1/sessions/{n1}").json()["version"] == 0)

# imported snapshots equal the source sessions' snapshots at export time
# (source DB comparisons happen after switching back below).

# malformed packages -------------------------------------------------------
bad = json.loads(json.dumps(pkg))
bad["schema_version"] = 2
r = import_package(bad)
check("import schema v2 rejected 422", r.status_code == 422, (r.status_code, r.text))
bad = json.loads(json.dumps(pkg))
bad["schema_version"] = 0
check("import schema v0 rejected 422", import_package(bad).status_code == 422)
bad = json.loads(json.dumps(pkg))
del bad["schema_version"]
check("import schema missing 422", import_package(bad).status_code == 422)
check("import schema string 422",
      import_package({"schema_version": "1", "sessions": pkg["sessions"]}).status_code == 422)
check("import schema bool 422",
      import_package({"schema_version": True, "sessions": pkg["sessions"]}).status_code == 422)
check("import extra top field 422",
      import_package({"schema_version": 1, "sessions": pkg["sessions"],
                      "extra": 1}).status_code == 422)

bad = json.loads(json.dumps(pkg))
bad["sessions"][1]["source_id"] = bad["sessions"][0]["source_id"]
r = import_package(bad)
check("import duplicate source_id 422", r.status_code == 422, (r.status_code, r.text))
check("import empty sessions 422",
      import_package({"schema_version": 1, "sessions": []}).status_code == 422)

# invalid protocol inside a package -> 422 (existing validation reused)
bad = json.loads(json.dumps(pkg))
bad["sessions"][0]["protocol"]["transitions"][1]["id"] = "t1"  # duplicate id
check("import invalid protocol 422", import_package(bad).status_code == 422)
bad = json.loads(json.dumps(pkg))
bad["sessions"][0]["trace_ids"] = ["nope"]
check("import unknown trace id 422",
      import_package(bad).status_code == 422)

# replay failures: entry (1-based) and failed_step (1-based) ----------------
# entry 2: change t4's message so trace [t1, t4] cannot replay step 2
bad = json.loads(json.dumps(pkg))
bad["sessions"][1]["protocol"]["transitions"][1]["message"] = "ZZ"
r = import_package(bad)
check("replay mismatch 422", r.status_code == 422, (r.status_code, r.text))
check("replay mismatch detail",
      r.json()["detail"] == {
          "error": "replay_failed", "entry": 2, "failed_step": 2,
          "reason": "transition 't4' is not enabled"},
      r.json()["detail"])
count_after_fail = db_count(_db2.name)

# entry 1 fails at step 1: unknown transition id in the trace
bad = json.loads(json.dumps(pkg))
bad["sessions"][0]["trace_ids"] = ["ghost"]
r = import_package(bad)
check("replay unknown id 422", r.status_code == 422)
check("replay unknown id detail",
      r.json()["detail"]["entry"] == 1 and r.json()["detail"]["failed_step"] == 1,
      r.json()["detail"])

# entry 3 fails at step 1: initial configuration is already completed but the
# trace still has a step ("no further step allowed after completion").
bad = json.loads(json.dumps(pkg))
bad["sessions"][2]["protocol"] = P_COMPLETED_INITIAL
bad["sessions"][2]["trace_ids"] = ["t1"]
r = import_package(bad)
check("replay completed-before-end 422", r.status_code == 422, (r.status_code, r.text))
check("replay completed-before-end detail",
      r.json()["detail"]["entry"] == 3 and r.json()["detail"]["failed_step"] == 1
      and r.json()["detail"]["reason"] == "configuration already completed",
      r.json()["detail"])

# atomicity: whole package rejected -> no partial sessions and nothing overwritten
check("failed imports create no rows",
      db_count(_db2.name) == count_after_fail,
      (db_count(_db2.name), count_after_fail))

# package where the first entry is fine but a later one fails -> still nothing
mixed = {"schema_version": 1, "sessions": [
    {"source_id": "x1", "source_version": 0, "protocol": P, "trace_ids": []},
    {"source_id": "x2", "source_version": 1, "protocol": P,
     "trace_ids": ["t1", "t4", "t1"]},  # t1 after completion
]}
r = import_package(mixed)
check("mixed package 422 at entry2 step3",
      r.status_code == 422 and r.json()["detail"]["entry"] == 2
      and r.json()["detail"]["failed_step"] == 3,
      (r.status_code, getattr(r, "text", "")))
check("mixed package atomic", db_count(_db2.name) == count_after_fail)

# entries must be objects with the exact fields
weird = {"schema_version": 1, "sessions": [
    {"source_id": "z", "source_version": 0, "protocol": P,
     "trace_ids": [1, 2], "locked": False}]}
check("non-string trace ids 422", import_package(weird).status_code == 422)
weird = {"schema_version": 1, "sessions": [
    {"source_id": "z", "source_version": -1, "protocol": P}]}
check("negative source_version 422", import_package(weird).status_code == 422)
weird = {"schema_version": 1, "sessions": [
    {"source_id": "z", "source_version": 0, "protocol": P, "locked": "yes"}]}
check("non-bool locked 422", import_package(weird).status_code == 422)
weird = {"schema_version": 1, "sessions": [
    {"source_id": "", "source_version": 0, "protocol": P}]}
check("empty source_id 422", import_package(weird).status_code == 422)

# nine entries -> 422
nine_pkg = {"schema_version": 1, "sessions": [
    {"source_id": f"s{i}", "source_version": 0, "protocol": P, "trace_ids": []}
    for i in range(9)]}
check("import nine entries 422", import_package(nine_pkg).status_code == 422)

# import still never consults the source database: source ids may coincide with
# arbitrary strings and the package stands alone.
standalone = {"schema_version": 1, "sessions": [
    {"source_id": "totally-made-up", "source_version": 42, "protocol": P,
     "trace_ids": ["t1"], "locked": True}]}
r = import_package(standalone)
check("standalone import 201", r.status_code == 201, (r.status_code, r.text))
row = r.json()["imported"][0]
check("standalone mapping", row["source_id"] == "totally-made-up"
      and row["id"] != "totally-made-up")
check("standalone version reset", row["version"] == 0 and row["locked"] is True)
check("standalone trace replayed",
      [s["transition_id"] for s in row["trace"]] == ["t1"]
      and row["configuration"]["queues"] == {"A_to_B": "m", "B_to_A": None})

# ------------------------------------------------ switch back to source DB --
sessions._DB_PATH = _db1.name
sessions._initialized = False
# source database is exactly as left after export: no imported ids, versions intact
check("source db untouched: n1 absent", client.get(f"/v1/sessions/{n1}").status_code == 404)
check("source db s1 still v2", client.get(f"/v1/sessions/{s1}").json()["version"] == 2)
check("source db s2 still locked v2",
      client.get(f"/v1/sessions/{s2}").json()["version"] == 2
      and client.get(f"/v1/sessions/{s2}").json()["locked"] is True)
# exported snapshots recompute identically to the source sessions (the new
# session starts at version 0 and gains the source_id mapping, by design)
src1 = client.get(f"/v1/sessions/{s1}").json()
comparable_keys = ("locked", "protocol", "configuration", "trace",
                   "enabled_transition_ids")
check("imported snapshot equals source snapshot",
      all(v1[k] == src1[k] for k in comparable_keys),
      {k: (v1[k], src1[k]) for k in comparable_keys if v1[k] != src1[k]})
src2 = client.get(f"/v1/sessions/{s2}").json()
check("source s2 trace still [t1]",
      [s["transition_id"] for s in src2["trace"]] == ["t1"])

# export then import on the same database also yields independent copies with
# fresh ids and version 0; the originals are never overwritten.
pkg2 = export_pairs([(s1, 2)]).json()
r = import_package(pkg2)
check("same-db roundtrip 201", r.status_code == 201)
new_id = r.json()["imported"][0]["id"]
check("same-db fresh id", new_id != s1)
check("same-db original intact",
      client.get(f"/v1/sessions/{s1}").json()["version"] == 2
      and client.get(f"/v1/sessions/{s1}").json()["locked"] is False)
check("same-db copy version 0",
      client.get(f"/v1/sessions/{new_id}").json()["version"] == 0)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
