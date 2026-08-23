"""Regressions for Python framework wiring and conservative L0 precision."""

from __future__ import annotations

import textwrap

from codegraph import CodeGraph
from codegraph import render


def _graph(tmp_path, files: dict[str, str]) -> CodeGraph:
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    return graph


def test_fastapi_depends_records_callback_reference_not_direct_call(tmp_path):
    graph = _graph(tmp_path, {
        "app/deps.py": """
            from fastapi import Depends

            def get_membership():
                return object()

            def endpoint(membership = Depends(get_membership)):
                return membership
        """,
    })
    try:
        _symbol, refs, _env = graph.references("app.deps.get_membership")
        assert [(row["kind"], row["src_fqn"]) for row in refs] == [
            ("references", "app.deps.endpoint")
        ]

        _symbol, callers, _env = graph.callers("app.deps.get_membership")
        assert callers == []
    finally:
        graph.close()


def test_framework_callback_arguments_are_references_in_body_and_decorator(
        tmp_path):
    graph = _graph(tmp_path, {
        "app/api.py": """
            def require_admin():
                return True

            class TenantMiddleware:
                pass

            @router.get("/tenants", dependencies=[Depends(require_admin)])
            def list_tenants():
                app.add_middleware(TenantMiddleware)
        """,
    })
    try:
        _symbol, auth_refs, _env = graph.references("app.api.require_admin")
        assert {(row["kind"], row["src_fqn"]) for row in auth_refs} == {
            ("references", "app.api.list_tenants")
        }

        _symbol, middleware_refs, _env = graph.references(
            "app.api.TenantMiddleware")
        assert {(row["kind"], row["src_fqn"]) for row in middleware_refs} == {
            ("references", "app.api.list_tenants")
        }
        assert graph.callers("app.api.TenantMiddleware")[1] == []
    finally:
        graph.close()


def test_cross_module_member_fallback_does_not_hijack_unique_function(tmp_path):
    graph = _graph(tmp_path, {
        "app/model_policy.py": """
            def get(model):
                return model
        """,
        "app/webhook.py": """
            def verify(request):
                return request.headers.get("stripe-signature", "")
        """,
    })
    try:
        _symbol, callers, _env = graph.callers("app.model_policy.get")
        assert callers == []
        edge = graph.indexer.conn.execute(
            "SELECT dst, confidence FROM edges WHERE dst_name='get'"
        ).fetchone()
        assert edge is not None
        assert edge["dst"] is None
        assert edge["confidence"] == "possible"
    finally:
        graph.close()


def test_same_module_dict_get_does_not_hijack_business_function(tmp_path):
    graph = _graph(tmp_path, {
        "app/model_policy.py": """
            BY_ID = {}

            def get(model):
                return model

            def lookup(model_id):
                return BY_ID.get(model_id)

            def explicit_use(model_id):
                return get(model_id)
        """,
    })
    try:
        _symbol, callers, _env = graph.callers("app.model_policy.get")
        assert [(row["other_fqn"], row["line"]) for row in callers] == [
            ("app.model_policy.explicit_use", 11)
        ]
    finally:
        graph.close()


def test_unknown_member_does_not_resolve_even_with_multiple_candidates(tmp_path):
    graph = _graph(tmp_path, {
        "main.py": """
            VALUES = {}

            def get(key):
                return key

            def lookup(key):
                return VALUES.get(key)
        """,
        "other.py": "def get(key):\n    return key\n",
    })
    try:
        assert graph.callers("main.get")[1] == []
        assert graph.callers("other.get")[1] == []
        edges = graph.indexer.conn.execute(
            "SELECT dst FROM edges WHERE dst_name='get' AND line=8"
        ).fetchall()
        assert len(edges) == 1 and edges[0]["dst"] is None
    finally:
        graph.close()


def test_leading_dot_fluent_member_stays_dangling_without_crashing(tmp_path):
    graph = _graph(tmp_path, {
        "app.py": """
            def execute(value):
                return value

            def query(db):
                return (db
                        .execute("select 1"))
        """,
    })
    try:
        assert graph.callers("app.execute")[1] == []
        edge = graph.indexer.conn.execute(
            "SELECT dst FROM edges WHERE dst_name='execute' AND line=7"
        ).fetchone()
        assert edge is not None and edge["dst"] is None
    finally:
        graph.close()


def test_nested_test_double_cannot_be_cross_module_fallback_target(tmp_path):
    graph = _graph(tmp_path, {
        "app/service.py": """
            def save(db, statement):
                return db.execute(statement)
        """,
        "tests/test_service.py": """
            def test_save():
                class FakeDb:
                    def execute(self, statement):
                        return statement
        """,
    })
    try:
        _symbol, callers, _env = graph.callers(
            "tests.test_service.test_save.FakeDb.execute")
        assert callers == []
        edge = graph.indexer.conn.execute(
            "SELECT dst FROM edges WHERE dst_name='execute' "
            "AND file_id=(SELECT id FROM files WHERE path='app/service.py')"
        ).fetchone()
        assert edge is not None and edge["dst"] is None
    finally:
        graph.close()


def test_production_candidate_wins_over_test_path_fallback(tmp_path):
    graph = _graph(tmp_path, {
        "app/real.py": "def process():\n    return 1\n",
        "tests/test_real.py": "def process():\n    return 2\n",
        "app/use.py": "def run():\n    return process()\n",
    })
    try:
        edge = graph.indexer.conn.execute(
            "SELECT f.path, e.confidence FROM edges e "
            "JOIN symbols s ON s.id=e.dst JOIN files f ON f.id=s.file_id "
            "WHERE e.dst_name='process' AND e.file_id=(SELECT id FROM files "
            "WHERE path='app/use.py')"
        ).fetchall()
        assert [(row["path"], row["confidence"]) for row in edge] == [
            ("app/real.py", "inferred")
        ]
    finally:
        graph.close()


def test_python_json_loads_is_not_unsafe_deserialization_sink(tmp_path):
    graph = _graph(tmp_path, {
        "app/parsing.py": """
            import json
            import pickle
            import yaml

            def safe_json():
                payload = input()
                return json.loads(payload)

            def unsafe_pickle():
                payload = input()
                return pickle.loads(payload)

            def unsafe_yaml():
                payload = input()
                return yaml.load(payload)
        """,
    })
    try:
        data, _env = graph.taint()
        by_origin = {
            finding["origin"]["func_fqn"]: finding["sink"]["qualified"]
            for finding in data["findings"]
            if finding["sink"]["callee"] in {"load", "loads"}
        }
        assert "app.parsing.safe_json" not in by_origin
        assert by_origin["app.parsing.unsafe_pickle"] == "pickle.loads"
        assert by_origin["app.parsing.unsafe_yaml"] == "yaml.load"
    finally:
        graph.close()


def test_deserializer_classification_uses_import_identity_not_alias_text(tmp_path):
    graph = _graph(tmp_path, {
        "app/aliases.py": """
            import pickle as json
            import json as pickle

            def unsafe_alias():
                payload = input()
                return json.loads(payload)

            def safe_alias():
                payload = input()
                return pickle.loads(payload)

            def shadowed(json):
                payload = input()
                return json.loads(payload)
        """,
    })
    try:
        data, _env = graph.taint()
        by_origin = {
            finding["origin"]["func_fqn"]: finding["sink"]["qualified"]
            for finding in data["findings"]
            if finding["sink"]["callee"] == "loads"
        }
        assert by_origin["app.aliases.unsafe_alias"] == "pickle.loads"
        assert "app.aliases.safe_alias" not in by_origin
        # Unknown parameter identity remains fail-closed; it cannot inherit the
        # safe stdlib-json identity merely because it is named ``json``.
        assert by_origin["app.aliases.shadowed"] == "loads"
    finally:
        graph.close()


def test_parameters_and_local_assignments_shadow_global_bindings(tmp_path):
    graph = _graph(tmp_path, {
        "app/wiring.py": """
            class Service:
                def run(self):
                    return True

            service = Service()

            def handler():
                return True

            def uses_global():
                register(handler)
                return service.run()

            def parameters(service, handler):
                register(handler)
                return service.run()

            def locals(payload):
                handler = payload
                service = payload
                register(handler)
                return service.run()

            def loops(services, handlers):
                for service in services:
                    service.run()
                for handler in handlers:
                    register(handler)

            def scoped(ctx, services, handlers):
                with ctx() as service:
                    service.run()
                try:
                    consume()
                except Error as handler:
                    register(handler)
                return ([service.run() for service in services],
                        [register(handler) for handler in handlers])

            def comprehension_scope(services, handlers):
                [service.run() for service in services]
                [register(handler) for handler in handlers]
                service.run()
                register(handler)

            def local_import():
                from app.other import handler
                register(handler)
        """,
        "app/other.py": "def handler():\n    return False\n",
    })
    try:
        _symbol, handler_refs, _env = graph.references("app.wiring.handler")
        assert [row["src_fqn"] for row in handler_refs] == [
            "app.wiring.uses_global", "app.wiring.comprehension_scope"
        ]
        _symbol, run_callers, _env = graph.callers("app.wiring.Service.run")
        assert [row["other_fqn"] for row in run_callers] == [
            "app.wiring.uses_global", "app.wiring.comprehension_scope"
        ]
        _symbol, imported_refs, _env = graph.references(
            "app.other.handler", kind="references")
        assert [row["src_fqn"] for row in imported_refs] == [
            "app.wiring.local_import"
        ]
    finally:
        graph.close()


def test_calls_summary_counts_only_certain_edges_as_reliable(tmp_path):
    graph = _graph(tmp_path, {
        "a.py": "def helper():\n    return 1\n",
        "b.py": "from a import helper\n\ndef use():\n    return helper()\n",
    })
    try:
        output = render.calls(*graph.callers("a.helper"), "callers de", "in")
        assert "0 confiáveis, 1 inferidas, 0 candidatos" in output
        assert "1 confiáveis" not in output
    finally:
        graph.close()
