"""Export de visualização: grafo do repo como HTML autocontido (offline).

Filosofia igual ao resto: sem dependência, sem CDN — um único .html que abre
no navegador. Nós = arquivos (ou símbolos), cor = domínio (comunidade), tamanho
= importância (PageRank). Simulação force-directed em canvas, embutida.

Repos grandes: agregamos por arquivo e limitamos aos N nós mais conectados
(a própria doc do Graphify admite travar acima de ~5k nós). Honesto: o corte
é declarado no cabeçalho da página.
"""

from __future__ import annotations

import json
import subprocess

from .util import like_escape

_MAX_NODES = 250

# modos SEMEADOS por um símbolo (ou pelos arquivos alterados): subgrafo focado,
# não o repo inteiro — é o que troca "mapa decorativo" por "ferramenta".
_SEEDED = ("neighborhood", "callers", "callees", "impact")
_IMPACT_KINDS = ("calls", "imports", "inherits", "references")
_CONF_RANK = {"possible": 0, "inferred": 1, "certain": 2}

# aliases para os dois modos legados (compat com level="file"/"symbol")
_ALIAS = {"modules": "file", "module": "file", "symbols": "symbol"}


def build_graph_data(conn, mode: str = "file", *, level: str | None = None,
                     scope: str | None = None, top: int = _MAX_NODES,
                     seed_ids: list | None = None, seed_label: str | None = None,
                     depth: int = 3, min_confidence: str | None = None,
                     language: str | None = None,
                     changed_files: set | None = None) -> dict:
    """Monta o subgrafo do modo pedido, já aplicando os filtros.

    Modos: `file`/`symbol` (mapa por arquivo/símbolo, legados), `neighborhood`
    (vizinhança de um símbolo), `callers`/`callees` (chamadas de entrada/saída),
    `impact` (o que depende do símbolo/arquivos alterados), `domains` (grafo
    entre comunidades). Filtros ortogonais: `min_confidence`, `language`,
    `changed_files` (destaca/restringe pelo que mudou no Git)."""
    mode = _ALIAS.get(mode or level or "file", mode or level or "file")
    seed_ids = list(seed_ids or [])
    changed_files = set(changed_files or ())

    if mode in _SEEDED:
        nodes, links, directed = _seeded_graph(
            conn, mode, seed_ids, depth, min_confidence, language)
    elif mode == "domains":
        nodes, links, directed = _domains_graph(conn, scope, min_confidence)
    elif mode == "symbol":
        nodes, links = _symbol_graph(conn, scope, top, min_confidence,
                                     language, changed_files)
        directed = True
    else:
        mode = "file"
        nodes, links = _file_graph(conn, scope, top, min_confidence,
                                   language, changed_files)
        directed = False

    for n in nodes:
        n.setdefault("changed", bool(changed_files) and _node_changed(n, changed_files))
    langs = sorted({n["language"] for n in nodes if n.get("language")})
    confs = sorted({l["confidence"] for l in links if l.get("confidence")},
                   key=lambda c: _CONF_RANK.get(c, 9))
    return {
        "mode": mode, "level": mode, "scope": scope or "",
        "seed": seed_label or "", "directed": directed,
        "filters": {"min_confidence": min_confidence or "",
                    "language": language or "", "changed": len(changed_files)},
        "languages": langs, "confidences": confs,
        "nodes": nodes, "links": links, "domains": _domain_legend(conn, nodes),
    }


def _min_conf_ok(conf: str | None, minc: str | None) -> bool:
    if not minc:
        return True
    return _CONF_RANK.get(conf or "possible", 0) >= _CONF_RANK.get(minc, 0)


def _node_changed(node, changed_files) -> bool:
    p = node.get("path") or node.get("label")
    return p in changed_files


def _scope_clause(scope: str | None):
    if not scope:
        return "", []
    prefix = like_escape(scope.rstrip("/").replace("\\", "/")) + "%"
    return " AND f.path LIKE ? ESCAPE '\\'", [prefix]


# -- git ---------------------------------------------------------------------

def git_changed_files(root, ref: str | None = None, staged: bool = False) -> set[str]:
    """Arquivos alterados no Git, repo-relativos (barras '/'). `ref`: diff contra
    um ref (ex.: 'main'); `staged`: só o índice; senão worktree (unstaged +
    staged + untracked). Degrada para vazio se não for repo git."""
    if ref:
        cmds = [["diff", "--name-only", ref]]
    elif staged:
        cmds = [["diff", "--name-only", "--cached"]]
    else:
        cmds = [["diff", "--name-only"], ["diff", "--name-only", "--cached"],
                ["ls-files", "--others", "--exclude-standard"]]
    out: set[str] = set()
    for c in cmds:
        try:
            r = subprocess.run(["git", "-C", str(root), *c],
                               capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        if r.returncode == 0:
            out |= {ln.strip().replace("\\", "/")
                    for ln in r.stdout.splitlines() if ln.strip()}
    return out


# -- modos de mapa (arquivo/símbolo) -----------------------------------------

def _file_graph(conn, scope, top, min_confidence, language, changed_files):
    where, args = _scope_clause(scope)
    frows = conn.execute(
        f"SELECT f.id, f.path, f.language, "
        f"(SELECT community FROM symbols s WHERE s.file_id=f.id "
        f"   AND community IS NOT NULL GROUP BY community "
        f"   ORDER BY COUNT(*) DESC LIMIT 1) AS domain, "
        f"(SELECT COALESCE(SUM(rank),0) FROM symbols s WHERE s.file_id=f.id) AS weight, "
        f"(SELECT COUNT(*) FROM symbols s WHERE s.file_id=f.id) AS nsyms "
        f"FROM files f WHERE 1=1{where}", args).fetchall()
    fmap = {r["id"]: dict(r) for r in frows}
    if language:
        fmap = {i: r for i, r in fmap.items() if r["language"] == language}
    rows = conn.execute(
        "SELECT s1.file_id AS a, s2.file_id AS b, e.confidence AS conf "
        "FROM edges e JOIN symbols s1 ON e.src=s1.id JOIN symbols s2 ON e.dst=s2.id "
        "WHERE e.src IS NOT NULL AND e.dst IS NOT NULL "
        "AND s1.file_id != s2.file_id").fetchall()
    pair: dict[tuple[int, int], dict] = {}
    degree: dict[int, int] = {}
    for r in rows:
        a, b = r["a"], r["b"]
        if a not in fmap or b not in fmap or not _min_conf_ok(r["conf"], min_confidence):
            continue
        key = (a, b) if a < b else (b, a)
        p = pair.setdefault(key, {"w": 0, "conf": r["conf"]})
        p["w"] += 1
        # confiança do par = a MAIS FORTE observada (para o filtro/estilo)
        if _CONF_RANK.get(r["conf"], 0) > _CONF_RANK.get(p["conf"], 0):
            p["conf"] = r["conf"]
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    # changed: restringe aos arquivos alterados + vizinhos de 1 salto
    if changed_files:
        changed_ids = {i for i, r in fmap.items() if r["path"] in changed_files}
        keep_adj = set(changed_ids)
        for (a, b) in pair:
            if a in changed_ids:
                keep_adj.add(b)
            if b in changed_ids:
                keep_adj.add(a)
        cand = [i for i in fmap if i in keep_adj]
    else:
        cand = list(fmap)
    keep = sorted(cand, key=lambda i: -degree.get(i, 0))[:top]
    keep_set = set(keep)
    nodes = [{"id": i, "label": fmap[i]["path"], "domain": fmap[i]["domain"],
              "weight": fmap[i]["weight"], "n": fmap[i]["nsyms"],
              "path": fmap[i]["path"], "language": fmap[i]["language"]}
             for i in keep]
    links = [{"source": a, "target": b, "w": p["w"], "confidence": p["conf"],
              "kind": "dep"}
             for (a, b), p in pair.items() if a in keep_set and b in keep_set]
    return nodes, links


def _symbol_graph(conn, scope, top, min_confidence, language, changed_files):
    where, args = _scope_clause(scope)
    lang_sql = " AND f.language=?" if language else ""
    lang_args = [language] if language else []
    srows = conn.execute(
        f"SELECT s.id, s.fqn, s.kind, s.rank, s.community AS domain, "
        f"f.path, f.language FROM symbols s JOIN files f ON s.file_id=f.id "
        f"WHERE 1=1{where}{lang_sql} ORDER BY s.rank DESC LIMIT ?",
        [*args, *lang_args, top]).fetchall()
    keep = {r["id"] for r in srows}
    nodes = [{"id": r["id"], "label": r["fqn"], "domain": r["domain"],
              "weight": r["rank"], "n": 1, "kind": r["kind"],
              "path": r["path"], "language": r["language"]} for r in srows]
    links = []
    if keep:
        ph = ",".join("?" * len(keep))
        for r in conn.execute(
            f"SELECT src, dst, kind, confidence, COUNT(*) w FROM edges "
            f"WHERE src IN ({ph}) AND dst IN ({ph}) AND src != dst "
            f"GROUP BY src, dst, kind", [*keep, *keep]).fetchall():
            if _min_conf_ok(r["confidence"], min_confidence):
                links.append({"source": r["src"], "target": r["dst"],
                              "kind": r["kind"], "confidence": r["confidence"],
                              "w": r["w"]})
    return nodes, links


# -- modos semeados (vizinhança / callers / callees / impacto) ---------------

def _seeded_graph(conn, mode, seed_ids, depth, min_confidence, language):
    directions = {"neighborhood": ("in", "out"), "callers": ("in",),
                  "callees": ("out",), "impact": ("in",)}[mode]
    kinds = (("calls",) if mode in ("callers", "callees")
             else _IMPACT_KINDS if mode == "impact" else None)
    seen = set(seed_ids)
    frontier = set(seed_ids)
    links: list[dict] = []
    linkseen: set[tuple] = set()
    for _ in range(max(1, depth)):
        if not frontier:
            break
        nxt: set = set()
        for direction in directions:
            for r in _edge_rows(conn, frontier, direction, kinds, min_confidence):
                lk = (r["src"], r["dst"], r["kind"])
                if lk not in linkseen:
                    linkseen.add(lk)
                    links.append({"source": r["src"], "target": r["dst"],
                                  "kind": r["kind"],
                                  "confidence": r["confidence"], "w": 1})
                other = r["dst"] if direction == "out" else r["src"]
                if other not in seen:
                    seen.add(other)
                    nxt.add(other)
        frontier = nxt
    nodes = _fetch_nodes(conn, seen, language)
    keep = {n["id"] for n in nodes}
    seeds = set(seed_ids)
    for n in nodes:
        n["seed"] = n["id"] in seeds
    links = [l for l in links if l["source"] in keep and l["target"] in keep]
    return nodes, links, True


def _edge_rows(conn, frontier, direction, kinds, min_confidence):
    ph = ",".join("?" * len(frontier))
    col = "src" if direction == "out" else "dst"
    args = list(frontier)
    kind_sql = ""
    if kinds:
        kind_sql = f" AND e.kind IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    rows = conn.execute(
        f"SELECT e.src, e.dst, e.kind, e.confidence FROM edges e "
        f"WHERE e.{col} IN ({ph}) AND e.src IS NOT NULL AND e.dst IS NOT NULL"
        f"{kind_sql}", args).fetchall()
    return [r for r in rows if _min_conf_ok(r["confidence"], min_confidence)]


def _fetch_nodes(conn, ids, language):
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT s.id, s.fqn, s.kind, s.rank, s.community AS domain, "
        f"f.path, f.language FROM symbols s JOIN files f ON s.file_id=f.id "
        f"WHERE s.id IN ({ph})", list(ids)).fetchall()
    nodes = [{"id": r["id"], "label": r["fqn"], "domain": r["domain"],
              "weight": r["rank"] or 0, "n": 1, "kind": r["kind"],
              "path": r["path"], "language": r["language"]} for r in rows]
    if language:
        nodes = [n for n in nodes if n["language"] == language]
    return nodes


# -- modo domínios (grafo entre comunidades) ---------------------------------

def _domains_graph(conn, scope, min_confidence):
    comm = conn.execute(
        "SELECT id, size, label FROM communities").fetchall()
    csize = {r["id"]: r for r in comm}
    rows = conn.execute(
        "SELECT s1.community AS a, s2.community AS b, e.confidence AS conf "
        "FROM edges e JOIN symbols s1 ON e.src=s1.id JOIN symbols s2 ON e.dst=s2.id "
        "WHERE s1.community IS NOT NULL AND s2.community IS NOT NULL "
        "AND s1.community != s2.community").fetchall()
    pair: dict[tuple[int, int], dict] = {}
    used: set[int] = set()
    for r in rows:
        if not _min_conf_ok(r["conf"], min_confidence):
            continue
        a, b = r["a"], r["b"]
        p = pair.setdefault((a, b), {"w": 0, "conf": r["conf"]})
        p["w"] += 1
        if _CONF_RANK.get(r["conf"], 0) > _CONF_RANK.get(p["conf"], 0):
            p["conf"] = r["conf"]
        used.update((a, b))
    nodes = [{"id": cid, "label": f"dom {cid}" + (f" — {c['label']}" if c["label"] else ""),
              "domain": cid, "weight": c["size"], "n": c["size"]}
             for cid, c in csize.items() if cid in used or not pair]
    links = [{"source": a, "target": b, "w": p["w"], "confidence": p["conf"],
              "kind": "dep"} for (a, b), p in pair.items()]
    return nodes, links, True


def _domain_legend(conn, nodes) -> list[dict]:
    ids = sorted({n["domain"] for n in nodes if n["domain"] is not None})
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, size, label FROM communities WHERE id IN ({ph}) "
        f"ORDER BY size DESC", ids).fetchall()
    return [{"id": r["id"], "size": r["size"], "label": r["label"]} for r in rows]


def render_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    shown = len(data["nodes"])
    scope = data["scope"] or "(repo inteiro)"
    seed = data.get("seed") or ""
    sub = f"modo {data['mode']}"
    if seed:
        sub += f" · {seed}"
    sub += f" · {shown} nós · escopo {scope}"
    return _TEMPLATE.replace("__DATA__", payload) \
                    .replace("__SUB__", _esc(sub))


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_TEMPLATE = r"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<title>graphcodemap — investigação</title>
<style>
  :root{color-scheme:dark light}
  body{margin:0;font:13px/1.4 system-ui,sans-serif;background:#0e1116;color:#d7dbe0;overflow:hidden}
  #hud{position:fixed;top:10px;left:10px;z-index:10;max-width:330px;
       background:#171b22cc;padding:10px 12px;border-radius:8px;backdrop-filter:blur(4px)}
  #hud h1{font-size:14px;margin:0 0 4px}
  #hud .sub{color:#8b949e;font-size:11px;margin-bottom:8px}
  #hud .grp{margin:6px 0;border-top:1px solid #30363d;padding-top:6px}
  #hud .grp b{font-size:10px;text-transform:uppercase;color:#8b949e;letter-spacing:.5px}
  #hud label{display:flex;align-items:center;gap:6px;padding:1px 0;font-size:12px;cursor:pointer}
  #hud label i{width:10px;height:10px;border-radius:2px;flex:0 0 auto}
  #legend{max-height:32vh;overflow:auto}
  #tip{position:fixed;pointer-events:none;background:#000c;color:#fff;padding:3px 6px;
       border-radius:4px;font-size:12px;display:none;z-index:20;max-width:60ch;word-break:break-all}
  canvas{display:block}
</style></head><body>
<div id="hud">
  <h1>graphcodemap</h1>
  <div class="sub">__SUB__<br>arraste · scroll zoom · cor = domínio · tamanho = PageRank
  · <span style="color:#f0883e">anel vermelho</span> = alterado · <span style="color:#fff">anel branco</span> = foco</div>
  <div id="confbox" class="grp"></div>
  <div id="langbox" class="grp"></div>
  <div id="legend" class="grp"></div>
</div>
<div id="tip"></div>
<canvas id="c"></canvas>
<script>
const DATA = __DATA__;
const cv = document.getElementById('c'), ctx = cv.getContext('2d');
let W, H;
function resize(){ W=cv.width=innerWidth; H=cv.height=innerHeight; }
addEventListener('resize', resize); resize();

const domColor = d => d==null ? '#666' : `hsl(${(d*137.5)%360} 60% 58%)`;
const CONF = {certain:{dash:[],a:.6,c:'#3fb950'}, inferred:{dash:[6,4],a:.42,c:'#d29922'},
              possible:{dash:[2,5],a:.3,c:'#8b949e'}, dep:{dash:[],a:.32,c:'#7d8590'}};
const cstyle = k => CONF[k] || CONF.dep;
const nodes = DATA.nodes, links = DATA.links, directed = DATA.directed;
const byId = new Map(nodes.map(n=>[n.id,n]));
const ws = nodes.map(n=>n.weight||0), wmax = Math.max(1e-9,...ws);
nodes.forEach(n=>{ n.r = (4 + 14*Math.sqrt((n.weight||0)/wmax)) * (n.seed?1.5:1);
  const a = Math.random()*6.28, rad = 150+Math.random()*300;
  n.x = W/2 + Math.cos(a)*rad; n.y = H/2 + Math.sin(a)*rad; n.vx=0; n.vy=0; });
links.forEach(l=>{ l.s=byId.get(l.source); l.t=byId.get(l.target); });
const linksOk = links.filter(l=>l.s&&l.t);

// --- filtros vivos (toggles) -------------------------------------------------
const activeConf = new Set(DATA.confidences && DATA.confidences.length
                           ? DATA.confidences : ['certain','inferred','possible','dep']);
activeConf.add('dep');
const activeLang = new Set(DATA.languages || []);
const nodeVis = n => !DATA.languages.length || n.seed || !n.language || activeLang.has(n.language);
const linkVis = l => activeConf.has(l.confidence||'dep') && nodeVis(l.s) && nodeVis(l.t);

function chk(box, label, on, swatch, cb){
  const el=document.createElement('label'); const c=document.createElement('input');
  c.type='checkbox'; c.checked=on; c.onchange=()=>{cb(c.checked); alpha=Math.max(alpha,.2);};
  const parts=[c];
  if(swatch){ const i=document.createElement('i'); i.style.background=swatch; parts.push(i); }
  const t=document.createElement('span'); t.textContent=label; parts.push(t);
  el.append(...parts); box.append(el);
}
const cbox=document.getElementById('confbox');
if((DATA.confidences||[]).length){
  cbox.innerHTML='<b>confiança</b>';
  for(const k of DATA.confidences)
    chk(cbox,k,true,cstyle(k).c,on=>on?activeConf.add(k):activeConf.delete(k));
}
const lbox=document.getElementById('langbox');
if((DATA.languages||[]).length>1){
  lbox.innerHTML='<b>linguagem</b>';
  for(const lg of DATA.languages)
    chk(lbox,lg,true,'#586069',on=>on?activeLang.add(lg):activeLang.delete(lg));
}

// --- força -------------------------------------------------------------------
let alpha = 1, drag=null;
function step(){
  if(alpha<0.005) return; alpha *= 0.985;
  for(let i=0;i<nodes.length;i++){ const a=nodes[i];
    for(let j=i+1;j<nodes.length;j++){ const b=nodes[j];
      let dx=a.x-b.x, dy=a.y-b.y, d2=dx*dx+dy*dy||1;
      const f=900/d2, d=Math.sqrt(d2); dx/=d; dy/=d;
      a.vx+=dx*f; a.vy+=dy*f; b.vx-=dx*f; b.vy-=dy*f; }
    a.vx += (W/2-a.x)*0.002; a.vy += (H/2-a.y)*0.002; }
  for(const l of linksOk){ let dx=l.t.x-l.s.x, dy=l.t.y-l.s.y;
    const d=Math.sqrt(dx*dx+dy*dy)||1, f=(d-80)*0.02*Math.min(1,l.w/4);
    dx/=d; dy/=d; l.s.vx+=dx*f; l.s.vy+=dy*f; l.t.vx-=dx*f; l.t.vy-=dy*f; }
  for(const n of nodes){ if(n===drag) continue;
    n.x+=n.vx*alpha; n.y+=n.vy*alpha; n.vx*=0.85; n.vy*=0.85; }
}
function arrow(s,t){ // ponta de seta perto do alvo (respeitando o raio)
  let dx=t.x-s.x, dy=t.y-s.y, d=Math.hypot(dx,dy)||1; dx/=d; dy/=d;
  const ex=t.x-dx*(t.r+2), ey=t.y-dy*(t.r+2), a=6;
  ctx.beginPath(); ctx.moveTo(ex,ey);
  ctx.lineTo(ex-dx*a-dy*a*0.6, ey-dy*a+dx*a*0.6);
  ctx.lineTo(ex-dx*a+dy*a*0.6, ey-dy*a-dx*a*0.6);
  ctx.closePath(); ctx.fill();
}
let view={x:0,y:0,k:1};
function draw(){
  step();
  ctx.setTransform(1,0,0,1,0,0); ctx.clearRect(0,0,W,H);
  ctx.setTransform(view.k,0,0,view.k,view.x,view.y);
  ctx.lineWidth=0.8;
  for(const l of linksOk){ if(!linkVis(l)) continue; const st=cstyle(l.confidence);
    ctx.globalAlpha=st.a; ctx.strokeStyle=st.c; ctx.setLineDash(st.dash);
    ctx.beginPath(); ctx.moveTo(l.s.x,l.s.y); ctx.lineTo(l.t.x,l.t.y); ctx.stroke();
    if(directed){ ctx.fillStyle=st.c; arrow(l.s,l.t); } }
  ctx.setLineDash([]); ctx.globalAlpha=1;
  for(const n of nodes){ if(!nodeVis(n)) continue;
    ctx.beginPath(); ctx.arc(n.x,n.y,n.r,0,6.283);
    ctx.fillStyle=domColor(n.domain); ctx.fill();
    if(n.seed){ ctx.lineWidth=2.4; ctx.strokeStyle='#fff'; }
    else if(n.changed){ ctx.lineWidth=2.2; ctx.strokeStyle='#f0883e'; }
    else { ctx.lineWidth=0.7; ctx.strokeStyle='#0e1116'; }
    ctx.stroke(); }
  requestAnimationFrame(draw);
}
draw();

// --- interação ---------------------------------------------------------------
const scr = (mx,my)=>({x:(mx-view.x)/view.k, y:(my-view.y)/view.k});
function pick(mx,my){ const p=scr(mx,my); let best=null,bd=1e9;
  for(const n of nodes){ if(!nodeVis(n)) continue;
    const dx=n.x-p.x, dy=n.y-p.y, d=dx*dx+dy*dy;
    if(d<Math.max(n.r*n.r,64) && d<bd){ bd=d; best=n; } } return best; }
const tip=document.getElementById('tip');
let pan=null;
cv.addEventListener('mousemove',e=>{
  if(drag){ const p=scr(e.clientX,e.clientY); drag.x=p.x; drag.y=p.y; alpha=Math.max(alpha,.3); return; }
  const n=pick(e.clientX,e.clientY);
  if(n){ tip.style.display='block'; tip.style.left=(e.clientX+10)+'px'; tip.style.top=(e.clientY+10)+'px';
    let s=n.label; if(n.kind) s+='  ·  '+n.kind; if(n.language) s+='  ·  '+n.language;
    if(n.domain!=null) s+='  ·  dom '+n.domain; if(n.changed) s+='  ·  ALTERADO';
    tip.textContent=s; }
  else tip.style.display='none';
});
cv.addEventListener('mousedown',e=>{ drag=pick(e.clientX,e.clientY);
  if(!drag){ pan={x:e.clientX-view.x,y:e.clientY-view.y}; } });
addEventListener('mousemove',e=>{ if(pan&&!drag){ view.x=e.clientX-pan.x; view.y=e.clientY-pan.y; } });
addEventListener('mouseup',()=>{ drag=null; pan=null; });
cv.addEventListener('wheel',e=>{ e.preventDefault();
  const s=Math.exp(-e.deltaY*0.001), mx=e.clientX,my=e.clientY;
  view.x=mx-(mx-view.x)*s; view.y=my-(my-view.y)*s; view.k*=s; },{passive:false});

// --- legenda de domínios -----------------------------------------------------
const leg=document.getElementById('legend');
if(DATA.domains.length){ leg.innerHTML='<b>domínios</b>';
  for(const d of DATA.domains){ const el=document.createElement('label');
    const i=document.createElement('i'); i.style.background=domColor(d.id);
    const t=document.createElement('span');
    t.textContent=`dom ${d.id}${d.label?' — '+d.label:''} (${d.size})`;
    el.style.cursor='default'; el.append(i,t); leg.append(el); }
}
</script></body></html>
"""
