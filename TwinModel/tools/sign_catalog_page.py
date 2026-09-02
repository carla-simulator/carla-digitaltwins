"""Build the sign-catalog review page: every atlas contact sheet with its curated labels, and
(when present) the rendered preview rows of the generated assets.

    python tools/sign_catalog_page.py --map ue/assets/sign_atlas_cells.yaml --sheets out/signs/sheets \\
        --cells out/signs/cells [--preview out/signs/preview] --out out/signs/catalog.html

Images are inlined as data URIs (cell crops at 96 px, preview rows at their size) so the page is
one self-contained file suitable for an artifact.
"""
import argparse
import base64
import html
import io
import json
import os
from pathlib import Path

import yaml
from PIL import Image


def data_uri(path: Path, max_w: int | None = None, quality: int = 80) -> str:
    im = Image.open(path).convert("RGB")
    if max_w and im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


CSS = """
:root{--bg:#f6f4ef;--ink:#1d1b17;--muted:#6b665c;--line:#d9d3c6;--card:#fffdf8;--acc:#b3341e;--blank:#e9e4d9}
:root:not([data-theme="light"]){}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#17161a;--ink:#ece8df;--muted:#a19b8f;--line:#37343a;--card:#201f24;--acc:#f07a5a;--blank:#2a292e}}
:root[data-theme="dark"]{--bg:#17161a;--ink:#ece8df;--muted:#a19b8f;--line:#37343a;--card:#201f24;--acc:#f07a5a;--blank:#2a292e}
body{background:var(--bg);color:var(--ink);font:14px/1.45 "IBM Plex Sans",system-ui,sans-serif;margin:0;padding:0 0 4rem}
header{padding:2rem 2.5rem 1rem;border-bottom:1px solid var(--line)}
h1{font:600 26px/1.2 "IBM Plex Serif",Georgia,serif;margin:0 0 .4rem;text-wrap:balance}
h2{font:600 18px/1.3 "IBM Plex Serif",Georgia,serif;margin:2.2rem 0 .6rem}
h3{font:600 14px/1.3 "IBM Plex Sans",system-ui;margin:1.4rem 0 .4rem;letter-spacing:.02em}
.sub{color:var(--muted);max-width:70ch}
nav{display:flex;flex-wrap:wrap;gap:.4rem .8rem;padding:.8rem 2.5rem;border-bottom:1px solid var(--line);font-size:13px}
nav a{color:var(--acc);text-decoration:none}
main{padding:0 2.5rem}
.stats{display:flex;gap:1.6rem;flex-wrap:wrap;margin:.6rem 0 0;font-variant-numeric:tabular-nums}
.stats b{display:block;font-size:22px;font-weight:600}
.stats span{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;max-width:960px}
.cell{background:var(--card);border:1px solid var(--line);border-radius:4px;padding:6px;display:flex;gap:8px;align-items:flex-start;min-height:104px}
.cell.blank{background:var(--blank);color:var(--muted);align-items:center;justify-content:center}
.cell img{width:96px;height:96px;flex:none;border-radius:3px;background:#fff}
.cell .t{font-size:12px;overflow-wrap:anywhere}
.cell .n{font-weight:600}
.cell .m{color:var(--muted)}
.cell .k{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11px;color:var(--acc)}
.row{margin:.5rem 0 1.2rem}
.row img{max-width:100%;border:1px solid var(--line);border-radius:4px;display:block}
.row .names{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11px;color:var(--muted);margin-top:4px;overflow-x:auto;white-space:nowrap}
.uniq{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:6px;max-width:960px}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True)
    ap.add_argument("--cells", required=True)
    ap.add_argument("--sheets", required=True)
    ap.add_argument("--preview", default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    with open(args.map) as f:
        doc = yaml.safe_load(f)
    cells_root = Path(args.cells)
    out = []
    n_signs = sum(1 for a in doc["atlases"] for c in a["cells"] if not c.get("blank"))
    n_xodr = sum(1 for a in doc["atlases"] for c in a["cells"] if c.get("xodr"))
    n_osm = sum(1 for a in doc["atlases"] for c in a["cells"] if c.get("osm"))
    styles = sorted({a["style"] for a in doc["atlases"]})
    out.append("<title>Traffic Sign Catalog</title><style>%s</style>" % CSS)
    out.append('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600&family=IBM+Plex+Serif:wght@600&family=IBM+Plex+Mono&display=swap">')
    out.append("<header><h1>Traffic Sign Catalog</h1><p class=sub>Every cell of CARLA's sign atlases, labelled and turned into a SignDataAsset + material. "
               "Cells are 1-based (column, row) from the top-left; the code under each name is its convention code, the red tokens the OpenDRIVE type and OSM tag it answers to.</p>"
               '<div class=stats><div><b>%d</b><span>signs</span></div><div><b>%d</b><span>atlases</span></div><div><b>%d</b><span>uniques</span></div>'
               '<div><b>%d</b><span>with OpenDRIVE code</span></div><div><b>%d</b><span>with OSM tag</span></div></div></header>'
               % (n_signs, len(doc["atlases"]), len(doc.get("uniques", [])), n_xodr, n_osm))
    out.append("<nav>" + " ".join('<a href="#%s">%s</a>' % (a["texture"].split("/")[-1], a["texture"].split("/")[-1].replace("SignAtlas", "")) for a in doc["atlases"]) + "</nav><main>")

    preview_index = None
    if args.preview:
        if os.path.exists(os.path.join(args.preview, "report.json")):
            with open(os.path.join(args.preview, "report.json")) as f:
                preview_index = json.load(f).get("museum", [])
        elif os.path.exists(os.path.join(args.preview, "index.json")):
            with open(os.path.join(args.preview, "index.json")) as f:
                preview_index = json.load(f)
    if preview_index:
        out.append("<h2 id=preview>In-game render</h2><p class=sub>Every generated DataAsset on a pole, laid out in rows on the EixampleDemo apron and photographed with a CARLA camera. Names left to right; a row can span two categories.</p>")
        for row in preview_index:
            p = Path(args.preview) / row["image"]
            if not p.exists():
                continue
            out.append('<div class=row><h3>%s / %s</h3><img src="%s" alt="%s"><div class=names>%s</div></div>' % (
                row["style"], row["category"], data_uri(p, 1600, 78), html.escape(row["image"]), html.escape("  ·  ".join(row["names"]))))
        shots = sorted(Path(args.preview).glob("street*_driver.png"))
        if shots:
            out.append("<h2>On the street</h2><p class=sub>Speed-limit signs placed at the OpenDRIVE signals of EixampleDemo (Vienna Convention style), seen from the lane 15 m before the sign.</p><div class=uniq>")
            for p in shots:
                out.append('<div class=row><img src="%s" alt="%s"><div class=names>%s</div></div>' % (data_uri(p, 800, 78), html.escape(p.name), html.escape(p.stem)))
            out.append("</div>")

    for style in styles:
        out.append("<h2>%s</h2>" % {"VC": "Vienna Convention (Europe)", "MUTCD": "MUTCD (United States)", "GB": "GB 5768 (China)", "Miscellaneous": "Miscellaneous"}.get(style, style))
        for a in doc["atlases"]:
            if a["style"] != style:
                continue
            name = a["texture"].split("/")[-1]
            out.append('<h3 id="%s">%s <span class=m style="color:var(--muted);font-weight:400">· %s</span></h3><div class=grid>' % (name, name, a["category"]))
            for c in sorted(a["cells"], key=lambda c: (c["y"], c["x"])):
                if c.get("blank"):
                    out.append('<div class="cell blank">%d,%d</div>' % (c["x"], c["y"]))
                    continue
                crop = cells_root / name / ("%d_%d.png" % (c["x"], c["y"]))
                img = '<img src="%s" alt="">' % data_uri(crop, 96, 82) if crop.exists() else ""
                keys = " ".join(filter(None, [("xodr " + c["xodr"]) if c.get("xodr") else "", c.get("osm", "")]))
                cat = (" · " + c["category"]) if c.get("category") and c["category"] != a["category"] else ""
                out.append('<div class=cell>%s<div class=t><div class=n>%s</div><div class=m>%d,%d · %s%s%s</div><div>%s</div><div class=k>%s</div></div></div>' % (
                    img, html.escape(c["name"]), c["x"], c["y"], html.escape(c.get("shape", "").replace("SM_", "")),
                    (" · " + html.escape(c["meaning"])) if c.get("meaning") else "", html.escape(cat),
                    html.escape(c.get("description", "")), html.escape(keys)))
            out.append("</div>")
    if doc.get("uniques"):
        out.append("<h2>One-off textures</h2><div class=uniq>")
        for u in doc["uniques"]:
            out.append('<div class=cell><div class=t><div class=n>%s</div><div class=m>%s · %s</div><div>%s</div></div></div>' % (
                html.escape(u["name"]), html.escape(u["texture"].split("/")[-1]), html.escape(u["shape"].replace("SM_", "")), html.escape(u.get("description", ""))))
        out.append("</div>")
    out.append("</main>")
    Path(args.out).write_text("\n".join(out))
    print("wrote %s (%.1f MB)" % (args.out, os.path.getsize(args.out) / 1e6))


if __name__ == "__main__":
    main()
