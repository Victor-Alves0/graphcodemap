"""Casamento QUALIFICADO de sink: receptor + método (P3b).

O benchmark do OWASP transformou isto de "melhoria desejável" em bloqueador
medido: **44% dos nossos falsos negativos (329 de 741) eram XSS e
trust-boundary**, e ambos são estruturalmente inatingíveis casando só pelo
último segmento do nome.

O motivo é concreto. O sink de XSS em Java é `println` — mas num `PrintWriter`
de resposta:

    response.getWriter().println(sujo)   ← vulnerável
    System.out.println(sujo)             ← inofensivo

Pelo último segmento os dois são `println`. Marcar `println` como sink pegaria
o primeiro e dispararia em todo log de qualquer código. A saída é casar o par
RECEPTOR.MÉTODO: `getWriter.println` é o sink; `out.println` não é.

Regra de normalização: último segmento do receptor + "." + nome do método.
Duas informações, não uma — e o suficiente para separar os casos acima sem
precisar de inferência de tipos."""

from __future__ import annotations

from codegraph import CodeGraph


def _hits(tmp_path, fname, src):
    (tmp_path / fname).write_text(src, encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    data, _env = g.taint()
    out = [f["sink"]["callee"] for f in data["findings"]]
    g.close()
    return out


# ============================================================================
# A. O caso que o benchmark provou impossível sem isto
# ============================================================================

def test_response_writer_println_is_a_sink(tmp_path):
    hits = _hits(tmp_path, "A.java",
                 'class A { void h(){ String p = req.getParameter("q");'
                 ' response.getWriter().println(p); } }')
    assert hits, "XSS via response.getWriter().println não detectado"


def test_system_out_println_is_not_a_sink(tmp_path):
    # o outro lado da moeda: logar dado do usuário não é XSS. Se este teste
    # falhar, ganhamos recall de XSS às custas de ruído em todo código.
    hits = _hits(tmp_path, "A.java",
                 'class A { void h(){ String p = req.getParameter("q");'
                 ' System.out.println(p); } }')
    assert hits == [], "System.out.println virou sink — canhão de falso positivo"


def test_response_writer_write_is_a_sink(tmp_path):
    hits = _hits(tmp_path, "A.java",
                 'class A { void h(){ String p = req.getParameter("q");'
                 ' response.getWriter().write(p); } }')
    assert hits


# ============================================================================
# B. O casamento por nome simples continua valendo
# ============================================================================

def test_unqualified_sinks_still_match(tmp_path):
    # `exec` continua sendo sink sem precisar de receptor conhecido
    hits = _hits(tmp_path, "A.java",
                 'class A { void h(){ String p = req.getParameter("q");'
                 ' Runtime.getRuntime().exec(p); } }')
    assert "exec" in hits


def test_python_unqualified_still_matches(tmp_path):
    hits = _hits(tmp_path, "a.py",
                 "import os\ndef h():\n    x = input()\n    os.system(x)\n")
    assert "system" in hits


# ============================================================================
# C. Compõe com flow-sensitivity (P1): sanitizado continua limpo
# ============================================================================

def test_qualified_sink_respects_sanitizer(tmp_path):
    hits = _hits(tmp_path, "A.java",
                 'class A { void h(){ String p = req.getParameter("q");'
                 ' p = escape(p); response.getWriter().println(p); } }')
    assert hits == []
