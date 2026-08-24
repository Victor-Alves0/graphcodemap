"""Detecção do root de projeto para o L1 (suporte a monorepo).

Um language server resolve corretamente quando aberto na raiz do SUBPROJETO
(`go.mod`, `Cargo.toml`, `pom.xml`, `composer.json`…), não na raiz do repo:
gopls precisa do módulo Go, rust-analyzer do crate, jdtls do projeto Maven/Gradle.
Num monorepo com vários subprojetos, abrir tudo na raiz do repo degrada (ou zera)
a resolução.

Este módulo é puro e determinístico: dado o caminho repo-relativo de um arquivo,
os marcadores da linguagem e a raiz do repo, devolve a raiz de projeto (o ancestral
mais próximo com um marcador, limitado à raiz do repo). Sem marcadores — ou nenhum
ancestral os tem — cai na raiz do repo, que é o comportamento de sempre."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from xml.etree import ElementTree


def _has_marker(d: Path, markers) -> bool:
    for m in markers:
        if "*" in m:
            if next(d.glob(m), None) is not None:
                return True
        elif (d / m).exists():
            return True
    return False


def matches_project_marker(rel: str, markers) -> bool:
    """Se ``rel`` casa exatamente com um marker/nome glob do resolver.

    O basename cobre os markers usuais (``pom.xml``, ``*.csproj``). Um marker
    configurado com diretório continua podendo casar com o caminho repo-relativo.
    Nenhum outro arquivo desconhecido é promovido a evento semântico.
    """
    normalized = rel.replace("\\", "/").strip("/")
    name = normalized.rsplit("/", 1)[-1]
    for marker in markers:
        pattern = str(marker).replace("\\", "/").strip("/")
        candidate = normalized if "/" in pattern else name
        if pattern and fnmatch.fnmatchcase(candidate, pattern):
            return True
    return False


def _root_from_directory(directory: Path, repo_root: Path, markers) -> Path:
    """Marker mais próximo a partir de um diretório já conhecido."""
    repo_root = repo_root.resolve()
    directory = directory.resolve()
    try:
        directory.relative_to(repo_root)
    except ValueError:
        return repo_root
    d = directory
    while True:
        if _has_marker(d, markers):
            return d
        if d == repo_root or d.parent == d:
            return repo_root
        d = d.parent


def _pom_modules(pom: Path) -> set[Path]:
    """Diretórios de módulos declarados por um POM, tolerando namespace XML."""
    try:
        root = ElementTree.parse(pom).getroot()
    except (OSError, ElementTree.ParseError):
        return set()
    modules = set()
    for container in root.iter():
        if container.tag.rsplit("}", 1)[-1] != "modules":
            continue
        for child in container:
            if child.tag.rsplit("}", 1)[-1] == "module" and child.text:
                value = child.text.strip().replace("\\", "/").rstrip("/")
                if value and "${" not in value:
                    modules.add((pom.parent / value).resolve())
    return modules


def _maven_reactor_root(module_root: Path, repo_root: Path) -> Path:
    """Sobe somente por aggregators que declaram o módulo atual.

    O POM mais próximo é correto para projetos Maven independentes num
    monorepo, mas errado para um reactor: abrir cada filho isoladamente perde
    dependências entre módulos. A cadeia ``<modules>`` prova quando podemos
    subir sem misturar projetos irmãos não relacionados.
    """
    current = module_root.resolve()
    repo_root = repo_root.resolve()
    while current != repo_root:
        parent = current.parent
        promoted = None
        while True:
            pom = parent / "pom.xml"
            if pom.is_file():
                if current in _pom_modules(pom):
                    promoted = parent
                    break
            if parent == repo_root or parent.parent == parent:
                break
            parent = parent.parent
        if promoted is None:
            break
        current = promoted
    return current


def _semantic_root(root: Path, repo_root: Path, markers) -> Path:
    """Aplica agrupamentos comprovados pelo build ao marker mais próximo."""
    if "pom.xml" in markers and (root / "pom.xml").is_file():
        return _maven_reactor_root(root, repo_root)
    return root


def marker_affected_roots(rel: str, repo_root: Path, markers) -> set[Path]:
    """Raízes cujo universo pode mudar ao criar/modificar/remover ``rel``.

    Com o marker presente, a nova raiz e sua raiz-pai mudam de universo (uma
    ganhou o subprojeto; a outra o perdeu). Depois da remoção, a raiz-pai basta:
    o antigo subprojeto agora pertence a ela. Projetos irmãos com marker próprio
    não aparecem no conjunto.
    """
    if not matches_project_marker(rel, markers):
        return set()
    repo_root = repo_root.resolve()
    marker = (repo_root / rel).resolve()
    try:
        marker.relative_to(repo_root)
    except ValueError:
        return set()
    marker_dir = marker.parent
    parent_start = marker_dir if marker_dir == repo_root else marker_dir.parent
    affected = {_semantic_root(
        _root_from_directory(parent_start, repo_root, markers),
        repo_root, markers)}
    if _has_marker(marker_dir, markers):
        affected.add(_semantic_root(marker_dir, repo_root, markers))
    return affected


def detect_project_root(rel: str, repo_root: Path, markers) -> Path:
    """Raiz do subprojeto que contém `rel` (repo-relativo).

    Sobe do diretório do arquivo até a raiz do repo (inclusive), parando no 1º
    ancestral com um marcador. Nunca sobe acima da raiz do repo."""
    if not markers:
        return repo_root.resolve()
    repo_root = repo_root.resolve()
    candidate = (repo_root / rel).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError:
        # Caminho malformado ou symlink que escapa do repo: nunca usar um
        # marcador externo como root de um servidor que analisará o workspace.
        return repo_root
    root = _root_from_directory(candidate.parent, repo_root, markers)
    return _semantic_root(root, repo_root, markers)


def group_by_root(rels, repo_root: Path, markers) -> dict[Path, list[str]]:
    """Agrupa caminhos repo-relativos pela raiz de projeto detectada.

    Sem marcadores, um único grupo (a raiz do repo) → um servidor, como antes."""
    groups: dict[Path, list[str]] = {}
    for rel in sorted(set(rels)):
        root = detect_project_root(rel, repo_root, markers)
        groups.setdefault(root, []).append(rel)
    return groups
