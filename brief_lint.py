#!/usr/bin/env python3
"""brief_lint.py — reject governor briefs whose output becomes an artifact/UI
but lack the user's standing artifact instruction (AGENTS.md §2).

Usage:
  brief_lint.py BRIEF [BRIEF ...] [--fix] [--artifact | --no-artifact] [--quiet]

How a brief is classified (first match wins):
  1. --artifact / --no-artifact flag.
  2. Frontmatter `output_type:` / `output:` value in ARTIFACT_TYPES -> in scope;
     any other value -> not in scope.
  3. No frontmatter hint -> keyword heuristic over the body (artifact, dashboard,
     UI, widget, wishlist, page, tracker, infographic). Errs toward "in scope",
     matching the rule "never dispatch a brief whose output becomes an
     artifact/UI without that line".

Exit codes: 0 = all briefs pass or are out of scope; 1 = at least one in-scope
brief lacks the standing line (after --fix, 0 if every missing one was fixed);
2 = usage or I/O error.

INTEGRATION (governor.py do_enqueue): the check runs on the resolved
brief text, after the --allow-no-brief placeholder fallback and before
the brief file is written or any queue event emitted, so a rejection
creates nothing:

    from brief_lint import lint_text

    if not brief_placeholder:
        ok, reason = lint_text(text)
        if not ok:
            raise GovernorError(
                "enqueue rejected: %s (fix: brief_lint.py --fix <brief>)"
                % reason)

Reservation children share their parent's brief by reference and are
never linted separately; the parent's brief is linted once.

Stdlib only; Python 3.8+.
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path
from typing import Dict, Optional, Tuple

STANDING_LINE = (
    "Standing instruction from the user (Nov 8 and Nov 13, 2026): artifacts are primarily "
    "infographic/chart/table, minimal narrative — lead with numbers as visualizations; "
    "prose only as short labels, captions, and footnotes; convert prose findings into "
    "charts/tables rather than embedding paragraphs."
)
FIX_BLOCK = f"\n\n## Standing instruction\n\n{STANDING_LINE}\n"

ARTIFACT_TYPES = {
    "artifact", "ui", "page", "webpage", "web-page", "dashboard", "tracker",
    "widget", "wishlist", "tool", "infographic", "report-artifact", "app",
}
KEYWORDS = re.compile(
    r"\b(artifacts?|dashboards?|web ?pages?|landing pages?|UI|widgets?|wishlists?|"
    r"infographics?|trackers?)\b",
    re.IGNORECASE,
)

_DASHES = dict.fromkeys(map(ord, "‒–—―−"), "-")
_QUOTES = {ord("‘"): "'", ord("’"): "'", ord("“"): '"', ord("”"): '"'}


def _normalize(text: str) -> str:
    """Make the presence check robust to dash/quote/whitespace/case drift."""
    text = unicodedata.normalize("NFKC", text).translate(_DASHES).translate(_QUOTES)
    text = re.sub(r"\s*-+\s*", " - ", text)  # "narrative—lead" == "narrative -- lead"
    return re.sub(r"\s+", " ", text).strip().lower()


def parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Minimal `---` frontmatter parser (key: value lines). Returns (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            meta: Dict[str, str] = {}
            for raw in lines[1:i]:
                if ":" in raw and not raw.lstrip().startswith("#"):
                    k, v = raw.split(":", 1)
                    meta[k.strip().lower()] = v.strip().strip("'\"").lower()
            return meta, "".join(lines[i + 1:])
    return {}, text  # unterminated frontmatter: treat whole thing as body


def classify(text: str, force: Optional[bool] = None) -> Tuple[bool, str]:
    """Return (in_scope, why)."""
    if force is not None:
        return force, "forced by flag"
    meta, body = parse_frontmatter(text)
    otype = meta.get("output_type") or meta.get("output")
    if otype:
        if otype in ARTIFACT_TYPES:
            return True, f"output_type: {otype}"
        return False, f"output_type: {otype} (not an artifact type)"
    m = KEYWORDS.search(body)
    if m:
        return True, f"keyword '{m.group(0)}' in body (add output_type: to frontmatter to be explicit)"
    return False, "no artifact signals"


def has_standing_line(text: str) -> bool:
    return _normalize(STANDING_LINE) in _normalize(text)


def lint_text(text: str, force: Optional[bool] = None) -> Tuple[bool, str]:
    in_scope, why = classify(text, force)
    if not in_scope:
        return True, f"out of scope ({why})"
    if has_standing_line(text):
        return True, f"ok ({why})"
    return False, f"in scope ({why}) but missing the user's standing artifact instruction"


def lint_file(path, force: Optional[bool] = None) -> Tuple[bool, str]:
    text = Path(path).read_text(encoding="utf-8")
    return lint_text(text, force)


def fix_file(path, force: Optional[bool] = None) -> bool:
    """Append the standing block if missing. Returns True if the file was changed.

    Appending keeps the user's own style words (already in the body) ahead of the
    standing line, per AGENTS.md §2 ordering.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    ok, _ = lint_text(text, force)
    if ok:
        return False
    p.write_text(text.rstrip("\n") + FIX_BLOCK, encoding="utf-8")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("briefs", nargs="+", help="brief file(s) to check")
    ap.add_argument("--fix", action="store_true", help="append the standing block where missing")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--artifact", dest="force", action="store_const", const=True,
                   help="treat brief(s) as in scope regardless of content")
    g.add_argument("--no-artifact", dest="force", action="store_const", const=False,
                   help="treat brief(s) as out of scope")
    ap.add_argument("--quiet", action="store_true", help="print failures only")
    args = ap.parse_args(argv)

    failed = 0
    for b in args.briefs:
        try:
            if args.fix and fix_file(b, args.force):
                print(f"FIXED  {b}: standing block appended")
                continue
            ok, reason = lint_file(b, args.force)
        except OSError as e:
            print(f"ERROR  {b}: {e}", file=sys.stderr)
            return 2
        if not ok:
            failed += 1
            print(f"FAIL   {b}: {reason}")
        elif not args.quiet:
            print(f"PASS   {b}: {reason}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
