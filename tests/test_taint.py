"""Taint fonte→sink: sources/sinks/sanitizers, intra e inter-procedural, JS."""

from __future__ import annotations

import json
import textwrap

import pytest

from codegraph import CodeGraph

PY = '''
import os


def get_input():
    return input()


def run_cmd(cmd):
    return os.system(cmd)


def main():
    data = get_input()
    run_cmd(data)


def sanitized():
    raw = input()
    ok = escape(raw)
    run_cmd(ok)


def direct():
    x = input()
    os.system(x)
'''

JS = '''
function handler(req) {
  const q = req;
  db.query(q);
}

function safe(req) {
  const clean = encodeURIComponent(req);
  db.query(clean);
}
'''


@pytest.fixture()
def cgt(tmp_path):
    (tmp_path / "app.py").write_text(textwrap.dedent(PY), encoding="utf-8")
    (tmp_path / "web.js").write_text(textwrap.dedent(JS), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    yield graph
    graph.close()


def _sink_funcs(findings):
    return {f["sink"]["callee"] for f in findings}


# -- scan ---------------------------------------------------------------------

def test_scan_finds_interprocedural_command_injection(cgt):
    data, _ = cgt.taint()
    assert data["findings"]
    # main: get_input() -> run_cmd -> os.system
    hit = [f for f in data["findings"] if f["origin"]["func_fqn"] == "app.main"]
    assert hit
    assert hit[0]["sink"]["callee"] == "system"
    assert any(st["callee"] == "run_cmd" for st in hit[0]["steps"])


def test_scan_direct_source_to_sink(cgt):
    data, _ = cgt.taint()
    direct = [f for f in data["findings"] if f["origin"]["func_fqn"] == "app.direct"]
    assert direct and direct[0]["sink"]["callee"] == "system"


def test_sanitizer_cuts_the_flow(cgt):
    data, _ = cgt.taint()
    # 'sanitized' passa por escape() antes do sink → nenhum achado dessa função
    assert not any(f["origin"]["func_fqn"] == "app.sanitized" for f in data["findings"])


# -- entry --------------------------------------------------------------------

def test_entry_mode_assumes_params_tainted(cgt):
    data, _ = cgt.taint(entry="web.handler")
    assert len(data["findings"]) == 1
    assert data["findings"][0]["sink"]["callee"] == "query"


def test_entry_sanitized_is_clean(cgt):
    data, _ = cgt.taint(entry="web.safe")
    assert data["findings"] == []


# -- confiança e franqueza ----------------------------------------------------

def test_confidence_present(cgt):
    data, _ = cgt.taint()
    assert all(f["confidence"] in ("certain", "inferred", "possible")
               for f in data["findings"])


def test_warns_it_is_may_taint(cgt):
    _, env = cgt.taint()
    assert any("may-taint" in w.lower() or "candidat" in w.lower() for w in env.warnings)


# -- configuração -------------------------------------------------------------

def test_custom_rules_add_sink(tmp_path):
    (tmp_path / "m.py").write_text(textwrap.dedent('''
        def f():
            x = input()
            danger(x)
    '''), encoding="utf-8")
    cfgdir = tmp_path / ".codegraph"
    cfgdir.mkdir(exist_ok=True)
    (cfgdir / "taint.json").write_text(json.dumps({"sinks": ["danger"]}),
                                       encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    try:
        data, _ = graph.taint()
        assert any(f["sink"]["callee"] == "danger" for f in data["findings"])
    finally:
        graph.close()


def test_remove_rule_suppresses(tmp_path):
    (tmp_path / "m.py").write_text(textwrap.dedent('''
        def f():
            x = input()
            eval(x)
    '''), encoding="utf-8")
    cfgdir = tmp_path / ".codegraph"
    cfgdir.mkdir(exist_ok=True)
    (cfgdir / "taint.json").write_text(
        json.dumps({"remove": {"sinks": ["eval"]}}), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    try:
        data, _ = graph.taint()
        assert not any(f["sink"]["callee"] == "eval" for f in data["findings"])
    finally:
        graph.close()
