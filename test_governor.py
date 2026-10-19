#!/usr/bin/env python3
"""Tests for governor.py. Run: python3 -m unittest test_governor -v
All tests use temp directories and synthetic snapshots; no real workers,
no builds."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "governor", os.path.join(HERE, "governor.py"))
gov = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gov)

T0 = 1_700_000_000.0  # fixed epoch for deterministic clocks
EMPTY = {"agents": [], "cron_active": []}


def running_snapshot(*agent_ids):
    return {"agents": [{"agent_id": a, "status": "running"} for a in agent_ids],
            "cron_active": []}


class GovernorCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gov-test-")
        shutil.copy(os.path.join(HERE, "limits.yaml"),
                    os.path.join(self.dir, "limits.yaml"))
        self.store = gov.Store(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def enqueue(self, title, **kw):
        kw.setdefault("now", T0)
        return gov.do_enqueue(self.store, title, **kw)

    def admit(self, snapshot=EMPTY, now=T0, load1=1.0):
        return gov.do_admit(self.store, snapshot, now=now, load1=load1)

    def items(self):
        return self.store.state["items"]


class TestBudgets(GovernorCase):
    def test_never_a_second_build(self):
        ids = [self.enqueue("build %d" % i, resource="build")["id"]
               for i in range(3)]
        out = self.admit()
        self.assertEqual(len(out["admitted"]), 1)
        first = out["admitted"][0]
        gov.do_started(self.store, first, "w-first", now=T0)
        out = self.admit(snapshot=running_snapshot("w-first"))
        self.assertEqual(out["admitted"], [])  # running build holds the slot
        gov.do_close(self.store, first, "done", now=T0 + 10)
        out = self.admit(now=T0 + 10)
        self.assertEqual(len(out["admitted"]), 1)
        self.assertIn(out["admitted"][0], ids)

    def test_total_cap_five_with_mix(self):
        b = self.enqueue("the build", resource="build")["id"]
        lights = [self.enqueue("light %d" % i)["id"] for i in range(6)]
        out = self.admit()
        self.assertEqual(len(out["admitted"]), 5)  # 1 build + 4 light
        self.assertIn(b, out["admitted"])
        for lid in lights:
            if lid in out["admitted"]:
                gov.do_started(self.store, lid, "w-" + lid, now=T0)
        gov.do_started(self.store, b, "w-build", now=T0)
        snap = running_snapshot(*["w-" + i for i in out["admitted"]],
                                "w-build")
        out2 = self.admit(snapshot=snap)
        self.assertEqual(out2["admitted"], [])  # total budget exhausted

    def test_light_cap_four(self):
        for i in range(6):
            self.enqueue("light %d" % i)
        out = self.admit()
        self.assertEqual(len(out["admitted"]), 4)

    def test_skip_does_not_block(self):
        b1 = self.enqueue("build one", resource="build")["id"]
        self.admit()
        gov.do_started(self.store, b1, "w-b1", now=T0)
        self.enqueue("build two", resource="build", now=T0 + 1)
        light = self.enqueue("light behind", now=T0 + 2)["id"]
        out = self.admit(snapshot=running_snapshot("w-b1"), now=T0 + 3)
        self.assertEqual(out["admitted"], [light])  # build waits, light passes


class TestPromotionOrder(GovernorCase):
    def test_aged_p4_does_not_precede_fresh_p2(self):
        aged_p4 = self.enqueue("old p4", cls="P4", now=T0 - 7200)["id"]
        fresh_p2 = self.enqueue("fresh p2", cls="P2", now=T0)["id"]
        fresh_p3 = self.enqueue("fresh p3", cls="P3", now=T0)["id"]
        # All three have effective class P2 after aging; council order is
        # (effective, base, enqueued): fresh P2, then aged P4, then... P3's
        # effective class is P3, so it goes last.
        out = self.admit(now=T0)
        self.assertEqual(out["admitted"], [fresh_p2, aged_p4, fresh_p3])

    def test_aging_promotes_p4_to_p2_after_two_hours(self):
        old = self.enqueue("old p4", cls="P4", now=T0 - 7200)["id"]
        p3 = self.enqueue("fresh p3", cls="P3", now=T0)["id"]
        item = self.items()[old]
        self.assertEqual(gov.effective_rank(item, T0, self.store.limits), 2)
        out = self.admit(now=T0)
        self.assertEqual(out["admitted"], [old, p3])

    def test_aging_caps_at_p2_and_never_demotes(self):
        ancient = self.enqueue("ancient p4", cls="P4", now=T0 - 86400)["id"]
        item = self.items()[ancient]
        self.assertEqual(gov.effective_rank(item, T0, self.store.limits), 2)
        p2 = self.enqueue("a p2", cls="P2", now=T0)["id"]
        self.assertEqual(gov.effective_rank(self.items()[p2], T0,
                                            self.store.limits), 2)

    def test_fifo_within_same_class(self):
        a = self.enqueue("first", cls="P3", now=T0)["id"]
        b = self.enqueue("second", cls="P3", now=T0 + 60)["id"]
        out = self.admit(now=T0 + 120)
        self.assertEqual(out["admitted"], [a, b])

    def test_blocked_dependency_waits(self):
        dep = self.enqueue("prerequisite")["id"]
        blocked = self.enqueue("dependent", depends_on=[dep], now=T0 + 1)["id"]
        out = self.admit(now=T0 + 2)
        self.assertEqual(out["admitted"], [dep])
        self.assertEqual(self.items()[blocked]["state"], "queued")


class TestFanout(GovernorCase):
    def test_bundle_children_consume_individual_slots(self):
        res = self.enqueue("code review bundle", cls="P4", fanout=11)
        self.assertEqual(len(res["children"]), 11)
        out = self.admit()
        # Parent (coordinator, light) + 4 children = 5 total, light cap 4
        # means parent + 3 children admitted if parent admitted first.
        admitted_items = [self.items()[i] for i in out["admitted"]]
        self.assertLessEqual(len(out["admitted"]), 5)
        lights_used = sum(1 for it in admitted_items
                          if it["resource"] == "light")
        self.assertLessEqual(lights_used, 4)
        children_admitted = [it for it in admitted_items if it.get("bundle")]
        self.assertEqual(len(out["admitted"]),
                         len(children_admitted)
                         + (1 if res["id"] in out["admitted"] else 0))
        queued_children = [it for it in self.items().values()
                           if it.get("bundle") and it["state"] == "queued"]
        self.assertEqual(len(children_admitted) + len(queued_children), 11)

    def test_bundle_parent_completes_when_children_done(self):
        res = self.enqueue("small bundle", fanout=2)
        out = self.admit()
        for iid in out["admitted"]:
            gov.do_started(self.store, iid, "w-" + iid, now=T0)
        snap = running_snapshot(*["w-" + i for i in out["admitted"]])
        for child in res["children"]:
            if self.items()[child]["state"] == "running":
                gov.do_close(self.store, child, "done", now=T0 + 5)
        out2 = self.admit(snapshot=snap, now=T0 + 6)
        for iid in out2["admitted"]:
            gov.do_started(self.store, iid, "w-" + iid, now=T0 + 6)
            gov.do_close(self.store, iid, "done", now=T0 + 7)
        self.assertEqual(self.items()[res["id"]]["state"], "done")


class TestReservations(GovernorCase):
    def test_stale_reservation_reaped(self):
        iid = self.enqueue("will stall")["id"]
        out = self.admit(now=T0)
        self.assertEqual(out["admitted"], [iid])
        old_admitted_at = self.items()[iid]["admitted_at"]
        out = self.admit(now=T0 + 6 * 60)  # stale after 5 min, never started
        self.assertIn(iid, out["requeued"])
        events = [e["event"] for e in self.store.read_events()]
        self.assertIn("requeue", events)
        # Re-admitted in the same pass with a fresh reservation.
        self.assertEqual(self.items()[iid]["state"], "admitted")
        self.assertGreater(self.items()[iid]["admitted_at"], old_admitted_at)

    def test_running_worker_absent_becomes_interrupted(self):
        iid = self.enqueue("doomed worker")["id"]
        self.admit(now=T0)
        gov.do_started(self.store, iid, "w-gone", now=T0)
        out = self.admit(now=T0 + 5 * 60)  # absent but within grace
        self.assertEqual(out["interrupted"], [])
        out = self.admit(now=T0 + 11 * 60)  # absent past 10 min
        self.assertEqual(out["interrupted"], [iid])
        self.assertEqual(self.items()[iid]["state"], "interrupted")
        # The freed slot admits new work in the same pass.
        nxt = self.enqueue("next up", now=T0 + 11 * 60)["id"]
        out = self.admit(now=T0 + 11 * 60)
        self.assertIn(nxt, out["admitted"])

    def test_cron_reservation_consumes_light_budget(self):
        for i in range(4):
            self.enqueue("light %d" % i)
        snap = {"agents": [], "cron_active": ["camera_sweep"]}  # 3 light-eq
        out = self.admit(snapshot=snap)
        self.assertEqual(len(out["admitted"]), 1)

    def test_circuit_breaker_blocks_admissions(self):
        self.enqueue("queued work")
        out = self.admit(load1=13.0)
        self.assertTrue(out["circuit_breaker"])
        self.assertEqual(out["admitted"], [])


class TestStateAndLock(GovernorCase):
    def test_rebuild_equivalence(self):
        a = self.enqueue("one", resource="build")["id"]
        self.enqueue("two")
        out = self.admit()
        gov.do_started(self.store, out["admitted"][0], "w-1", now=T0)
        gov.do_close(self.store, out["admitted"][0], "done", now=T0 + 30)
        self.admit(now=T0 + 40)
        before = json.loads(json.dumps(self.items()))
        self.store.rebuild()
        self.store.save_state()
        self.assertEqual(self.items(), before)
        self.assertEqual(self.items()[a]["state"], "done")

    def test_idempotent_enqueue(self):
        r1 = self.enqueue("same job", idempotency_key="repo:branch-x")
        r2 = self.enqueue("same job", idempotency_key="repo:branch-x",
                          now=T0 + 5)
        self.assertEqual(r1["id"], r2["id"])
        self.assertTrue(r2.get("duplicate"))
        self.assertEqual(len(self.items()), 1)

    def test_lock_contention_fails_cleanly(self):
        # A waiter whose wait budget is shorter than the steal threshold
        # must fail cleanly: past the threshold a held lock is presumed
        # abandoned and stealing is the designed behavior (see the steal
        # test), so contention is exercised inside the no-steal window.
        path = os.path.join(self.dir, "limits.yaml")
        with open(path) as f:
            text = f.read()
        with open(path, "w") as f:
            f.write(text.replace("wait_seconds: 10", "wait_seconds: 1"))
        self.store = gov.Store(self.dir)
        iid = self.enqueue("queued under lock")["id"]
        self.store.acquire_lock()  # simulate another holder
        try:
            other = gov.Store(self.dir)
            with self.assertRaises(gov.LockBusy):
                gov.do_admit(other, EMPTY, now=T0, load1=1.0)
        finally:
            self.store.release_lock()
        # No double admission, item untouched.
        self.assertEqual(self.items()[iid]["state"], "queued")
        out = self.admit()
        self.assertEqual(out["admitted"], [iid])

    def test_stale_lock_is_stolen(self):
        # Seed metrics so the steal threshold is at its floor-ish value,
        # then plant a lock older than it.
        for i in range(6):
            self.store.record_pass(0.05, 0)
        threshold = self.store.steal_threshold()
        self.assertGreaterEqual(threshold, 5.0)
        os.mkdir(self.store.lock_dir)
        with open(os.path.join(self.store.lock_dir, "holder.json"), "w") as f:
            json.dump({"pid": 999999, "ts": time.time() - threshold - 30}, f)
        other = gov.Store(self.dir)
        other.acquire_lock(wait_seconds=2)  # must steal, not raise
        other.release_lock()

    def test_steal_threshold_uses_measured_p95(self):
        self.assertEqual(self.store.steal_threshold(), 5.0)  # floor, no data
        for i in range(10):
            self.store.record_pass(1.0, 0)
        # p95 of 1.0s passes x10 multiple = 10s, above the 5s floor.
        self.assertAlmostEqual(self.store.steal_threshold(), 10.0, places=5)


class TestCli(unittest.TestCase):
    def test_cli_round_trip(self):
        d = tempfile.mkdtemp(prefix="gov-cli-")
        try:
            shutil.copy(os.path.join(HERE, "limits.yaml"),
                        os.path.join(d, "limits.yaml"))
            env = dict(os.environ, GOVERNOR_LOAD1="1.0")
            script = os.path.join(HERE, "governor.py")

            def run(*argv):
                return subprocess.run(
                    [sys.executable, script, "--dir", d] + list(argv),
                    capture_output=True, text=True, env=env, timeout=30)

            r = run("enqueue", "--title", "cli job", "--resource", "build")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            iid = json.loads(r.stdout)["id"]
            with open(os.path.join(d, "snap.json"), "w") as f:
                json.dump({"agents": [], "cron_active": []}, f)
            r = run("admit", "--snapshot", os.path.join(d, "snap.json"))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(json.loads(r.stdout)["admitted"], [iid])
            r = run("started", "--id", iid, "--worker", "w-cli")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = run("status")
            self.assertEqual(r.returncode, 0)
            self.assertIn("cli job", r.stdout)
            r = run("done", "--id", iid)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = run("rebuild")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(json.loads(r.stdout)["rebuilt_items"], 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
