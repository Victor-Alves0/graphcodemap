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

from .util import like_escape

_MAX_NODES = 250


def build_graph_data(conn, level: str = "file", scope: str | None = None,
                     top: int = _MAX_NODES) -> dict:
    if level == "symbol":
        nodes, links = _symbol_graph(conn, scope, top)
    else:
        nodes, links = _file_graph(conn, scope, top)
    domains = _domain_legend(conn, nodes)
    return {"level": level, "scope": scope or "",
            "nodes": nodes, "links": links, "domains": domains}


def _scope_clause(scope: str | None):
    if not scope:
        return "", []
    prefix = like_escape(scope.rstrip("/").replace("\\", "/")) + "%"
    return " AND f.path LIKE ? ESCAPE '\\'", [prefix]


def _file_graph(conn, scope, top):
    where, args = _scope_clause(scope)
    frows = conn.execute(
        f"SELECT f.id, f.path, "
        f"(SELECT community FROM symbols s WHERE s.file_id=f.id "
        f"   AND community IS NOT NULL GROUP BY community "
        f"   ORDER BY COUNT(*) DESC LIMIT 1) AS domain, "
        f"(SELECT COALESCE(SUM(rank),0) FROM symbols s WHERE s.file_id=f.id) AS weight, "
        f"(SELECT COUNT(*) FROM symbols s WHERE s.file_id=f.id) AS nsyms "
        f"FROM files f WHERE 1=1{where}", args).fetchall()
    fmap = {r["id"]: dict(r) for r in frows}
    edge = conn.execute(
        "SELECT s1.file_id AS a, s2.file_id AS b, COUNT(*) AS w FROM edges e "
        "JOIN symbols s1 ON e.src=s1.id JOIN symbols s2 ON e.dst=s2.id "
        "WHERE e.src IS NOT NULL AND e.dst IS NOT NULL "
        "AND s1.file_id != s2.file_id GROUP BY s1.file_id, s2.file_id").fetchall()
    # agrega não-direcionado
    pair: dict[tuple[int, int], int] = {}
    degree: dict[int, int] = {}
    for r in edge:
        a, b = r["a"], r["b"]
        if a not in fmap or b not in fmap:
            continue
        key = (a, b) if a < b else (b, a)
        pair[key] = pair.get(key, 0) + r["w"]
        degree[a] = degree.get(a, 0) + r["w"]
        degree[b] = degree.get(b, 0) + r["w"]
    keep = sorted(degree, key=lambda i: -degree[i])[:top]
    keep_set = set(keep)
    nodes = [{"id": i, "label": fmap[i]["path"], "domain": fmap[i]["domain"],
              "weight": fmap[i]["weight"], "n": fmap[i]["nsyms"]} for i in keep]
    links = [{"source": a, "target": b, "w": w}
             for (a, b), w in pair.items() if a in keep_set and b in keep_set]
    return nodes, links


def _symbol_graph(conn, scope, top):
    where, args = _scope_clause(scope)
    srows = conn.execute(
        f"SELECT s.id, s.fqn, s.kind, s.rank, s.community AS domain "
        f"FROM symbols s JOIN files f ON s.file_id=f.id "
        f"WHERE 1=1{where} ORDER BY s.rank DESC LIMIT ?", [*args, top]).fetchall()
    keep = {r["id"]: dict(r) for r in srows}
    nodes = [{"id": r["id"], "label": r["fqn"], "domain": r["domain"],
              "weight": r["rank"], "n": 1, "kind": r["kind"]} for r in srows]
    ph = ",".join("?" * len(keep))
    links = []
    if keep:
        for r in conn.execute(
            f"SELECT src, dst, COUNT(*) w FROM edges WHERE kind='calls' "
            f"AND src IN ({ph}) AND dst IN ({ph}) AND src != dst "
            f"GROUP BY src, dst", [*keep, *keep]).fetchall():
            links.append({"source": r["src"], "target": r["dst"], "w": r["w"]})
    return nodes, links


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
    return _TEMPLATE.replace("__DATA__", payload) \
                    .replace("__SHOWN__", str(shown)) \
                    .replace("__LEVEL__", data["level"]) \
                    .replace("__SCOPE__", scope)


_TEMPLATE = r"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<title>graphcodemap — mapa do repo</title>
<style>
  :root{color-scheme:dark light}
  body{margin:0;font:13px/1.4 system-ui,sans-serif;background:#0e1116;color:#d7dbe0;overflow:hidden}
  #hud{position:fixed;top:10px;left:10px;z-index:10;max-width:320px;
       background:#171b22cc;padding:10px 12px;border-radius:8px;backdrop-filter:blur(4px)}
  #hud h1{font-size:14px;margin:0 0 4px}
  #hud .sub{color:#8b949e;font-size:11px;margin-bottom:8px}
  #legend{max-height:40vh;overflow:auto}
  #legend div{display:flex;align-items:center;gap:6px;padding:1px 0;cursor:default}
  #legend i{width:10px;height:10px;border-radius:2px;flex:0 0 auto}
  #tip{position:fixed;pointer-events:none;background:#000c;color:#fff;padding:3px 6px;
       border-radius:4px;font-size:12px;display:none;z-index:20;max-width:60ch;word-break:break-all}
  canvas{display:block}
</style></head><body>
<div id="hud">
  <h1>graphcodemap</h1>
  <div class="sub">nível __LEVEL__ · __SHOWN__ nós · escopo __SCOPE__<br>
  arraste p/ mover · scroll p/ zoom · cor = domínio · tamanho = PageRank</div>
  <div id="legend"></div>
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
const nodes = DATA.nodes, links = DATA.links;
const byId = new Map(nodes.map(n=>[n.id,n]));
// escala de tamanho
const ws = nodes.map(n=>n.weight||0), wmax = Math.max(1e-9,...ws);
nodes.forEach(n=>{ n.r = 4 + 14*Math.sqrt((n.weight||0)/wmax);
  const a = Math.random()*6.28, rad = 200+Math.random()*250;
  n.x = W/2 + Math.cos(a)*rad; n.y = H/2 + Math.sin(a)*rad; n.vx=0; n.vy=0; });
links.forEach(l=>{ l.s=byId.get(l.source); l.t=byId.get(l.target); });
const linksOk = links.filter(l=>l.s&&l.t);

// force sim (O(n^2), ok até algumas centenas de nós)
let alpha = 1;
function step(){
  if(alpha<0.005) return;
  alpha *= 0.985;
  for(let i=0;i<nodes.length;i++){ const a=nodes[i];
    for(let j=i+1;j<nodes.length;j++){ const b=nodes[j];
      let dx=a.x-b.x, dy=a.y-b.y, d2=dx*dx+dy*dy||1;
      const f = 900/d2; const d=Math.sqrt(d2);
      dx/=d; dy/=d; a.vx+=dx*f; a.vy+=dy*f; b.vx-=dx*f; b.vy-=dy*f;
    }
    a.vx += (W/2-a.x)*0.002; a.vy += (H/2-a.y)*0.002;   // gravidade ao centro
  }
  for(const l of linksOk){ let dx=l.t.x-l.s.x, dy=l.t.y-l.s.y;
    const d=Math.sqrt(dx*dx+dy*dy)||1; const f=(d-70)*0.02*Math.min(1,l.w/4);
    dx/=d; dy/=d; l.s.vx+=dx*f; l.s.vy+=dy*f; l.t.vx-=dx*f; l.t.vy-=dy*f; }
  for(const n of nodes){ if(n===drag) continue;
    n.x+=n.vx*alpha; n.y+=n.vy*alpha; n.vx*=0.85; n.vy*=0.85; }
}

let view={x:0,y:0,k:1}, drag=null;
function draw(){
  step();
  ctx.setTransform(1,0,0,1,0,0); ctx.clearRect(0,0,W,H);
  ctx.setTransform(view.k,0,0,view.k,view.x,view.y);
  ctx.globalAlpha=0.18; ctx.strokeStyle='#7d8590'; ctx.lineWidth=0.6;
  for(const l of linksOk){ ctx.beginPath(); ctx.moveTo(l.s.x,l.s.y);
    ctx.lineTo(l.t.x,l.t.y); ctx.stroke(); }
  ctx.globalAlpha=1;
  for(const n of nodes){ ctx.beginPath(); ctx.arc(n.x,n.y,n.r,0,6.283);
    ctx.fillStyle=domColor(n.domain); ctx.fill();
    ctx.lineWidth=0.7; ctx.strokeStyle='#0e1116'; ctx.stroke(); }
  requestAnimationFrame(draw);
}
draw();

// interação
const scr = (mx,my)=>({x:(mx-view.x)/view.k, y:(my-view.y)/view.k});
function pick(mx,my){ const p=scr(mx,my); let best=null,bd=1e9;
  for(const n of nodes){ const dx=n.x-p.x, dy=n.y-p.y, d=dx*dx+dy*dy;
    if(d<Math.max(n.r*n.r,64) && d<bd){ bd=d; best=n; } } return best; }
const tip=document.getElementById('tip');
cv.addEventListener('mousemove',e=>{
  if(drag){ const p=scr(e.clientX,e.clientY); drag.x=p.x; drag.y=p.y; alpha=Math.max(alpha,.3); return; }
  const n=pick(e.clientX,e.clientY);
  if(n){ tip.style.display='block'; tip.style.left=(e.clientX+10)+'px';
    tip.style.top=(e.clientY+10)+'px';
    tip.textContent=`${n.label}  ·  ${n.n} símbolos${n.domain!=null?'  ·  dom '+n.domain:''}`; }
  else tip.style.display='none';
});
cv.addEventListener('mousedown',e=>{ drag=pick(e.clientX,e.clientY);
  if(!drag){ pan={x:e.clientX-view.x,y:e.clientY-view.y}; } });
let pan=null;
addEventListener('mousemove',e=>{ if(pan&&!drag){ view.x=e.clientX-pan.x; view.y=e.clientY-pan.y; } });
addEventListener('mouseup',()=>{ drag=null; pan=null; });
cv.addEventListener('wheel',e=>{ e.preventDefault();
  const s=Math.exp(-e.deltaY*0.001), mx=e.clientX,my=e.clientY;
  view.x=mx-(mx-view.x)*s; view.y=my-(my-view.y)*s; view.k*=s; },{passive:false});

// legenda
const leg=document.getElementById('legend');
if(DATA.domains.length){
  for(const d of DATA.domains){ const row=document.createElement('div');
    const sw=document.createElement('i'); sw.style.background=domColor(d.id);
    const t=document.createElement('span');
    t.textContent=`dom ${d.id}${d.label?' — '+d.label:''} (${d.size})`;
    row.append(sw,t); leg.append(row); }
} else { leg.textContent='(sem domínios — rode com o índice de comunidades)'; }
</script></body></html>
"""
