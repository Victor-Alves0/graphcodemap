"""Verificação DINÂMICA contra repositórios reais.

Mudança de filosofia de teste. Os testes sintéticos (`tmp_path` + trecho
escrito à mão) provam que o motor funciona no caso que EU imaginei — e foi
exatamente assim que passamos a acreditar num recall que não existia: o OWASP
Benchmark é código gerado com estilo uniforme (1517 arquivos encadeiam
`response.getWriter().println()` contra 17 que usam variável local), então ele
media a forma que já tratávamos.

Aqui nada é fixo. Apontamos o motor para repositórios REAIS, que podem mudar
entre execuções, e afirmamos apenas INVARIANTES — propriedades que precisam
valer para qualquer código, hoje e depois de qualquer commit upstream. Um teste
assim não envelhece e não pode ser satisfeito por acidente.

O invariante central é a promessa do produto, tornada verificável:

    SEM CAMINHO, SEM ACHADO.

Todo achado tem que apontar para um arquivo que existe, numa linha que existe,
e essa linha tem que REALMENTE conter a chamada alegada. Se o motor reportar um
sink numa linha onde aquele sink não aparece, o achado é infalsificável — e um
achado infalsificável é pior que nenhum.

Como rodar (EXPLÍCITO por decisão, não por preguiça):

    CODEGRAPH_REAL_REPOS="/caminho/app1;/caminho/app2" pytest tests/test_real_repos.py

Sem a variável, tudo se pula. Não há descoberta automática: a primeira versão
caía em `benchrepos/` sozinha, puxava redis (59 MB) para dentro da suíte padrão
e a fazia estourar o timeout. Uma suíte que depende de repositórios que não
moram neste repositório não pode ser o caminho default — e travar o build de
quem só rodou `pytest` seria o oposto de robustez."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from codegraph import CodeGraph

_ROOT = Path(__file__).resolve().parents[1]
# teto para não transformar a suíte num benchmark; o objetivo é robustez, não
# cobertura exaustiva
_DEADLINE_MS = 60_000
_MAX_FILES = 4000


def _discover() -> list[Path]:
    """Só o que foi pedido explicitamente. Nada de varrer diretórios por conta
    própria — o custo de indexar um repo grande é do usuário, não uma surpresa."""
    env = os.environ.get("CODEGRAPH_REAL_REPOS", "").strip()
    if not env:
        return []
    return [Path(p) for p in env.split(os.pathsep) if p and Path(p).is_dir()]


REPOS = _discover()
_IDS = [p.name for p in REPOS]
pytestmark = [
    pytest.mark.realrepo,
    pytest.mark.skipif(not REPOS,
                       reason="defina CODEGRAPH_REAL_REPOS para rodar contra "
                              "repositórios reais"),
]


@pytest.fixture(scope="module")
def analysed():
    """Indexa cada repo UMA vez por sessão e guarda (findings, engine)."""
    out = {}
    for repo in REPOS:
        g = CodeGraph(repo)
        g.index()
        data, env = g.taint(max_findings=10 ** 6, deadline_ms=_DEADLINE_MS)
        out[repo.name] = (repo, data, env)
        g.close()
    return out


def _line_of(path: Path, lineno: int) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if i == lineno:
                    return line
    except OSError:
        return None
    return None


# ============================================================================
# A. O invariante central: todo achado é CONFERÍVEL por uma pessoa
# ============================================================================

@pytest.mark.parametrize("name", _IDS)
def test_every_finding_points_at_a_real_file(analysed, name):
    repo, data, _env = analysed[name]
    for f in data["findings"]:
        p = repo / f["sink"]["site_path"]
        assert p.is_file(), f"achado aponta para arquivo inexistente: {p}"


@pytest.mark.parametrize("name", _IDS)
def test_every_finding_points_at_a_real_line(analysed, name):
    repo, data, _env = analysed[name]
    for f in data["findings"]:
        s = f["sink"]
        line = _line_of(repo / s["site_path"], s["line"])
        assert line is not None, (
            f"linha {s['line']} não existe em {s['site_path']}")


@pytest.mark.parametrize("name", _IDS)
def test_sink_call_actually_appears_on_the_reported_line(analysed, name):
    """SEM CAMINHO, SEM ACHADO — a versão executável da promessa.

    Se o motor diz "sink `system` em foo.py:42", a linha 42 tem que conter
    `system`. Um achado que não sobrevive a esta conferência é ruído com
    aparência de evidência."""
    repo, data, _env = analysed[name]
    ruins = []
    for f in data["findings"]:
        s = f["sink"]
        line = _line_of(repo / s["site_path"], s["line"]) or ""
        # o motor casa pelo último segmento; basta o nome aparecer na linha
        if s["callee"] not in line:
            ruins.append(f"{s['site_path']}:{s['line']} não contém "
                         f"{s['callee']!r} → {line.strip()[:70]}")
    assert not ruins, "achados não conferíveis:\n  " + "\n  ".join(ruins[:5])


@pytest.mark.parametrize("name", _IDS)
def test_origin_is_also_verifiable(analysed, name):
    repo, data, _env = analysed[name]
    for f in data["findings"]:
        o = f["origin"]
        p = repo / o["path"]
        assert p.is_file(), f"origem aponta para arquivo inexistente: {p}"
        assert _line_of(p, o["line"]) is not None


# ============================================================================
# B. Robustez: código real quebra o que código sintético não quebra
# ============================================================================

@pytest.mark.parametrize("name", _IDS)
def test_indexing_real_repo_does_not_crash(analysed, name):
    repo, _data, _env = analysed[name]
    g = CodeGraph(repo)
    st = g.stats()
    g.close()
    assert st["files"] > 0, "nenhum arquivo indexado"


@pytest.mark.parametrize("name", _IDS)
def test_analysis_is_deterministic(analysed, name):
    """Duas execuções sobre o MESMO conteúdo dão o mesmo resultado. Sem isto,
    nenhum número medido significa coisa alguma."""
    repo, data, _env = analysed[name]
    g = CodeGraph(repo)
    g.index()
    again, _ = g.taint(max_findings=10 ** 6, deadline_ms=_DEADLINE_MS)
    g.close()

    def key(fs):
        return sorted((f["sink"]["site_path"], f["sink"]["line"],
                       f["sink"]["callee"]) for f in fs)

    assert key(again["findings"]) == key(data["findings"])


@pytest.mark.parametrize("name", _IDS)
def test_findings_stay_inside_the_repo(analysed, name):
    repo, data, _env = analysed[name]
    for f in data["findings"]:
        rel = f["sink"]["site_path"]
        assert not Path(rel).is_absolute(), f"caminho absoluto vazou: {rel}"
        assert ".." not in Path(rel).parts, f"caminho escapa do repo: {rel}"


# ============================================================================
# C. Contrato do achado (vale para qualquer repo)
# ============================================================================

@pytest.mark.parametrize("name", _IDS)
def test_every_finding_declares_both_axes(analysed, name):
    repo, data, _env = analysed[name]
    for f in data["findings"]:
        assert f["confidence"] in ("certain", "inferred", "possible")
        assert f["flow_evidence"] in ("flow-sensitive", "over-approximated")


@pytest.mark.parametrize("name", _IDS)
def test_chain_is_non_empty_and_ordered(analysed, name):
    repo, data, _env = analysed[name]
    for f in data["findings"]:
        steps = f["steps"]
        assert steps, "achado sem cadeia — não haveria o que conferir"
        assert steps[-1]["callee"] == f["sink"]["callee"], (
            "o último passo da cadeia tem que ser o sink")


@pytest.mark.parametrize("name", _IDS)
def test_test_fixtures_are_marked_and_come_last(analysed, name):
    """Achado em fixture de teste é verdadeiro e sem interesse. Fica no
    relatório, marcado, e no fim — senão afoga o código de produção (medido:
    68 dos 73 achados no Express são a suíte dele ecoando a requisição)."""
    repo, data, _env = analysed[name]
    fs = data["findings"]
    for f in fs:
        assert isinstance(f.get("in_test"), bool), "achado sem a marca in_test"
    marcas = [f["in_test"] for f in fs]
    assert marcas == sorted(marcas), "achado de produção listado após um de teste"


@pytest.mark.parametrize("name", _IDS)
def test_telemetry_is_present_and_coherent(analysed, name):
    repo, data, env = analysed[name]
    assert data["elapsed_ms"] >= 0 and data["steps"] >= 0
    # se truncou, tem que dizer; se não truncou, não pode alegar corte
    assert (data["limit_hit"] is not None) == bool(env.truncated)


# ============================================================================
# D. RECALL: um oráculo INDEPENDENTE do motor
#
# Os testes acima são de precisão — provam que o que o motor DIZ se sustenta.
# Nenhum deles pega o defeito oposto, e mais grave: o motor calar sobre uma
# vulnerabilidade óbvia. Um scanner que não reporta nada passa em todos eles.
#
# Por isso o oráculo aqui NÃO usa a maquinaria do motor. Ele lê o texto do
# repositório com uma regra deliberadamente burra e estreita:
#
#     uma linha que chama um SINK conhecido e, na MESMA linha, lê dado de
#     requisição, sem nenhum sanitizer à vista, é uma vulnerabilidade.
#
# Estreito de propósito. Cruzar a fronteira da linha exigiria reimplementar a
# análise — e um oráculo que reimplementa o motor não testa nada, só concorda
# consigo mesmo. Ficando no caso de uma linha só, ele é conferível a olho nu, e
# o que ele acusa é indefensável: se `eval(req.body.x)` está escrito ali e o
# motor não falou, o motor perdeu.
#
# É dinâmico como o resto: a lista sai do conteúdo atual dos arquivos, então
# muda sozinha quando o upstream muda.
# ============================================================================

_SRC_TOKEN = re.compile(
    r"\b(?:req|request)\s*\.\s*(?:POST|GET|body|query|params|form|args|json|"
    r"files|FILES|cookies|headers|values|payload|data|match_info)\b"
    # superglobais do PHP: a variável É a requisição, não há receptor
    r"|\$_(?:GET|POST|REQUEST|COOKIE|FILES)\b")
_COMMENT = re.compile(r"^\s*(?://|#|\*|/\*)")
# leitura usada só como ÍNDICE: `users[req.params.id]`. O motor descarta o
# índice de um subscrito por decisão declarada (ver `_chain_path`) — o que
# chega ao sink é o elemento do array, não o texto da requisição. Exigir isso
# aqui seria o oráculo cobrando o oposto de uma escolha de projeto, não pegando
# uma lacuna.
_READ_AS_INDEX = re.compile(r"\[[^\]]*\b(?:req|request)\s*\.")


def _calls_sink(line: str, sinks) -> str | None:
    """O nome do sink chamado nesta linha, se houver.

    Casar por substring acusaria `res.download(` como o sink `load` — foi o que
    a primeira versão fez, e uma falha inventada pelo próprio oráculo é pior que
    a lacuna que ele deveria pegar. Exige fronteira à esquerda, mas ACEITA o
    ponto: `mathjs.eval(` e `db.query(` são o caso normal."""
    for s in sinks:
        if re.search(rf"(?<![A-Za-z0-9_$]){re.escape(s)}\s*\(", line):
            return s
    return None
_CODE_EXT = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".rb", ".php"}
# vendorizado/gerado: não é o código do app, e minificado gera linha gigante
_SKIP_DIR = {"node_modules", "vendor", "assets", "dist", "build", "static",
             ".git", "site-packages", "migrations", "test", "tests"}


def _rules_for(repo: Path):
    from codegraph.taint_rules import load_rules

    return load_rules(repo, {"python", "javascript", "typescript", "ruby", "php"})


def _obvious_vulns(repo: Path) -> list[tuple[str, int, str, str]]:
    """(path, linha, sink, texto) para cada linha indefensável do repositório."""
    rules = _rules_for(repo)
    out = []
    for p in repo.rglob("*"):
        if p.suffix not in _CODE_EXT or not p.is_file():
            continue
        rel = p.relative_to(repo)
        if _SKIP_DIR & set(rel.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if len(line) > 400 or _COMMENT.match(line):
                continue          # minificado ou comentado: não é código vivo
            leituras = _SRC_TOKEN.findall(line)
            if not leituras or len(_READ_AS_INDEX.findall(line)) == len(leituras):
                continue
            if any(s in line for s in rules.sanitizers):
                continue          # há um sanitizer na linha: indecidível a olho nu
            hit = _calls_sink(line, rules.sinks)
            if hit is not None:
                out.append((rel.as_posix(), i, hit, line.strip()[:90]))
    return out


@pytest.mark.parametrize("name", _IDS)
def test_no_obvious_vulnerability_is_missed(analysed, name):
    """SEM SILÊNCIO DIANTE DO ÓBVIO — o contrapeso de 'sem caminho, sem achado'."""
    repo, data, _env = analysed[name]
    esperados = _obvious_vulns(repo)
    if not esperados:
        pytest.skip("este repositório não tem fonte→sink na mesma linha")
    achados = {(f["sink"]["site_path"], f["sink"]["line"]) for f in data["findings"]}
    perdidos = [e for e in esperados if (e[0], e[1]) not in achados]
    assert not perdidos, (
        f"{len(perdidos)}/{len(esperados)} vulnerabilidades óbvias NÃO reportadas:\n  "
        + "\n  ".join(f"{p}:{ln} [{sink}] {txt}" for p, ln, sink, txt in perdidos[:10]))


def _sanitized_sink_lines(repo: Path) -> list[tuple[str, int, str]]:
    """Linhas em que um sink recebe leitura de requisição JÁ sanitizada.

    É o espelho de `_obvious_vulns`: ali o motor tem obrigação de falar, aqui
    tem obrigação de calar. Exige que TODAS as leituras da linha estejam
    envolvidas por sanitizer — uma linha meio-sanitizada é vulnerável e não
    entra."""
    rules = _rules_for(repo)
    envolto = re.compile(
        r"\b(?:" + "|".join(re.escape(s.rsplit(".", 1)[-1]) for s in rules.sanitizers)
        + r")\s*\(\s*(?:req|request)\s*\.")
    out = []
    for p in repo.rglob("*"):
        if p.suffix not in _CODE_EXT or not p.is_file():
            continue
        rel = p.relative_to(repo)
        if _SKIP_DIR & set(rel.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if len(line) > 400 or _COMMENT.match(line):
                continue
            leituras = len(_SRC_TOKEN.findall(line))
            if not leituras or leituras != len(envolto.findall(line)):
                continue
            if _calls_sink(line, rules.sinks) is not None:
                out.append((rel.as_posix(), i, line.strip()[:90]))
    return out


@pytest.mark.parametrize("name", _IDS)
def test_a_sanitized_read_is_not_a_finding(analysed, name):
    """Acusar quem escreveu a versão SEGURA é o pior falso positivo que existe:
    ensina a ignorar a ferramenta."""
    repo, data, _env = analysed[name]
    limpas = _sanitized_sink_lines(repo)
    if not limpas:
        pytest.skip("este repositório não sanitiza a leitura na linha do sink")
    achados = {(f["sink"]["site_path"], f["sink"]["line"]) for f in data["findings"]}
    ruins = [f"{p}:{ln} {txt}" for p, ln, txt in limpas if (p, ln) in achados]
    assert not ruins, ("achado sobre leitura JÁ sanitizada:\n  "
                       + "\n  ".join(ruins[:5]))


@pytest.mark.parametrize("name", _IDS)
def test_the_same_defect_is_reported_once(analysed, name):
    """Mesma origem, mesmo sink, mesmo argumento = um defeito só. Repetir infla
    o relatório e faz o leitor desconfiar de tudo que está nele."""
    repo, data, _env = analysed[name]
    vistos: dict = {}
    for f in data["findings"]:
        s, o = f["sink"], f["origin"]
        k = (o["path"], o["line"], s["site_path"], s["line"], s["callee"],
             s["arg_index"])
        vistos[k] = vistos.get(k, 0) + 1
    repetidos = [k for k, n in vistos.items() if n > 1]
    assert not repetidos, f"achados repetidos: {repetidos[:5]}"


@pytest.mark.parametrize("name", _IDS)
def test_origin_of_a_direct_read_names_the_request(analysed, name):
    """Quando a fonte é lida direto no argumento do sink, a origem tem que
    apontar para a linha DAQUELA leitura — não para uma linha qualquer da
    função. Sem isto o achado é verdadeiro mas a explicação é ficção."""
    repo, data, _env = analysed[name]
    diretos = {(p, ln) for p, ln, _s, _t in _obvious_vulns(repo)}
    if not diretos:
        pytest.skip("este repositório não tem fonte→sink na mesma linha")
    ruins = []
    for f in data["findings"]:
        s, o = f["sink"], f["origin"]
        if (s["site_path"], s["line"]) not in diretos:
            continue
        texto = _line_of(repo / o["path"], o["line"]) or ""
        if not _SRC_TOKEN.search(texto):
            ruins.append(f"{s['site_path']}:{s['line']} → origem "
                         f"{o['path']}:{o['line']} não lê requisição: "
                         f"{texto.strip()[:70]}")
    assert not ruins, "origem incoerente:\n  " + "\n  ".join(ruins[:5])
