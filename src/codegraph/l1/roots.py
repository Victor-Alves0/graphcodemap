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

from pathlib import Path


def _has_marker(d: Path, markers) -> bool:
    for m in markers:
        if "*" in m:
            if next(d.glob(m), None) is not None:
                return True
        elif (d / m).exists():
            return True
    return False


def detect_project_root(rel: str, repo_root: Path, markers) -> Path:
    """Raiz do subprojeto que contém `rel` (repo-relativo).

    Sobe do diretório do arquivo até a raiz do repo (inclusive), parando no 1º
    ancestral com um marcador. Nunca sobe acima da raiz do repo."""
    if not markers:
        return repo_root
    repo_root = repo_root.resolve()
    d = (repo_root / rel).resolve().parent
    while True:
        if _has_marker(d, markers):
            return d
        if d == repo_root or d.parent == d:
            return repo_root
        d = d.parent


def group_by_root(rels, repo_root: Path, markers) -> dict[Path, list[str]]:
    """Agrupa caminhos repo-relativos pela raiz de projeto detectada.

    Sem marcadores, um único grupo (a raiz do repo) → um servidor, como antes."""
    groups: dict[Path, list[str]] = {}
    for rel in rels:
        root = detect_project_root(rel, repo_root, markers)
        groups.setdefault(root, []).append(rel)
    return groups
