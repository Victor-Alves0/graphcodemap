"""Generate the editable GraphCodeMap architecture map.

The output is plain Excalidraw JSON so the diagram can be opened at
https://excalidraw.com or in any compatible editor.  Coordinates are kept here
instead of hand-editing generated JSON, making architectural updates reviewable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "graphcodemap-architecture.excalidraw"

INK = "#1e1e1e"
MUTED = "#868e96"
BLUE = "#d0ebff"
GREEN = "#d3f9d8"
PURPLE = "#e5dbff"
YELLOW = "#fff3bf"
RED = "#ffe3e3"
GRAY = "#e9ecef"
ORANGE = "#ffe8cc"
WHITE = "#ffffff"
FUTURE = "#e03131"
OPTIONAL = "#7048e8"


def stable_int(value: str) -> int:
    return int(hashlib.blake2b(value.encode(), digest_size=4).hexdigest(), 16)


class Diagram:
    def __init__(self) -> None:
        self.elements: list[dict] = []
        self.boxes: dict[str, tuple[float, float, float, float]] = {}
        self.sequence = 0

    def _base(self, element_id: str, kind: str, x: float, y: float,
              width: float, height: float, *, stroke: str = INK,
              background: str = "transparent", stroke_style: str = "solid",
              stroke_width: int = 2, roughness: int = 1,
              opacity: int = 100) -> dict:
        self.sequence += 1
        return {
            "id": element_id,
            "type": kind,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "angle": 0,
            "strokeColor": stroke,
            "backgroundColor": background,
            "fillStyle": "solid",
            "strokeWidth": stroke_width,
            "strokeStyle": stroke_style,
            "roughness": roughness,
            "opacity": opacity,
            "groupIds": [],
            "frameId": None,
            "index": f"a{self.sequence:04d}",
            "roundness": {"type": 3} if kind == "rectangle" else None,
            "seed": stable_int(element_id),
            "version": 1,
            "versionNonce": stable_int(element_id + ":nonce"),
            "isDeleted": False,
            "boundElements": [],
            "updated": 1787544000000,
            "link": None,
            "locked": False,
        }

    def text(self, element_id: str, x: float, y: float, value: str,
             *, size: int = 20, color: str = INK, align: str = "left",
             family: int = 1) -> None:
        lines = value.splitlines() or [""]
        width = max(24.0, max(len(line) for line in lines) * size * 0.58)
        height = max(size * 1.25, len(lines) * size * 1.25)
        item = self._base(element_id, "text", x, y, width, height,
                          stroke=color, stroke_width=1, roughness=0)
        item.update({
            "fontSize": size,
            "fontFamily": family,
            "text": value,
            "rawText": value,
            "textAlign": align,
            "verticalAlign": "top",
            "containerId": None,
            "originalText": value,
            "autoResize": True,
            "lineHeight": 1.25,
            "baseline": int(height - size * 0.25),
        })
        self.elements.append(item)

    def section(self, element_id: str, x: float, y: float, width: float,
                height: float, title: str) -> None:
        item = self._base(element_id, "rectangle", x, y, width, height,
                          stroke="#adb5bd", stroke_width=2,
                          stroke_style="dashed", roughness=0)
        self.elements.append(item)
        self.text(element_id + "-title", x + 24, y + 15, title,
                  size=24, color="#495057")

    def box(self, element_id: str, x: float, y: float, width: float,
            height: float, title: str, body: str, *, fill: str = BLUE,
            stroke: str = INK, stroke_style: str = "solid",
            title_size: int = 20, body_size: int = 15) -> None:
        item = self._base(element_id, "rectangle", x, y, width, height,
                          stroke=stroke, background=fill,
                          stroke_style=stroke_style)
        self.elements.append(item)
        self.boxes[element_id] = (x, y, width, height)
        self.text(element_id + "-title", x + 16, y + 12, title,
                  size=title_size, color=stroke)
        if body:
            self.text(element_id + "-body", x + 16, y + 45, body,
                      size=body_size, color=stroke)

    def arrow(self, element_id: str, start: tuple[float, float],
              end: tuple[float, float], *, color: str = INK,
              style: str = "solid", width: int = 2,
              bidirectional: bool = False,
              via: list[tuple[float, float]] | None = None) -> None:
        absolute = [start, *(via or []), end]
        x, y = start
        points = [[px - x, py - y] for px, py in absolute]
        item = self._base(
            element_id, "arrow", x, y,
            max(abs(px - x) for px, _ in absolute),
            max(abs(py - y) for _, py in absolute),
            stroke=color, stroke_style=style, stroke_width=width,
        )
        item.update({
            "points": points,
            "lastCommittedPoint": None,
            "startBinding": None,
            "endBinding": None,
            "startArrowhead": "arrow" if bidirectional else None,
            "endArrowhead": "arrow",
            "elbowed": False,
        })
        self.elements.append(item)

    def connect(self, element_id: str, source: str, target: str, *,
                source_side: str = "right", target_side: str = "left",
                color: str = INK, style: str = "solid",
                bidirectional: bool = False,
                via: list[tuple[float, float]] | None = None) -> None:
        sx, sy, sw, sh = self.boxes[source]
        tx, ty, tw, th = self.boxes[target]
        sides = {
            "left": lambda x, y, w, h: (x, y + h / 2),
            "right": lambda x, y, w, h: (x + w, y + h / 2),
            "top": lambda x, y, w, h: (x + w / 2, y),
            "bottom": lambda x, y, w, h: (x + w / 2, y + h),
        }
        self.arrow(element_id, sides[source_side](sx, sy, sw, sh),
                   sides[target_side](tx, ty, tw, th), color=color,
                   style=style, bidirectional=bidirectional, via=via)


def build() -> dict:
    d = Diagram()

    d.text("title", 80, 35, "GraphCodeMap — arquitetura viva do produto", size=36)
    d.text(
        "subtitle", 82, 88,
        "Fase 1: Java + Python | núcleo = repositório → grafo consultável → análises",
        size=19, color="#495057",
    )

    d.box("legend", 2470, 30, 1050, 125, "Como ler", "", fill=WHITE,
          stroke="#495057", title_size=18)
    d.text("legend-current", 2500, 78, "AZUL/VERDE  existe hoje", size=15)
    d.text("legend-partial", 2760, 78, "AMARELO  parcial", size=15)
    d.text("legend-optional", 2960, 78, "ROXO  opcional", size=15)
    d.text("legend-open", 3170, 78, "VERMELHO  gate aberto", size=15)
    d.arrow("legend-solid", (2500, 120), (2630, 120))
    d.text("legend-solid-label", 2650, 106, "fluxo atual", size=14)
    d.arrow("legend-dashed", (2870, 120), (3000, 120), color=FUTURE,
            style="dashed")
    d.text("legend-dashed-label", 3020, 106, "fluxo futuro", size=14)

    # 1. Entradas, facade and queries.
    d.section("section-surfaces", 60, 175, 2100, 555,
              "1. Quem usa e como entra no sistema")
    d.box("developer", 105, 270, 260, 105, "Desenvolvedor",
          "explora, depura\ne avalia impacto", fill=GREEN)
    d.box("ai-agent", 105, 435, 260, 105, "Agente de IA",
          "consome fatos\nestruturados", fill=GREEN)

    d.box("cli", 450, 230, 245, 100, "CLI · cli.py",
          "uso humano e CI", fill=GREEN)
    d.box("library", 450, 365, 245, 100, "Library · CodeGraph",
          "API Python embutida", fill=GREEN)
    d.box("mcp", 450, 500, 245, 125, "MCP · mcp_server.py",
          "tools para agentes\nstdio + watcher", fill=GREEN)

    d.box("facade", 785, 345, 275, 145, "Fachada CodeGraph",
          "une Indexer + QueryEngine\nsem duplicar fatos", fill=BLUE)
    d.box("query", 1160, 285, 420, 245, "QueryEngine · query.py",
          "uma leitura comum do grafo\nread-repair + Envelope\nconfiança + freshness\ncompleteness + limites", fill=BLUE)

    d.box("navigation", 1680, 220, 400, 100, "Busca e navegação",
          "find · info · refs · tree · history", fill=GREEN, body_size=14)
    d.box("impact", 1680, 345, 400, 100, "Impacto e investigação",
          "impact · change-impact · related-tests", fill=GREEN, body_size=14)
    d.box("reachability", 1680, 470, 400, 100, "Reachability e segurança",
          "reaches · dataflow · taint", fill=YELLOW, body_size=14)
    d.box("presentation", 1680, 595, 400, 100, "Mapa e explicação",
          "overview · communities · visualize", fill=GREEN, body_size=14)

    for actor, surface, suffix in [
        ("developer", "cli", "dev-cli"),
        ("developer", "library", "dev-lib"),
        ("ai-agent", "mcp", "agent-mcp"),
    ]:
        d.connect(suffix, actor, surface)
    for surface, suffix in [("cli", "cli-facade"), ("library", "lib-facade"),
                            ("mcp", "mcp-facade")]:
        d.connect(suffix, surface, "facade")
    d.connect("facade-query", "facade", "query")
    for target in ("navigation", "impact", "reachability", "presentation"):
        d.connect("query-" + target, "query", target)

    # 2. Repository to persistent graph.
    d.section("section-index", 60, 770, 2100, 730,
              "2. Como o repositório vira grafo persistente")
    d.box("repository", 105, 885, 285, 140, "Repositório",
          "pastas + todos os arquivos\nGit + escopos/ignore\ncódigo = fonte da verdade",
          fill=GRAY)
    d.box("scan", 465, 885, 265, 140, "Scanner · indexer.py",
          "árvore física + hash exato\npoda ignorados antes\nde atravessar", fill=BLUE)
    d.box("parser", 805, 885, 250, 140, "Parsers",
          "tree-sitter\nAST tolerante a erro\nsem build obrigatório", fill=BLUE)
    d.box("focus-extractors", 1130, 820, 275, 155,
          "Extractors Java/Python",
          "declarações + params/locals\nfields/properties\nreads/writes/returns L0", fill=BLUE)
    d.box("experimental-extractors", 1130, 1015, 275, 140,
          "Outros extractors",
          "compatibilidade experimental\nnão significa paridade", fill=GRAY)
    d.box("framework", 1130, 1195, 275, 140, "Semântica framework",
          "java_framework.py\nSpring conservador\nproveniência explícita", fill=YELLOW)
    d.box("indexer", 1480, 925, 285, 230, "Indexer L0 · indexer.py",
          "IDs estáveis\ntransação por arquivo\nresolve/dangling/relink\nconfidence + provenance\núnico escritor estrutural", fill=BLUE)
    d.box("sqlite", 1835, 850, 275, 380, "SQLite · db.py",
          ".codegraph/graph.db · WAL\n\nrepository_nodes + files\nsymbols + FTS5 · edges\ngraph_revisions\ngraph_stage_runs\ndescriptions + communities\n\nlocal-first e incremental", fill=YELLOW)

    d.connect("repo-scan", "repository", "scan")
    d.connect("scan-parser", "scan", "parser")
    d.connect("parser-focus", "parser", "focus-extractors")
    d.connect("parser-experimental", "parser", "experimental-extractors")
    d.connect("focus-indexer", "focus-extractors", "indexer")
    d.connect("experimental-indexer", "experimental-extractors", "indexer")
    d.connect("framework-indexer", "framework", "indexer")
    d.connect("indexer-db", "indexer", "sqlite")
    d.connect("query-db", "query", "sqlite", source_side="bottom",
              target_side="top", bidirectional=True,
              via=[(1370, 680), (1972, 680)])
    d.text("query-db-label", 1415, 650, "consulta / read-repair", size=14)

    d.box("boot", 105, 1190, 255, 95, "Boot scan",
          "sincroniza ao iniciar", fill=ORANGE, body_size=14)
    d.box("watcher", 410, 1190, 255, 95, "Watcher · watcher.py",
          "eventos + debounce", fill=ORANGE, body_size=14)
    d.box("read-repair", 715, 1190, 280, 95, "Read-repair",
          "verifica durante consulta", fill=ORANGE, body_size=14)
    for source, suffix in [("boot", "boot-index"), ("watcher", "watch-index"),
                           ("read-repair", "repair-index")]:
        d.connect(suffix, source, "indexer", source_side="right",
                  target_side="bottom", via=[(1450, 1370)])
    d.box("freshness-gap", 105, 1330, 890, 115, "Frescor estrito implementado",
          "Read-repair compara bytes/hash exato; size+mtime são somente hints. Edit igual em tamanho e mtime também converge.",
          fill=GREEN, body_size=14)

    # 3. Semantic and derived layers.
    d.section("section-layers", 2215, 175, 1335, 775,
              "3. Camadas que refinam ou enriquecem o grafo")
    d.box("setup", 2260, 270, 305, 135, "Setup · setup_tools.py",
          "detecta toolchains\nplano explícito --install\nconfiguração local", fill=ORANGE)
    d.box("toolchains", 2650, 270, 300, 135, "Toolchains externos",
          "JDK 21 + JDTLS\nJedi/Python\nsem instalação escondida", fill=GRAY)
    d.box("l1", 3030, 245, 455, 195, "L1 semântico · l1/*",
          "POR QUÊ: nomes não bastam\nJDTLS (Java) · Jedi (Python)\nresolve definição/receiver/overload\npromove aresta para certain", fill=PURPLE,
          stroke=OPTIONAL)
    d.connect("setup-tools", "setup", "toolchains")
    d.connect("tools-l1", "toolchains", "l1")
    d.connect("db-l1", "sqlite", "l1", source_side="right",
              target_side="bottom", color=OPTIONAL, bidirectional=True,
              via=[(2175, 1040), (3410, 1040), (3410, 440)])

    d.box("l2", 2290, 535, 465, 175, "L2 · rank.py + community.py",
          "POR QUÊ: priorizar o que importa\nPageRank = centralidade\nLouvain = domínios\nrecomputação lazy/determinística", fill=PURPLE,
          stroke=OPTIONAL)
    d.box("l3", 2825, 535, 465, 175, "L3 · l3/provider.py",
          "POR QUÊ: contexto legível\ndescrições LLM sob demanda\ncache por body_hash\nopcional; não cria verdade estrutural", fill=PURPLE,
          stroke=OPTIONAL)
    d.connect("db-l2", "sqlite", "l2", source_side="right",
              target_side="bottom", color=OPTIONAL, bidirectional=True,
              via=[(2180, 1110), (2520, 1110)])
    d.connect("db-l3", "sqlite", "l3", source_side="right",
              target_side="bottom", color=OPTIONAL, bidirectional=True,
              via=[(2180, 1160), (3055, 1160)])
    d.box("readiness-gap", 2260, 770, 1225, 125,
          "Gate aberto: ciclo de vida semântico",
          "L1 precisa publicar not_started/running/complete/partial de modo atômico; uma consulta nunca pode ver reset vazio como resultado completo.",
          fill=RED, stroke=FUTURE, stroke_style="dashed", body_size=15)

    # 4. Dataflow and vulnerability analysis.
    d.section("section-flow", 2215, 980, 1335, 520,
              "4. Fluxo de valores e vulnerabilidades (hoje: sob demanda)")
    d.box("flow-request", 2260, 1075, 240, 125, "Query solicita fluxo",
          "data_flow / reaches\n/ taint", fill=GREEN, body_size=14)
    d.box("facts", 2560, 1075, 270, 125, "Fatos · dataflow.py",
          "reabre AST do arquivo\nparams/assign/calls/return", fill=YELLOW,
          body_size=14)
    d.box("cfg", 2890, 1075, 270, 125, "CFG · flowsens.py",
          "branch/loop/kills\nmay-taint conservador", fill=YELLOW,
          body_size=14)
    d.box("interproc", 3220, 1075, 270, 125, "Composição interproc.",
          "query.py combina\nsumários + call graph", fill=YELLOW,
          body_size=14)
    d.connect("flow-facts", "flow-request", "facts")
    d.connect("facts-cfg", "facts", "cfg")
    d.connect("cfg-interproc", "cfg", "interproc")
    d.connect("query-flow", "query", "flow-request", source_side="right",
              target_side="left", via=[(2180, 410), (2180, 1137)])
    d.connect("repo-facts", "repository", "facts", source_side="bottom",
              target_side="left", color="#f08c00",
              via=[(250, 1470), (2190, 1470), (2190, 1137)])
    d.text("repo-facts-label", 1590, 1435, "reparse o código: fatos ainda não persistem", size=14,
           color="#c26500")

    d.box("taint-rules", 2400, 1280, 355, 145, "Regras e catálogos",
          "taint_rules.py + taint_catalog.py\nsource · propagator · sanitizer · sink",
          fill=ORANGE, body_size=14)
    d.box("finding", 2920, 1280, 430, 145, "Finding / caminho inspecionável",
          "source → transformações → sink\nlocalização + regra + completeness",
          fill=GREEN, body_size=15)
    d.connect("rules-interproc", "taint-rules", "interproc",
              source_side="top", target_side="bottom")
    d.connect("interproc-finding", "interproc", "finding",
              source_side="bottom", target_side="top")
    d.arrow("future-persist-flow", (2560, 1450), (2110, 1220), color=FUTURE,
            style="dashed")
    d.text("future-persist-label", 2250, 1430,
           "G3: persistir flows_to interprocedural/CFG", size=14,
           color=FUTURE)

    # 5. Incremental correctness lifecycle.
    d.section("section-live", 60, 1540, 2100, 365,
              "5. Por que o mapa consegue acompanhar mudanças")
    live_boxes = [
        ("edit", 105, "1 · Edit/add/delete", "evento ou próxima query"),
        ("owned", 435, "2 · Descobrir owner", "símbolo = arquivo definidor\naresta = site da referência"),
        ("transaction", 790, "3 · Transação por arquivo", "delete fatos de F\nparse + insert de F"),
        ("dangling", 1160, "4 · Preservar inbound", "alvo removido → dst NULL\ndst_name permanece"),
        ("relink", 1515, "5 · Re-resolver", "novo alvo reconecta\nsem perder evidência"),
        ("invalidate", 1835, "6 · Versionar snapshot", "Git + dirty hash\nstages L0/L1/L2/L3/flow"),
    ]
    widths = {"edit": 255, "owned": 285, "transaction": 300,
              "dangling": 285, "relink": 250, "invalidate": 265}
    for element_id, x, title, body in live_boxes:
        d.box(element_id, x, 1645, widths[element_id], 155, title, body,
              fill=BLUE, body_size=14, title_size=17)
    for left, right in zip([item[0] for item in live_boxes],
                           [item[0] for item in live_boxes][1:]):
        d.connect(f"{left}-{right}", left, right)
    d.text("live-invariant", 110, 1830,
           "Invariante: o código é a fonte da verdade; o banco é cache derivado e reconstruível.",
           size=18, color="#495057")

    # 6. Evidence and roadmap.
    d.section("section-quality", 2215, 1540, 1335, 830,
              "6. Como sabemos se a arquitetura virou produto")
    d.box("contract", 2260, 1640, 360, 135, "Product Contract",
          "fonte canônica\nJava + Python\nmesmos fatos em todas APIs", fill=BLUE)
    d.box("tests", 2690, 1640, 360, 135, "Testes focados",
          "unitários + invariantes\nfixtures compartilhadas\nregressões incrementais", fill=GREEN)
    d.box("external-evidence", 3120, 1640, 360, 135, "Evidência externa",
          "dogfood → canários reais\nOWASP/Juliet por último\nbenchmark ≠ especificação", fill=GRAY)
    d.connect("contract-tests", "contract", "tests")
    d.connect("tests-evidence", "tests", "external-evidence")

    gates = [
        ("g0", 2260, 1835, "G0 · Observabilidade", "PARCIAL\nestados/coverage completos", YELLOW),
        ("g1", 2680, 1835, "G1 · Grafo estrutural", "CORE FEITO\nfalta prova em canários", YELLOW),
        ("g2", 3100, 1835, "G2 · Linking semântico", "calls confiáveis\nreadiness atômica", RED),
        ("g3", 2260, 1995, "G3 · Dataflow persistente", "def-use + fluxo\nentre funções", RED),
        ("g4", 2680, 1995, "G4 · Vulnerabilidades", "Java/Python sobre\no mesmo flow graph", RED),
        ("g5", 3100, 1995, "G5 · Aceitação", "wheel + CLI + MCP\nrepos comuns pinados", RED),
    ]
    for element_id, x, y, title, body, fill in gates:
        d.box(element_id, x, y, 360, 125, title, body, fill=fill,
              stroke=FUTURE if fill == RED else INK,
              stroke_style="dashed" if fill == RED else "solid",
              title_size=17, body_size=14)
    d.connect("g0-g1", "g0", "g1")
    d.connect("g1-g2", "g1", "g2")
    d.connect("g2-g3", "g2", "g3", source_side="bottom", target_side="right",
              via=[(3480, 2160), (2225, 2160), (2225, 2057)])
    d.connect("g3-g4", "g3", "g4")
    d.connect("g4-g5", "g4", "g5")

    d.box("truth", 2260, 2170, 1220, 145, "Estado honesto em 2026-08-24",
          "Alpha structural graph: árvore física, hashes/revisões e Java/Python estrutural persistem. Ainda NÃO é CPG completo: flows_to interprocedural e linking semântico robusto permanecem gates.",
          fill=YELLOW, body_size=16)

    return {
        "type": "excalidraw",
        "version": 2,
        "source": "https://github.com/Victor-Alves0/graphcodemap",
        "elements": d.elements,
        "appState": {
            "gridSize": 20,
            "gridStep": 5,
            "gridModeEnabled": False,
            "viewBackgroundColor": "#ffffff",
        },
        "files": {},
    }


def main() -> None:
    OUTPUT.write_text(
        json.dumps(build(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()
