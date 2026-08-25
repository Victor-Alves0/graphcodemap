"""Persistent Java/Python value-flow graph.

The existing :mod:`codegraph.dataflow` module remains the transfer-function
authority.  This module turns its normalized facts into a reusable graph whose
nodes and edges survive process boundaries.  It intentionally models *may
flow*: branch/loop feasibility and heap aliasing remain explicit limitations,
not silently fabricated certainty.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from .db import read_l1_lifecycle, record_current_stage, retry_on_locked
from .util import byte_column, content_hash

DATAFLOW_STAGE_VERSION = "persistent-v2"
FOCUS_LANGUAGES = frozenset({"java", "python"})
CALLABLE_KINDS = ("function", "method", "constructor", "property")

_UPSERT_NODE_SQL = (
    "INSERT INTO dataflow_nodes(id,function_id,symbol_id,file_id,kind,"
    "name,access_path,line,col,content_hash,details_json) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
    "function_id=excluded.function_id,symbol_id=excluded.symbol_id,"
    "file_id=excluded.file_id,kind=excluded.kind,name=excluded.name,"
    "access_path=excluded.access_path,line=excluded.line,col=excluded.col,"
    "content_hash=excluded.content_hash,details_json=excluded.details_json"
)
_UPSERT_EDGE_SQL = (
    "INSERT INTO dataflow_edges(id,owner_function_id,src_node_id,"
    "dst_node_id,file_id,relation,line,col,confidence,interprocedural,"
    "evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(id) DO UPDATE SET confidence=excluded.confidence,"
    "interprocedural=excluded.interprocedural,"
    "evidence_json=excluded.evidence_json"
)


def _stable_id(prefix: str, *parts) -> str:
    payload = "\0".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}:{hashlib.blake2b(payload, digest_size=16).hexdigest()}"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _path(value) -> tuple[str, ...]:
    if isinstance(value, tuple):
        return tuple(str(part) for part in value if str(part))
    if isinstance(value, list):
        return tuple(str(part) for part in value if str(part))
    if isinstance(value, str):
        return tuple(part for part in value.split(".") if part)
    return ()


@dataclass
class _Counts:
    functions: int = 0
    rebuilt: int = 0
    reused: int = 0
    nodes: int = 0
    edges: int = 0
    unmapped_paths: int = 0
    parse_failures: int = 0
    partial_functions: int = 0
    path_attempts: int = 0
    mapped_paths: int = 0


class _FunctionBuilder:
    def __init__(self, engine, function: dict, facts, language: str):
        self.engine = engine
        self.conn = engine.conn
        self.function = function
        self.facts = facts
        self.language = language
        self.function_id = function["id"]
        self.file_id = function["file_id"]
        self.body_hash = function["body_hash"]
        self.node_ids: set[str] = set()
        self.edge_ids: set[str] = set()
        # Building is pure with respect to the persistent value graph until
        # ``_flush``.  Besides reducing Python/SQLite crossings, this keeps the
        # existing transaction boundary and deterministic first-edge-wins
        # behaviour intact.
        self._node_rows: list[tuple] = []
        self._edge_rows: list[tuple] = []
        self.unmapped = 0
        self.path_attempts = 0
        self.mapped_paths = 0
        self._target_rows: dict[str, dict | None] = {}
        self._target_parameters: dict[tuple[str, int], dict | None] = {}
        self._resolved_call_targets: dict[tuple, list[dict]] = {}
        self.variables = self._variables()
        self.fields = self._fields()

    def _variables(self) -> dict[str, list[dict]]:
        rows = self.conn.execute(
            "SELECT id, file_id, kind, name, fqn, body_hash, start_line, "
            "start_col FROM symbols WHERE parent_id=? "
            "AND kind IN ('parameter','local') ORDER BY start_line,start_col,id",
            (self.function_id,),
        ).fetchall()
        out: dict[str, list[dict]] = {}
        for row in rows:
            out.setdefault(row["name"], []).append(dict(row))
        return out

    def _fields(self) -> dict[str, dict]:
        parent_id = self.function.get("parent_id")
        if not parent_id:
            return {}
        rows = self.conn.execute(
            "SELECT id, file_id, kind, name, fqn, body_hash, start_line, "
            "start_col FROM symbols WHERE parent_id=? AND kind='field' "
            "ORDER BY start_line,start_col,id", (parent_id,),
        ).fetchall()
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["name"], []).append(dict(row))
        return {name: values[0] for name, values in grouped.items()
                if len(values) == 1}

    def _upsert_node(self, node_id: str, *, kind: str, name: str,
                     access_path: str | None, line: int | None,
                     col: int | None, content: str, symbol_id: str | None = None,
                     function_id: str | None = None, file_id: int | None = None,
                     details: dict | None = None) -> str:
        if node_id in self.node_ids:
            return node_id
        self._node_rows.append(
            (node_id, function_id, symbol_id, file_id or self.file_id, kind,
             name, access_path, line, col, content, _json(details or {})))
        self.node_ids.add(node_id)
        return node_id

    def _symbol_node(self, symbol: dict) -> str:
        node_id = f"symbol:{symbol['id']}"
        return self._upsert_node(
            node_id, kind=symbol["kind"], name=symbol["name"],
            access_path=symbol["name"], line=symbol.get("start_line"),
            col=symbol.get("start_col"), content=symbol["body_hash"],
            symbol_id=symbol["id"], function_id=None,
            file_id=symbol["file_id"], details={"fqn": symbol["fqn"]})

    def _edge(self, src: str, dst: str, relation: str, *,
              line: int | None = None, col: int | None = None,
              confidence: str = "certain", interprocedural: bool = False,
              evidence: dict | None = None) -> str:
        edge_id = _stable_id(
            "flow", self.function_id, src, dst, relation, line, col)
        if edge_id in self.edge_ids:
            return edge_id
        self._edge_rows.append(
            (edge_id, self.function_id, src, dst, self.file_id, relation,
             line, col, confidence, int(interprocedural), _json(evidence or {})))
        self.edge_ids.add(edge_id)
        return edge_id

    def _flush(self) -> None:
        # Nodes precede edges to satisfy the foreign keys even when a caller
        # materializes a callee parameter/return before the callee is visited.
        if self._node_rows:
            self.conn.executemany(_UPSERT_NODE_SQL, self._node_rows)
        if self._edge_rows:
            self.conn.executemany(_UPSERT_EDGE_SQL, self._edge_rows)

    def _plain_symbol(self, name: str, line: int | None = None) -> dict | None:
        local = self.variables.get(name, ())
        if len(local) == 1:
            return local[0]
        if len(local) > 1 and line is not None:
            rows = self.conn.execute(
                "SELECT DISTINCT s.id,s.file_id,s.kind,s.name,s.fqn,s.body_hash,"
                "s.start_line,s.start_col FROM edges e JOIN symbols s ON s.id=e.dst "
                "WHERE e.src=? AND e.line=? AND e.kind IN ('reads','writes') "
                "AND s.name=?", (self.function_id, line, name),
            ).fetchall()
            if len(rows) == 1:
                return dict(rows[0])
        if not local and name in self.fields:
            return self.fields[name]
        return None

    def _value_node(self, raw_path, *, line: int | None = None) -> str | None:
        self.path_attempts += 1
        path = _path(raw_path)
        if not path:
            self.unmapped += 1
            return None
        # Explicit receiver field accesses share the stable class-field node.
        if len(path) == 2 and path[0] in {"self", "cls", "this"}:
            field = self.fields.get(path[1])
            if field is not None:
                node = self._symbol_node(field)
                base = self._plain_symbol(path[0], line)
                if base is not None:
                    self._edge(self._symbol_node(base), node, "access", line=line)
                self.mapped_paths += 1
                return node
        if len(path) == 1:
            symbol = self._plain_symbol(path[0], line)
            if symbol is None:
                self.unmapped += 1
                return None
            self.mapped_paths += 1
            return self._symbol_node(symbol)

        base = self._plain_symbol(path[0], line)
        if base is None:
            self.unmapped += 1
            return None
        parent_node = self._symbol_node(base)
        # A tainted object may flow to any read below it, while a tainted child
        # never flows back to its parent or a sibling (prefix rule).
        for width in range(2, len(path) + 1):
            prefix = path[:width]
            dotted = ".".join(prefix)
            node_id = _stable_id(
                "value", self.function_id, base["id"], dotted)
            node = self._upsert_node(
                node_id, kind="value", name=prefix[-1], access_path=dotted,
                line=line, col=None, content=self.body_hash,
                function_id=self.function_id,
                details={"base_symbol_id": base["id"]})
            self._edge(parent_node, node, "access", line=line)
            parent_node = node
        self.mapped_paths += 1
        return parent_node

    def _return_node(self, target: dict | None = None) -> str:
        target = target or self.function
        node_id = f"return:{target['id']}"
        return self._upsert_node(
            node_id, kind="return", name=f"{target['fqn']}::$return",
            access_path=None, line=target.get("start_line"), col=None,
            content=target["body_hash"], function_id=target["id"],
            file_id=target["file_id"], details={"function_fqn": target["fqn"]})

    def _call_argument_node(self, call, index: int) -> str:
        node_id = _stable_id(
            "callarg", self.function_id, call.line, call.col, call.callee, index)
        return self._upsert_node(
            node_id, kind="call_argument",
            name=f"{call.callee}#{index}", access_path=None,
            line=call.line, col=call.col, content=self.body_hash,
            function_id=self.function_id,
            details={
                "callee": call.callee, "qualified": call.qualified,
                "arg_index": index, "span": call.span,
                "site_path": self.function["path"],
            })

    def _target_row(self, target: dict) -> dict | None:
        if target["dst"] in self._target_rows:
            return self._target_rows[target["dst"]]
        row = self.conn.execute(
            "SELECT s.*, f.path FROM symbols s JOIN files f ON f.id=s.file_id "
            "WHERE s.id=?", (target["dst"],),
        ).fetchone()
        value = dict(row) if row is not None else None
        self._target_rows[target["dst"]] = value
        return value

    def _target_parameter(self, target: dict, index: int) -> dict | None:
        if index < 0:
            return None
        key = (target["dst"], index)
        if key in self._target_parameters:
            return self._target_parameters[key]
        rows = [dict(row) for row in self.conn.execute(
            "SELECT id,file_id,kind,name,fqn,body_hash,start_line,start_col "
            "FROM symbols WHERE parent_id=? AND kind='parameter' "
            "ORDER BY start_line,start_col,id", (target["dst"],))]
        if target.get("language") == "python":
            rows = [row for row in rows if row["name"] not in {"self", "cls"}]
        value = rows[index] if index < len(rows) else None
        self._target_parameters[key] = value
        return value

    def _resolved_targets(self, call) -> list[dict]:
        key = (call.line, call.col, call.callee)
        if key not in self._resolved_call_targets:
            self._resolved_call_targets[key] = self.engine._df_resolve_calls(
                self.function_id, call.line, call.callee, call.col)
        return self._resolved_call_targets[key]

    def _assignment_call(self, assignment):
        if assignment.rhs_call is None or assignment.span is None:
            return None
        candidates = [
            call for call in self.facts.calls
            if call.span and call.callee == assignment.rhs_call
            and assignment.span[0] <= call.span[0]
            and call.span[1] <= assignment.span[1]
        ]
        if not candidates:
            return None
        width = max(call.span[1] - call.span[0] for call in candidates)
        widest = [call for call in candidates
                  if call.span[1] - call.span[0] == width]
        return widest[0] if len(widest) == 1 else None

    def build(self) -> tuple[int, int, int, int, int]:
        self.conn.execute(
            "DELETE FROM dataflow_edges WHERE owner_function_id=?",
            (self.function_id,),
        )

        for assignment in self.facts.assigns:
            targets = [self._value_node(path, line=assignment.line)
                       for path in assignment.targets]
            sources = [self._value_node(path, line=assignment.line)
                       for path in assignment.rhs_ids]
            for src in filter(None, sources):
                for dst in filter(None, targets):
                    self._edge(src, dst, "assignment", line=assignment.line,
                               evidence={"span": assignment.span,
                                         "augmented": assignment.is_aug})

            call = self._assignment_call(assignment)
            if call is not None:
                for target in self._resolved_targets(call):
                    target_row = self._target_row(target)
                    if target_row is None:
                        continue
                    ret = self._return_node(target_row)
                    for dst in filter(None, targets):
                        self._edge(
                            ret, dst, "call_return", line=assignment.line,
                            col=call.col, confidence=target["confidence"],
                            evidence={"callee": target_row["fqn"]})

        for call in self.facts.calls:
            targets = self._resolved_targets(call)
            for index, paths in call.args:
                argument = self._call_argument_node(call, index)
                for path in paths:
                    src = self._value_node(path, line=call.line)
                    if src is not None:
                        self._edge(
                            src, argument, "call_argument", line=call.line,
                            col=call.col, evidence={"via": ".".join(_path(path))})
                for target in targets:
                    parameter = self._target_parameter(target, index)
                    if parameter is None:
                        continue
                    self._edge(
                        argument, self._symbol_node(parameter), "call_parameter",
                        line=call.line, col=call.col,
                        confidence=target["confidence"], interprocedural=True,
                        evidence={"callee_fqn": target["fqn"],
                                  "arg_index": index})

        ret = self._return_node()
        for returned in self.facts.returns:
            for path in returned.ids:
                src = self._value_node(path)
                if src is not None:
                    self._edge(src, ret, "return_value",
                               evidence={"span": returned.span})
            if returned.top_call and returned.span:
                candidates = [
                    call for call in self.facts.calls
                    if call.span and call.callee == returned.top_call
                    and returned.span[0] <= call.span[0]
                    and call.span[1] <= returned.span[1]
                ]
                if candidates:
                    width = max(call.span[1] - call.span[0]
                                for call in candidates)
                    widest = [call for call in candidates
                              if call.span[1] - call.span[0] == width]
                    if len(widest) == 1:
                        call = widest[0]
                        for target in self._resolved_targets(call):
                            target_row = self._target_row(target)
                            if target_row is not None:
                                self._edge(
                                    self._return_node(target_row), ret,
                                    "call_return", line=call.line, col=call.col,
                                    confidence=target["confidence"],
                                    evidence={"callee": target_row["fqn"]})

        self._flush()

        # Synthetic access-path/call nodes are stable. Remove only stale nodes
        # owned by this function and no longer referenced by its rebuilt edges.
        # Correlated anti-lookups use the src/dst indexes; the previous global
        # UNION was rebuilt and scanned once for every function.
        self.conn.execute(
            "DELETE FROM dataflow_nodes WHERE function_id=? AND kind!='return' "
            "AND NOT EXISTS (SELECT 1 FROM dataflow_edges "
            "WHERE src_node_id=dataflow_nodes.id) "
            "AND NOT EXISTS (SELECT 1 FROM dataflow_edges "
            "WHERE dst_node_id=dataflow_nodes.id)", (self.function_id,),
        )
        return (len(self.node_ids), len(self.edge_ids), self.unmapped,
                self.path_attempts, self.mapped_paths)


def _callable_rows(conn) -> list[dict]:
    placeholders = ",".join("?" * len(CALLABLE_KINDS))
    return [dict(row) for row in conn.execute(
        "SELECT s.*, f.path, f.language, f.content_hash AS file_hash, "
        "f.parse_status "
        "FROM symbols s JOIN files f ON f.id=s.file_id "
        f"WHERE f.language IN ('java','python') AND s.kind IN ({placeholders}) "
        "ORDER BY f.path,s.start_line,s.start_col,s.id", CALLABLE_KINDS)]


def _file_callable_locator(data: bytes, tree, language: str) -> tuple[dict, dict]:
    """Index callable AST positions once for all symbols in one source file.

    ``QueryEngine._df_facts`` historically walks the complete tree for every
    callable.  Large Python modules therefore paid an O(callables * AST) lookup
    cost.  These maps preserve ``find_function_node`` semantics: an exact byte
    column wins, while a legacy line-only position is valid only when unique.
    """
    from . import dataflow as df

    by_position: dict[tuple[int, int], object] = {}
    by_line: dict[int, list] = {}
    stack = [tree.root_node]
    callable_types = df._func_types(language)
    while stack:
        node = stack.pop()
        if node.type in callable_types:
            line = node.start_point[0] + 1
            col = byte_column(data, node.start_byte)
            by_position.setdefault((line, col), node)
            by_line.setdefault(line, []).append(node)
        stack.extend(reversed(node.named_children))
    return by_position, by_line


def _function_facts(engine, function: dict, parse_cache: dict,
                    locator_cache: dict):
    """Equivalent batched-position variant of ``QueryEngine._df_facts``."""
    from . import dataflow as df

    language = function["language"]
    if not df.supported(language):
        return None, language
    key = (function["file_id"], function["file_hash"],
           function["start_line"], function.get("start_col"))
    missing = object()
    facts = engine._facts_cache.get(key, missing)
    if facts is not missing:
        engine._facts_cache.move_to_end(key)
        return facts, language

    data, tree = engine._df_parse(function["path"], language, parse_cache)
    if tree is None:
        return None, language
    locator_key = (function["path"], function["file_hash"], language)
    locator = locator_cache.get(locator_key)
    if locator is None:
        locator = _file_callable_locator(data, tree, language)
        locator_cache[locator_key] = locator
    by_position, by_line = locator
    start_line = function["start_line"]
    start_col = function.get("start_col")
    if start_col is not None:
        node = by_position.get((start_line, start_col))
    else:
        candidates = by_line.get(start_line, ())
        node = candidates[0] if len(candidates) == 1 else None
    facts = df.extract_facts(data, node, language) if node is not None else None
    engine._facts_cache[key] = facts
    if len(engine._facts_cache) > engine._facts_cache_cap:
        engine._facts_cache.popitem(last=False)
    return facts, language


def _input_hash(conn, function: dict) -> str:
    calls = [tuple(row) for row in conn.execute(
        "SELECT e.line,e.col,e.dst,e.dst_name,e.confidence,e.resolver,"
        "COALESCE(dst.body_hash,'') FROM edges e "
        "LEFT JOIN symbols dst ON dst.id=e.dst WHERE e.src=? AND e.kind='calls' "
        "ORDER BY e.line,e.col,e.dst_name,e.dst,e.confidence,e.resolver",
        (function["id"],),
    )]
    return content_hash(_json({
        "version": DATAFLOW_STAGE_VERSION,
        "function": function["id"],
        "body_hash": function["body_hash"],
        "calls": calls,
    }).encode("utf-8"))


def current_stage(conn) -> dict | None:
    row = conn.execute(
        "SELECT g.status,g.stage_version,g.details_json,g.artifact_hash "
        "FROM graph_stage_runs g JOIN meta m "
        "ON m.key='current_graph_revision' "
        "AND g.revision_id=CAST(m.value AS INTEGER) "
        "WHERE g.stage='dataflow'",
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["details"] = json.loads(out.pop("details_json"))
    except (TypeError, ValueError):
        out["details"] = {}
    return out


def _semantic_inputs_queryable(conn) -> tuple[bool, dict]:
    """Reject preserved L1 edges after their semantic world was invalidated.

    L1 intentionally keeps its last published edges visible while a new
    snapshot is pending. G3 may not relabel that prior snapshot as current.
    Repositories that never published L1 remain queryable through L0.
    """
    has_l1 = bool(conn.execute(
        "SELECT EXISTS(SELECT 1 FROM edges WHERE resolver='l1')"
    ).fetchone()[0])
    if not has_l1:
        return True, {"mode": "l0", "reason": "no_published_l1_edges"}
    stage = conn.execute(
        "SELECT g.status,g.revision_id FROM graph_stage_runs g JOIN meta m "
        "ON m.key='current_graph_revision' "
        "AND g.revision_id=CAST(m.value AS INTEGER) "
        "WHERE g.stage='l1'"
    ).fetchone()
    lifecycle = read_l1_lifecycle(conn)
    stage_status = stage["status"] if stage is not None else "missing"
    lifecycle_status = lifecycle.get("status", "not_started")
    published = bool(lifecycle.get("published"))
    queryable = (
        stage_status in {"complete", "partial"}
        and lifecycle_status in {"complete", "partial"}
        and published
    )
    return queryable, {
        "mode": "l1",
        "stage_status": stage_status,
        "lifecycle_status": lifecycle_status,
        "published": published,
        "revision_id": stage["revision_id"] if stage is not None else None,
        "reason": None if queryable else "awaiting_l1_revalidation",
    }


def build(engine, *, force: bool = False) -> dict:
    """Materialize or incrementally refresh the focus-language value graph."""
    conn = engine.conn

    def write():
        counts = _Counts()
        cache: dict = {}
        locator_cache: dict = {}
        functions = _callable_rows(conn)
        live_functions = {row["id"] for row in functions}
        conn.execute("BEGIN IMMEDIATE")
        try:
            queryable, semantic_inputs = _semantic_inputs_queryable(conn)
            if not queryable:
                details = {
                    "persistent": True,
                    "queryable": False,
                    "focus_languages": sorted(FOCUS_LANGUAGES),
                    "functions": len(functions),
                    "rebuilt": 0,
                    "reused": 0,
                    "nodes_written": 0,
                    "edges_written": 0,
                    "nodes": conn.execute(
                        "SELECT COUNT(*) FROM dataflow_nodes").fetchone()[0],
                    "edges": conn.execute(
                        "SELECT COUNT(*) FROM dataflow_edges").fetchone()[0],
                    "unmapped_paths": 0,
                    "path_attempts": 0,
                    "mapped_paths": 0,
                    "mapping_coverage_pct": 0.0,
                    "parse_failures": 0,
                    "partial_functions": 0,
                    "reason": "awaiting_l1_revalidation",
                    "semantic_inputs": semantic_inputs,
                }
                record_current_stage(
                    conn, "dataflow", DATAFLOW_STAGE_VERSION, "partial",
                    details, commit=False)
                conn.commit()
                return {"status": "partial", **details}
            for function in functions:
                counts.functions += 1
                input_hash = _input_hash(conn, function)
                state = conn.execute(
                    "SELECT input_hash,status FROM dataflow_function_state "
                    "WHERE function_id=?", (function["id"],),
                ).fetchone()
                if (not force and state is not None
                        and state["input_hash"] == input_hash
                        and state["status"] == "complete"):
                    counts.reused += 1
                    continue
                facts, language = _function_facts(
                    engine, function, cache, locator_cache)
                if facts is None:
                    counts.parse_failures += 1
                    conn.execute(
                        "INSERT INTO dataflow_function_state(function_id,file_id,"
                        "language,input_hash,status,details_json,built_at) "
                        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(function_id) DO UPDATE SET "
                        "input_hash=excluded.input_hash,status=excluded.status,"
                        "details_json=excluded.details_json,built_at=excluded.built_at",
                        (function["id"], function["file_id"], language,
                         input_hash, "partial", _json({"reason": "facts_unavailable"}),
                         int(time.time())),
                    )
                    continue
                builder = _FunctionBuilder(engine, function, facts, language)
                nodes, edges, unmapped, attempts, mapped = builder.build()
                counts.nodes += nodes
                counts.edges += edges
                counts.unmapped_paths += unmapped
                counts.path_attempts += attempts
                counts.mapped_paths += mapped
                counts.rebuilt += 1
                function_status = (
                    "complete" if function["parse_status"] == "ok"
                    else "partial")
                if function_status == "partial":
                    counts.partial_functions += 1
                conn.execute(
                    "INSERT INTO dataflow_function_state(function_id,file_id,language,"
                    "input_hash,status,details_json,built_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(function_id) DO UPDATE SET file_id=excluded.file_id,"
                    "language=excluded.language,input_hash=excluded.input_hash,"
                    "status=excluded.status,details_json=excluded.details_json,"
                    "built_at=excluded.built_at",
                    (function["id"], function["file_id"], language, input_hash,
                     function_status, _json({
                         "nodes": nodes, "edges": edges,
                         "unmapped_paths": unmapped,
                         "path_attempts": attempts,
                         "mapped_paths": mapped,
                         "parse_status": function["parse_status"],
                     }),
                     int(time.time())),
                )

            if live_functions:
                placeholders = ",".join("?" * len(live_functions))
                conn.execute(
                    f"DELETE FROM dataflow_function_state WHERE function_id NOT IN "
                    f"({placeholders})", tuple(sorted(live_functions)))
                conn.execute(
                    f"DELETE FROM dataflow_nodes WHERE function_id IS NOT NULL "
                    f"AND function_id NOT IN ({placeholders})",
                    tuple(sorted(live_functions)))
            else:
                conn.execute("DELETE FROM dataflow_function_state")
                conn.execute("DELETE FROM dataflow_nodes WHERE function_id IS NOT NULL")
            conn.execute(
                "DELETE FROM dataflow_nodes WHERE symbol_id IS NOT NULL "
                "AND symbol_id NOT IN (SELECT id FROM symbols)")

            status = ("partial" if counts.parse_failures
                      or counts.partial_functions else "complete")
            details = {
                "persistent": True,
                "queryable": True,
                "semantic_inputs": semantic_inputs,
                "focus_languages": sorted(FOCUS_LANGUAGES),
                "functions": counts.functions,
                "rebuilt": counts.rebuilt,
                "reused": counts.reused,
                "nodes_written": counts.nodes,
                "edges_written": counts.edges,
                "nodes": conn.execute(
                    "SELECT COUNT(*) FROM dataflow_nodes").fetchone()[0],
                "edges": conn.execute(
                    "SELECT COUNT(*) FROM dataflow_edges").fetchone()[0],
                "unmapped_paths": counts.unmapped_paths,
                "path_attempts": counts.path_attempts,
                "mapped_paths": counts.mapped_paths,
                "mapping_coverage_pct": round(
                    100 * counts.mapped_paths / max(1, counts.path_attempts), 1),
                "parse_failures": counts.parse_failures,
                "partial_functions": counts.partial_functions,
                "cfg": "may-flow; branch/loop feasibility conservative",
                "heap": {
                    "java": "explicit this/field only; aliases conservative",
                    "python": "explicit self/cls fields and access paths; aliases conservative",
                },
            }
            record_current_stage(
                conn, "dataflow", DATAFLOW_STAGE_VERSION, status, details,
                commit=False)
            conn.commit()
            return {"status": status, **details}
        except BaseException:
            conn.rollback()
            raise

    return retry_on_locked(write)


def ensure(engine) -> dict:
    stage = current_stage(engine.conn)
    if (stage is not None and stage["status"] == "complete"
            and stage["stage_version"] == DATAFLOW_STAGE_VERSION
            and stage["details"].get("persistent")):
        details = dict(stage["details"])
        details.update({
            "cached": True,
            "rebuilt": 0,
            "reused": details.get("functions", 0),
            "nodes_written": 0,
            "edges_written": 0,
        })
        return {"status": "complete", **details}
    return build(engine)


def path(engine, source_selector: str, target_selector: str | None = None,
         *, max_hops: int = 64, max_paths: int = 20):
    """Return persisted value-flow paths from a symbol-backed value node."""
    from .query import Envelope

    env = Envelope()
    # A caller can be unchanged while a callee, its parameters or an L1 target
    # changed.  Verify the complete indexed scope before trusting a reusable
    # interprocedural artifact.
    engine._repair_all(env)
    built = ensure(engine)
    source = engine._resolve_fresh(source_selector, env)
    if not built.get("queryable", True):
        env.warn("dataflow persistente indisponível: o snapshot L1 anterior "
                 "foi invalidado; execute refine antes de consultar caminhos.")
        return source, {"target": target_selector, "paths": [],
                        "persistent": True, "stage": current_stage(engine.conn),
                        "verdict": "unknown"}, env
    source_node = engine.conn.execute(
        "SELECT id FROM dataflow_nodes WHERE symbol_id=?",
        (source["id"],),
    ).fetchone()
    if source_node is None:
        env.warn("dataflow persistente: o símbolo não é um valor Java/Python "
                 "materializado (use um parâmetro, local ou campo).")
        return source, {"target": target_selector, "paths": [],
                        "persistent": True}, env

    target = None
    target_nodes: set[str] | None = None
    if target_selector:
        target = engine._resolve_fresh(target_selector, env)
        target_nodes = {row["id"] for row in engine.conn.execute(
            "SELECT id FROM dataflow_nodes WHERE symbol_id=? OR function_id=?",
            (target["id"], target["id"]),
        )}

    terminal_args: list[str] = []
    if target_nodes is not None:
        if not target_nodes:
            rows = []
        else:
            target_placeholders = ",".join("?" * len(target_nodes))
            terminal_filter = f"AND w.node_id IN ({target_placeholders})"
            terminal_args = sorted(target_nodes)
    else:
        terminal_filter = "AND n.kind IN ('call_argument','return')"

    if target_nodes is None or target_nodes:
        rows = engine.conn.execute(
        "WITH RECURSIVE walk(node_id,hops,confidence,trail,edge_trail) AS ("
        " SELECT ?,0,'certain',','||?||',',''"
        " UNION ALL "
        " SELECT e.dst_node_id,w.hops+1,"
        " CASE WHEN w.confidence='possible' OR e.confidence='possible' "
        " THEN 'possible' WHEN w.confidence='inferred' "
        " OR e.confidence='inferred' THEN 'inferred' ELSE 'certain' END,"
        " w.trail||e.dst_node_id||',',"
        " w.edge_trail||CASE WHEN w.edge_trail='' THEN '' ELSE ',' END||e.id"
        " FROM walk w JOIN dataflow_edges e ON e.src_node_id=w.node_id"
        " WHERE w.hops<? AND instr(w.trail,','||e.dst_node_id||',')=0"
        ") SELECT w.node_id,w.hops,w.confidence,w.trail,w.edge_trail "
        "FROM walk w JOIN dataflow_nodes n ON n.id=w.node_id "
        f"WHERE w.hops>0 {terminal_filter} "
        "ORDER BY w.hops,w.node_id LIMIT 10000",
        (source_node["id"], source_node["id"], max_hops, *terminal_args),
        ).fetchall()

    candidates = []
    for row in rows:
        node = engine.conn.execute(
            "SELECT * FROM dataflow_nodes WHERE id=?", (row["node_id"],)
        ).fetchone()
        if node is None:
            continue
        node_ids = [item for item in row["trail"].strip(",").split(",") if item]
        placeholders = ",".join("?" * len(node_ids))
        by_id = {item["id"]: dict(item) for item in engine.conn.execute(
            f"SELECT id,kind,name,access_path,line,col,details_json "
            f"FROM dataflow_nodes WHERE id IN ({placeholders})", node_ids)}
        projection = []
        for node_id in node_ids:
            item = by_id[node_id]
            try:
                details = json.loads(item.pop("details_json"))
            except (TypeError, ValueError):
                details = {}
            item["details"] = details
            projection.append(item)
        edge_ids = [item for item in row["edge_trail"].split(",") if item]
        edge_projection = []
        if edge_ids:
            edge_placeholders = ",".join("?" * len(edge_ids))
            edges_by_id = {item["id"]: dict(item) for item in
                           engine.conn.execute(
                f"SELECT id,relation,line,col,confidence,interprocedural,"
                f"evidence_json FROM dataflow_edges WHERE id IN "
                f"({edge_placeholders})", edge_ids)}
            for edge_id in edge_ids:
                edge = edges_by_id[edge_id]
                try:
                    evidence = json.loads(edge.pop("evidence_json"))
                except (TypeError, ValueError):
                    evidence = {}
                edge["kind"] = "flows_to"
                edge["interprocedural"] = bool(edge["interprocedural"])
                edge["evidence"] = evidence
                edge_projection.append(edge)
        candidates.append({
            "hops": row["hops"], "confidence": row["confidence"],
            "nodes": projection, "edges": edge_projection,
        })
        if len(candidates) >= max_paths:
            break
    if len(candidates) >= max_paths:
        env.truncated = True
    env.warn("dataflow persistente é may-flow: CFG e aliases/heap são "
             "conservadores; confirme caminhos de segurança no código.")
    return source, {
        "target": dict(target) if target else None,
        "paths": candidates, "persistent": True,
        "stage": current_stage(engine.conn),
    }, env
