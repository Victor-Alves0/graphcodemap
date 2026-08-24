from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from codegraph import CodeGraph, l1
from codegraph.l1.jdtls import JdtlsResolver
from codegraph.l1.python_jedi import JediResolver
from codegraph.tool_config import apply_saved_environment


FIXTURE = Path(__file__).parent / "fixtures" / "semantic_link_matrix"

PYTHON_MATRIX = {
    "direct": (
        "linkmatrix.app.direct_local", "linkmatrix.app.local_helper"),
    "imported": (
        "linkmatrix.app.imported", "linkmatrix.services.imported_helper"),
    "typed_receiver": (
        "linkmatrix.app.typed_receiver", "linkmatrix.services.Service.direct"),
    "inherited": (
        "linkmatrix.app.inherited_receiver",
        "linkmatrix.services.BaseService.inherited"),
    "interface": (
        "linkmatrix.app.interface_receiver", "linkmatrix.services.Runner.run"),
}

JAVA_MATRIX = {
    "direct": (
        "linkmatrix.app.App.directLocal", "linkmatrix.app.App.localHelper"),
    "imported": (
        "linkmatrix.app.App.imported",
        "linkmatrix.service.Utility.importedHelper"),
    "typed_receiver": (
        "linkmatrix.app.App.typedReceiver",
        "linkmatrix.service.Service.direct"),
    "inherited": (
        "linkmatrix.app.App.inheritedReceiver",
        "linkmatrix.service.BaseService.inherited"),
    "interface": (
        "linkmatrix.app.App.interfaceReceiver", "linkmatrix.api.Runner.run"),
    "overload": (
        "linkmatrix.app.App.overload", "linkmatrix.service.Service.overload"),
    "method_reference": (
        "linkmatrix.app.App.methodReference",
        "linkmatrix.service.BaseService.inherited"),
}


def _copy_fixture(tmp_path: Path, language: str) -> Path:
    target = tmp_path / language
    shutil.copytree(FIXTURE / language, target)
    return target


def _assert_matrix(graph: CodeGraph, matrix: dict[str, tuple[str, str]]) -> None:
    for category, (caller, callee) in matrix.items():
        rows = graph.indexer.conn.execute(
            "SELECT e.confidence, e.resolver, dst.fqn, dst.signature "
            "FROM edges e JOIN symbols src ON src.id=e.src "
            "LEFT JOIN symbols dst ON dst.id=e.dst "
            "WHERE e.kind='calls' AND src.fqn=? ORDER BY dst.fqn, dst.signature",
            (caller,),
        ).fetchall()
        exact = [row for row in rows if row["fqn"] == callee]
        assert len(exact) == 1, (
            category, caller, callee, [dict(row) for row in rows])
        assert exact[0]["resolver"] == "l1", (category, dict(exact[0]))
        assert exact[0]["confidence"] == "certain", (
            category, dict(exact[0]))


@pytest.mark.skipif(not JediResolver.available(), reason="jedi unavailable")
def test_python_real_resolver_passes_shared_semantic_matrix(tmp_path):
    graph = CodeGraph(_copy_fixture(tmp_path, "python"))
    graph.index()

    stats = l1.refine(graph.indexer)

    assert stats["status"] == "complete"
    _assert_matrix(graph, PYTHON_MATRIX)
    assert stats["coverage"]["total_sites"] == len(PYTHON_MATRIX)
    assert stats["coverage"]["certain_sites"] == len(PYTHON_MATRIX)
    assert stats["coverage"]["certain_pct"] == 100.0
    graph.close()


def _live_java_enabled() -> bool:
    if os.environ.get("CODEGRAPH_RUN_SEMANTIC_LIVE") != "1":
        return False
    apply_saved_environment()
    return JdtlsResolver.available()


@pytest.mark.semantic_live
@pytest.mark.skipif(
    not _live_java_enabled(),
    reason="set CODEGRAPH_RUN_SEMANTIC_LIVE=1 with JDTLS configured",
)
def test_java_real_resolver_passes_shared_semantic_matrix(tmp_path):
    graph = CodeGraph(_copy_fixture(tmp_path, "java"))
    graph.index()

    stats = l1.refine(graph.indexer)

    assert stats["status"] == "complete", stats
    _assert_matrix(graph, JAVA_MATRIX)
    overload = graph.indexer.conn.execute(
        "SELECT dst.signature FROM edges e JOIN symbols src ON src.id=e.src "
        "JOIN symbols dst ON dst.id=e.dst WHERE e.kind='calls' "
        "AND e.resolver='l1' AND src.fqn='linkmatrix.app.App.overload'"
    ).fetchone()
    assert overload is not None and "int" in (overload["signature"] or "")
    assert stats["coverage"]["total_sites"] == len(JAVA_MATRIX)
    assert stats["coverage"]["certain_sites"] == len(JAVA_MATRIX)
    assert stats["coverage"]["certain_pct"] == 100.0
    graph.close()
