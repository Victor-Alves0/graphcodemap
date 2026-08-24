"""Reproduções adversariais para o contrato incremental.

Os ``xfail`` restantes dependem de mudanças na camada de query; as invariantes
locais do indexador/watcher são regressões normais e devem permanecer verdes.
"""

from __future__ import annotations

import os
import threading
import types

import pytest

from codegraph import CodeGraph
from codegraph.db import SCHEMA_VERSION
from codegraph.indexer import Indexer
from codegraph.watcher import Watcher


def _python_call_graph(tmp_path):
    (tmp_path / "lib.py").write_text(
        "def target():\n    return 1\n", encoding="utf-8")
    (tmp_path / "use.py").write_text(
        "from lib import target\n\ndef caller():\n    return target()\n",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    graph.index()
    return graph


def test_change_impact_cannot_index_file_outside_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    (tmp_path / "host_secret.py").write_text(
        "def host_secret_token_abc():\n    return 42\n", encoding="utf-8"
    )
    graph = CodeGraph(repo)
    graph.index()
    diff = (
        "diff --git a/../host_secret.py b/../host_secret.py\n"
        "--- a/../host_secret.py\n"
        "+++ b/../host_secret.py\n"
    )

    graph.change_impact(diff)
    indexed_paths = {
        row["path"] for row in graph.indexer.conn.execute("SELECT path FROM files")
    }
    leaked, _env = graph.find_symbol("host_secret_token_abc")
    graph.close()
    assert "../host_secret.py" not in indexed_paths
    assert not leaked


def test_index_does_not_follow_source_symlink_outside_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = tmp_path / "host_secret.py"
    secret.write_text(
        "def symlink_host_secret():\n    return 42\n", encoding="utf-8"
    )
    link = repo / "linked.py"
    try:
        link.symlink_to(secret)
    except OSError as exc:
        pytest.skip(f"symlink indisponível neste host: {exc}")

    graph = CodeGraph(repo)
    graph.index()
    leaked, _env = graph.find_symbol("symlink_host_secret")
    indexed_paths = {
        row["path"] for row in graph.indexer.conn.execute("SELECT path FROM files")
    }
    graph.close()
    assert "linked.py" not in indexed_paths
    assert not leaked


def test_change_impact_includes_callers_of_renamed_symbol(tmp_path):
    graph = _python_call_graph(tmp_path)
    (tmp_path / "lib.py").write_text(
        "def renamed():\n    return 1\n", encoding="utf-8"
    )

    data, _env = graph.change_impact("lib.py")

    graph.close()
    assert any(row["fqn"] == "use.caller" for row in data["impacted"])


def test_change_impact_java_file_includes_callers_of_nested_method(tmp_path):
    (tmp_path / "Service.java").write_text(
        """package app;
public class Service {
    public static void work() {}
}
""",
        encoding="utf-8",
    )
    (tmp_path / "Use.java").write_text(
        """package app;
public class Use {
    public void run() { Service.work(); }
}
""",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    graph.index()

    data, _env = graph.change_impact("Service.java")

    graph.close()
    assert [row["fqn"] for row in data["changed_symbols"]] == [
        "app.Service", "app.Service.work",
    ]
    assert any(row["fqn"] == "app.Use.run" for row in data["impacted"])


def test_change_impact_python_file_includes_callers_of_nested_method(tmp_path):
    (tmp_path / "service.py").write_text(
        """class Service:
    def work(self):
        return 1
""",
        encoding="utf-8",
    )
    (tmp_path / "use.py").write_text(
        """from service import Service

def run():
    service = Service()
    return service.work()
""",
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    graph.index()

    data, _env = graph.change_impact("service.py")

    graph.close()
    assert [row["fqn"] for row in data["changed_symbols"]] == [
        "service.Service", "service.Service.work", "service.Service.work.self",
    ]
    assert any(row["fqn"] == "use.run" for row in data["impacted"])


def test_nonempty_callers_query_discovers_new_caller_file(tmp_path):
    graph = _python_call_graph(tmp_path)
    # Já existe use.caller, então a consulta não cai no read-repair global de
    # resultado vazio. O novo arquivo não pertence ao conjunto `involved`.
    (tmp_path / "second.py").write_text(
        "from lib import target\n\ndef second():\n    return target()\n",
        encoding="utf-8",
    )

    _sym, rows, env = graph.callers("lib.target")

    graph.close()
    assert any(row["other_fqn"] == "second.second" for row in rows)
    assert env.fresh is False


def test_no_watcher_discovers_new_caller_inside_backstop_window(tmp_path):
    graph = _python_call_graph(tmp_path)
    # Consome uma sweep e deixa `_last_full_sweep` recente. Sem watcher isso não
    # pode criar uma janela de 30s em que arquivos novos ficam invisíveis.
    graph.find_symbol("target")
    (tmp_path / "third.py").write_text(
        "from lib import target\n\ndef third():\n    return target()\n",
        encoding="utf-8",
    )

    _sym, rows, env = graph.callers("lib.target")

    graph.close()
    assert any(row["other_fqn"] == "third.third" for row in rows)
    assert env.fresh is False


def test_same_size_same_second_edit_is_not_reported_as_fresh(tmp_path):
    path = tmp_path / "a.py"
    before = "def before():\n    return 1\n"
    after = "def after_():\n    return 2\n"
    assert len(before.encode()) == len(after.encode())
    path.write_text(before, encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    old_ns = path.stat().st_mtime_ns

    path.write_text(after, encoding="utf-8")
    # Mantém o mesmo segundo e o mesmo tamanho, mas muda a fração nanossegundo:
    # a persistência antiga com int(st_mtime) não distinguia os snapshots.
    second, fraction = divmod(old_ns, 1_000_000_000)
    same_second_ns = second * 1_000_000_000 + (fraction + 100_000_000) % 1_000_000_000
    os.utime(path, ns=(same_second_ns, same_second_ns))

    old_rows, old_env = graph.find_symbol("before")
    new_rows, new_env = graph.find_symbol("after_")
    graph.close()
    assert not old_rows
    assert new_rows
    assert old_env.fresh is False or new_env.fresh is False


def test_watcher_is_not_current_while_drain_is_applying_batch(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("def before():\n    return 1\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.close()

    watcher = Watcher(tmp_path, debounce=60)
    watcher.ix = Indexer(tmp_path)
    watcher._observer = types.SimpleNamespace(is_alive=lambda: True)
    watcher._pending = {"a.py"}
    path.write_text("def after_():\n    return 2\n", encoding="utf-8")

    entered = threading.Event()
    release = threading.Event()
    real_index_file = watcher.ix.index_file

    def blocked_index_file(rel, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_index_file(rel, *args, **kwargs)

    watcher.ix.index_file = blocked_index_file
    thread = threading.Thread(target=watcher.drain, daemon=True)
    thread.start()
    try:
        assert entered.wait(5)
        # Neste ponto _pending já foi esvaziado, mas o arquivo ainda não foi
        # aplicado. QueryEngine usa este sinal para decidir pular o read-repair.
        assert watcher.is_current() is False
    finally:
        release.set()
        thread.join(5)
        watcher._observer = None
        watcher.ix.close()


def test_deleted_l1_target_invalidates_inbound_edge_provenance(tmp_path):
    graph = _python_call_graph(tmp_path)
    edge = graph.indexer.conn.execute(
        "SELECT e.id FROM edges e JOIN files f ON e.file_id=f.id "
        "WHERE f.path='use.py' AND e.kind='calls' AND e.dst_name LIKE '%target' "
        "LIMIT 1"
    ).fetchone()
    assert edge is not None
    graph.indexer.conn.execute(
        "UPDATE edges SET confidence='certain', resolver='l1' WHERE id=?",
        (edge["id"],),
    )
    graph.indexer.conn.commit()

    graph.indexer.remove_file("lib.py")
    stale = graph.indexer.conn.execute(
        "SELECT dst, confidence, resolver FROM edges WHERE id=?", (edge["id"],)
    ).fetchone()
    graph.close()
    assert stale["dst"] is None
    assert (stale["confidence"], stale["resolver"]) == ("possible", "l0")


def test_returned_target_does_not_keep_l1_provenance_without_revalidation(tmp_path):
    graph = _python_call_graph(tmp_path)
    edge = graph.indexer.conn.execute(
        "SELECT e.id FROM edges e JOIN files f ON e.file_id=f.id "
        "WHERE f.path='use.py' AND e.kind='calls' AND e.dst_name LIKE '%target' "
        "LIMIT 1"
    ).fetchone()
    assert edge is not None
    graph.indexer.conn.execute(
        "UPDATE edges SET confidence='certain', resolver='l1' WHERE id=?",
        (edge["id"],),
    )
    graph.indexer.conn.commit()
    graph.indexer.remove_file("lib.py")

    (tmp_path / "lib.py").write_text(
        "def target():\n    return 2\n", encoding="utf-8")
    graph.indexer.index_file("lib.py")
    graph.indexer.resolve_edges()
    returned = graph.indexer.conn.execute(
        "SELECT dst, confidence, resolver FROM edges WHERE id=?", (edge["id"],)
    ).fetchone()
    graph.close()
    assert returned["dst"] is not None
    assert (returned["confidence"], returned["resolver"]) == ("inferred", "l0")


def test_removed_l1_overload_stays_possible_without_receiver_type(tmp_path):
    (tmp_path / "a.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    (tmp_path / "caller.py").write_text(
        "def call(obj):\n    return obj.run()\n", encoding="utf-8"
    )
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.indexer.conn.execute(
        "UPDATE edges SET dst=(SELECT id FROM symbols WHERE fqn='a.run'), "
        "confidence='inferred', resolver='l1' "
        "WHERE kind='calls' AND dst_name='run'"
    )
    graph.indexer.conn.commit()

    graph.indexer.remove_file("a.py")
    invalidated = graph.indexer.conn.execute(
        "SELECT dst, confidence, resolver FROM edges "
        "WHERE kind='calls' AND dst_name='run'"
    ).fetchall()
    assert [(row["dst"], row["confidence"], row["resolver"])
            for row in invalidated] == [(None, "possible", "l0")]

    graph.indexer.resolve_edges()
    resolved = graph.indexer.conn.execute(
        "SELECT s.fqn, e.confidence, e.resolver FROM edges e "
        "LEFT JOIN symbols s ON e.dst=s.id "
        "WHERE e.kind='calls' AND e.dst_name='run'"
    ).fetchall()
    graph.close()
    # Removing one overload does not turn ``obj.run()`` into proof that the
    # remaining unrelated short-name candidate is its receiver method.  The
    # former fallback is the same class of error as dict.get → business.get.
    assert [(row["fqn"], row["confidence"], row["resolver"])
            for row in resolved] == [(None, "possible", "l0")]


@pytest.mark.parametrize("operation", ["index", "remove"])
def test_incremental_file_operations_reject_parent_escape(tmp_path, operation):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.py").write_text(
        "def outside():\n    return 1\n", encoding="utf-8"
    )
    indexer = Indexer(repo)
    try:
        with pytest.raises(ValueError, match="fora da raiz"):
            if operation == "index":
                indexer.index_file("../outside.py")
            else:
                indexer.remove_file("../outside.py")
        assert indexer.conn.execute(
            "SELECT 1 FROM files WHERE path LIKE '%outside.py'"
        ).fetchone() is None
    finally:
        indexer.close()


def test_index_persists_nanosecond_mtime(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("def value():\n    return 1\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    stored = graph.indexer.conn.execute(
        "SELECT mtime FROM files WHERE path='a.py'"
    ).fetchone()["mtime"]
    graph.close()
    assert stored == path.stat().st_mtime_ns


def test_mtime_schema_upgrade_rebuilds_derived_index(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("def value():\n    return 1\n", encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    graph.indexer.conn.execute(
        "UPDATE meta SET value='4' WHERE key='schema_version'"
    )
    graph.indexer.conn.commit()
    graph.close()

    rebuilt = CodeGraph(tmp_path)
    assert rebuilt.indexer.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert rebuilt.indexer.conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()["value"] == SCHEMA_VERSION
    rebuilt.index()
    stored = rebuilt.indexer.conn.execute(
        "SELECT mtime FROM files WHERE path='a.py'"
    ).fetchone()["mtime"]
    rebuilt.close()
    assert stored == path.stat().st_mtime_ns
