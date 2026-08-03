"""Anti-explosão de taint/reaches: deadline, budget determinístico de passos,
cancelamento cooperativo, memoização global e truncated honesto — sobre grafos
patológicos (diamante que reconverge + ciclos).

Trava a regressão que pendurou um turno: varredura/entry sobre call graph
grande rodando "para sempre". O contrato: termina dentro do orçamento e
DECLARA que truncou (env.truncated + limit_hit), nunca gira infinito nem
devolve parcial em silêncio."""

from __future__ import annotations

import textwrap
import time

import pytest

from codegraph import CodeGraph


# ---------------------------------------------------------------------------
# Geradores de grafo patológico
# ---------------------------------------------------------------------------

def _diamond(width: int, layers: int, *, source: bool) -> str:
    """DAG em camadas: entrada f0_0, camadas 1..L-1 com `width` nós cada, cada
    nó chama TODOS os nós da camada seguinte com o mesmo dado tainted; folhas
    chamam system(dado) (sink). Poucos nós, exponenciais caminhos → explode sem
    memo global. `source=True` semeia via input() (modo scan); senão f0_0 recebe
    o dado por parâmetro (modo entry)."""
    L = layers
    out = ["import os", ""]
    # entrada
    if source:
        head = ["def f0_0():", "    a = input()"]
    else:
        head = ["def f0_0(a):"]
    for j in range(width):
        head.append(f"    f1_{j}(a)")
    out += head + [""]
    # camadas intermediárias
    for l in range(1, L - 1):
        for i in range(width):
            out.append(f"def f{l}_{i}(a):")
            for j in range(width):
                out.append(f"    f{l+1}_{j}(a)")
            out.append("")
    # folhas → sink
    for i in range(width):
        out.append(f"def f{L-1}_{i}(a):")
        out.append("    os.system(a)")
        out.append("")
    return "\n".join(out)


def _chain(n: int) -> str:
    """Cadeia linear source→...→sink de comprimento n (para testar depth por modo)."""
    out = ["import os", "", "def s0():", "    a = input()", "    s1(a)", ""]
    for i in range(1, n - 1):
        out += [f"def s{i}(a):", f"    s{i+1}(a)", ""]
    out += [f"def s{n-1}(a):", "    os.system(a)", ""]
    return "\n".join(out)


def _wide(n: int) -> str:
    """MUITOS nós únicos, recursão RASA: f0(a) chama f1..fn(a); cada fi chama
    system(a). O memo global NÃO colapsa isto (nós distintos) → é o formato que
    força os tetos de tempo/passos (custo ~ n, não exponencial nem trivial)."""
    out = ["import os", "", "def f0(a):"]
    out += [f"    f{i}(a)" for i in range(1, n + 1)]
    out.append("")
    for i in range(1, n + 1):
        out += [f"def f{i}(a):", "    os.system(a)", ""]
    return "\n".join(out)


CYCLE = '''
import os

def a(x):
    b(x)

def b(x):
    a(x)
    os.system(x)

def entry():
    d = input()
    a(d)
'''


def _graph(tmp_path, files):
    for rel, body in files.items():
        (tmp_path / rel).write_text(textwrap.dedent(body), encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    return g


# ---------------------------------------------------------------------------
# Item 2 — memoização global colapsa o diamante (mesmo SEM budget)
# ---------------------------------------------------------------------------

def test_entry_diamond_terminates_via_global_memo(tmp_path):
    # 3^~10 caminhos; sem memo global, o nº de traces explode. Com memo, cada
    # (func,arg) expande uma vez → termina rápido e acha o sink.
    g = _graph(tmp_path, {"g.py": _diamond(3, 10, source=False)})
    t0 = time.monotonic()
    data, env = g.taint(entry="g.f0_0", depth=12)
    dt = time.monotonic() - t0
    assert dt < 5.0, f"não colapsou o diamante: {dt:.1f}s"
    assert data["findings"], "deveria alcançar o sink system()"
    assert data["explored"] < 500          # nós únicos, não caminhos
    assert env.truncated is False          # terminou de fato, sem cortar
    assert data["limit_hit"] is None
    g.close()


def test_scan_shared_subtree_terminates(tmp_path):
    # várias fontes convergindo no MESMO subgrafo profundo: memo global evita
    # reexpandir a subárvore por fonte (O(fontes×subárvore) → O(nós únicos)).
    src = _diamond(3, 9, source=True)
    # três arquivos = três fontes independentes que caem no mesmo formato
    g = _graph(tmp_path, {"a.py": src, "b.py": src.replace("f", "g"),
                          "c.py": src.replace("f", "h")})
    t0 = time.monotonic()
    data, env = g.taint(depth=10)
    dt = time.monotonic() - t0
    assert dt < 8.0, f"varredura não colapsou: {dt:.1f}s"
    assert data["findings"]
    g.close()


# ---------------------------------------------------------------------------
# Item 1 — deadline com resultado parcial
# ---------------------------------------------------------------------------

def test_deadline_returns_partial_and_truncated(tmp_path):
    # grafo LARGO (4000 nós únicos): o memo não colapsa, então o deadline morde.
    g = _graph(tmp_path, {"g.py": _wide(4000)})
    t0 = time.monotonic()
    data, env = g.taint(entry="g.f0", depth=8, deadline_ms=15, max_findings=10**9)
    dt = time.monotonic() - t0
    assert dt < 5.0, f"deadline não parou a tempo: {dt:.1f}s"
    assert env.truncated is True
    assert data["limit_hit"] == "deadline"
    assert "elapsed_ms" in data
    g.close()


# ---------------------------------------------------------------------------
# Item 4 — budget determinístico de passos + truncated
# ---------------------------------------------------------------------------

def test_max_steps_is_deterministic(tmp_path):
    g = _graph(tmp_path, {"g.py": _wide(2000)})
    a, _ = g.taint(entry="g.f0", depth=8, max_steps=300, max_findings=10**9)
    b, _ = g.taint(entry="g.f0", depth=8, max_steps=300, max_findings=10**9)
    assert a["limit_hit"] == "steps" and b["limit_hit"] == "steps"
    assert a["steps"] == b["steps"]                 # reprodutível por máquina
    assert a["steps"] <= 300 + 50                   # respeita o teto (folga p/ o passo que estoura)
    g.close()


def test_max_findings_sets_truncated(tmp_path):
    g = _graph(tmp_path, {"g.py": _diamond(3, 6, source=False)})
    data, env = g.taint(entry="g.f0_0", depth=8, max_findings=1)
    assert len(data["findings"]) <= 1
    assert env.truncated is True
    assert data["limit_hit"] == "findings"
    g.close()


# ---------------------------------------------------------------------------
# Item 3 — cancelamento cooperativo
# ---------------------------------------------------------------------------

def test_should_cancel_stops_clean(tmp_path):
    g = _graph(tmp_path, {"g.py": _wide(2000)})
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 3      # cancela após alguns passos

    data, env = g.taint(entry="g.f0", depth=8, should_cancel=cancel, max_findings=10**9)
    assert env.truncated is True
    assert data["limit_hit"] == "cancelled"
    g.close()


# ---------------------------------------------------------------------------
# Terminação em ciclos (per-path visited) — não deve depender de budget
# ---------------------------------------------------------------------------

def test_taint_terminates_on_cycle(tmp_path):
    g = _graph(tmp_path, {"c.py": CYCLE})
    data, env = g.taint(depth=10)
    # termina e ainda acha o sink dentro do ciclo
    assert any(f["sink"]["callee"] == "system" for f in data["findings"])
    g.close()


# ---------------------------------------------------------------------------
# Item 6 — telemetria no retorno
# ---------------------------------------------------------------------------

def test_telemetry_fields_present(tmp_path):
    g = _graph(tmp_path, {"g.py": _diamond(3, 6, source=False)})
    data, _ = g.taint(entry="g.f0_0", depth=8)
    for k in ("elapsed_ms", "explored", "steps", "limit_hit", "scanned"):
        assert k in data, f"faltou telemetria: {k}"
    assert isinstance(data["elapsed_ms"], int)
    g.close()


# ---------------------------------------------------------------------------
# Item 8 — defaults de depth por modo (scan raso, entry fundo)
# ---------------------------------------------------------------------------

def test_mode_depth_defaults(tmp_path):
    # cadeia longa até o sink. scan (parte de s0=input, default RASO=3) não
    # alcança; entry em s1 (tem parâmetro; default FUNDO=6) alcança. Sem depth.
    g = _graph(tmp_path, {"m.py": _chain(7)})
    scan, _ = g.taint()                    # default de varredura (raso)
    entry, _ = g.taint(entry="m.s1")       # default de entry (fundo)
    scan_hit = any(f["sink"]["callee"] == "system" for f in scan["findings"])
    entry_hit = any(f["sink"]["callee"] == "system" for f in entry["findings"])
    assert entry_hit, "entry com default fundo deveria alcançar o sink"
    assert not scan_hit, "scan com default raso não deveria alcançar o sink distante"
    g.close()


# ---------------------------------------------------------------------------
# Item 5 — reaches compartilha a mesma infra
# ---------------------------------------------------------------------------

def test_reaches_budget_and_truncated(tmp_path):
    g = _graph(tmp_path, {"g.py": _wide(4000)})
    sym, data, env = g.reaches("g.f0", sink="system", depth=8, deadline_ms=15)
    # termina rápido e declara o corte
    assert env.truncated is True
    assert data["limit_hit"] == "deadline"
    g.close()


def test_reaches_terminates_on_cycle(tmp_path):
    g = _graph(tmp_path, {"c.py": CYCLE})
    sym, data, env = g.reaches("c.entry", sink="system", depth=10)
    assert isinstance(data["paths"], list)     # terminou
    g.close()


def test_reaches_path_cap_sets_truncated(tmp_path):
    g = _graph(tmp_path, {"g.py": _diamond(3, 8, source=False)})
    sym, data, env = g.reaches("g.f0_0", sink="system", depth=10, max_paths=2)
    assert len(data["paths"]) <= 2
    assert env.truncated is True
    assert data["limit_hit"] == "paths"
    g.close()


# ---------------------------------------------------------------------------
# Backward-compat: sem budget e grafo pequeno → nada de truncated
# ---------------------------------------------------------------------------

def test_small_graph_no_truncation(tmp_path):
    g = _graph(tmp_path, {
        "app.py": "import os\n\ndef h():\n    x = input()\n    os.system(x)\n"})
    data, env = g.taint()
    assert data["findings"]
    assert env.truncated is False
    assert data["limit_hit"] is None
    g.close()
