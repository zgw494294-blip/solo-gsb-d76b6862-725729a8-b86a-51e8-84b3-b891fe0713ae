"""End-to-end checks for the transition-feasibility (executability) query."""

import threading

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# Two endpoints, each with states 0 -> final 1. The extra receives t6/t7 let a
# single configuration exhibit message mismatches and multiple simultaneous
# blocking reasons.
#
# t1 A sends m (0->1); t2 B sends n (0->1)
# t3 B receives m (0->1); t4 A receives n (0->1)
# t5 A sends z (1->1)  -- wrong source in the initial configuration
# t6 B receives x (0->0); t7 A receives y (1->1)
F = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 1},
        {"id": "t2", "from": "B", "to": "A", "action": "send", "message": "n",
         "source": 0, "target": 1},
        {"id": "t3", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 1},
        {"id": "t4", "from": "B", "to": "A", "action": "receive", "message": "n",
         "source": 0, "target": 1},
        {"id": "t5", "from": "A", "to": "B", "action": "send", "message": "z",
         "source": 1, "target": 1},
        {"id": "t6", "from": "A", "to": "B", "action": "receive", "message": "x",
         "source": 0, "target": 0},
        {"id": "t7", "from": "B", "to": "A", "action": "receive", "message": "y",
         "source": 1, "target": 1},
    ],
}

# One send then one receive reaches the completion configuration.
Q = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 1},
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 1},
    ],
}

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def create(protocol):
    r = client.post("/v1/sessions", json=protocol)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def run(sid, tid, ver):
    return client.post(f"/v1/sessions/{sid}/transitions",
                       json={"transition_id": tid, "expected_version": ver})


def feasibility(sid, ver):
    return client.post(f"/v1/sessions/{sid}/transition-feasibility",
                       json={"expected_version": ver})


def by_id(body):
    return {entry["id"]: entry for entry in body["transitions"]}


# 1. initial configuration: sends enabled, empty receive slot reports only that
sid = create(F)
r = feasibility(sid, 0)
check("initial 200", r.status_code == 200, r.text)
body = r.json()
check("initial echo", body.get("session_id") == sid and body.get("version") == 0, body)
check("initial order ascending by id",
      [e["id"] for e in body["transitions"]] == ["t1", "t2", "t3", "t4", "t5", "t6", "t7"],
      body)

entries = by_id(body)
check("t1 enabled with empty reasons", entries["t1"] == {"id": "t1", "enabled": True, "reasons": []}, entries["t1"])
check("t2 enabled with empty reasons", entries["t2"]["enabled"] is True and entries["t2"]["reasons"] == [], entries["t2"])
check("t3 empty receive slot",
      entries["t3"] == {"id": "t3", "enabled": False, "reasons": [
          {"error": "receive_queue_empty", "endpoint": "B", "direction": "A_to_B",
           "actual": None, "required": "m"}]}, entries["t3"])
check("t4 empty receive slot", entries["t4"]["reasons"] == [
    {"error": "receive_queue_empty", "endpoint": "A", "direction": "B_to_A",
     "actual": None, "required": "n"}], entries["t4"])
check("t5 wrong source only", entries["t5"]["enabled"] is False and entries["t5"]["reasons"] == [
    {"error": "state_mismatch", "endpoint": "A", "direction": "A_to_B",
     "actual": 0, "required": 1}], entries["t5"])
check("t6 empty slot", entries["t6"]["reasons"] == [
    {"error": "receive_queue_empty", "endpoint": "B", "direction": "A_to_B",
     "actual": None, "required": "x"}], entries["t6"])
# t7: wrong source AND empty slot — both reported; no mismatch on an empty slot
check("t7 state + empty both reported", entries["t7"]["reasons"] == [
    {"error": "state_mismatch", "endpoint": "A", "direction": "B_to_A",
     "actual": 0, "required": 1},
    {"error": "receive_queue_empty", "endpoint": "A", "direction": "B_to_A",
     "actual": None, "required": "y"}], entries["t7"])
check("enabled ids match the session view",
      sorted(t for t, e in entries.items() if e["enabled"])
      == client.get(f"/v1/sessions/{sid}").json()["enabled_transition_ids"],
      (entries,))

# 2. after t1 (A=1,B=0, A_to_B="m"): state + occupied, real mismatch, all cases
assert run(sid, "t1", 0).status_code == 200
body = feasibility(sid, 1).json()
entries = by_id(body)

check("post-t1 t1 reports state mismatch AND occupied slot", entries["t1"]["reasons"] == [
    {"error": "state_mismatch", "endpoint": "A", "direction": "A_to_B",
     "actual": 1, "required": 0},
    {"error": "send_queue_occupied", "endpoint": "A", "direction": "A_to_B",
     "actual": "m", "required": None}], entries["t1"])
check("t2 still enabled", entries["t2"]["enabled"] is True and entries["t2"]["reasons"] == [], entries["t2"])
check("t3 receives the matching m", entries["t3"]["enabled"] is True and entries["t3"]["reasons"] == [], entries["t3"])
# t4: wrong source and empty B_to_A slot; emptiness must not also be a mismatch
check("t4 state + empty, no mismatch", entries["t4"]["reasons"] == [
    {"error": "state_mismatch", "endpoint": "A", "direction": "B_to_A",
     "actual": 1, "required": 0},
    {"error": "receive_queue_empty", "endpoint": "A", "direction": "B_to_A",
     "actual": None, "required": "n"}], entries["t4"])
# t5: source matches but the A_to_B slot already holds m
check("t5 occupied send slot", entries["t5"]["reasons"] == [
    {"error": "send_queue_occupied", "endpoint": "A", "direction": "A_to_B",
     "actual": "m", "required": None}], entries["t5"])
# t6: B in source state but the slot head m differs from x
check("t6 message mismatch with actual/required", entries["t6"]["reasons"] == [
    {"error": "message_mismatch", "endpoint": "B", "direction": "A_to_B",
     "actual": "m", "required": "x"}], entries["t6"])
# t7: source matches, B_to_A empty -> only emptiness
check("t7 empty slot only", entries["t7"]["reasons"] == [
    {"error": "receive_queue_empty", "endpoint": "A", "direction": "B_to_A",
     "actual": None, "required": "y"}], entries["t7"])
check("post-t1 enabled set", sorted(t for t, e in entries.items() if e["enabled"]) == ["t2", "t3"], entries)

# 3. after t2 in a fresh session (A=0,B=1,B_to_A="n"): state + mismatch coexist
s2 = create(F)
assert run(s2, "t2", 0).status_code == 200
entries = by_id(feasibility(s2, 1).json())
check("after t2 t6 state mismatch + empty slot", entries["t6"]["reasons"] == [
    {"error": "state_mismatch", "endpoint": "B", "direction": "A_to_B",
     "actual": 1, "required": 0},
    {"error": "receive_queue_empty", "endpoint": "B", "direction": "A_to_B",
     "actual": None, "required": "x"}], entries["t6"])
check("after t2 t7 state mismatch + message mismatch both reported",
      entries["t7"]["reasons"] == [
          {"error": "state_mismatch", "endpoint": "A", "direction": "B_to_A",
           "actual": 0, "required": 1},
          {"error": "message_mismatch", "endpoint": "A", "direction": "B_to_A",
           "actual": "n", "required": "y"}], entries["t7"])
check("after t2 t4 enabled on n", entries["t4"]["enabled"] is True
      and entries["t4"]["reasons"] == [], entries["t4"])

# 4. completion configuration: everything disabled with only session_completed
sq = create(Q)
assert run(sq, "t1", 0).status_code == 200
assert run(sq, "t4", 1).status_code == 200
body = feasibility(sq, 2).json()
check("completion count", len(body["transitions"]) == 2, body)
for entry in body["transitions"]:
    check(f"completion {entry['id']} disabled+completed only",
          entry["enabled"] is False and entry["reasons"] == [{"error": "session_completed"}],
          entry)
check("completion view enabled ids empty",
      client.get(f"/v1/sessions/{sq}").json()["enabled_transition_ids"] == [], body)

# 5. protocol without transitions -> []
empty_protocol = {
    "A": {"states": [0], "initial": 0, "finals": [0]},
    "B": {"states": [0], "initial": 0, "finals": [0]},
    "transitions": [],
}
se = create(empty_protocol)
# initial config is a completion, but with no transitions the list is simply []
r = feasibility(se, 0)
check("no transitions 200 + empty list",
      r.status_code == 200 and r.json()["transitions"] == [], r.text)

# 6. unknown session -> 404; it takes precedence over staleness
r = feasibility("does-not-exist", 0)
check("unknown 404", r.status_code == 404 and r.json() == {"detail": "unknown session"},
      (r.status_code, r.text))
r = feasibility("does-not-exist", 99)
check("unknown beats stale 404", r.status_code == 404, r.status_code)

# 7. stale / future expected_version -> 409 with current_version
r = feasibility(sid, 0)
check("stale 409 body",
      r.status_code == 409
      and r.json() == {"detail": {"error": "version_conflict", "current_version": 1}},
      (r.status_code, r.text))
r = feasibility(sid, 99)
check("future version 409", r.status_code == 409
      and r.json()["detail"]["current_version"] == 1, r.text)

# 8. body validation -> 422
r = client.post(f"/v1/sessions/{sid}/transition-feasibility", json={})
check("missing version 422", r.status_code == 422, r.status_code)
r = client.post(f"/v1/sessions/{sid}/transition-feasibility", json={"expected_version": -1})
check("negative version 422", r.status_code == 422, r.status_code)
r = client.post(f"/v1/sessions/{sid}/transition-feasibility", json={"expected_version": 1.5})
check("non-integer version 422", r.status_code == 422, r.status_code)
r = client.post(f"/v1/sessions/{sid}/transition-feasibility",
                json={"expected_version": 1, "extra": 1})
check("extra field 422", r.status_code == 422, r.status_code)

# 9. read-only: the query never writes or consumes a version
before = client.get(f"/v1/sessions/{sid}").json()
feasibility(sid, 1)
feasibility(sid, 1)
after = client.get(f"/v1/sessions/{sid}").json()
check("no write", before["version"] == after["version"] == 1
      and before["trace"] == after["trace"], (before, after))

# 10. locked sessions can be queried; the lock is never a reported reason
client.put(f"/v1/sessions/{sq}/lock", json={"expected_version": 2, "locked": True})
r = feasibility(sq, 3)
check("locked queryable 200", r.status_code == 200, (r.status_code, r.text))
check("locked still reports protocol reasons only",
      all(e["reasons"] == [{"error": "session_completed"}] for e in r.json()["transitions"]),
      r.text)

# 11. after a protocol replacement the query uses the new definitions
rp = create(F)
assert run(rp, "t1", 0).status_code == 200
new_protocol = {
    **F,
    "transitions": [
        {**t, **({"message": "ZZ"} if t["id"] == "t1" else {})}
        for t in F["transitions"]
    ],
}
# Replay t1 under the new definition succeeds (send), so version goes to 2.
r = client.put(f"/v1/sessions/{rp}/protocol",
               json={"expected_version": 1, "protocol": new_protocol})
check("replace 200", r.status_code == 200, r.text)
body = feasibility(rp, 2).json()
entries = by_id(body)
# The A_to_B slot now holds ZZ: t3 expecting m sees a mismatch naming both,
# t6 expecting x sees ZZ, and a re-send t1 sees the slot occupied by ZZ.
check("replaced definitions used (t3 mismatch)", entries["t3"]["reasons"] == [
    {"error": "message_mismatch", "endpoint": "B", "direction": "A_to_B",
     "actual": "ZZ", "required": "m"}], entries["t3"])
check("replaced definitions used (t6 mismatch)", entries["t6"]["reasons"] == [
    {"error": "message_mismatch", "endpoint": "B", "direction": "A_to_B",
     "actual": "ZZ", "required": "x"}], entries["t6"])
check("replaced definitions used (t1 slot)", entries["t1"]["reasons"] == [
    {"error": "state_mismatch", "endpoint": "A", "direction": "A_to_B",
     "actual": 1, "required": 0},
    {"error": "send_queue_occupied", "endpoint": "A", "direction": "A_to_B",
     "actual": "ZZ", "required": None}], entries["t1"])
r = feasibility(rp, 1)
check("old version after replace 409", r.status_code == 409
      and r.json()["detail"]["current_version"] == 2, r.text)

# 12. concurrency: a query vs an execution either sees the whole named-version
#     snapshot or gets 409, never a cross-version mix.
target = create(F)
assert run(target, "t1", 0).status_code == 200  # version 1: A=1, slot A_to_B="m"
results = {}
barrier = threading.Barrier(8)


def query_worker(tag):
    barrier.wait()
    rr = feasibility(target, 1)
    results[tag] = rr.status_code, (None if rr.status_code != 200 else rr.json())


def execute_worker():
    barrier.wait()
    results["execute"] = (run(target, "t2", 1).status_code, None)


threads = [threading.Thread(target=query_worker, args=(f"q{i}",)) for i in range(7)]
threads.append(threading.Thread(target=execute_worker))
for t in threads:
    t.start()
for t in threads:
    t.join()

check("concurrent execute 200", results["execute"][0] == 200, results)
for i in range(7):
    code, snapshot = results[f"q{i}"]
    check(f"concurrent query{i} 200/409", code in (200, 409), results)
    if code == 200:
        e = by_id(snapshot)
        check(f"concurrent query{i} whole v1 snapshot",
              snapshot["version"] == 1
              and e["t1"]["reasons"][1] == {
                  "error": "send_queue_occupied", "endpoint": "A",
                  "direction": "A_to_B", "actual": "m", "required": None}
              and e["t2"]["enabled"] is True
              and e["t3"]["enabled"] is True,
              snapshot)

final = client.get(f"/v1/sessions/{target}").json()
check("session advanced exactly once", final["version"] == 2
      and [s["transition_id"] for s in final["trace"]] == ["t1", "t2"],
      (final["version"], final["trace"]))
# The committed version 2 (A=1,B=1, BA="n") is now queryable as a whole.
entries = by_id(feasibility(target, 2).json())
check("post-commit v2 coherent",
      entries["t4"]["enabled"] is False  # A=1 vs source 0, BA has n not for t4 owner
      and {"error": "state_mismatch", "endpoint": "A", "direction": "B_to_A",
           "actual": 1, "required": 0} in entries["t4"]["reasons"]
      and {"error": "message_mismatch", "endpoint": "A", "direction": "B_to_A",
           "actual": "n", "required": "n"} not in entries["t4"]["reasons"],
      entries["t4"])
# t7 at v2: A=1 matches source 1, slot BA holds n != y -> pure mismatch
check("post-commit t7 mismatch", entries["t7"]["reasons"] == [
    {"error": "message_mismatch", "endpoint": "A", "direction": "B_to_A",
     "actual": "n", "required": "y"}], entries["t7"])

# 13. old endpoints still work
r = client.get("/healthz")
check("healthz intact", r.status_code == 200)
r = client.post("/v1/sessions", json=Q)
check("sessions create intact", r.status_code == 201)
r = client.post("/v1/deadlocks", json=Q)
check("deadlocks intact", r.status_code == 200)
r = client.post(f"/v1/sessions/{sq}/message-tracks", json={"expected_version": 3})
check("message-tracks intact", r.status_code == 200)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    raise SystemExit(1)
print("ALL TRANSITION-FEASIBILITY CHECKS PASSED")
