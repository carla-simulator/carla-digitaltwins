"""Turn a cached Overpass JSON download (twinmodel's ``data/overpass_*.json``) into OSM XML the
StreetMap importer reads (UStreetMapFactory: nodes with tags, ways with nd refs; relations are
dropped).

    python tools/overpass_to_osm.py data/overpass_41.39050_2.16300_41.39450_2.16900_<digest>.json out/eixample.osm

Node tags are what the sign generator needs (highway=stop / give_way / crossing, traffic_sign=*,
maxspeed=*); way maxspeed tags become a mid-way speed sign in the importer.
"""
import json
import sys
from xml.sax.saxutils import quoteattr


def convert(src, dst):
    doc = json.load(open(src))
    elements = doc.get("elements", doc if isinstance(doc, list) else [])
    # Overpass lists an element once per result set that contains it (the tagged hit of a
    # node[...] filter and again, bare, from the `>` recursion): merge by id, tags united,
    # or the importer keeps whichever copy comes last.
    merged = {"node": {}, "way": {}}
    for e in elements:
        kind = e.get("type")
        if kind not in merged:
            continue
        cur = merged[kind].get(e["id"])
        if cur is None:
            merged[kind][e["id"]] = dict(e, tags=dict(e.get("tags") or {}))
        else:
            cur["tags"].update(e.get("tags") or {})
            for k in ("lat", "lon", "nodes"):
                if k in e and k not in cur:
                    cur[k] = e[k]
    nodes = list(merged["node"].values())
    ways = list(merged["way"].values())
    lats = [n["lat"] for n in nodes]
    lons = [n["lon"] for n in nodes]
    with open(dst, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6" generator="twinmodel overpass_to_osm">\n')
        if nodes:
            f.write(' <bounds minlat="%.7f" minlon="%.7f" maxlat="%.7f" maxlon="%.7f"/>\n' % (min(lats), min(lons), max(lats), max(lons)))
        for n in nodes:
            tags = n.get("tags") or {}
            head = ' <node id="%d" lat="%.7f" lon="%.7f" version="1"' % (n["id"], n["lat"], n["lon"])
            if not tags:
                f.write(head + "/>\n")
                continue
            f.write(head + ">\n")
            for k, v in tags.items():
                f.write("  <tag k=%s v=%s/>\n" % (quoteattr(k), quoteattr(str(v))))
            f.write(" </node>\n")
        for w in ways:
            f.write(' <way id="%d" version="1">\n' % w["id"])
            for ref in w.get("nodes", []):
                f.write('  <nd ref="%d"/>\n' % ref)
            for k, v in (w.get("tags") or {}).items():
                f.write("  <tag k=%s v=%s/>\n" % (quoteattr(k), quoteattr(str(v))))
            f.write(" </way>\n")
        f.write("</osm>\n")
    tagged = [n for n in nodes if n.get("tags")]
    sign_like = [n for n in tagged if any(k in n["tags"] for k in ("traffic_sign", "maxspeed"))
                 or n["tags"].get("highway") in ("stop", "give_way", "crossing", "traffic_signals")]
    return len(nodes), len(ways), len(tagged), len(sign_like)


if __name__ == "__main__":
    n, w, t, s = convert(sys.argv[1], sys.argv[2])
    print("%s: %d nodes (%d tagged, %d sign-like), %d ways" % (sys.argv[2], n, t, s, w))
