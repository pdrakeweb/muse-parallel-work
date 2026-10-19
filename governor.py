#!/usr/bin/env python3
"""Parallelism governor for this Muse VM (DESIGN.md, as modified by the
Oct 18, 2026 council verdict).

Stdlib only. The queue is files under this directory:
  queue.jsonl  append-only event log, source of truth
  state.json   materialized state, atomically rewritten, rebuildable
  briefs/      one self-contained brief file per item
  metrics.jsonl  admission-pass durations (drives the lock-steal threshold)
  limits.yaml  every number

The script cannot see live workers by itself: the calling agent obtains a
snapshot from subagent.list and passes it to `admit` as JSON:
  {"agents": [{"agent_id": "...", "status": "running|done|failed|..."}],
   "cron_active": ["email_triage", "camera_sweep"]}
"""
import argparse
import json
import math
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLASS_RANK = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
TERMINAL = ("done", "failed", "cancelled")


class GovernorError(Exception):
    pass


class LockBusy(GovernorError):
    pass


# ---------- limits (tiny parser for the flat two-level limits.yaml) ----------

def _coerce(v):
    v = v.strip()
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v.strip('"').strip("'")


def load_limits(path):
    limits = {}
    section = None
    with open(path) as f:
        for raw in f:
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            if not line.startswith(" "):
                section = line.rstrip(":").strip()
                limits[section] = {}
            else:
                key, _, val = line.strip().partition(":")
                limits[section][key.strip()] = _coerce(val)
    return limits


def lim(limits, section, key, default=None):
    return limits.get(section, {}).get(key, default)


# ---------- small helpers ----------

def now_iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def slugify(title):
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (s[:40] or "item").rstrip("-")


def read_loadavg():
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def percentile(values, pct):
    if not values:
        return None
    vs = sorted(values)
    idx = max(0, math.ceil(pct / 100.0 * len(vs)) - 1)
    return vs[idx]


# ---------- store ----------

class Store:
    def __init__(self, base_dir=None):
        self.dir = base_dir or BASE_DIR
        self.limits_path = os.path.join(self.dir, "limits.yaml")
        self.queue_path = os.path.join(self.dir, "queue.jsonl")
        self.state_path = os.path.join(self.dir, "state.json")
        self.prev_path = os.path.join(self.dir, "state.prev.json")
        self.metrics_path = os.path.join(self.dir, "metrics.jsonl")
        self.briefs_dir = os.path.join(self.dir, "briefs")
        self.lock_dir = os.path.join(self.dir, "lock")
        self.limits = load_limits(self.limits_path)
        self._lock_depth = 0
        os.makedirs(self.briefs_dir, exist_ok=True)
        for p in (self.queue_path, self.metrics_path):
            if not os.path.exists(p):
                open(p, "a").close()
        self.state = self._load_state()

    # ----- state io -----
    def _empty_state(self):
        return {"version": 1, "items": {}, "updated_at": None}

    def _load_state(self):
        try:
            with open(self.state_path) as f:
                st = json.load(f)
            if isinstance(st, dict) and "items" in st:
                return st
        except Exception:
            pass
        return self.rebuild()

    def save_state(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(self.state_path):
            try:
                shutil.copyfile(self.state_path, self.prev_path)
            except Exception:
                pass
        os.replace(tmp, self.state_path)

    def append_event(self, event):
        with open(self.queue_path, "a") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read_events(self):
        events = []
        with open(self.queue_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    break  # torn final line: damage bounded to it (design S10)
        return events

    def rebuild(self):
        items = {}
        for ev in self.read_events():
            item = ev.get("item")
            if item and item.get("id"):
                items[item["id"]] = item
        self.state = {"version": 1, "items": items, "updated_at": None}
        return self.state

    # ----- metrics / lock-steal threshold -----
    def pass_durations(self):
        vals = []
        try:
            with open(self.metrics_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            vals.append(float(json.loads(line)["duration_s"]))
                        except Exception:
                            continue
        except FileNotFoundError:
            pass
        maxn = int(lim(self.limits, "metrics", "max_pass_samples", 200))
        return vals[-maxn:]

    def record_pass(self, duration_s, admitted_count):
        rec = {"ts": now_iso(time.time()), "duration_s": round(duration_s, 6),
               "admitted": admitted_count}
        with open(self.metrics_path, "a") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def steal_threshold(self):
        """Council modification 3: from measured pass durations
        (a multiple of observed p95, with a sane floor). Until enough
        passes are measured, the floor alone applies."""
        floor = float(lim(self.limits, "lock_steal", "floor_seconds", 5))
        mult = float(lim(self.limits, "lock_steal", "p95_multiple", 10))
        min_samples = int(lim(self.limits, "lock_steal", "min_samples", 5))
        durations = self.pass_durations()
        if len(durations) < min_samples:
            return floor
        p95 = percentile(durations, 95)
        return max(floor, mult * p95)

    # ----- lock -----
    def acquire_lock(self, wait_seconds=None):
        if self._lock_depth > 0:  # reentrant within one process (fan-out enqueue)
            self._lock_depth += 1
            return
        if wait_seconds is None:
            wait_seconds = float(lim(self.limits, "lock", "wait_seconds", 10))
        retry = float(lim(self.limits, "lock", "retry_interval_seconds", 0.1))
        deadline = time.time() + wait_seconds
        while True:
            try:
                os.mkdir(self.lock_dir)
                with open(os.path.join(self.lock_dir, "holder.json"), "w") as f:
                    json.dump({"pid": os.getpid(), "ts": time.time()}, f)
                self._lock_depth = 1
                return
            except FileExistsError:
                holder_ts = None
                try:
                    with open(os.path.join(self.lock_dir, "holder.json")) as f:
                        holder_ts = float(json.load(f)["ts"])
                except Exception:
                    try:
                        holder_ts = os.path.getmtime(self.lock_dir)
                    except OSError:
                        continue
                age = time.time() - holder_ts
                if age > self.steal_threshold():
                    shutil.rmtree(self.lock_dir, ignore_errors=True)
                    continue
                if time.time() >= deadline:
                    raise LockBusy(
                        "governor lock held (age %.1fs, steal threshold %.1fs)"
                        % (age, self.steal_threshold()))
                time.sleep(retry)

    def release_lock(self):
        if self._lock_depth > 1:
            self._lock_depth -= 1
            return
        self._lock_depth = 0
        shutil.rmtree(self.lock_dir, ignore_errors=True)


# ---------- priority ----------

def effective_rank(item, now, limits):
    """Aging: +1 class per aging interval queued, never above the cap
    class, and never demoting a class already at/above the cap."""
    base = CLASS_RANK[item["class"]]
    cap = CLASS_RANK[lim(limits, "aging", "cap_class", "P2")]
    if base <= cap:
        return base
    step_min = float(lim(limits, "aging", "promote_after_minutes", 60))
    waited_min = max(0.0, (now - item["enqueued_at"]) / 60.0)
    steps = int(waited_min // step_min) if step_min > 0 else 0
    return max(cap, base - steps)


def rank_name(rank):
    for name, r in CLASS_RANK.items():
        if r == rank:
            return name
    return "P?"


def promotion_key(item, now, limits):
    """Council modification 2: (effective class, base class, enqueue time).
    An aged P4 must not precede a fresh P2 at equal effective class."""
    return (effective_rank(item, now, limits),
            CLASS_RANK[item["class"]],
            item["enqueued_at"],
            item["id"])


def deps_done(item, items):
    for dep in item.get("depends_on", []):
        other = items.get(dep)
        if other is None or other["state"] != "done":
            return False
    return True


def usage(items, snapshot, limits):
    """Slots consumed by admitted/running items plus cron reservations
    declared in the snapshot (light-equivalents, design S8)."""
    u = {"build": 0, "light": 0, "total": 0}
    for it in items.values():
        if it["state"] in ("admitted", "running"):
            u[it["resource"]] += 1
            u["total"] += 1
    cron_table = limits.get("cron_reservations", {})
    for name in (snapshot or {}).get("cron_active", []) or []:
        n = int(cron_table.get(name, 0))
        u["light"] += n
        u["total"] += n
    return u


def fits(item, u, limits):
    if u["total"] + 1 > int(lim(limits, "budgets", "total_workers", 5)):
        return False
    if item["resource"] == "build":
        return u["build"] + 1 <= int(lim(limits, "budgets", "build_slots", 1))
    return u["light"] + 1 <= int(lim(limits, "budgets", "light_slots", 4))


# ---------- operations (each assumes the caller holds the lock, except
# enqueue/admit/started/done/fail which take it themselves) ----------

def _emit(store, event_type, item, now, extra=None):
    ev = {"ts": now_iso(now), "event": event_type, "item": dict(item)}
    if extra:
        ev.update(extra)
    store.append_event(ev)


def _new_id(title, now, items):
    dt = datetime.fromtimestamp(now).astimezone()
    base = "%s-%s-%s" % (dt.strftime("%Y%m%d-%H%M%S"), slugify(title),
                         uuid.uuid4().hex[:4])
    ident, n = base, 2
    while ident in items:
        ident = "%s-%d" % (base, n)
        n += 1
    return ident


def do_enqueue(store, title, cls="P3", resource="light", owner_chat_id=None,
               owner_chat_name=None, brief_text=None, brief_file=None,
               resume_pointer=None, depends_on=None, idempotency_key=None,
               fanout=1, now=None, _bundle=None, _bundle_index=None):
    now = time.time() if now is None else now
    if cls not in CLASS_RANK or cls == "P1":
        raise GovernorError("class must be P2, P3 or P4 (P1 bypasses the queue)")
    if resource not in ("build", "light"):
        raise GovernorError("resource must be build or light")
    store.acquire_lock()
    try:
        items = store.state["items"]
        if idempotency_key:
            for it in items.values():
                if (it.get("idempotency_key") == idempotency_key
                        and it["state"] in ("queued", "admitted", "running")):
                    pos = queue_position(store, it["id"], now)
                    return {"id": it["id"], "position": pos, "duplicate": True}
        if fanout and int(fanout) > 1 and _bundle is None:
            parent = do_enqueue(
                store, title, cls=cls, resource="light",
                owner_chat_id=owner_chat_id, owner_chat_name=owner_chat_name,
                brief_text=brief_text, brief_file=brief_file,
                resume_pointer=resume_pointer, depends_on=depends_on,
                idempotency_key=idempotency_key, fanout=int(fanout), now=now,
                _bundle="self")
            children = []
            for i in range(int(fanout)):
                child = do_enqueue(
                    store, "%s (child %d of %d)" % (title, i + 1, int(fanout)),
                    cls=cls, resource=resource, owner_chat_id=owner_chat_id,
                    owner_chat_name=owner_chat_name, brief_text=brief_text,
                    resume_pointer=resume_pointer, depends_on=depends_on,
                    now=now, _bundle=parent["id"], _bundle_index=i)
                children.append(child["id"])
            return {"id": parent["id"], "bundle": parent["id"],
                    "children": children,
                    "position": queue_position(store, parent["id"], now)}
        ident = _new_id(title, now, items)
        brief_path = os.path.join(store.briefs_dir, ident + ".md")
        text = brief_text
        if text is None and brief_file:
            with open(brief_file) as f:
                text = f.read()
        if text is None:
            text = "# %s\n\n(brief not supplied at enqueue)\n" % title
        with open(brief_path, "w") as f:
            f.write(text)
        item = {
            "id": ident, "title": title, "class": cls, "resource": resource,
            "owner_chat_id": owner_chat_id, "owner_chat_name": owner_chat_name,
            "brief": os.path.relpath(brief_path, store.dir),
            "resume_pointer": resume_pointer,
            "depends_on": list(depends_on or []),
            "idempotency_key": idempotency_key,
            "fanout": int(fanout) if _bundle == "self" else 1,
            "bundle": None if _bundle in (None, "self") else _bundle,
            "bundle_parent": _bundle == "self",
            "bundle_index": _bundle_index,
            "state": "queued",
            "enqueued_at": now, "enqueued_iso": now_iso(now),
            "admitted_at": None, "started_at": None, "finished_at": None,
            "worker_agent_id": None, "attempts": 0, "last_error": None,
        }
        items[ident] = item
        _emit(store, "enqueue", item, now)
        store.state["updated_at"] = now_iso(now)
        store.save_state()
        return {"id": ident, "position": queue_position(store, ident, now)}
    finally:
        store.release_lock()


def queue_position(store, item_id, now):
    items = store.state["items"]
    queued = [it for it in items.values() if it["state"] == "queued"]
    queued.sort(key=lambda it: promotion_key(it, now, store.limits))
    for i, it in enumerate(queued, 1):
        if it["id"] == item_id:
            return i
    return None


def _bundle_autocomplete(store, now, events_out):
    items = store.state["items"]
    for it in list(items.values()):
        if not it.get("bundle_parent") or it["state"] not in ("admitted", "running"):
            continue
        children = [c for c in items.values() if c.get("bundle") == it["id"]]
        if children and all(c["state"] in TERMINAL for c in children):
            it["state"] = "done"
            it["finished_at"] = now
            _emit(store, "done", it, now, {"via": "bundle_autocomplete"})
            events_out.append(("bundle_done", it["id"]))


def _reconcile(store, snapshot, now):
    """Compare state against the caller-supplied worker snapshot.
    Returns (interrupted_ids, requeued_ids)."""
    interrupted, requeued = [], []
    items = store.state["items"]
    agents = {}
    for a in (snapshot or {}).get("agents", []) or []:
        aid = a.get("agent_id") or a.get("id")
        if aid:
            agents[aid] = (a.get("status") or "").lower()
    stale_min = float(lim(store.limits, "reservations",
                          "admitted_stale_minutes", 5))
    absent_min = float(lim(store.limits, "reservations",
                           "running_absent_minutes", 10))
    for it in items.values():
        wid = it.get("worker_agent_id")
        if it["state"] == "running":
            status = agents.get(wid) if wid else None
            if status in ("done", "completed"):
                it["state"] = "done"
                it["finished_at"] = now
                _emit(store, "done", it, now, {"via": "reconcile"})
            elif status in ("failed", "interrupted", "unknown"):
                it["state"] = "interrupted"
                it["finished_at"] = now
                it["last_error"] = "worker reported %s" % status
                _emit(store, "interrupt", it, now, {"via": "reconcile"})
                interrupted.append(it["id"])
            elif wid not in agents and it.get("started_at") is not None \
                    and (now - it["started_at"]) > absent_min * 60:
                it["state"] = "interrupted"
                it["finished_at"] = now
                it["last_error"] = "worker absent from snapshot"
                _emit(store, "interrupt", it, now, {"via": "reconcile_absent"})
                interrupted.append(it["id"])
        elif it["state"] == "admitted":
            live = wid is not None and agents.get(wid) == "running"
            if not live and it.get("admitted_at") is not None \
                    and (now - it["admitted_at"]) > stale_min * 60:
                it["state"] = "queued"
                it["admitted_at"] = None
                it["worker_agent_id"] = None
                _emit(store, "requeue", it, now, {"via": "stale_reservation"})
                requeued.append(it["id"])
    return interrupted, requeued


def do_admit(store, snapshot, now=None, load1=None):
    """One admission pass. Returns a result dict; the caller spawns the
    admitted items outside this call."""
    started = time.monotonic()
    now = time.time() if now is None else now
    load1 = read_loadavg() if load1 is None else load1
    store.acquire_lock()
    try:
        interrupted, requeued = _reconcile(store, snapshot, now)
        _bundle_autocomplete(store, now, [])
        threshold = float(lim(store.limits, "circuit_breaker",
                               "loadavg_threshold", 5.0))
        breaker = load1 > threshold
        admitted = []
        if not breaker:
            u = usage(store.state["items"], snapshot, store.limits)
            queued = [it for it in store.state["items"].values()
                      if it["state"] == "queued"
                      and deps_done(it, store.state["items"])]
            queued.sort(key=lambda it: promotion_key(it, now, store.limits))
            for it in queued:  # skip items that do not fit; never block behind one
                if fits(it, u, store.limits):
                    it["state"] = "admitted"
                    it["admitted_at"] = now
                    it["attempts"] += 1
                    u[it["resource"]] += 1
                    u["total"] += 1
                    _emit(store, "admit", it, now)
                    admitted.append(it["id"])
        store.state["updated_at"] = now_iso(now)
        store.save_state()
        duration = time.monotonic() - started
        result = {"admitted": admitted, "interrupted": interrupted,
                  "requeued": requeued, "circuit_breaker": breaker,
                  "load1": load1, "duration_s": round(duration, 6)}
        store.record_pass(duration, len(admitted))
        return result
    finally:
        store.release_lock()


def do_started(store, item_id, worker_agent_id, now=None):
    now = time.time() if now is None else now
    store.acquire_lock()
    try:
        it = store.state["items"].get(item_id)
        if it is None:
            raise GovernorError("unknown item %s" % item_id)
        if it["state"] != "admitted":
            raise GovernorError("item %s is %s, not admitted"
                                % (item_id, it["state"]))
        it["state"] = "running"
        it["worker_agent_id"] = worker_agent_id
        it["started_at"] = now
        _emit(store, "start", it, now)
        store.state["updated_at"] = now_iso(now)
        store.save_state()
        return {"id": item_id, "state": "running"}
    finally:
        store.release_lock()


def do_close(store, item_id, outcome, error=None, now=None):
    now = time.time() if now is None else now
    store.acquire_lock()
    try:
        it = store.state["items"].get(item_id)
        if it is None:
            raise GovernorError("unknown item %s" % item_id)
        if it["state"] in TERMINAL:
            raise GovernorError("item %s already %s" % (item_id, it["state"]))
        it["state"] = outcome
        it["finished_at"] = now
        if error:
            it["last_error"] = error
        _emit(store, outcome, it, now)
        _bundle_autocomplete(store, now, [])
        store.state["updated_at"] = now_iso(now)
        store.save_state()
        return {"id": item_id, "state": outcome}
    finally:
        store.release_lock()


# ---------- status ----------

def status_text(store, now=None, load1=None):
    now = time.time() if now is None else now
    load1 = read_loadavg() if load1 is None else load1
    limits = store.limits
    items = store.state["items"]
    u = usage(items, None, limits)
    lines = []
    lines.append("Parallelism governor status (%s)" % now_iso(now))
    lines.append("Limits: build %d, light %d, total %d | load1 %.2f "
                 "(circuit breaker at %.1f) | aging +1 class per %s min, cap %s"
                 % (lim(limits, "budgets", "build_slots", 1),
                    lim(limits, "budgets", "light_slots", 4),
                    lim(limits, "budgets", "total_workers", 5),
                    load1,
                    float(lim(limits, "circuit_breaker", "loadavg_threshold", 5.0)),
                    lim(limits, "aging", "promote_after_minutes", 60),
                    lim(limits, "aging", "cap_class", "P2")))
    gates = limits.get("gates", {})
    lines.append("Gates: janitor cron %s; interim AGENTS.md cap %s"
                 % ("ACTIVE" if gates.get("janitor_cron_active") else "NOT active",
                    "IN FORCE" if gates.get("interim_cap_in_force") else "retired"))
    active = [it for it in items.values() if it["state"] in ("admitted", "running")]
    active.sort(key=lambda it: (it["state"], it.get("started_at") or 0))
    lines.append("Running/admitted (%d workers: build %d/%d, light %d/%d, total %d/%d):"
                 % (len(active), u["build"], lim(limits, "budgets", "build_slots", 1),
                    u["light"], lim(limits, "budgets", "light_slots", 4),
                    u["total"], lim(limits, "budgets", "total_workers", 5)))
    for it in active:
        since = it.get("started_at") or it.get("admitted_at") or now
        lines.append("  - [%s %s %s] %s — %s — %d min — %s"
                     % (it["state"], it["class"], it["resource"], it["title"],
                        it["id"], max(0, int((now - since) / 60)),
                        it.get("owner_chat_name") or it.get("owner_chat_id") or "?"))
    if not active:
        lines.append("  (none)")
    queued = [it for it in items.values() if it["state"] == "queued"]
    queued.sort(key=lambda it: promotion_key(it, now, limits))
    lines.append("Queued (%d, in promotion order):" % len(queued))
    for i, it in enumerate(queued, 1):
        eff = rank_name(effective_rank(it, now, limits))
        blocked = "" if deps_done(it, items) else " [blocked: deps]"
        lines.append("  %d. [%s eff %s %s] %s — %s%s"
                     % (i, it["class"], eff, it["resource"], it["title"],
                        it["id"], blocked))
    if not queued:
        lines.append("  (none)")
    durations = store.pass_durations()
    if durations:
        recent = ", ".join("%.3fs" % d for d in durations[-5:])
        lines.append("Recent admission passes: %s | p95 %.3fs | lock-steal "
                     "threshold %.1fs"
                     % (recent, percentile(durations, 95),
                        store.steal_threshold()))
    else:
        lines.append("Recent admission passes: none recorded yet | lock-steal "
                     "threshold %.1fs (floor; no measured passes)"
                     % store.steal_threshold())
    done24 = sum(1 for it in items.values()
                 if it["state"] == "done" and it.get("finished_at")
                 and now - it["finished_at"] < 86400)
    failed = sum(1 for it in items.values() if it["state"] == "failed")
    lines.append("Done in last 24h: %d | Failed (all time): %d" % (done24, failed))
    return "\n".join(lines)


# ---------- CLI ----------

def _load_snapshot(path):
    if path is None:
        return {"agents": [], "cron_active": []}
    raw = sys.stdin.read() if path == "-" else open(path).read()
    data = json.loads(raw)
    if isinstance(data, list):
        return {"agents": data, "cron_active": []}
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(prog="governor.py",
                                 description="Parallelism governor (DESIGN.md)")
    ap.add_argument("--dir", default=BASE_DIR, help="governor directory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("enqueue")
    p.add_argument("--title", required=True)
    p.add_argument("--class", dest="cls", default="P3", choices=["P2", "P3", "P4"])
    p.add_argument("--resource", default="light", choices=["build", "light"])
    p.add_argument("--owner-chat-id")
    p.add_argument("--owner-chat-name")
    p.add_argument("--brief-text")
    p.add_argument("--brief-file")
    p.add_argument("--resume-pointer")
    p.add_argument("--depends-on", action="append", default=[])
    p.add_argument("--idempotency-key")
    p.add_argument("--fanout", type=int, default=1)

    p = sub.add_parser("admit")
    p.add_argument("--snapshot", help="JSON snapshot file from subagent.list "
                                     "('-' for stdin); omit for an empty snapshot")

    p = sub.add_parser("started")
    p.add_argument("--id", required=True)
    p.add_argument("--worker", required=True)

    p = sub.add_parser("done")
    p.add_argument("--id", required=True)

    p = sub.add_parser("fail")
    p.add_argument("--id", required=True)
    p.add_argument("--error")

    sub.add_parser("status")
    sub.add_parser("rebuild")

    args = ap.parse_args(argv)
    store = Store(args.dir)
    try:
        if args.cmd == "enqueue":
            out = do_enqueue(store, args.title, cls=args.cls,
                             resource=args.resource,
                             owner_chat_id=args.owner_chat_id,
                             owner_chat_name=args.owner_chat_name,
                             brief_text=args.brief_text,
                             brief_file=args.brief_file,
                             resume_pointer=args.resume_pointer,
                             depends_on=args.depends_on,
                             idempotency_key=args.idempotency_key,
                             fanout=args.fanout)
            print(json.dumps(out))
        elif args.cmd == "admit":
            snapshot = _load_snapshot(args.snapshot)
            load_override = os.environ.get("GOVERNOR_LOAD1")
            out = do_admit(store, snapshot,
                           load1=float(load_override) if load_override else None)
            print(json.dumps(out))
        elif args.cmd == "started":
            print(json.dumps(do_started(store, args.id, args.worker)))
        elif args.cmd == "done":
            print(json.dumps(do_close(store, args.id, "done")))
        elif args.cmd == "fail":
            print(json.dumps(do_close(store, args.id, "failed",
                                      error=args.error)))
        elif args.cmd == "status":
            load_override = os.environ.get("GOVERNOR_LOAD1")
            print(status_text(store,
                              load1=float(load_override) if load_override else None))
        elif args.cmd == "rebuild":
            store.acquire_lock()
            try:
                store.rebuild()
                store.state["updated_at"] = now_iso(time.time())
                store.save_state()
            finally:
                store.release_lock()
            print(json.dumps({"rebuilt_items": len(store.state["items"])}))
        return 0
    except LockBusy as e:
        print(json.dumps({"error": "lock_busy", "detail": str(e)}))
        return 3
    except GovernorError as e:
        print(json.dumps({"error": "governor", "detail": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
