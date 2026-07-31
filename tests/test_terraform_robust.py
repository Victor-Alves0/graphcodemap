"""Bateria de robustez do extractor Terraform/HCL (L0).

HCL é orientado a BLOCOS: `resource "type" "name" { … }`. O tipo do bloco (o
primeiro identifier) decide o que ele é; os rótulos (string_lit) formam o
ENDEREÇO pelo qual o bloco é referenciado no resto do módulo. O "membro" do
padrão vira aqui o atributo de `locals`; "herança" não existe; o valor real é o
GRAFO DE DEPENDÊNCIA (`var.x`, `aws_instance.web`, `module.vpc`, `data.t.n`) —
capturado como arestas `references` que resolvem por sufixo de fqn.

`_syms`/`_refs` rodam o extractor direto (module="app"); `_graph` monta um índice
real para exercer a resolução entre arquivos do mesmo diretório (um módulo TF é
um diretório: os .tf compartilham o namespace).
"""

from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph
from codegraph.extract import extract
from codegraph.languages import get_parser


def _extract(src: str, module="app"):
    src_b = textwrap.dedent(src).encode("utf-8")
    tree = get_parser("terraform").parse(src_b)
    return extract("terraform", src_b, module, tree)


def _syms(src: str, module="app"):
    syms, _ = _extract(src, module)
    return {(s.kind, s.fqn) for s in syms}


def _sym_by_name(src: str, name: str):
    syms, _ = _extract(src)
    return [s for s in syms if s.name == name]


def _refs(src: str, kind: str):
    _, refs = _extract(src)
    return {r.dst_name for r in refs if r.kind == kind}


def _graph(tmp_path, files: dict[str, str]):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    g = CodeGraph(tmp_path)
    g.index()
    return g


def _edges(g, kind=None):
    sql = ("SELECT e.kind, ss.fqn src, e.dst_name, sd.fqn dst, e.confidence conf "
           "FROM edges e LEFT JOIN symbols ss ON e.src=ss.id "
           "LEFT JOIN symbols sd ON e.dst=sd.id")
    rows = g.indexer.conn.execute(sql).fetchall()
    return [dict(r) for r in rows if kind is None or r["kind"] == kind]


# ============================================================================
# A. Símbolos — superfície (cada tipo de bloco vira símbolo)
# ============================================================================

def test_resource_is_symbol():
    assert ("resource", "app.aws_instance.web") in _syms(
        'resource "aws_instance" "web" {}')


def test_data_is_symbol():
    assert ("data", "app.data.aws_ami.ubuntu") in _syms(
        'data "aws_ami" "ubuntu" {}')


def test_variable_is_symbol():
    assert ("variable", "app.var.region") in _syms('variable "region" {}')


def test_output_is_symbol():
    assert ("output", "app.output.ip") in _syms('output "ip" { value = 1 }')


def test_module_is_symbol():
    assert ("module", "app.module.vpc") in _syms(
        'module "vpc" { source = "x" }')


def test_provider_is_symbol():
    assert ("provider", "app.provider.aws") in _syms('provider "aws" {}')


def test_local_is_symbol():
    assert ("local", "app.local.name") in _syms('locals { name = "x" }')


def test_multiple_locals_each_a_symbol():
    s = _syms('locals {\n a = 1\n b = 2\n c = 3\n}')
    assert ("local", "app.local.a") in s
    assert ("local", "app.local.b") in s
    assert ("local", "app.local.c") in s


def test_terraform_block_is_not_a_symbol():
    # o bloco `terraform {}` é config, não um alvo endereçável
    s = _syms('terraform {\n required_version = ">= 1.0"\n}')
    assert s == set()


def test_resource_name_is_last_segment():
    # name = rótulo do recurso (para find_symbol "web"); fqn embute o tipo
    syms = _sym_by_name('resource "aws_instance" "web" {}', "web")
    assert syms and syms[0].kind == "resource"
    assert syms[0].fqn == "app.aws_instance.web"


def test_data_name_is_last_segment():
    syms = _sym_by_name('data "aws_ami" "ubuntu" {}', "ubuntu")
    assert syms and syms[0].fqn == "app.data.aws_ami.ubuntu"


# ============================================================================
# B. FQN e namespace (o fqn embute o endereço; sem parent falso)
# ============================================================================

def test_variable_fqn_has_var_prefix():
    assert ("variable", "app.var.region") in _syms('variable "region" {}')


def test_resource_fqn_has_no_module_when_root(tmp_path):
    # module_fqn vem do caminho: main.tf -> "main"
    g = _graph(tmp_path, {"main.tf": 'resource "aws_vpc" "main" {}\n'})
    rows = g.indexer.conn.execute(
        "SELECT fqn FROM symbols WHERE kind='resource'").fetchall()
    assert any(r["fqn"] == "main.aws_vpc.main" for r in rows)
    g.close()


def test_local_fqn_has_local_prefix():
    assert ("local", "app.local.cidr") in _syms('locals { cidr = "10.0.0.0/8" }')


def test_definitions_have_no_parent():
    # blocos de topo são módulo-nível: parent_fqn None (não inventar pai falso)
    syms, _ = _extract('resource "aws_instance" "web" {}')
    r = [s for s in syms if s.kind == "resource"][0]
    assert r.parent_fqn is None


# ============================================================================
# C. Doc / assinatura
# ============================================================================

def test_signature_is_block_header():
    syms = _sym_by_name('resource "aws_instance" "web" {\n ami = "x"\n}', "web")
    assert syms and 'resource "aws_instance" "web"' in syms[0].signature


def test_variable_description_becomes_doc():
    syms = _sym_by_name(
        'variable "region" {\n description = "AWS region"\n}', "region")
    assert syms and syms[0].doc == "AWS region"


def test_output_description_becomes_doc():
    syms = _sym_by_name(
        'output "ip" {\n value = 1\n description = "public ip"\n}', "ip")
    assert syms and syms[0].doc == "public ip"


# ============================================================================
# D. Imports (module source)
# ============================================================================

def test_module_source_is_import():
    got = _refs('module "vpc" {\n source = "./modules/vpc"\n}', "imports")
    assert "./modules/vpc" in got


def test_registry_module_source_is_import():
    got = _refs(
        'module "vpc" {\n source = "terraform-aws-modules/vpc/aws"\n}', "imports")
    assert "terraform-aws-modules/vpc/aws" in got


def test_resource_has_no_import():
    got = _refs('resource "aws_instance" "web" {\n ami = "x"\n}', "imports")
    assert got == set()


# ============================================================================
# E. Referências (o grafo de dependência)
# ============================================================================

def test_var_reference():
    got = _refs('resource "r" "n" {\n x = var.region\n}', "references")
    assert "var.region" in got


def test_local_reference():
    got = _refs('resource "r" "n" {\n x = local.cidr\n}', "references")
    assert "local.cidr" in got


def test_module_reference():
    got = _refs('output "o" {\n value = module.vpc.vpc_id\n}', "references")
    assert "module.vpc" in got


def test_data_reference():
    got = _refs('resource "r" "n" {\n ami = data.aws_ami.ubuntu.id\n}',
                "references")
    assert "data.aws_ami.ubuntu" in got


def test_resource_reference():
    got = _refs('resource "r" "n" {\n subnet = aws_subnet.main.id\n}',
                "references")
    assert "aws_subnet.main" in got


def test_reference_inside_interpolation():
    got = _refs('resource "r" "n" {\n name = "web-${var.env}"\n}', "references")
    assert "var.env" in got


def test_reference_inside_function_call():
    got = _refs('resource "r" "n" {\n n = max(var.a, var.b)\n}', "references")
    assert "var.a" in got and "var.b" in got


def test_reference_inside_nested_block():
    got = _refs(
        'resource "aws_instance" "web" {\n'
        '  network_interface {\n subnet_id = aws_subnet.main.id\n  }\n}',
        "references")
    assert "aws_subnet.main" in got


def test_count_index_is_not_a_reference():
    got = _refs('resource "r" "n" {\n count = 2\n name = count.index\n}',
                "references")
    assert not any(g.startswith("count") for g in got)


def test_each_key_is_not_a_reference():
    got = _refs('resource "r" "n" {\n name = each.key\n}', "references")
    assert not any(g.startswith("each") for g in got)


def test_path_module_is_not_a_reference():
    got = _refs('resource "r" "n" {\n src = path.module\n}', "references")
    assert not any(g.startswith("path") for g in got)


def test_self_is_not_a_reference():
    got = _refs('resource "r" "n" {\n x = self.id\n}', "references")
    assert not any(g.startswith("self") for g in got)


def test_locals_value_reference_attributed():
    # a referência dentro de um local pertence àquele local
    _, refs = _extract('locals {\n tags = var.base_tags\n}')
    r = [x for x in refs if x.kind == "references" and x.dst_name == "var.base_tags"]
    assert r and r[0].src_fqn == "app.local.tags"


def test_reference_src_is_the_enclosing_block():
    _, refs = _extract('resource "aws_instance" "web" {\n x = var.region\n}')
    r = [x for x in refs if x.dst_name == "var.region"][0]
    assert r.src_fqn == "app.aws_instance.web"


# ============================================================================
# F. Resolução (grafo real — o pay-off)
# ============================================================================

def test_var_reference_resolves_same_file(tmp_path):
    g = _graph(tmp_path, {
        "main.tf": 'variable "region" {}\n'
                   'resource "aws_instance" "web" {\n region = var.region\n}\n',
    })
    refs = [e for e in _edges(g, "references") if e["dst_name"] == "var.region"]
    assert refs and all(e["dst"] and e["dst"].endswith("var.region") for e in refs)
    assert all(e["conf"] == "inferred" for e in refs)
    g.close()


def test_var_reference_resolves_cross_file(tmp_path):
    # variables.tf e main.tf são o MESMO módulo (mesmo diretório)
    g = _graph(tmp_path, {
        "variables.tf": 'variable "region" {\n default = "us-east-1"\n}\n',
        "main.tf": 'resource "aws_instance" "web" {\n region = var.region\n}\n',
    })
    refs = [e for e in _edges(g, "references") if e["dst_name"] == "var.region"]
    assert refs and all(e["dst"] and e["dst"].endswith("var.region") for e in refs)
    g.close()


def test_resource_reference_resolves(tmp_path):
    g = _graph(tmp_path, {
        "main.tf": 'resource "aws_subnet" "main" {}\n'
                   'resource "aws_instance" "web" {\n'
                   '  subnet_id = aws_subnet.main.id\n}\n',
    })
    refs = [e for e in _edges(g, "references")
            if e["dst_name"] == "aws_subnet.main"]
    assert refs and all(
        e["dst"] and e["dst"].endswith("aws_subnet.main") for e in refs)
    g.close()


def test_data_reference_resolves(tmp_path):
    g = _graph(tmp_path, {
        "main.tf": 'data "aws_ami" "ubuntu" {}\n'
                   'resource "aws_instance" "web" {\n'
                   '  ami = data.aws_ami.ubuntu.id\n}\n',
    })
    refs = [e for e in _edges(g, "references")
            if e["dst_name"] == "data.aws_ami.ubuntu"]
    assert refs and all(
        e["dst"] and e["dst"].endswith("data.aws_ami.ubuntu") for e in refs)
    g.close()


def test_module_reference_resolves(tmp_path):
    g = _graph(tmp_path, {
        "main.tf": 'module "vpc" {\n source = "./vpc"\n}\n'
                   'output "id" {\n value = module.vpc.vpc_id\n}\n',
    })
    refs = [e for e in _edges(g, "references") if e["dst_name"] == "module.vpc"]
    assert refs and all(e["dst"] and e["dst"].endswith("module.vpc") for e in refs)
    g.close()


def test_symbol_info_on_resource(tmp_path):
    g = _graph(tmp_path, {"main.tf": 'resource "aws_vpc" "main" {}\n'})
    info, _ = g.symbol_info("aws_vpc.main")
    assert info["symbol"]["kind"] == "resource"
    g.close()


def test_references_query_finds_dependents(tmp_path):
    # "quem usa var.region?" — o pay-off para o usuário
    g = _graph(tmp_path, {
        "main.tf": 'variable "region" {}\n'
                   'resource "aws_instance" "a" {\n region = var.region\n}\n'
                   'resource "aws_instance" "b" {\n region = var.region\n}\n',
    })
    sym, rows, _ = g.query.references("var.region")
    assert len({r["src_fqn"] for r in rows}) == 2
    g.close()


def test_unresolved_registry_module_stays_dangling(tmp_path):
    g = _graph(tmp_path, {
        "main.tf": 'module "vpc" {\n source = "terraform-aws-modules/vpc/aws"\n}\n',
    })
    imp = [e for e in _edges(g, "imports")]
    assert imp and all(e["dst"] is None for e in imp)  # externo: correto dangling
    g.close()


# ============================================================================
# G. Casos de borda
# ============================================================================

def test_empty_file_produces_nothing():
    syms, refs = _extract("")
    assert syms == [] and refs == []


def test_comment_only_file():
    syms, refs = _extract("# só um comentário\n// outro\n")
    assert syms == [] and refs == []


def test_syntax_error_does_not_crash():
    # bloco não fechado — o parser marca erro, o extractor não deve estourar
    syms, _ = _extract('resource "aws_instance" "web" {\n ami = ')
    assert isinstance(syms, list)


def test_nested_object_value_does_not_create_symbols():
    # tags = { Name = "x" } — o objeto não vira símbolo; só o bloco é símbolo
    s = _syms('resource "r" "n" {\n tags = {\n Name = "web"\n }\n}')
    assert s == {("resource", "app.r.n")}


def test_for_loop_var_is_not_a_resource_reference():
    # [for s in var.subnets : s.id] — `s` é var de laço, não recurso
    got = _refs('locals {\n ids = [for s in var.subnets : s.id]\n}', "references")
    assert "var.subnets" in got
    assert "s.id" not in got


def test_multiple_labels_resource_address():
    # rótulos com underscore/pontuação idiomática do TF
    assert ("resource", "app.aws_s3_bucket.my-bucket") in _syms(
        'resource "aws_s3_bucket" "my-bucket" {}')


def test_provider_alias_reference_head_skipped_when_bare():
    # `provider = aws` (bare, sem get_attr) não é referência de recurso
    got = _refs('resource "r" "n" {\n provider = aws\n}', "references")
    assert "aws" not in got
