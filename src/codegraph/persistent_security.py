"""Security rules over the persisted Java/Python value graph.

This is deliberately separate from the compatibility taint engine.  Detection
below reads value paths only from ``dataflow_nodes`` and ``dataflow_edges``;
syntax facts and the on-demand taint evaluator are not consulted.

The first slice is entry-scoped path traversal.  Parameters of the explicitly
selected entry are the trust-boundary sources.  A missing path is therefore
``unknown``, never proof that the code is safe: external source-return nodes and
sanitizer transformations are not yet canonical in the persisted graph.
"""

from __future__ import annotations

import json

from .flowgraph import current_stage, ensure

PATH_TRAVERSAL_CWE = "CWE-22"
_CONFIDENCE_RANK = {"possible": 0, "inferred": 1, "certain": 2}

# Exact syntax identities intentionally keep this first rule family narrow.
# Argument indexes are path-bearing positions; -1 (unknown/keyword) is retained
# conservatively by the classifier.
_PYTHON_SINKS = {
    "open": ("python.path.open", frozenset({0})),
    "builtins.open": ("python.path.open", frozenset({0})),
    "io.open": ("python.path.io-open", frozenset({0})),
    "os.open": ("python.path.os-open", frozenset({0})),
    "os.remove": ("python.path.remove", frozenset({0})),
    "os.unlink": ("python.path.unlink", frozenset({0})),
    "os.rename": ("python.path.rename", frozenset({0, 1})),
    "os.replace": ("python.path.replace", frozenset({0, 1})),
    "shutil.copy": ("python.path.copy", frozenset({0, 1})),
    "shutil.copyfile": ("python.path.copyfile", frozenset({0, 1})),
    "shutil.move": ("python.path.move", frozenset({0, 1})),
    "shutil.rmtree": ("python.path.rmtree", frozenset({0})),
    "send_file": ("python.path.send-file", frozenset({0})),
    "send_from_directory": (
        "python.path.send-from-directory", frozenset({0, 1})),
}

_JAVA_SINKS = {
    "File": ("java.path.file", frozenset({0, 1})),
    "FileInputStream": ("java.path.file-input-stream", frozenset({0})),
    "FileOutputStream": ("java.path.file-output-stream", frozenset({0})),
    "FileReader": ("java.path.file-reader", frozenset({0})),
    "FileWriter": ("java.path.file-writer", frozenset({0})),
    "RandomAccessFile": ("java.path.random-access-file", frozenset({0})),
    "Paths.get": ("java.path.paths-get", frozenset({0})),
    "Path.of": ("java.path.path-of", frozenset({0})),
    "Files.newInputStream": (
        "java.path.files-new-input-stream", frozenset({0})),
    "Files.newOutputStream": (
        "java.path.files-new-output-stream", frozenset({0})),
    "Files.readAllBytes": (
        "java.path.files-read-all-bytes", frozenset({0})),
    "Files.readString": ("java.path.files-read-string", frozenset({0})),
    "Files.write": ("java.path.files-write", frozenset({0})),
    "Files.writeString": ("java.path.files-write-string", frozenset({0})),
    "Files.copy": ("java.path.files-copy", frozenset({0, 1})),
    "Files.move": ("java.path.files-move", frozenset({0, 1})),
    "Files.delete": ("java.path.files-delete", frozenset({0})),
    "Files.deleteIfExists": (
        "java.path.files-delete-if-exists", frozenset({0})),
}

# These are evidence labels only.  The persisted graph currently cannot prove
# that a call's *result* is the value that continues to a sink, so seeing one
# never suppresses a candidate.
_SANITIZER_LABELS = frozenset({
    "os.path.basename", "werkzeug.utils.secure_filename",
    "FilenameUtils.getName", "Path.getFileName",
})


def _json_object(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _identity(details: dict) -> tuple[str, str | None]:
    callee = str(details.get("callee") or "")
    qualified = details.get("qualified")
    return callee, str(qualified) if qualified else None


def _identities(details: dict) -> tuple[str, ...]:
    callee, qualified = _identity(details)
    receiver_type = details.get("receiver_type")
    typed = (f"{receiver_type}.{callee}"
             if receiver_type and callee else None)
    values = [typed, qualified, callee]
    return tuple(dict.fromkeys(value for value in values if value))


def _catalog_matches(details: dict, catalog) -> tuple[str, ...]:
    matches = []
    for identity in _identities(details):
        for configured in catalog:
            if (identity == configured
                    or ("." in identity and "." in configured and (
                        identity.endswith("." + configured)
                        or configured.endswith("." + identity)))):
                matches.append(configured)
    return tuple(dict.fromkeys(matches))


def _trusted_source(details: dict, identity: str, rules) -> bool:
    literals = {
        int(index): value for index, value in details.get("arg_literals", ())
        if isinstance(index, int) and isinstance(value, str)
    }
    for source, index, trusted in rules.trusted_source_literals:
        if identity == source or identity.endswith("." + source):
            return literals.get(index) in trusted
    return False


def _framework_source(details: dict) -> bool:
    if details.get("framework_source"):
        return True
    try:
        from .dataflow import is_framework_source_call, is_framework_source_path

        rhs_paths = [tuple(path) for path in details.get("rhs_paths", ())
                     if isinstance(path, (list, tuple))]
        return (is_framework_source_call(details.get("qualified"), rhs_paths)
                or any(is_framework_source_path(path) for path in rhs_paths))
    except (ImportError, TypeError, ValueError):
        return False


def _details_are_source(details: dict, rules) -> bool:
    if _framework_source(details):
        return True
    for identity in _catalog_matches(details, rules.sources):
        if not _trusted_source(details, identity, rules):
            return True
    return False


def _source_evidence(details: dict, rules) -> dict:
    matches = _catalog_matches(details, rules.sources)
    return {
        "kind": "persisted external source result",
        "matched_rules": list(matches),
        "framework_source": _framework_source(details),
        "identities": list(_identities(details)),
    }


def _details_are_sanitizer(details: dict, sanitizers) -> bool:
    return bool(_catalog_matches(details, sanitizers))


def _sink_rule(language: str, details: dict):
    callee, qualified = _identity(details)
    argument = details.get("arg_index", -1)
    if not isinstance(argument, int):
        argument = -1

    if language == "python":
        # A naked open is builtins.open.  Receiver-qualified Image.open and
        # similar APIs must not inherit that rule merely by basename.
        if callee == "open" and qualified not in {
                None, "open", "builtins.open", "io.open", "os.open"}:
            return None
        keys = tuple(dict.fromkeys(filter(None, (qualified, callee))))
        catalog = _PYTHON_SINKS
    elif language == "java":
        keys = tuple(dict.fromkeys(filter(None, (
            qualified, callee,
            qualified.rsplit(".", 1)[-1] if qualified else None,
        ))))
        catalog = _JAVA_SINKS
    else:
        return None

    for key in keys:
        item = catalog.get(key)
        if item is None:
            continue
        rule_id, indexes = item
        if argument == -1 or argument in indexes:
            return {
                "rule_id": rule_id,
                "cwe": PATH_TRAVERSAL_CWE,
                "callee": callee,
                "qualified": qualified,
                "argument_index": argument,
                "accepted_argument_indexes": sorted(indexes),
            }
    return None


def _project_node(row) -> dict:
    item = dict(row)
    item["details"] = _json_object(item.pop("details_json", None))
    return item


def _project_edge(row) -> dict:
    item = dict(row)
    item["kind"] = "flows_to"
    item["interprocedural"] = bool(item["interprocedural"])
    item["evidence"] = _json_object(item.pop("evidence_json", None))
    return item


def _confidence(flow_confidence: str) -> str:
    """Include inferred API classification in the candidate confidence."""
    rank = min(_CONFIDENCE_RANK.get(flow_confidence, 0),
               _CONFIDENCE_RANK["inferred"])
    return next(name for name, value in _CONFIDENCE_RANK.items()
                if value == rank)


def _entry_sources(conn, entry_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT n.id,n.function_id,n.symbol_id,n.file_id,n.kind,n.name,"
        "n.access_path,n.line,n.col,n.details_json,f.path,f.language "
        "FROM dataflow_nodes n JOIN symbols s ON s.id=n.symbol_id "
        "JOIN files f ON f.id=n.file_id WHERE n.kind='parameter' "
        "AND s.parent_id=? ORDER BY n.id", (entry_id,)
    ).fetchall()
    return [item for item in map(_project_node, rows)
            if item["name"] not in {"self", "cls"}]


def _repository_sources(conn, rules,
                        scope: str | None = None) -> list[dict]:
    args: list[object] = []
    scope_filter = ""
    if scope:
        normalized = scope.replace("\\", "/").strip("/")
        if normalized not in {"", "."}:
            scope_filter = " AND (f.path=? OR f.path LIKE ?)"
            args.extend((normalized, normalized + "/%"))
    rows = conn.execute(
        "SELECT n.id,n.function_id,n.symbol_id,n.file_id,n.kind,n.name,"
        "n.access_path,n.line,n.col,n.details_json,f.path,f.language "
        "FROM dataflow_nodes n JOIN files f ON f.id=n.file_id "
        "WHERE n.kind='call_result'" + scope_filter + " ORDER BY n.id",
        tuple(args),
    ).fetchall()
    return [node for node in map(_project_node, rows)
            if _details_are_source(node["details"], rules)]


def _sanitizer_nodes(conn, sanitizers) -> set[str]:
    rows = conn.execute(
        "SELECT id,details_json FROM dataflow_nodes "
        "WHERE kind='call_result' ORDER BY id"
    ).fetchall()
    return {
        row["id"] for row in rows
        if _details_are_sanitizer(
            _json_object(row["details_json"]), sanitizers)
    }


def _path_rows(conn, source_ids: list[str], max_hops: int,
               scan_limit: int):
    placeholders = ",".join("?" * len(source_ids))
    return conn.execute(
        "WITH RECURSIVE walk(source_id,node_id,hops,confidence,trail,"
        "edge_trail) AS ("
        f" SELECT id,id,0,'certain',','||id||',','' FROM dataflow_nodes "
        f" WHERE id IN ({placeholders})"
        " UNION ALL "
        " SELECT w.source_id,e.dst_node_id,w.hops+1,"
        " CASE WHEN w.confidence='possible' OR e.confidence='possible' "
        " THEN 'possible' WHEN w.confidence='inferred' "
        " OR e.confidence='inferred' THEN 'inferred' ELSE 'certain' END,"
        " w.trail||e.dst_node_id||',',"
        " w.edge_trail||CASE WHEN w.edge_trail='' THEN '' ELSE ',' END||e.id"
        " FROM walk w JOIN dataflow_edges e ON e.src_node_id=w.node_id"
        " WHERE w.hops<? AND instr(w.trail,','||e.dst_node_id||',')=0"
        " ORDER BY 3,1,2,5 LIMIT ?"
        ") SELECT w.source_id,w.node_id,w.hops,w.confidence,w.trail,"
        "w.edge_trail,n.kind,n.details_json,f.language FROM walk w "
        "JOIN dataflow_nodes n ON n.id=w.node_id "
        "JOIN files f ON f.id=n.file_id "
        "ORDER BY w.hops,w.source_id,w.node_id,w.edge_trail",
        (*source_ids, max_hops, scan_limit + 1),
    ).fetchall()


def _materialize_path(conn, trail: str, edge_trail: str) -> tuple[list, list]:
    node_ids = [item for item in trail.strip(",").split(",") if item]
    node_ph = ",".join("?" * len(node_ids))
    by_node = {row["id"]: _project_node(row) for row in conn.execute(
        "SELECT n.id,n.function_id,n.symbol_id,n.file_id,n.kind,n.name,"
        "n.access_path,n.line,n.col,n.details_json,f.path,f.language "
        "FROM dataflow_nodes n JOIN files f ON f.id=n.file_id "
        f"WHERE n.id IN ({node_ph})", node_ids)}
    nodes = [by_node[node_id] for node_id in node_ids]

    edge_ids = [item for item in edge_trail.split(",") if item]
    if not edge_ids:
        return nodes, []
    edge_ph = ",".join("?" * len(edge_ids))
    by_edge = {row["id"]: _project_edge(row) for row in conn.execute(
        "SELECT id,owner_function_id,src_node_id,dst_node_id,file_id,relation,"
        "line,col,confidence,interprocedural,evidence_json "
        f"FROM dataflow_edges WHERE id IN ({edge_ph})", edge_ids)}
    return nodes, [by_edge[edge_id] for edge_id in edge_ids]


def _sanitizer_evidence(nodes: list[dict]) -> list[dict]:
    found = []
    for node in nodes[:-1]:
        if node["kind"] != "call_argument":
            continue
        callee, qualified = _identity(node["details"])
        identity = qualified or callee
        if identity in _SANITIZER_LABELS:
            found.append({
                "identity": identity, "node_id": node["id"],
                "path": node["path"], "line": node["line"],
                "col": node["col"],
            })
    return found


def path_traversal(engine, entry_selector: str | None = None, *,
                   scope: str | None = None, max_hops: int = 64,
                   max_findings: int = 50) -> tuple[dict | None, dict, object]:
    """Find persisted source paths to Java/Python file/path arguments.

    With ``entry_selector``, its parameters are the explicit trust boundary.
    Without it, persisted external call-result nodes seed a bounded repo scan.
    """
    from .query import Envelope

    if max_hops < 1:
        raise ValueError("max_hops deve ser >= 1")
    if max_findings < 1:
        raise ValueError("max_findings deve ser >= 1")

    env = Envelope(dynamic_dispatch=True)
    entry = (engine._resolve_fresh(entry_selector, env)
             if entry_selector else None)
    if engine._repair_all(env):
        # A full scope repair is required before trusting trans-file callees.
        # Re-resolve because the selected declaration itself may have changed.
        if entry_selector:
            entry = engine._resolve_selector(entry_selector)
    language = None
    if entry is not None:
        language_row = engine.conn.execute(
            "SELECT language FROM files WHERE id=?", (entry["file_id"],)
        ).fetchone()
        language = language_row["language"] if language_row else None

    if entry is not None and language not in {"java", "python"}:
        env.warn("path traversal persistente: entry fora do foco Java/Python")
        return entry, {
            "rule_id": "path-traversal",
            "cwe": PATH_TRAVERSAL_CWE,
            "mode": "entry",
            "engine": "persistent-dataflow",
            "persistent": True,
            "verdict": "unknown",
            "findings": [],
            "stage": current_stage(engine.conn),
            "freshness": {"fresh": env.fresh, "repaired": not env.fresh},
            "completeness": {
                "complete": False,
                "limitations": ["supported languages are Java and Python"],
            },
        }, env

    built = ensure(engine)
    stage = current_stage(engine.conn)
    if built.get("queryable") is False:
        env.warn(
            "path traversal persistente: o stage atual não é consultável; "
            "snapshot anterior não foi usado.")
        return entry, {
            "rule_id": "path-traversal",
            "cwe": PATH_TRAVERSAL_CWE,
            "mode": "entry" if entry is not None else "scan",
            "language": language,
            "engine": "persistent-dataflow",
            "persistent": True,
            "verdict": "unknown",
            "findings": [],
            "stage": stage,
            "freshness": {"fresh": env.fresh, "repaired": not env.fresh},
            "completeness": {
                "complete": False,
                "graph_stage_status": stage.get("status") if stage else "missing",
                "entry_sources": 0,
                "max_hops": max_hops,
                "max_findings": max_findings,
                "truncated": False,
                "limitations": [
                    "current persistent dataflow stage is not queryable",
                    "previous snapshots are never used as current evidence",
                ],
            },
        }, env

    from .taint_rules import load_rules

    focus_languages = {
        row["language"] for row in engine.conn.execute(
            "SELECT DISTINCT language FROM files "
            "WHERE language IN ('java','python')")
    }
    rules = load_rules(engine.root, focus_languages)
    sanitizers = set()
    for item_language in focus_languages:
        sanitizers.update(rules.sanitizers_for_context(
            item_language, "non-xss"))
    sanitizers.update(_SANITIZER_LABELS)
    sources = (_entry_sources(engine.conn, entry["id"])
               if entry is not None else _repository_sources(
                   engine.conn, rules, scope=scope))
    sanitizer_ids = _sanitizer_nodes(engine.conn, sanitizers)
    scan_limit = max(1000, max_findings * max_hops * 8)
    walk_rows = (_path_rows(
        engine.conn, [source["id"] for source in sources], max_hops,
        scan_limit) if sources else [])
    if len(walk_rows) > scan_limit:
        env.truncated = True
    rows = [row for row in walk_rows[:scan_limit]
            if row["hops"] > 0 and row["kind"] == "call_argument"]
    source_by_id = {source["id"]: source for source in sources}
    findings = []
    candidate_count = 0
    sanitized_paths = 0
    seen_paths: set[tuple[str, str, str]] = set()
    for row in rows:
        sink_details = _json_object(row["details_json"])
        rule = _sink_rule(row["language"], sink_details)
        if rule is None:
            continue
        key = (row["source_id"], row["node_id"], row["edge_trail"])
        if key in seen_paths:
            continue
        seen_paths.add(key)
        candidate_count += 1
        if len(findings) >= max_findings:
            continue
        nodes, edges = _materialize_path(
            engine.conn, row["trail"], row["edge_trail"])
        sanitizer_path = [node for node in nodes[1:]
                          if node["id"] in sanitizer_ids]
        if sanitizer_path:
            sanitized_paths += 1
            continue
        source = source_by_id[row["source_id"]]
        sink = nodes[-1]
        sanitizer_candidates = _sanitizer_evidence(nodes)
        flow_confidence = row["confidence"]
        findings.append({
            "rule_id": rule["rule_id"],
            "cwe": rule["cwe"],
            "verdict": "candidate",
            "confidence": _confidence(flow_confidence),
            "source": {
                **source,
                "evidence": (
                    "explicit entry parameter" if entry is not None
                    else _source_evidence(source["details"], rules)),
            },
            "sink": {
                **sink,
                "evidence": rule,
            },
            "path": {
                "hops": row["hops"],
                "nodes": nodes,
                "edges": edges,
            },
            "evidence": {
                "kind": "persistent-value-path",
                "flow_confidence": flow_confidence,
                "stage_version": stage.get("stage_version") if stage else None,
                "artifact_hash": stage.get("artifact_hash") if stage else None,
            },
            "sanitization": {
                "status": "not_present",
                "policy": "persisted sanitizer-result nodes cut the path",
                "candidates_on_path": sanitizer_candidates,
            },
        })
    if candidate_count > max_findings:
        env.truncated = True

    limitations = [
        ("sources are parameters of the explicitly selected entry"
         if entry is not None else
         "repo scan seeds only configured/framework sources represented by "
         "persisted call-result nodes"),
        "sanitizers cut only when the transformed call result is a persisted "
        "node on the path",
        "may-flow does not prove branch feasibility, aliases, reflection, or "
        "dynamic dispatch",
    ]
    mapping_coverage = built.get("mapping_coverage_pct")
    if mapping_coverage is not None and mapping_coverage < 100:
        limitations.append(
            f"persistent value mapping coverage is {mapping_coverage}%")
    completeness = {
        "complete": False,
        "graph_stage_status": stage.get("status") if stage else "missing",
        "entry_sources": len(sources),
        "sanitized_paths": sanitized_paths,
        "scope": scope,
        "max_hops": max_hops,
        "max_findings": max_findings,
        "truncated": env.truncated,
        "limitations": limitations,
    }
    env.warn(
        "path traversal persistente é may-flow; ausência de "
        "achado é unknown, não prova de segurança.")
    return entry, {
        "rule_id": "path-traversal",
        "cwe": PATH_TRAVERSAL_CWE,
        "mode": "entry" if entry is not None else "scan",
        "language": language,
        "languages": sorted(focus_languages),
        "scope": scope,
        "engine": "persistent-dataflow",
        "persistent": True,
        "verdict": "candidate" if findings else "unknown",
        "findings": findings,
        "stage": stage,
        "freshness": {"fresh": env.fresh, "repaired": not env.fresh},
        "completeness": completeness,
    }, env
