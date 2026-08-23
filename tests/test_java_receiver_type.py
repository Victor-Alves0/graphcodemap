"""Desambiguação de chamada Java pelo TIPO DECLARADO do receptor.

Motivação (caso vollmed, med.voll.api): dois métodos homônimos
`atualizarInformacoes` — um em `Medico`, outro em `PacienteService`. Sem tipo
de receptor, o resolver casava por nome e fazia fan-out: TODA chamada de
`x.atualizarInformacoes(...)` virava aresta `possible` para os DOIS, poluindo o
grafo com dependências que não existem (`MedicoController → PacienteService`).

O extractor agora segue o tipo declarado de campos, parâmetros e locais tipados
e qualifica a chamada — a colisão some e a aresta vira `inferred`, única.

Limite deliberado e honesto: `var x = repo.getReferenceById(...)` esconde o
tipo (retorno genérico do Spring Data). Isso NÃO é resolvido aqui — continua
`possible`. É trabalho de L1 (jdtls), e fingir precisão seria pior que admiti-la.
"""

from __future__ import annotations

import pytest

from codegraph import CodeGraph

FIELD_CALLER = '''
package app;
public class PacienteController {
    private PacienteService service;
    public void alterar(PacienteAtualizacaoDTO dados) {
        service.atualizarInformacoes(dados);
    }
}
'''

PARAM_CALLER = '''
package app;
public class Handler {
    public void run(PacienteService service, DadosMedico dados) {
        service.atualizarInformacoes(dados);
    }
}
'''

VAR_CALLER = '''
package app;
public class MedicoController {
    private MedicoRepository repository;
    public void atualizar(DadosMedico dados) {
        var medico = repository.getReferenceById(dados.id());
        medico.atualizarInformacoes(dados);
    }
}
'''

SERVICE = '''
package app;
public class PacienteService {
    public Paciente atualizarInformacoes(PacienteAtualizacaoDTO dados) {
        return null;
    }
}
'''

ENTITY = '''
package app;
public class Medico {
    public void atualizarInformacoes(DadosMedico dados) {}
}
'''


def _edges(cg, src_fqn, name="atualizarInformacoes"):
    """(dst_fqn, confidence) das arestas `calls` que saem de src_fqn."""
    return [(r["dst"], r["conf"]) for r in cg.indexer.conn.execute(
        "SELECT sd.fqn dst, e.confidence conf FROM edges e "
        "JOIN symbols ss ON e.src=ss.id LEFT JOIN symbols sd ON e.dst=sd.id "
        "WHERE e.kind='calls' AND ss.fqn=? AND e.dst_name LIKE ?",
        (src_fqn, f"%{name}%"))]


@pytest.fixture()
def cg(tmp_path):
    (tmp_path / "PacienteService.java").write_text(SERVICE, encoding="utf-8")
    (tmp_path / "Medico.java").write_text(ENTITY, encoding="utf-8")
    (tmp_path / "PacienteController.java").write_text(FIELD_CALLER, encoding="utf-8")
    (tmp_path / "Handler.java").write_text(PARAM_CALLER, encoding="utf-8")
    (tmp_path / "MedicoController.java").write_text(VAR_CALLER, encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    yield g
    g.close()


def test_field_typed_receiver_resolves_uniquely(cg):
    # campo `private PacienteService service` → a chamada é do PacienteService,
    # e SÓ dele: nenhuma aresta espúria para Medico
    edges = _edges(cg, "app.PacienteController.alterar")
    dsts = [d for d, _ in edges]
    assert any(d and d.endswith("PacienteService.atualizarInformacoes") for d in dsts)
    assert not any(d and d.endswith("Medico.atualizarInformacoes") for d in dsts)


def test_field_typed_receiver_is_inferred_not_possible(cg):
    edges = _edges(cg, "app.PacienteController.alterar")
    resolved = [(d, c) for d, c in edges if d]
    assert resolved and all(c == "inferred" for _, c in resolved)


def test_parameter_typed_receiver_resolves(cg):
    # parâmetro `PacienteService service` funciona igual a campo
    dsts = [d for d, _ in _edges(cg, "app.Handler.run")]
    assert any(d and d.endswith("PacienteService.atualizarInformacoes") for d in dsts)
    assert not any(d and d.endswith("Medico.atualizarInformacoes") for d in dsts)


def test_var_receiver_stays_possible(cg):
    # `var medico = repository.getReferenceById(...)` esconde o tipo: o L0 não
    # pode saber que é Medico. Fica ambíguo (possible) — é o limite honesto.
    edges = _edges(cg, "app.MedicoController.atualizar")
    dsts = {d for d, _ in edges if d}
    assert any(d.endswith("Medico.atualizarInformacoes") for d in dsts)
    assert all(c == "possible" for _, c in edges if c)


def test_var_receiver_is_not_over_pruned(cg):
    # o fan-out do var ainda inclui o alvo CORRETO (Medico) — a mudança não pode
    # ter passado a esconder a aresta certa junto com a espúria
    dsts = {d for d, _ in _edges(cg, "app.MedicoController.atualizar") if d}
    assert any(d.endswith("Medico.atualizarInformacoes") for d in dsts)
