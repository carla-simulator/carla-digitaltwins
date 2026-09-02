"""Build the interactive curation-review page for the sign catalog: every atlas cell that still
lacks a convention code or an OpenDRIVE type, with the crop, the current label and click-to-answer
controls. Answers are stored in the artifact's database (collection ``review``, one document per
cell) and merged back with ``sign_catalog.py apply-review``.

    python tools/sign_review_page.py --map ue/assets/sign_atlas_cells.yaml --cells out/signs/cells \\
        --out out/signs/review.html [--all]
"""
import argparse
import base64
import io
import json
from pathlib import Path

import yaml
from PIL import Image

# StVO numbers as used by OpenDRIVE signal types (subtype carries the value for 274 etc.)
XODR = [
    ("", "— none / not applicable —"),
    ("101", "101 Danger (general)"), ("102", "102 Crossroads, priority to the right"), ("103", "103 Curve"),
    ("105", "105 Double curve"), ("108", "108 Steep descent"), ("110", "110 Steep ascent"),
    ("112", "112 Uneven road"), ("114", "114 Slippery road"), ("117", "117 Crosswind"),
    ("120", "120 Road narrows"), ("121", "121 Road narrows on one side"), ("123", "123 Road works"),
    ("124", "124 Traffic queues"), ("125", "125 Two-way traffic"), ("131", "131 Traffic signals ahead"),
    ("133", "133 Pedestrians"), ("136", "136 Children"), ("138", "138 Cyclists"), ("142", "142 Wild animals"),
    ("151", "151 Level crossing"), ("201", "201 St Andrew's cross"), ("205", "205 Give way"), ("206", "206 Stop"),
    ("208", "208 Priority to oncoming traffic"), ("209", "209 Prescribed turn"), ("211", "211 Prescribed turn here"),
    ("214", "214 Prescribed direction"), ("215", "215 Roundabout"), ("220", "220 One-way street"),
    ("222", "222 Pass on this side"), ("224", "224 Bus stop"), ("229", "229 Taxi rank"), ("237", "237 Cycle path"),
    ("238", "238 Bridle path"), ("239", "239 Footpath"), ("240", "240 Shared foot / cycle path"),
    ("241", "241 Separated foot / cycle path"), ("242", "242 Pedestrian zone"), ("244", "244 Bicycle street"),
    ("245", "245 Bus lane"), ("250", "250 No vehicles"), ("251", "251 No motor cars"), ("253", "253 No trucks"),
    ("254", "254 No bicycles"), ("255", "255 No motorcycles"), ("259", "259 No pedestrians"),
    ("260", "260 No motor vehicles"), ("261", "261 No hazardous goods"), ("262", "262 Weight limit"),
    ("263", "263 Axle-load limit"), ("264", "264 Width limit"), ("265", "265 Height limit"),
    ("266", "266 Length limit"), ("267", "267 No entry"), ("268", "268 Snow chains required"),
    ("269", "269 No water-polluting cargo"), ("270", "270 Environmental zone"), ("272", "272 No U-turn"),
    ("273", "273 Minimum distance"), ("274", "274 Maximum speed (subtype = value)"), ("274.1", "274.1 Speed zone"),
    ("275", "275 Minimum speed"), ("276", "276 No overtaking"), ("277", "277 No overtaking for trucks"),
    ("278", "278 End of speed limit"), ("279", "279 End of minimum speed"), ("280", "280 End of no-overtaking"),
    ("281", "281 End of no-overtaking for trucks"), ("282", "282 End of all restrictions"),
    ("283", "283 No stopping"), ("286", "286 No parking"), ("290", "290 No-parking zone"),
    ("301", "301 Priority at next intersection"), ("306", "306 Priority road"), ("307", "307 End of priority road"),
    ("308", "308 Priority over oncoming traffic"), ("310", "310 Town entry"), ("311", "311 Town exit"),
    ("314", "314 Parking"), ("315", "315 Parking on sidewalk"), ("325", "325 Traffic-calmed area"),
    ("330", "330 Motorway"), ("331", "331 Expressway"), ("350", "350 Pedestrian crossing"),
    ("354", "354 Water protection area"), ("356", "356 Tram"), ("357", "357 Dead end"),
    ("380", "380 Recommended speed"), ("385", "385 Direction sign"), ("386", "386 Tourist sign"),
    ("other", "other (type it in the note)"),
]

CODE_HINT = {
    "VC": "Vienna Convention code, e.g. A14b, B2a, C1, D1a",
    "MUTCD": "MUTCD code, e.g. R1-1, R2-1, W11-2, D1-1",
    "GB": "GB 5768 code, e.g. 警告 W1, 禁令 P1, 指示 I1 (or 'jing 1', 'jin 12')",
    "Miscellaneous": "any convention code",
}

CSS = """
:root{--bg:#f3f1ec;--ink:#1c1b18;--muted:#6f6a60;--line:#d8d2c4;--card:#fffefa;--acc:#1f5fa8;--ok:#2d7d46;--warn:#b8641c;--bad:#b3341e;--pill:#e9e4d8}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#17161a;--ink:#ece8df;--muted:#a19b8f;--line:#37343a;--card:#201f24;--acc:#7fb0f0;--ok:#6fc48a;--warn:#e6a25a;--bad:#f07a5a;--pill:#2a292e}}
:root[data-theme="dark"]{--bg:#17161a;--ink:#ece8df;--muted:#a19b8f;--line:#37343a;--card:#201f24;--acc:#7fb0f0;--ok:#6fc48a;--warn:#e6a25a;--bad:#f07a5a;--pill:#2a292e}
body{background:var(--bg);color:var(--ink);font:14px/1.45 "IBM Plex Sans",system-ui,sans-serif;margin:0;padding:0 0 6rem}
header{padding:1.6rem 2rem 1rem;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
h1{font:600 22px/1.2 "IBM Plex Serif",Georgia,serif;margin:0 0 .3rem}
h2{font:600 17px/1.3 "IBM Plex Serif",Georgia,serif;margin:2rem 0 .6rem;display:flex;gap:.8rem;align-items:baseline}
h2 small{color:var(--muted);font:400 12px "IBM Plex Sans",system-ui}
.sub{color:var(--muted);max-width:80ch;margin:0}
.bar{display:flex;gap:1.2rem;align-items:center;flex-wrap:wrap;margin-top:.6rem;font-size:13px}
.bar b{font-variant-numeric:tabular-nums}
.bar label{display:flex;gap:.35rem;align-items:center;cursor:pointer}
#status{padding:.15rem .6rem;border-radius:999px;background:var(--pill);color:var(--muted)}
#status.ok{color:var(--ok)}#status.bad{color:var(--bad)}
main{padding:0 2rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px;display:grid;grid-template-columns:128px 1fr;gap:10px}
.card.done{border-color:var(--ok)}
.card.hide{display:none}
.card img{width:128px;height:128px;border-radius:4px;background:#fff;border:1px solid var(--line)}
.card .cell{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11px;color:var(--muted);margin-top:4px}
.f{display:flex;flex-direction:column;gap:6px;min-width:0}
.name{font-weight:600;overflow-wrap:anywhere}
.desc{color:var(--muted);font-size:12.5px}
.seg{display:flex;gap:4px;flex-wrap:wrap}
.seg button{border:1px solid var(--line);background:var(--pill);color:var(--ink);border-radius:999px;padding:3px 10px;font:12.5px "IBM Plex Sans",system-ui;cursor:pointer}
.seg button.on{background:var(--acc);border-color:var(--acc);color:#fff}
.seg button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--acc);outline-offset:1px}
.row{display:grid;grid-template-columns:76px 1fr;gap:6px;align-items:center;font-size:12.5px}
.row span{color:var(--muted)}
input,select{width:100%;box-sizing:border-box;border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:4px;padding:4px 6px;font:12.5px "IBM Plex Sans",system-ui}
.two{display:grid;grid-template-columns:1fr 90px;gap:6px}
.st{font-size:11px;color:var(--muted);text-align:right}
.st.ok{color:var(--ok)}.st.bad{color:var(--bad)}
nav{display:flex;flex-wrap:wrap;gap:.3rem .7rem;padding:.6rem 2rem;border-bottom:1px solid var(--line);font-size:12px}
nav a{color:var(--acc);text-decoration:none}
@media (prefers-reduced-motion: no-preference){.card{transition:border-color .2s}}
"""

JS = r"""
const CELLS = __CELLS__;
const XODR = __XODR__;
const HINT = __HINT__;
let db = null;
const answers = {};
const $ = (s, r=document) => r.querySelector(s);
function opt(sel, cur){return XODR.map(([v,l]) => `<option value="${v}" ${v===cur?'selected':''}>${l}</option>`).join('')}
function card(c){
  const el = document.createElement('div'); el.className='card'; el.id='c_'+c.id;
  const xodrType = (c.xodr||'').split('-')[0], xodrSub = (c.xodr||'').split('-')[1]||'';
  const known = XODR.some(([v]) => v===xodrType);
  el.innerHTML = `
    <div><img src="${c.img}" alt=""><div class="cell">${c.atlas.replace('T_','').replace('SignAtlas','')} · ${c.x},${c.y} · ${c.shape.replace('SM_','')}</div></div>
    <div class="f">
      <div class="name">${esc(c.name)}</div>
      <div class="desc">${esc(c.description||'')}${c.osm?' · <span style="font-family:IBM Plex Mono,monospace">'+esc(c.osm)+'</span>':''}</div>
      <div class="seg" data-k="verdict">
        <button data-v="ok">Label is right</button><button data-v="wrong">Wrong sign</button><button data-v="notsign">Not a sign / skip</button>
      </div>
      <div class="row rename" hidden><span>It is</span><input data-k="name" placeholder="what the sign actually is"></div>
      <div class="row"><span>Code</span><input data-k="code" placeholder="${esc(HINT[c.style]||'')}" value="${esc(c.meaning||'')}"></div>
      <div class="row"><span>OpenDRIVE</span><div class="two"><select data-k="xodr">${opt(null, known?xodrType:'')}</select><input data-k="sub" placeholder="subtype" value="${esc(xodrSub)}"></div></div>
      <div class="row"><span>Note</span><input data-k="note" placeholder="optional"></div>
      <div class="st" data-st>unanswered</div>
    </div>`;
  el.querySelectorAll('.seg button').forEach(b => b.addEventListener('click', () => {
    el.querySelectorAll('.seg button').forEach(x => x.classList.toggle('on', x===b));
    $('.rename', el).hidden = b.dataset.v !== 'wrong';
    save(c, el);
  }));
  el.querySelectorAll('input,select').forEach(i => i.addEventListener('change', () => save(c, el)));
  return el;
}
function esc(s){return String(s).replace(/[&<>"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]))}
function collect(c, el){
  const v = $('.seg button.on', el);
  return {atlas:c.atlas, style:c.style, x:c.x, y:c.y, orig_name:c.name,
    verdict: v ? v.dataset.v : '', name: $('[data-k=name]', el).value.trim(), code: $('[data-k=code]', el).value.trim(),
    xodr: $('[data-k=xodr]', el).value, sub: $('[data-k=sub]', el).value.trim(), note: $('[data-k=note]', el).value.trim(),
    updatedAt: new Date().toISOString()};
}
const timers = {};
function save(c, el){
  const a = collect(c, el); answers[c.id] = a; paint(c, el, a, 'saving…', '');
  clearTimeout(timers[c.id]);
  timers[c.id] = setTimeout(async () => {
    if(!db){ paint(c, el, a, 'not saved: storage unavailable', 'bad'); try{localStorage.setItem('sign-review:'+c.id, JSON.stringify(a))}catch(e){} return; }
    try { await db.doc('review/'+c.id).set(a); paint(c, el, a, 'saved', 'ok'); }
    catch(e){ paint(c, el, a, 'not saved: '+(e && e.code || e), 'bad'); }
  }, 400);
}
function paint(c, el, a, msg, cls){
  const st = $('[data-st]', el); st.textContent = msg; st.className = 'st '+cls;
  el.classList.toggle('done', !!a.verdict);
  if(a.verdict && $('#hide').checked) el.classList.add('hide');
  progress();
}
function progress(){
  const n = Object.values(answers).filter(a => a.verdict).length;
  $('#done').textContent = n;
}
function apply(c, el, a){
  answers[c.id] = a;
  el.querySelectorAll('.seg button').forEach(x => x.classList.toggle('on', x.dataset.v===a.verdict));
  $('.rename', el).hidden = a.verdict !== 'wrong';
  for (const k of ['name','code','sub','note']) { const i = $(`[data-k=${k}]`, el); if (i && a[k] !== undefined) i.value = a[k]; }
  const s = $('[data-k=xodr]', el); if (a.xodr !== undefined) s.value = a.xodr;
  paint(c, el, a, a.verdict ? 'saved' : 'unanswered', a.verdict ? 'ok' : '');
}
(async () => {
  const groups = {};
  for (const c of CELLS) (groups[c.atlas] = groups[c.atlas] || []).push(c);
  const main = $('main'), nav = $('nav');
  for (const [atlas, cells] of Object.entries(groups)) {
    const id = atlas.replace(/[^A-Za-z0-9_]/g,'');
    nav.insertAdjacentHTML('beforeend', `<a href="#${id}">${atlas.replace('T_','').replace('SignAtlas','')}</a>`);
    main.insertAdjacentHTML('beforeend', `<h2 id="${id}">${atlas.replace('T_','').replace('SignAtlas',' ')}<small>${cells[0].style} · ${cells[0].category} · ${cells.length} cells</small></h2>`);
    const g = document.createElement('div'); g.className='grid'; main.appendChild(g);
    for (const c of cells) { const el = card(c); g.appendChild(el); c._el = el; }
  }
  $('#total').textContent = CELLS.length;
  $('#hide').addEventListener('change', () => CELLS.forEach(c => c._el.classList.toggle('hide', $('#hide').checked && !!(answers[c.id]||{}).verdict)));
  // restore any answers kept only in this browser
  for (const c of CELLS) { try { const s = localStorage.getItem('sign-review:'+c.id); if (s) apply(c, c._el, JSON.parse(s)); } catch(e){} }
  try { db = await claude.use('db'); } catch(e) { db = null; }
  const st = $('#status');
  if (!db) { st.textContent = 'storage unavailable — answers stay in this browser only'; st.className='bad'; return; }
  st.textContent = 'connected'; st.className = 'ok';
  try {
    const snap = await db.collection('review').get();
    snap.docs.forEach(d => { const c = CELLS.find(x => x.id === d.id); if (c && d.exists) apply(c, c._el, d.data()); });
  } catch(e) { st.textContent = 'could not load earlier answers: ' + (e && e.code || e); st.className='bad'; }
})();
"""


def data_uri(path: Path, size: int) -> str:
    im = Image.open(path).convert("RGB")
    im = im.resize((size, size))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=84)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True)
    ap.add_argument("--cells", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--all", action="store_true", help="include every non-blank cell, not only the gaps")
    args = ap.parse_args()
    doc = yaml.safe_load(open(args.map))
    cells = []
    for a in doc["atlases"]:
        atlas = a["texture"].split("/")[-1]
        for c in sorted(a["cells"], key=lambda c: (c["y"], c["x"])):
            if c.get("blank"):
                continue
            if not args.all and c.get("meaning") and c.get("xodr"):
                continue
            crop = Path(args.cells) / atlas / ("%d_%d.png" % (c["x"], c["y"]))
            cells.append({
                "id": "%s__%d_%d" % (atlas.replace("T_", "").replace("SignAtlas", ""), c["x"], c["y"]),
                "atlas": atlas, "style": a["style"], "category": c.get("category") or a["category"],
                "x": c["x"], "y": c["y"], "name": c["name"], "description": c.get("description", ""),
                "shape": c.get("shape", ""), "meaning": c.get("meaning", ""), "xodr": str(c.get("xodr", "") or ""),
                "osm": c.get("osm", ""), "img": data_uri(crop, 128) if crop.exists() else "",
            })
    page = [
        "<title>Sign Catalog Review</title>",
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600&family=IBM+Plex+Serif:wght@600&family=IBM+Plex+Mono&display=swap">',
        "<style>%s</style>" % CSS,
        "<header><h1>Sign Catalog Review</h1>"
        "<p class=sub>These atlas cells still lack a convention code or an OpenDRIVE type. For each one: say whether the "
        "label is right, type the code if you know it, and pick the OpenDRIVE type (subtype only where it carries a value, "
        "e.g. 274 → 30). Every change saves on its own.</p>"
        '<div class=bar><span><b id=done>0</b> / <b id=total>0</b> answered</span>'
        '<label><input type=checkbox id=hide> hide answered</label><span id=status>connecting…</span></div></header>',
        "<nav></nav><main></main>",
        "<script>%s</script>" % JS.replace("__CELLS__", json.dumps(cells)).replace("__XODR__", json.dumps(XODR)).replace("__HINT__", json.dumps(CODE_HINT)),
    ]
    Path(args.out).write_text("\n".join(page))
    print("wrote %s: %d cells, %.1f MB" % (args.out, len(cells), Path(args.out).stat().st_size / 1e6))


if __name__ == "__main__":
    main()
