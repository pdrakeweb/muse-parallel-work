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
        # Fixture pin: the budget/breaker tests were authored against
        # build 1 / light 4 / total 5 / breaker 12.0 (DESIGN S4;
        # STATUS.md). The live limits.yaml moved on (build 2 on Oct 20,
        # 2026; light 9, total 10, breaker 15.0 on Oct 21, 2026), which
        # silently reddens those tests; the suite tests mechanics
        # against a fixed budget fixture, so pin the authored values
        # here. The live limits.yaml itself is never modified.
        path = os.path.join(self.dir, "limits.yaml")
        with open(path) as f:
            text = f.read()
        for old, new in (("build_slots: 2", "build_slots: 1"),
                         ("light_slots: 9", "light_slots: 4"),
                         ("total_workers: 10", "total_workers: 5"),
                         ("loadavg_threshold: 15.0",
                          "loadavg_threshold: 12.0")):
            text = text.replace(old, new)
        with open(path, "w") as f:
            f.write(text)
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


class TestJanitorSnapshot(GovernorCase):
    """The janitor's snapshot is built from state + /proc evidence only.
    Protection rules (brief, Oct 21 2026): young items always protected;
    builds protected while any compiler process is alive; explicit
    liveness evidence protects; otherwise the worker is omitted and the
    ordinary running-absent TTL applies."""

    def _running(self, title, resource="light", started_at=T0, **kw):
        iid = self.enqueue(title, resource=resource, now=started_at, **kw)["id"]
        out = self.admit(now=started_at)
        self.assertIn(iid, out["admitted"])
        gov.do_started(self.store, iid, "w-" + iid, now=started_at)
        return iid

    def _protected_ids(self, snap):
        return {a["agent_id"] for a in snap["agents"]}

    def test_young_light_worker_protected(self):
        iid = self._running("young light")
        snap = gov.build_janitor_snapshot(self.store, now=T0 + 30 * 60)
        self.assertIn("w-" + iid, self._protected_ids(snap))
        self.assertEqual(snap["cron_active"], [])

    def test_old_light_worker_unprotected_without_evidence(self):
        iid = self._running("old light")
        snap = gov.build_janitor_snapshot(self.store, now=T0 + 61 * 60)
        self.assertNotIn("w-" + iid, self._protected_ids(snap))

    def test_old_light_worker_protected_by_live_evidence(self):
        iid = self._running("evidenced light")
        snap = gov.build_janitor_snapshot(
            self.store, now=T0 + 61 * 60, live_agent_ids={"w-" + iid})
        self.assertIn("w-" + iid, self._protected_ids(snap))

    def test_build_protected_while_compiler_alive(self):
        iid = self._running("old build", resource="build")
        snap = gov.build_janitor_snapshot(self.store, now=T0 + 180 * 60,
                                          compiler_alive=True)
        self.assertIn("w-" + iid, self._protected_ids(snap))

    def test_build_unprotected_when_no_compiler(self):
        iid = self._running("stale build", resource="build")
        snap = gov.build_janitor_snapshot(self.store, now=T0 + 180 * 60,
                                          compiler_alive=False)
        self.assertNotIn("w-" + iid, self._protected_ids(snap))

    def test_admitted_items_contribute_no_agents(self):
        iid = self.enqueue("just admitted")["id"]
        self.admit(now=T0)
        self.assertEqual(self.items()[iid]["state"], "admitted")
        snap = gov.build_janitor_snapshot(self.store, now=T0)
        self.assertEqual(snap["agents"], [])


class TestCheckpoints(GovernorCase):
    """Checkpoint heartbeats (Oct 21, 2026): a worker on a
    tracker-linked item writes a GOVERNOR-CHECKPOINT line into its
    Work Tracker entry's details every ~5 minutes. A checkpoint younger
    than 10 minutes protects the running item in the janitor snapshot
    exactly like the <60-minute blanket; stale/missing checkpoints,
    unlinked items, and checkpoints carried by the wrong entry confer
    nothing."""

    NOW = T0 + 70 * 60  # worker started 70 min ago: past every old rule

    def _details(self, item_id, ts, state="working"):
        return ('Human notes stay.\nGOVERNOR-CHECKPOINT '
                '{"item": "%s", "ts": %s, "state": "%s"}'
                % (item_id, ts, state))

    def _running_old(self, title, tracker_ref=None, resource="light"):
        iid = self.enqueue(title, resource=resource, now=T0,
                           tracker_ref=tracker_ref)["id"]
        self.admit(now=T0)
        gov.do_started(self.store, iid, "w-" + iid, now=T0)
        return iid

    def _cps(self, iid, ts, tracker_id=72):
        return {iid: {"ts": ts, "tracker_id": tracker_id,
                      "state": "working"}}

    def _protected(self, snap):
        return {a["agent_id"] for a in snap["agents"]}

    def test_parse_checkpoint_reads_last_marker_line(self):
        details = (self._details("item-a", T0, "early") + "\n"
                   + self._details("item-a", T0 + 300, "later"))
        cp = gov.parse_checkpoint(details)
        self.assertEqual(cp["item"], "item-a")
        self.assertEqual(cp["ts"], T0 + 300)
        self.assertEqual(cp["state"], "later")

    def test_parse_checkpoint_absent_or_malformed(self):
        self.assertIsNone(gov.parse_checkpoint(None))
        self.assertIsNone(gov.parse_checkpoint(""))
        self.assertIsNone(gov.parse_checkpoint("no marker here"))
        self.assertIsNone(
            gov.parse_checkpoint("GOVERNOR-CHECKPOINT {not json"))
        self.assertIsNone(
            gov.parse_checkpoint('GOVERNOR-CHECKPOINT {"item": "x"}'))

    def test_extract_checkpoints_maps_entries_and_prefers_freshest(self):
        entries = [
            {"id": 72, "details": self._details("item-a", T0)},
            {"id": 73, "details": self._details("item-a", T0 + 240)},
            {"id": 74, "details": "no checkpoint in this one"},
            {"id": 75, "details": self._details("item-b", T0 + 60)},
            "not-a-dict",
        ]
        out = gov.extract_checkpoints(entries)
        self.assertEqual(set(out), {"item-a", "item-b"})
        self.assertEqual(out["item-a"]["ts"], T0 + 240)
        self.assertEqual(out["item-a"]["tracker_id"], 73)
        self.assertEqual(out["item-b"]["tracker_id"], 75)

    def test_fresh_checkpoint_protects_old_worker(self):
        iid = self._running_old("checkpointed", tracker_ref=72)
        snap = gov.build_janitor_snapshot(
            self.store, now=self.NOW,
            checkpoints=self._cps(iid, self.NOW - 60))
        self.assertIn("w-" + iid, self._protected(snap))

    def test_stale_checkpoint_confers_nothing(self):
        iid = self._running_old("stale checkpoint", tracker_ref=72)
        snap = gov.build_janitor_snapshot(
            self.store, now=self.NOW,
            checkpoints=self._cps(iid, self.NOW - 11 * 60))
        self.assertNotIn("w-" + iid, self._protected(snap))

    def test_checkpoint_requires_matching_tracker_ref(self):
        iid = self._running_old("wrong entry", tracker_ref=72)
        snap = gov.build_janitor_snapshot(
            self.store, now=self.NOW,
            checkpoints=self._cps(iid, self.NOW - 60, tracker_id=99))
        self.assertNotIn("w-" + iid, self._protected(snap))

    def test_unlinked_item_gets_no_checkpoint_protection(self):
        iid = self._running_old("no link")  # tracker_ref None
        snap = gov.build_janitor_snapshot(
            self.store, now=self.NOW,
            checkpoints=self._cps(iid, self.NOW - 60))
        self.assertNotIn("w-" + iid, self._protected(snap))

    def test_checkpoint_never_protects_terminal_item(self):
        iid = self._running_old("already done", tracker_ref=72)
        gov.do_close(self.store, iid, "done", now=T0 + 30 * 60)
        snap = gov.build_janitor_snapshot(
            self.store, now=self.NOW,
            checkpoints=self._cps(iid, self.NOW - 60))
        self.assertEqual(snap["agents"], [])

    def test_admit_keeps_checkpointed_worker_running(self):
        iid = self._running_old("long quiet phase", tracker_ref=72)
        snap = gov.build_janitor_snapshot(
            self.store, now=self.NOW,
            checkpoints=self._cps(iid, self.NOW - 120))
        out = gov.do_admit(self.store, snap, now=self.NOW, load1=1.0)
        self.assertEqual(out["interrupted"], [])
        self.assertEqual(self.items()[iid]["state"], "running")
        # Control: the same worker without checkpoint evidence is
        # interrupted by the running-absent TTL in the same pass shape.
        iid2 = self._running_old("no heartbeat", tracker_ref=73)
        snap2 = gov.build_janitor_snapshot(self.store, now=self.NOW,
                                           checkpoints={})
        out2 = gov.do_admit(self.store, snap2, now=self.NOW, load1=1.0)
        self.assertIn(iid2, out2["interrupted"])

    def test_tracker_ref_recorded_at_enqueue_and_started(self):
        iid = self.enqueue("linked at enqueue", tracker_ref="72")["id"]
        self.assertEqual(self.items()[iid]["tracker_ref"], 72)
        iid2 = self.enqueue("linked at start")["id"]
        self.assertIsNone(self.items()[iid2]["tracker_ref"])
        self.admit(now=T0)
        gov.do_started(self.store, iid2, "w-2", now=T0,
                       tracker_ref="75")
        self.assertEqual(self.items()[iid2]["tracker_ref"], 75)


class TestCheckpointCli(unittest.TestCase):
    def test_checkpoint_cli_round_trip(self):
        d = tempfile.mkdtemp(prefix="gov-checkpoint-cli-")
        try:
            shutil.copy(os.path.join(HERE, "limits.yaml"),
                        os.path.join(d, "limits.yaml"))
            env = dict(os.environ, GOVERNOR_LOAD1="1.0")
            script = os.path.join(HERE, "governor.py")

            def run(*argv):
                return subprocess.run(
                    [sys.executable, script, "--dir", d] + list(argv),
                    capture_output=True, text=True, env=env, timeout=30)

            r = run("enqueue", "--title", "checkpoint cli job",
                    "--tracker-ref", "72")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            iid = json.loads(r.stdout)["id"]
            r = run("admit")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = run("started", "--id", iid, "--worker", "w-cli")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            details = ('notes\nGOVERNOR-CHECKPOINT {"item": "%s", '
                       '"ts": %s, "state": "cli demo"}'
                       % (iid, time.time()))
            tracker_path = os.path.join(d, "tracker.json")
            with open(tracker_path, "w") as f:
                json.dump({"data": {"items": [
                    {"id": 72, "details": details}]}}, f)
            cps_path = os.path.join(d, "checkpoints.json")
            r = run("extract-checkpoints", "--tracker-json", tracker_path,
                    "--out", cps_path)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(json.loads(r.stdout)["count"], 1)
            r = run("janitor-snapshot", "--checkpoints", cps_path)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            diag = json.loads(r.stdout)
            self.assertEqual(diag["checkpoint_protected"], [iid])
            self.assertIn({"agent_id": "w-cli", "status": "running"},
                          diag["snapshot"]["agents"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestSetTrackerRef(GovernorCase):
    """Retrofit command (Oct 21, 2026): tracker_ref settable on an
    existing queued/admitted/running item — items enqueued before
    their tracker entry existed can still gain checkpoint protection.
    Terminal items are refused; the value rides the event log, so it
    survives a rebuild."""

    def test_set_on_queued(self):
        iid = self.enqueue("queued relaunch")["id"]
        out = gov.do_set_tracker_ref(self.store, iid, "74", now=T0)
        self.assertEqual(out, {"id": iid, "state": "queued",
                               "tracker_ref": 74})
        self.assertEqual(self.items()[iid]["tracker_ref"], 74)

    def test_set_on_admitted(self):
        iid = self.enqueue("admitted relaunch")["id"]
        self.admit(now=T0)
        self.assertEqual(self.items()[iid]["state"], "admitted")
        out = gov.do_set_tracker_ref(self.store, iid, 75, now=T0 + 1)
        self.assertEqual(out["state"], "admitted")
        self.assertEqual(self.items()[iid]["tracker_ref"], 75)

    def test_set_on_running(self):
        iid = self.enqueue("running relaunch")["id"]
        self.admit(now=T0)
        gov.do_started(self.store, iid, "w-run", now=T0)
        out = gov.do_set_tracker_ref(self.store, iid, "76", now=T0 + 60)
        self.assertEqual(out["state"], "running")
        item = self.items()[iid]
        self.assertEqual(item["tracker_ref"], 76)
        self.assertEqual(item["worker_agent_id"], "w-run")  # untouched

    def test_refused_on_done(self):
        iid = self.enqueue("finished")["id"]
        self.admit(now=T0)
        gov.do_started(self.store, iid, "w-done", now=T0)
        gov.do_close(self.store, iid, "done", now=T0 + 10)
        with self.assertRaises(gov.GovernorError):
            gov.do_set_tracker_ref(self.store, iid, 77, now=T0 + 20)
        self.assertIsNone(self.items()[iid]["tracker_ref"])

    def test_refused_on_failed(self):
        iid = self.enqueue("doomed")["id"]
        self.admit(now=T0)
        gov.do_started(self.store, iid, "w-fail", now=T0)
        gov.do_close(self.store, iid, "failed", error="boom", now=T0 + 10)
        with self.assertRaises(gov.GovernorError):
            gov.do_set_tracker_ref(self.store, iid, 77, now=T0 + 20)

    def test_unknown_item_refused(self):
        with self.assertRaises(gov.GovernorError):
            gov.do_set_tracker_ref(self.store, "no-such-item", 77, now=T0)

    def test_survives_rebuild(self):
        iid = self.enqueue("rebuild me")["id"]
        gov.do_set_tracker_ref(self.store, iid, "88", now=T0)
        events = [e["event"] for e in self.store.read_events()]
        self.assertIn("set_tracker_ref", events)
        self.store.rebuild()
        self.store.save_state()
        self.assertEqual(self.items()[iid]["tracker_ref"], 88)

    def test_checkpoint_protection_after_retrofit(self):
        # The point of the retrofit: a long-running item linked after
        # the fact is protected by a fresh checkpoint in its entry.
        iid = self.enqueue("old runner")["id"]
        self.admit(now=T0)
        gov.do_started(self.store, iid, "w-old", now=T0)
        gov.do_set_tracker_ref(self.store, iid, 72, now=T0 + 3600)
        now = T0 + 70 * 60
        cps = {iid: {"ts": now - 60, "tracker_id": 72,
                     "state": "working"}}
        snap = gov.build_janitor_snapshot(self.store, now=now,
                                          checkpoints=cps)
        self.assertIn("w-old", {a["agent_id"] for a in snap["agents"]})


class TestSetTrackerRefCli(unittest.TestCase):
    def test_set_tracker_ref_cli(self):
        d = tempfile.mkdtemp(prefix="gov-setref-cli-")
        try:
            shutil.copy(os.path.join(HERE, "limits.yaml"),
                        os.path.join(d, "limits.yaml"))
            env = dict(os.environ, GOVERNOR_LOAD1="1.0")
            script = os.path.join(HERE, "governor.py")

            def run(*argv):
                return subprocess.run(
                    [sys.executable, script, "--dir", d] + list(argv),
                    capture_output=True, text=True, env=env, timeout=30)

            r = run("enqueue", "--title", "unlinked job")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            iid = json.loads(r.stdout)["id"]
            r = run("set-tracker-ref", "--id", iid, "--tracker-ref", "91")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(json.loads(r.stdout)["tracker_ref"], 91)
            r = run("done", "--id", iid)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = run("set-tracker-ref", "--id", iid, "--tracker-ref", "92")
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("done", json.loads(r.stdout)["detail"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestBlockResources(GovernorCase):
    def test_block_build_admits_light_only(self):
        b = self.enqueue("blocked build", resource="build")["id"]
        l = self.enqueue("free light")["id"]
        out = gov.do_admit(self.store, EMPTY, now=T0, load1=1.0,
                           block_resources=frozenset(["build"]))
        self.assertEqual(out["admitted"], [l])
        self.assertEqual(out["blocked_resources"], ["build"])
        self.assertEqual(self.items()[b]["state"], "queued")
        out2 = self.admit(now=T0 + 1)  # unblocked pass admits the build
        self.assertEqual(out2["admitted"], [b])


class TestCompilerScan(unittest.TestCase):
    def test_scan_reads_comm_names_from_proc_root(self):
        d = tempfile.mkdtemp(prefix="gov-proc-")
        try:
            for pid, comm in ((123, "rustc"), (456, "bash"),
                              (789, "clippy-driver"), (999, None)):
                os.mkdir(os.path.join(d, str(pid)))
                if comm is not None:
                    with open(os.path.join(d, str(pid), "comm"), "w") as f:
                        f.write(comm + "\n")
            os.mkdir(os.path.join(d, "self"))  # non-numeric entry ignored
            found = gov.scan_compiler_processes(d)
            self.assertEqual({p["comm"] for p in found},
                             {"rustc", "clippy-driver"})
            self.assertEqual({p["pid"] for p in found}, {123, 789})
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_scan_missing_root_returns_empty(self):
        self.assertEqual(gov.scan_compiler_processes("/nonexistent-proc"),
                         [])


class TestRequeueInterrupted(GovernorCase):
    def _interrupted(self, title, start_at=T0):
        iid = self.enqueue(title, now=start_at)["id"]
        self.admit(now=start_at)
        gov.do_started(self.store, iid, "w-" + iid, now=start_at)
        out = self.admit(now=start_at + 11 * 60)  # absent past TTL
        self.assertEqual(out["interrupted"], [iid])
        return iid

    def test_interrupted_below_attempt_cap_requeues(self):
        iid = self._interrupted("flaky once")  # attempts == 1
        out = gov.do_requeue_interrupted(self.store, now=T0 + 12 * 60)
        self.assertEqual(out["requeued"], [iid])
        self.assertEqual(out["failed"], [])
        item = self.items()[iid]
        self.assertEqual(item["state"], "queued")
        self.assertIsNone(item["worker_agent_id"])
        self.assertIsNone(item["started_at"])

    def test_interrupted_at_attempt_cap_fails(self):
        iid = self._interrupted("flaky twice")          # attempts 1
        gov.do_requeue_interrupted(self.store, now=T0 + 12 * 60)
        self.admit(now=T0 + 13 * 60)                    # attempts 2
        gov.do_started(self.store, iid, "w-again", now=T0 + 13 * 60)
        out = self.admit(now=T0 + 24 * 60)
        self.assertEqual(out["interrupted"], [iid])
        out = gov.do_requeue_interrupted(self.store, now=T0 + 25 * 60)
        self.assertEqual(out["requeued"], [])
        self.assertEqual(out["failed"], [iid])
        item = self.items()[iid]
        self.assertEqual(item["state"], "failed")
        self.assertIn("max attempts", item["last_error"])

    def test_other_states_untouched(self):
        done_id = self.enqueue("finisher")["id"]
        self.admit(now=T0)
        gov.do_started(self.store, done_id, "w-done", now=T0)
        gov.do_close(self.store, done_id, "done", now=T0 + 1)
        queued_id = self.enqueue("still waiting", now=T0 + 2)["id"]
        out = gov.do_requeue_interrupted(self.store, now=T0 + 3)
        self.assertEqual(out, {"requeued": [], "failed": []})
        self.assertEqual(self.items()[done_id]["state"], "done")
        self.assertEqual(self.items()[queued_id]["state"], "queued")


class TestJanitorFlow(GovernorCase):
    """End-to-end conservative behavior: a 16-hour phantom is reconciled
    and requeued by a janitor pass, while a young chat-owned worker and
    a compiler-backed build are left strictly alone."""

    def test_phantom_reaped_young_and_build_protected(self):
        phantom = self.enqueue("phantom light", now=T0)["id"]
        self.admit(now=T0)
        gov.do_started(self.store, phantom, "w-phantom", now=T0)
        t1 = T0 + 16 * 3600
        young = self.enqueue("young light", now=t1 - 300)["id"]
        build = self.enqueue("live build", resource="build",
                             now=t1 - 300)["id"]
        out = self.admit(now=t1 - 300)
        self.assertIn(young, out["admitted"])
        self.assertIn(build, out["admitted"])
        gov.do_started(self.store, young, "w-young", now=t1 - 300)
        gov.do_started(self.store, build, "w-build", now=t1 - 300)
        # The phantom is already interrupted by the empty-snapshot pass
        # above (any pass reconciles it); the janitor's contribution is
        # the snapshot that protects the live workers plus the requeue.
        self.assertEqual(self.items()[phantom]["state"], "interrupted")
        snap = gov.build_janitor_snapshot(self.store, now=t1,
                                          compiler_alive=True)
        protected = {a["agent_id"] for a in snap["agents"]}
        self.assertEqual(protected, {"w-young", "w-build"})
        out = gov.do_admit(self.store, snap, now=t1, load1=1.0)
        self.assertEqual(out["interrupted"], [])
        self.assertEqual(self.items()[young]["state"], "running")
        self.assertEqual(self.items()[build]["state"], "running")
        out = gov.do_requeue_interrupted(self.store, now=t1)
        self.assertEqual(out["requeued"], [phantom])
        self.assertEqual(self.items()[phantom]["state"], "queued")


class TestJanitorCli(unittest.TestCase):
    def test_janitor_cli_round_trip(self):
        d = tempfile.mkdtemp(prefix="gov-janitor-cli-")
        try:
            shutil.copy(os.path.join(HERE, "limits.yaml"),
                        os.path.join(d, "limits.yaml"))
            env = dict(os.environ, GOVERNOR_LOAD1="1.0")
            script = os.path.join(HERE, "governor.py")

            def run(*argv):
                return subprocess.run(
                    [sys.executable, script, "--dir", d] + list(argv),
                    capture_output=True, text=True, env=env, timeout=30)

            r = run("enqueue", "--title", "janitor cli job")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            snap_path = os.path.join(d, "janitor-snap.json")
            r = run("janitor-snapshot", "--out", snap_path)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            diag = json.loads(r.stdout)
            self.assertIn("unexplained_compilers", diag)
            with open(snap_path) as f:
                snap = json.load(f)
            self.assertEqual(snap["cron_active"], [])
            r = run("admit", "--snapshot", snap_path, "--block-build")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(json.loads(r.stdout)["blocked_resources"],
                             ["build"])
            r = run("requeue-interrupted")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(json.loads(r.stdout),
                             {"requeued": [], "failed": []})
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestCheckpointParserTolerance(GovernorCase):
    """Parser tolerance (hardening, Oct 21, 2026): the marker is found
    ANYWHERE in the details text, not only at line start — a worker
    buried the marker mid-paragraph and line-start-only parsing
    silently missed it. Binding rules are unchanged."""

    def _cp(self, item_id, ts, state="working"):
        return ('GOVERNOR-CHECKPOINT {"item": "%s", "ts": %s, '
                '"state": "%s"}' % (item_id, ts, state))

    def test_embedded_mid_paragraph_parses(self):
        details = ("The worker is mid-way through the gates run and "
                   + self._cp("item-a", T0, "gates running")
                   + " with more prose after it.\nA second paragraph.")
        cp = gov.parse_checkpoint(details)
        self.assertIsNotNone(cp)
        self.assertEqual(cp["item"], "item-a")
        self.assertEqual(cp["ts"], T0)
        self.assertEqual(cp["state"], "gates running")

    def test_last_occurrence_wins_mixed_positions(self):
        line_start_old = self._cp("item-a", T0, "old") + "\n"
        embedded_new = ("prose " + self._cp("item-a", T0 + 300, "new")
                        + " more prose")
        cp = gov.parse_checkpoint(line_start_old + embedded_new)
        self.assertEqual(cp["ts"], T0 + 300)
        self.assertEqual(cp["state"], "new")
        # And the reverse layout: embedded older, line-start newer.
        embedded_old = "prose " + self._cp("item-a", T0, "old") + "\n"
        line_start_new = self._cp("item-a", T0 + 300, "new")
        cp = gov.parse_checkpoint(embedded_old + line_start_new)
        self.assertEqual(cp["ts"], T0 + 300)
        self.assertEqual(cp["state"], "new")

    def test_malformed_json_after_embedded_marker_ignored(self):
        self.assertIsNone(gov.parse_checkpoint(
            "prose GOVERNOR-CHECKPOINT {not json at all"))
        # A malformed LAST marker does not hide an earlier valid one.
        details = (self._cp("item-a", T0, "good")
                   + "\nprose GOVERNOR-CHECKPOINT {broken")
        cp = gov.parse_checkpoint(details)
        self.assertEqual(cp["state"], "good")


class TestCheckpointLine(GovernorCase):
    """Helper command (hardening, Oct 21, 2026): the checkpoint line's
    item id and timestamp are machine-derived — workers append the
    printed line verbatim and never hand-write either."""

    NOW = T0 + 70 * 60

    def _running_linked(self, title, tracker_ref=72):
        iid = self.enqueue(title, now=T0, tracker_ref=tracker_ref)["id"]
        self.admit(now=T0)
        gov.do_started(self.store, iid, "w-" + iid, now=T0)
        return iid

    def test_line_for_linked_item_round_trips_and_binds(self):
        iid = self._running_linked("generates a line")
        line = gov.do_checkpoint_line(self.store, iid, "gates running",
                                      now=self.NOW)
        self.assertTrue(line.startswith(gov.CHECKPOINT_MARKER + " "))
        self.assertEqual(line.count("\n"), 0)
        cp = gov.parse_checkpoint("human notes\n" + line)
        self.assertEqual(cp["item"], iid)
        self.assertEqual(cp["ts"], self.NOW)
        self.assertEqual(cp["state"], "gates running")
        item = self.items()[iid]
        cps = {iid: {"ts": cp["ts"], "tracker_id": 72,
                     "state": cp["state"]}}
        self.assertTrue(gov._checkpoint_fresh(item, cps, self.NOW + 30))

    def test_state_with_quotes_and_unicode_survives(self):
        iid = self._running_linked("tricky state")
        state = 'fixing "quoted" bits — ünïcode ✓ and \\backslash'
        line = gov.do_checkpoint_line(self.store, iid, state, now=self.NOW)
        cp = gov.parse_checkpoint(line)
        self.assertEqual(cp["state"], state)

    def test_refused_for_unknown_item(self):
        with self.assertRaises(gov.GovernorError):
            gov.do_checkpoint_line(self.store, "no-such-item", "x",
                                   now=self.NOW)

    def test_refused_for_unlinked_item(self):
        iid = self.enqueue("no tracker link", now=T0)["id"]
        with self.assertRaises(gov.GovernorError) as ctx:
            gov.do_checkpoint_line(self.store, iid, "x", now=self.NOW)
        self.assertIn("tracker_ref", str(ctx.exception))

    def test_refused_for_terminal_items(self):
        iid = self._running_linked("will finish")
        gov.do_close(self.store, iid, "done", now=T0 + 10)
        with self.assertRaises(gov.GovernorError):
            gov.do_checkpoint_line(self.store, iid, "x", now=self.NOW)
        iid2 = self._running_linked("will fail", tracker_ref=73)
        gov.do_close(self.store, iid2, "failed", error="boom",
                     now=T0 + 10)
        with self.assertRaises(gov.GovernorError):
            gov.do_checkpoint_line(self.store, iid2, "x", now=self.NOW)

    def test_queued_linked_item_can_generate(self):
        # A worker checkpointing before its first admit window still
        # gets a line; only unknown/unlinked/terminal are refused.
        iid = self.enqueue("queued but linked", now=T0,
                           tracker_ref=72)["id"]
        line = gov.do_checkpoint_line(self.store, iid, "starting",
                                      now=self.NOW)
        self.assertEqual(gov.parse_checkpoint(line)["item"], iid)


class TestCheckpointLineCli(unittest.TestCase):
    def test_checkpoint_line_cli(self):
        d = tempfile.mkdtemp(prefix="gov-cpline-cli-")
        try:
            shutil.copy(os.path.join(HERE, "limits.yaml"),
                        os.path.join(d, "limits.yaml"))
            env = dict(os.environ, GOVERNOR_LOAD1="1.0")
            script = os.path.join(HERE, "governor.py")

            def run(*argv):
                return subprocess.run(
                    [sys.executable, script, "--dir", d] + list(argv),
                    capture_output=True, text=True, env=env, timeout=30)

            r = run("enqueue", "--title", "linked job",
                    "--tracker-ref", "72")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            iid = json.loads(r.stdout)["id"]
            before = time.time()
            r = run("checkpoint-line", "--id", iid, "--state",
                    "cli state")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            line = r.stdout.strip()
            cp = gov.parse_checkpoint(line)
            self.assertEqual(cp["item"], iid)
            self.assertEqual(cp["state"], "cli state")
            self.assertGreaterEqual(cp["ts"], before - 1)
            self.assertLessEqual(cp["ts"], time.time() + 1)
            r = run("enqueue", "--title", "unlinked job")
            iid2 = json.loads(r.stdout)["id"]
            r = run("checkpoint-line", "--id", iid2, "--state", "x")
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("tracker_ref",
                          json.loads(r.stdout)["detail"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestMalformedCheckpoints(GovernorCase):
    """Malformation visibility (hardening, Oct 21, 2026): a running,
    tracker-linked item whose linked entry CONTAINS the marker but
    yields no valid bound checkpoint is flagged — unparseable, wrong
    item name, or absurd timestamp. No marker at all is the ordinary
    stale case and is NOT flagged."""

    NOW = T0 + 70 * 60
    YEAR = 365 * 24 * 3600

    def _running_linked(self, title, tracker_ref):
        iid = self.enqueue(title, now=T0, tracker_ref=tracker_ref)["id"]
        self.admit(now=T0)
        gov.do_started(self.store, iid, "w-" + iid, now=T0)
        return iid

    def _details(self, item_id, ts, state="working"):
        return ('notes\nGOVERNOR-CHECKPOINT {"item": "%s", "ts": %s, '
                '"state": "%s"}' % (item_id, ts, state))

    def _malformed(self, entries):
        return gov.find_malformed_checkpoints(self.store, entries,
                                              now=self.NOW)

    def test_wrong_item_marker_flagged(self):
        iid = self._running_linked("wrong name", 72)
        entries = [{"id": 72,
                    "details": self._details("some-other-item",
                                             self.NOW - 60)}]
        self.assertEqual(self._malformed(entries), [iid])

    def test_year_stale_ts_flagged(self):
        iid = self._running_linked("year stale", 72)
        entries = [{"id": 72,
                    "details": self._details(iid, self.NOW - self.YEAR)}]
        self.assertEqual(self._malformed(entries), [iid])

    def test_unparseable_marker_flagged(self):
        iid = self._running_linked("broken json", 72)
        entries = [{"id": 72,
                    "details": "notes GOVERNOR-CHECKPOINT {oops"}]
        self.assertEqual(self._malformed(entries), [iid])

    def test_wellformed_fresh_not_flagged(self):
        iid = self._running_linked("healthy", 72)
        entries = [{"id": 72,
                    "details": self._details(iid, self.NOW - 60)}]
        self.assertEqual(self._malformed(entries), [])

    def test_no_marker_not_flagged(self):
        self._running_linked("never checkpointed", 72)
        entries = [{"id": 72, "details": "plain human notes only"}]
        self.assertEqual(self._malformed(entries), [])

    def test_marker_in_wrong_entry_not_flagged_for_this_item(self):
        # The marker sits in some OTHER entry; this item's linked
        # entry carries none — ordinary absence, not malformation.
        iid = self._running_linked("clean link", 72)
        entries = [{"id": 72, "details": "nothing here"},
                   {"id": 99,
                    "details": self._details(iid, self.NOW - 60)}]
        self.assertEqual(self._malformed(entries), [])

    def test_queued_item_not_examined(self):
        iid = self.enqueue("queued with bad entry", now=T0,
                           tracker_ref=72)["id"]
        entries = [{"id": 72,
                    "details": self._details("some-other-item",
                                             self.NOW - 60)}]
        self.assertEqual(self._malformed(entries), [])


if __name__ == "__main__":
    unittest.main()
