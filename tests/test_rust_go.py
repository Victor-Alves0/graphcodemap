from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph

LIB_RS = '''
use std::collections::HashMap;

const MAX: usize = 4;

/// Métricas agregadas.
struct Metrics {
    executed: usize,
}

impl Metrics {
    fn new() -> Self {
        Self { executed: 0 }
    }

    fn add(&mut self) {
        self.executed += 1;
    }
}

trait Processor {
    fn process(&self) -> u32;
}

struct ComplexProcessor;

impl Processor for ComplexProcessor {
    fn process(&self) -> u32 {
        fibonacci(5)
    }
}

fn fibonacci(n: u32) -> u32 {
    if n < 2 { n } else { fibonacci(n - 1) + fibonacci(n - 2) }
}

fn main() {
    let mut m = Metrics::new();
    m.add();
    let p = ComplexProcessor;
    p.process();
    println!("{}", fibonacci(3));
}
'''

MAIN_GO = '''
package main

import (
    "encoding/json"
    "fmt"
)

const MaxWorkers = 4

var ErrBad = fmt.Errorf("bad")

type Metrics struct {
    Executed int
}

func (m *Metrics) Average() float64 {
    return float64(m.Executed)
}

type Processor interface {
    Process(t int) error
}

type BaseProcessor struct {
    Name string
}

type ComplexProcessor struct {
    BaseProcessor
}

func (p *ComplexProcessor) Process(t int) error {
    return nil
}

func Fibonacci(n int) int {
    if n < 2 {
        return n
    }
    return Fibonacci(n-1) + Fibonacci(n-2)
}

func main() {
    m := &Metrics{}
    fmt.Println(m.Average())
    fmt.Println(Fibonacci(10))
    b, _ := json.Marshal(m)
    _ = b
    m.Load()
    s := make([]int, 0)
    s = append(s, 1)
    _ = s
}
'''

CACHE_PY = '''
class Cache:
    def load(self):
        return {}
'''


@pytest.fixture()
def cg2(tmp_path):
    (tmp_path / "lib.rs").write_text(textwrap.dedent(LIB_RS), encoding="utf-8")
    (tmp_path / "main.go").write_text(textwrap.dedent(MAIN_GO), encoding="utf-8")
    (tmp_path / "cache.py").write_text(textwrap.dedent(CACHE_PY), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    yield graph
    graph.close()


def fqns(rows):
    return {r["fqn"] for r in rows}


# -- Rust ---------------------------------------------------------------------

def test_rust_symbols(cg2):
    assert cg2.stats()["parse_partial"] == 0
    rows, _ = cg2.find_symbol("Metrics", kind="struct")
    assert "lib.Metrics" in fqns(rows)
    rows, _ = cg2.find_symbol("new")
    assert "lib.Metrics.new" in fqns(rows)
    rows, _ = cg2.find_symbol("Processor", kind="interface")
    assert "lib.Processor" in fqns(rows)
    rows, _ = cg2.find_symbol("MAX", kind="constant")
    assert "lib.MAX" in fqns(rows)


def test_rust_impl_method_containment(cg2):
    info, _ = cg2.symbol_info("lib.Metrics")
    assert {c["name"] for c in info["children"]} >= {"new", "add"}


def test_rust_doc_comment(cg2):
    info, _ = cg2.symbol_info("lib.Metrics")
    assert "Métricas agregadas" in (info["symbol"]["doc"] or "")


def test_rust_trait_impl_inherits(cg2):
    sym, rows, _ = cg2.references("lib.Processor", kind="inherits")
    assert any(r["src_fqn"] == "lib.ComplexProcessor" for r in rows)


def test_rust_scoped_call_inferred(cg2):
    # Metrics::new() em main → alvo único → inferred
    sym, rows, _ = cg2.callers("lib.Metrics.new")
    confs = {r["other_fqn"]: r["confidence"] for r in rows}
    assert confs.get("lib.main") == "inferred"


def test_rust_recursion_edge(cg2):
    sym, rows, _ = cg2.callers("lib.fibonacci")
    assert any(r["other_fqn"] == "lib.fibonacci" for r in rows)


def test_rust_call_inside_macro(cg2):
    # println!("{}", fibonacci(3)) — call dentro de token_tree de macro
    sym, rows, _ = cg2.callers("lib.fibonacci")
    assert any(r["other_fqn"] == "lib.main" for r in rows)


def test_name_fallback_is_language_scoped(cg2):
    # 'process' existe em Rust (trait+impl); um call bare-name em Rust não
    # pode receber candidatos de outra linguagem
    sym, rows, _ = cg2.callers("lib.ComplexProcessor.process")
    assert all(r["site_path"].endswith(".rs") for r in rows)


def test_no_case_insensitive_cross_language_bind(cg2):
    # m.Load() em Go NÃO pode resolver para Cache.load em Python
    # (era o bug: LIKE case-insensitive + sufixo de fqn sem escopo de linguagem)
    sym, rows, _ = cg2.callers("cache.Cache.load")
    assert all(r["site_path"].endswith(".py") for r in rows)
    row = cg2.indexer.conn.execute(
        "SELECT dst FROM edges WHERE dst_name='Load'").fetchone()
    assert row is not None and row["dst"] is None  # dangling honesto


def test_go_builtins_not_emitted(cg2):
    n = cg2.indexer.conn.execute(
        "SELECT COUNT(*) FROM edges WHERE dst_name IN ('make','append')"
    ).fetchone()[0]
    assert n == 0


# -- Go -----------------------------------------------------------------------

def test_go_symbols(cg2):
    rows, _ = cg2.find_symbol("Average")
    assert "main.Metrics.Average" in fqns(rows)
    rows, _ = cg2.find_symbol("Processor", kind="interface")
    assert "main.Processor" in fqns(rows)
    rows, _ = cg2.find_symbol("MaxWorkers", kind="constant")
    assert "main.MaxWorkers" in fqns(rows)
    rows, _ = cg2.find_symbol("ErrBad", kind="variable")
    assert "main.ErrBad" in fqns(rows)


def test_go_method_receiver_containment(cg2):
    info, _ = cg2.symbol_info("main.Metrics")
    assert any(c["name"] == "Average" for c in info["children"])


def test_go_struct_embedding_inherits(cg2):
    sym, rows, _ = cg2.references("main.BaseProcessor", kind="inherits")
    assert any(r["src_fqn"] == "main.ComplexProcessor" for r in rows)


def test_go_call_inferred(cg2):
    sym, rows, _ = cg2.callers("main.Fibonacci")
    confs = {(r["other_fqn"], r["confidence"]) for r in rows}
    assert ("main.main", "inferred") in confs
    assert ("main.Fibonacci", "inferred") in confs  # recursão


def test_go_interface_methods_indexed(cg2):
    rows, _ = cg2.find_symbol("main.Processor.Process")
    assert "main.Processor.Process" in fqns(rows)


def test_method_call_via_receiver_is_possible(cg2):
    # m.Average() — receptor sem tipo conhecido em L0: candidatos por nome
    sym, rows, _ = cg2.callers("main.Metrics.Average")
    assert any(r["confidence"] in ("inferred", "possible") for r in rows)
