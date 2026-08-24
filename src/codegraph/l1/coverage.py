"""Deterministic semantic-link coverage over persisted call sites.

The report describes what the graph can prove after an L1 pass.  It does not
guess whether an unresolved call is external, reflective or dynamically
dispatched when no resolver target was published; that honest boundary is
reported as ``l1_no_local_target``.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter


def _languages(items) -> set[str]:
    out: set[str] = set()
    for item in items or ():
        if isinstance(item, str):
            out.add(item)
        elif isinstance(item, dict):
            out.update(item.get("languages", ()))
    return out


def semantic_coverage(conn: sqlite3.Connection, *,
                      applicable=(), attempted=(), unavailable=(),
                      sample_limit: int = 20) -> dict:
    """Classify every applicable persisted callsite by semantic outcome."""
    applicable_set = _languages(applicable)
    attempted_set = _languages(attempted)
    unavailable_set = _languages(unavailable)
    if not applicable_set:
        applicable_set = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT f.language FROM edges e "
                "JOIN files f ON f.id=e.file_id WHERE e.kind='calls'")
        }
    if not applicable_set:
        return {
            "total_sites": 0, "certain_sites": 0,
            "semantic_sites": 0, "fallback_sites": 0,
            "unresolved_sites": 0, "certain_pct": 0.0,
            "semantic_coverage_pct": 0.0,
            "local_candidate_sites": 0,
            "missed_local_candidate_sites": 0,
            "no_local_graph_candidate_sites": 0,
            "local_candidate_coverage_pct": 0.0,
            "outcomes": {}, "by_language": {}, "samples": [],
        }

    placeholders = ",".join("?" * len(applicable_set))
    rows = conn.execute(
        "SELECT f.path, f.language, e.line, e.col, MIN(e.dst_name) callee, "
        "SUM(CASE WHEN e.resolver='l1' AND e.confidence='certain' "
        "AND e.dst IS NOT NULL THEN 1 ELSE 0 END) l1_certain, "
        "SUM(CASE WHEN e.resolver='l1' AND e.confidence='inferred' "
        "AND e.dst IS NOT NULL THEN 1 ELSE 0 END) l1_multiple, "
        "SUM(CASE WHEN e.resolver='l0' AND e.confidence='certain' "
        "AND e.dst IS NOT NULL THEN 1 ELSE 0 END) l0_certain, "
        "SUM(CASE WHEN e.resolver='l0' AND e.confidence='inferred' "
        "AND e.dst IS NOT NULL THEN 1 ELSE 0 END) l0_unique, "
        "SUM(CASE WHEN e.resolver='l0' AND e.confidence='possible' "
        "AND e.dst IS NOT NULL THEN 1 ELSE 0 END) l0_possible, "
        "SUM(CASE WHEN e.dst IS NULL THEN 1 ELSE 0 END) dangling "
        "FROM edges e JOIN files f ON f.id=e.file_id "
        f"WHERE e.kind='calls' AND f.language IN ({placeholders}) "
        "GROUP BY e.file_id, e.line, e.col ORDER BY f.path, e.line, e.col",
        sorted(applicable_set),
    ).fetchall()

    def outcome(row) -> str:
        if row["l1_certain"]:
            return "l1_certain"
        if row["l1_multiple"]:
            return "l1_multiple_targets"
        if row["language"] in unavailable_set:
            return "resolver_unavailable"
        if row["l0_certain"]:
            return "l0_syntax_certain"
        attempted = row["language"] in attempted_set
        if row["l0_unique"]:
            return ("l1_no_promotion_l0_unique" if attempted
                    else "l0_unique_not_refined")
        if row["l0_possible"] > 1:
            return ("l1_no_unique_target" if attempted
                    else "l0_multiple_candidates")
        if row["l0_possible"]:
            return ("l1_no_promotion_l0_possible" if attempted
                    else "l0_possible_not_refined")
        if row["dangling"]:
            return ("l1_no_local_target" if attempted
                    else "unresolved_not_refined")
        return "unclassified"

    counts: Counter[str] = Counter()
    language_stats: dict[str, dict] = {}
    samples = []
    fallback = 0
    unresolved = 0
    local_candidates = 0
    missed_local_candidates = 0
    no_local_graph_candidates = 0
    for row in rows:
        label = outcome(row)
        counts[label] += 1
        language = row["language"]
        lang = language_stats.setdefault(language, {
            "total_sites": 0, "certain_sites": 0, "semantic_sites": 0,
            "fallback_sites": 0, "unresolved_sites": 0,
            "local_candidate_sites": 0,
            "missed_local_candidate_sites": 0,
            "no_local_graph_candidate_sites": 0,
            "outcomes": Counter(),
        })
        lang["total_sites"] += 1
        lang["outcomes"][label] += 1
        has_l1 = bool(row["l1_certain"] or row["l1_multiple"])
        has_l0 = bool(
            row["l0_certain"] or row["l0_unique"] or row["l0_possible"])
        # A persisted L0 target is concrete evidence of a local graph
        # candidate.  Same-name symbols alone are not: common method names in
        # dependencies would otherwise make this denominator wildly noisy.
        has_candidate = has_l1 or has_l0
        if has_candidate:
            local_candidates += 1
            lang["local_candidate_sites"] += 1
        if has_l1:
            lang["semantic_sites"] += 1
            if row["l1_certain"]:
                lang["certain_sites"] += 1
        elif has_candidate:
            missed_local_candidates += 1
            lang["missed_local_candidate_sites"] += 1
        else:
            no_local_graph_candidates += 1
            lang["no_local_graph_candidate_sites"] += 1
        if not has_l1 and has_l0:
            fallback += 1
            lang["fallback_sites"] += 1
        elif not has_l1:
            unresolved += 1
            lang["unresolved_sites"] += 1
        if label != "l1_certain" and len(samples) < max(0, sample_limit):
            samples.append({
                "path": row["path"], "language": row["language"],
                "line": row["line"], "col": row["col"],
                "callee": row["callee"], "outcome": label,
            })
    total = len(rows)
    certain = counts["l1_certain"]
    semantic = certain + counts["l1_multiple_targets"]
    by_language = {}
    for language, lang in sorted(language_stats.items()):
        total_lang = lang["total_sites"]
        candidate_lang = lang["local_candidate_sites"]
        lang["certain_pct"] = round(
            100 * lang["certain_sites"] / total_lang, 1)
        lang["semantic_coverage_pct"] = round(
            100 * lang["semantic_sites"] / total_lang, 1)
        lang["local_candidate_coverage_pct"] = round(
            100 * lang["semantic_sites"] / candidate_lang, 1
        ) if candidate_lang else 0.0
        lang["outcomes"] = dict(sorted(lang["outcomes"].items()))
        by_language[language] = lang
    return {
        "total_sites": total,
        "certain_sites": certain,
        "semantic_sites": semantic,
        "fallback_sites": fallback,
        "unresolved_sites": unresolved,
        "certain_pct": round(100 * certain / total, 1) if total else 0.0,
        "semantic_coverage_pct": (
            round(100 * semantic / total, 1) if total else 0.0),
        # This denominator is actionable without pretending external/dynamic
        # calls should resolve to code that exists in the repository.
        "local_candidate_sites": local_candidates,
        "missed_local_candidate_sites": missed_local_candidates,
        "no_local_graph_candidate_sites": no_local_graph_candidates,
        "local_candidate_coverage_pct": (
            round(100 * semantic / local_candidates, 1)
            if local_candidates else 0.0),
        "outcomes": dict(sorted(counts.items())),
        "by_language": by_language,
        "samples": samples,
    }


def current_semantic_coverage(conn: sqlite3.Connection,
                              sample_limit: int = 20) -> dict:
    """Recompute coverage using the last persisted L1 applicability context."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key='l1_last_run'").fetchone()
    try:
        last = json.loads(row["value"]) if row is not None else {}
    except (TypeError, ValueError):
        last = {}
    lifecycle_row = conn.execute(
        "SELECT value FROM meta WHERE key='l1_lifecycle'").fetchone()
    try:
        lifecycle = (json.loads(lifecycle_row["value"])
                     if lifecycle_row is not None else {})
    except (TypeError, ValueError):
        lifecycle = {}
    # A new L0 revision invalidates the previous semantic attempt.  Its result
    # must be described as "not refined", not as a failed promotion from the
    # old revision.  During ``running`` readers intentionally see the previous
    # published snapshot, so the previous context remains correct.
    not_started = lifecycle.get("status", "not_started") == "not_started"
    return semantic_coverage(
        conn,
        # Public coverage is whole-graph, even if the last incremental refine
        # touched only one project root/language.  Languages outside that pass
        # remain visible as ``*_not_refined`` instead of disappearing.
        applicable=(),
        attempted=() if not_started else last.get("attempted", ()),
        unavailable=() if not_started else last.get("unavailable", ()),
        sample_limit=sample_limit,
    )
