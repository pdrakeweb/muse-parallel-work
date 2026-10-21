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

Exception: the governor-janitor cron (self-contained mode, Oct 21, 2026)
cannot use subagent.list — cron-lineage sessions verifiably see zero
agents. It builds its snapshot from governor state + /proc evidence via
`janitor-snapshot` / build_janitor_snapshot below, and blocks build
admissions outright when compiler processes are alive that no running
build item explains (`admit --block-build`).

Workers on tracker-linked items (item field `tracker_ref`, the Work
Tracker entry id) also write GOVERNOR-CHECKPOINT heartbeats into that
entry's details at least every 5 minutes. The janitor reads the tracker
entries back, extracts the checkpoints (`extract-checkpoints`), and
passes them to the snapshot: a checkpoint younger than 10 minutes
protects its item exactly like a live sighting.
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


# ---------- janitor support (self-contained mode, Oct 21 2026) ----------

COMPILER_COMMS = ("cargo", "rustc", "clippy-driver")
# Brief hard rules: never reap a light item younger than 60 minutes; the
# same blanket is applied to builds (a conservative superset), and a build
# item of any age is protected while compiler processes are alive.
JANITOR_PROTECT_MINUTES = 60.0
JANITOR_MAX_ATTEMPTS = 2  # DESIGN S10: auto-requeue below this, then fail

# Checkpoint heartbeats (Oct 21, 2026): a worker on a tracker-linked
# item writes a GOVERNOR-CHECKPOINT line into its Work Tracker entry's
# details at least every 5 minutes (see parse_checkpoint for the
# format). A checkpoint younger than CHECKPOINT_FRESH_MINUTES is
# liveness evidence equal to the <60-minute blanket above; a stale or
# missing checkpoint confers nothing, and checkpoints never protect
# terminal items (the snapshot only ever lists running ones).
CHECKPOINT_MARKER = "GOVERNOR-CHECKPOINT"
CHECKPOINT_FRESH_MINUTES = 10.0
CHECKPOINT_SKEW_SECONDS = 60.0  # tolerated future skew on checkpoint ts


def _norm_tracker_ref(ref):
    """Tracker entry ids are integers; accept int-like strings so CLI
    callers can pass them verbatim. Anything else is kept as-is."""
    if ref is None:
        return None
    try:
        return int(ref)
    except (TypeError, ValueError):
        return str(ref)


def parse_checkpoint(details):
    """Extract the last GOVERNOR-CHECKPOINT heartbeat from a Work
    Tracker entry's details text. Format (a single line, conventionally
    the last line of details):
      GOVERNOR-CHECKPOINT {"item": "<governor item id>",
                            "ts": <epoch seconds>,
                            "state": "<one-line state>"}
    Returns {"item", "ts", "state"} or None when absent/malformed."""
    if not details:
        return None
    found = None
    for line in str(details).splitlines():
        line = line.strip()
        if not line.startswith(CHECKPOINT_MARKER):
            continue
        try:
            data = json.loads(line[len(CHECKPOINT_MARKER):].strip())
            found = {"item": str(data["item"]), "ts": float(data["ts"]),
                     "state": str(data.get("state", ""))}
        except Exception:
            continue
    return found


def extract_checkpoints(tracker_items):
    """Map governor item id -> {"ts", "tracker_id", "state"} from Work
    Tracker entries (the item dicts the tracker's listitems action
    returns). When two entries claim the same governor item, the
    freshest checkpoint wins."""
    out = {}
    for entry in tracker_items or []:
        if not isinstance(entry, dict):
            continue
        cp = parse_checkpoint(entry.get("details"))
        if cp is None:
            continue
        prev = out.get(cp["item"])
        if prev is None or cp["ts"] > prev["ts"]:
            out[cp["item"]] = {"ts": cp["ts"],
                               "tracker_id": entry.get("id"),
                               "state": cp["state"]}
    return out


def _checkpoint_fresh(item, checkpoints, now):
    """True when the item's linked tracker entry carries a checkpoint
    naming this item, younger than CHECKPOINT_FRESH_MINUTES. The link
    is the item's tracker_ref: without it the item gets no checkpoint
    protection, and a checkpoint carried by any other entry confers
    nothing."""
    if not checkpoints:
        return False
    ref = item.get("tracker_ref")
    if ref is None:
        return False
    cp = checkpoints.get(item["id"])
    if not cp:
        return False
    if str(cp.get("tracker_id")) != str(ref):
        return False
    try:
        age = now - float(cp["ts"])
    except (TypeError, ValueError):
        return False
    return -CHECKPOINT_SKEW_SECONDS <= age < CHECKPOINT_FRESH_MINUTES * 60


def scan_compiler_processes(proc_root="/proc"):
    """Live compiler processes from /proc (by comm name). Evidence for
    the janitor's guards; best-effort and never raises."""
    found = []
    try:
        names = os.listdir(proc_root)
    except Exception:
        return found
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, name, "comm")) as f:
                comm = f.read().strip()
        except Exception:
            continue
        if comm in COMPILER_COMMS:
            found.append({"pid": int(name), "comm": comm})
    return found


def build_janitor_snapshot(store, now=None, compiler_alive=False,
                           live_agent_ids=(), checkpoints=None):
    """Admission snapshot for the janitor, derived ONLY from governor
    state plus /proc evidence — never from subagent.list (cron-lineage
    sessions see zero agents; visibility tests Oct 19 and Oct 21, 2026).

    A running item's worker is reported "running" (protected from
    reconciliation) when ANY of these hold:
      - the item started less than JANITOR_PROTECT_MINUTES ago;
      - it is a build item and compiler processes are alive anywhere on
        the VM (they cannot be attributed to one item, so every build
        is protected);
      - its worker id is in live_agent_ids (explicit liveness evidence);
      - its linked Work Tracker entry carries a checkpoint younger
        than CHECKPOINT_FRESH_MINUTES (checkpoints: the map produced
        by extract_checkpoints; see _checkpoint_fresh for the binding
        rules). Only running items are ever listed, so a checkpoint
        can never protect a terminal item or block a close-out.
    Otherwise the worker is omitted, and the ordinary reconcile rules
    (running-absent TTL) decide. cron_active is always empty: cron
    reservations are declared by chat passes that can see the crons;
    janitor admissions are bounded by state-derived usage, the load
    circuit-breaker, and the per-tick spawn cap in the janitor's body."""
    now = time.time() if now is None else now
    live = set(live_agent_ids or ())
    agents = []
    for it in store.state["items"].values():
        if it["state"] != "running":
            continue
        wid = it.get("worker_agent_id")
        if not wid:
            continue
        young = (now - (it.get("started_at") or 0)) \
            < JANITOR_PROTECT_MINUTES * 60
        if young or (it["resource"] == "build" and compiler_alive) \
                or wid in live or _checkpoint_fresh(it, checkpoints, now):
            agents.append({"agent_id": wid, "status": "running"})
    return {"agents": agents, "cron_active": []}


def do_requeue_interrupted(store, now=None, max_attempts=JANITOR_MAX_ATTEMPTS):
    """DESIGN S10 standing rule for interrupted items: below the attempt
    cap an interrupted item returns to queued at its original class; at
    the cap it is failed, so the item reaches a terminal state its owner
    can see instead of sitting interrupted forever."""
    now = time.time() if now is None else now
    store.acquire_lock()
    try:
        requeued, failed = [], []
        for it in store.state["items"].values():
            if it["state"] != "interrupted":
                continue
            it["worker_agent_id"] = None
            it["started_at"] = None
            if int(it.get("attempts", 0)) < int(max_attempts):
                it["state"] = "queued"
                it["finished_at"] = None
                _emit(store, "requeue", it, now,
                      {"via": "janitor_interrupted"})
                requeued.append(it["id"])
            else:
                it["state"] = "failed"
                it["finished_at"] = now
                it["last_error"] = ((it.get("last_error") or "")
                                    + " [janitor: max attempts reached]"
                                    ).strip()
                _emit(store, "failed", it, now,
                      {"via": "janitor_max_attempts"})
                failed.append(it["id"])
        store.state["updated_at"] = now_iso(now)
        store.save_state()
        return {"requeued": requeued, "failed": failed}
    finally:
        store.release_lock()


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
               tracker_ref=None,
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
            "tracker_ref": _norm_tracker_ref(tracker_ref),
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


def do_admit(store, snapshot, now=None, load1=None,
             block_resources=frozenset()):
    """One admission pass. Returns a result dict; the caller spawns the
    admitted items outside this call. Resources in block_resources are
    treated as fully occupied for this pass (the janitor's unexplained-
    compiler guard blocks "build" this way) while other resources are
    admitted normally."""
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
                if it["resource"] in block_resources:
                    continue
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
                  "blocked_resources": sorted(block_resources),
                  "load1": load1, "duration_s": round(duration, 6)}
        store.record_pass(duration, len(admitted))
        return result
    finally:
        store.release_lock()


def do_started(store, item_id, worker_agent_id, now=None, tracker_ref=None):
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
        if tracker_ref is not None:
            it["tracker_ref"] = _norm_tracker_ref(tracker_ref)
        _emit(store, "start", it, now)
        store.state["updated_at"] = now_iso(now)
        store.save_state()
        return {"id": item_id, "state": "running"}
    finally:
        store.release_lock()


def do_set_tracker_ref(store, item_id, tracker_ref, now=None):
    """Retrofit (Oct 21, 2026): set/refresh an existing item's Work
    Tracker link after enqueue — the checkpoint build made tracker_ref
    settable only at enqueue/started, but items enqueued before their
    tracker entry existed (e.g. the two LLMdil relaunches) had no way
    to gain checkpoint protection. Allowed on queued/admitted/running
    items; terminal items are refused (their checkpoints could never
    be read, and rewriting a closed record would corrupt the log's
    meaning). Goes through the normal event/state machinery, so the
    value survives a rebuild like every other field."""
    now = time.time() if now is None else now
    store.acquire_lock()
    try:
        it = store.state["items"].get(item_id)
        if it is None:
            raise GovernorError("unknown item %s" % item_id)
        if it["state"] not in ("queued", "admitted", "running"):
            raise GovernorError(
                "item %s is %s; tracker_ref can only be set on a "
                "queued, admitted or running item"
                % (item_id, it["state"]))
        it["tracker_ref"] = _norm_tracker_ref(tracker_ref)
        _emit(store, "set_tracker_ref", it, now,
              {"tracker_ref": it["tracker_ref"]})
        store.state["updated_at"] = now_iso(now)
        store.save_state()
        return {"id": item_id, "state": it["state"],
                "tracker_ref": it["tracker_ref"]}
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
    p.add_argument("--tracker-ref",
                   help="Work Tracker entry id this item is linked to "
                        "(enables checkpoint protection)")
    p.add_argument("--fanout", type=int, default=1)

    p = sub.add_parser("admit")
    p.add_argument("--snapshot", help="JSON snapshot file from subagent.list "
                                     "('-' for stdin); omit for an empty snapshot")
    p.add_argument("--block-build", action="store_true",
                   help="treat build capacity as fully occupied this pass "
                        "(janitor unexplained-compiler guard)")

    p = sub.add_parser("janitor-snapshot")
    p.add_argument("--out", help="also write the snapshot JSON to this file")
    p.add_argument("--checkpoints",
                   help="JSON file of extracted checkpoints (output of "
                        "extract-checkpoints); fresh checkpoints protect "
                        "their running items")

    p = sub.add_parser("extract-checkpoints")
    p.add_argument("--tracker-json", required=True,
                   help="file holding the Work Tracker listitems result "
                        "JSON (or a bare items list)")
    p.add_argument("--out", help="also write the checkpoints map JSON "
                                 "to this file")

    sub.add_parser("requeue-interrupted")

    p = sub.add_parser("started")
    p.add_argument("--id", required=True)
    p.add_argument("--worker", required=True)
    p.add_argument("--tracker-ref",
                   help="set/refresh the item's Work Tracker entry link")

    p = sub.add_parser("set-tracker-ref")
    p.add_argument("--id", required=True)
    p.add_argument("--tracker-ref", required=True,
                   help="Work Tracker entry id to link to an existing "
                        "queued/admitted/running item")

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
                             tracker_ref=args.tracker_ref,
                             fanout=args.fanout)
            print(json.dumps(out))
        elif args.cmd == "admit":
            snapshot = _load_snapshot(args.snapshot)
            load_override = os.environ.get("GOVERNOR_LOAD1")
            blocked = frozenset(["build"]) if args.block_build else frozenset()
            out = do_admit(store, snapshot,
                           load1=float(load_override) if load_override else None,
                           block_resources=blocked)
            print(json.dumps(out))
        elif args.cmd == "janitor-snapshot":
            compilers = scan_compiler_processes()
            checkpoints = None
            if args.checkpoints:
                with open(args.checkpoints) as f:
                    checkpoints = json.load(f)
            snap = build_janitor_snapshot(
                store, compiler_alive=bool(compilers),
                checkpoints=checkpoints)
            if args.out:
                with open(args.out, "w") as f:
                    f.write(json.dumps(snap) + "\n")
            running_builds = [it["id"] for it in store.state["items"].values()
                              if it["state"] == "running"
                              and it["resource"] == "build"]
            checkpoint_protected = sorted(
                it["id"] for it in store.state["items"].values()
                if it["state"] == "running"
                and _checkpoint_fresh(it, checkpoints, time.time()))
            print(json.dumps({
                "snapshot": snap,
                "compiler_processes": compilers,
                "running_build_items": running_builds,
                "unexplained_compilers": bool(compilers) and not running_builds,
                "checkpoint_protected": checkpoint_protected,
            }))
        elif args.cmd == "extract-checkpoints":
            with open(args.tracker_json) as f:
                raw = json.load(f)
            entries = raw
            if isinstance(raw, dict):
                entries = (raw.get("data") or {}).get("items") \
                    or raw.get("items") or []
            checkpoints = extract_checkpoints(entries)
            if args.out:
                with open(args.out, "w") as f:
                    f.write(json.dumps(checkpoints) + "\n")
            print(json.dumps({"checkpoints": checkpoints,
                              "count": len(checkpoints)}))
        elif args.cmd == "requeue-interrupted":
            print(json.dumps(do_requeue_interrupted(store)))
        elif args.cmd == "started":
            print(json.dumps(do_started(store, args.id, args.worker,
                                        tracker_ref=args.tracker_ref)))
        elif args.cmd == "set-tracker-ref":
            print(json.dumps(do_set_tracker_ref(store, args.id,
                                                args.tracker_ref)))
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
