"""Human corrections to the OSM input and to the derived twin, applied at build time.

OSM is hand-digitised: way centrelines are coarse, lane counts miss the bus and parking lanes,
junction nodes sit where somebody clicked.  Rather than editing the generated twin (which the
next build would overwrite) a reviewer records *corrections* in a small JSON file, keyed by the
stable OSM ids, and every build replays them::

    data/corrections/<name>.json          (input, next to the Overpass cache; keep it in git)
    twinmodel build ... --corrections data/corrections/eixample.json
    tools/twin_editor.py                  (the web editor that writes the file)

The file is a list of operations applied in order.  Three stages, three op families:

**Stage A - OSM patch** (``apply_osm``, on the raw Overpass JSON before ``parse_osm``).  Everything
downstream (lane graph, junction clustering, surfaces) regenerates from the corrected input, so
topology stays consistent by construction.  New elements carry negative ids, like JOSM.

======================  ==========================================================================
``node.move``           ``node``, ``lat``, ``lon``
``node.add``            ``node`` (< 0), ``lat``, ``lon``, ``tags``?
``node.delete``         ``node`` - also drops it from every way
``node.tags``           ``node``, ``set`` {k: v}, ``unset`` [k]
``way.tags``            ``way``, ``set`` {k: v}, ``unset`` [k]        (lanes, lanes:psv, parking:*, oneway...)
``way.nodes``           ``way``, ``nodes`` [id...]  - replaces the node list (vertices added / removed / reordered)
``way.add``             ``way`` (< 0), ``nodes`` [id...], ``tags`` {}
``way.delete``          ``way``
``way.split``           ``way``, ``node``, ``new_way`` (< 0) - the part after ``node`` becomes ``new_way`` (tags copied)
======================  ==========================================================================

**Stage B - lane graph** (inside / right after ``build_lanegraph``).

======================  ==========================================================================
``road.end``            ``way``, ``node``, ``shift_m`` - moves the end of the road built from OSM way
                        ``way`` at the junction containing OSM node ``node`` along its own line:
                        negative = pull the end (and with it the stop line, the signal, the
                        junction mouth) back from the junction, positive = push it in.  Applied
                        by ``lanegraph`` while trimming (``end_shifts``) so the connecting roads
                        are built from the moved end.
``junction.polygon``    ``nodes`` [osm node ids], ``polygon`` [[lon, lat], ...] - the junction's
                        pavement outline; ``surfaces`` keeps it verbatim (``polygon_source =
                        "correction"``) instead of deriving one.
======================  ==========================================================================

**Stage C - surfaces** (``drivable_patch``, after refinement, before the final ``build_surfaces``).

======================  ==========================================================================
``drivable.add``        ``polygon`` [[lon, lat], ...] - paves this area (sidewalk / verge -> road)
``curb.line``           ``base`` [[lon, lat], ...] (the kerb line as the twin had it), ``line`` (where the
                        reviewer dragged it), ``as``? - the strip between the two lines is un-paved where
                        it lies inside the drivable surface (the kerb moved into the road: sidewalk
                        grows) and paved where it lies outside (the kerb moved out).  This is the kerb
                        editor's op; the two polygon ops below are the manual alternative.
``drivable.cut``        ``polygon`` [[lon, lat], ...], ``as``? - un-paves it; the area becomes
                        ``as`` (default ``sidewalk``; ``median`` / ``verge`` / ``ground``).  This is
                        how a kerb line or a sidewalk contour is corrected: kerbs and islands are
                        re-derived from the patched drivable outline.
======================  ==========================================================================

Every op may carry ``id``, ``note``, ``author``, ``ts`` and ``disabled`` (skipped when true).
Ops that match nothing are reported (``report["unmatched"]``), never fatal: a re-fetched OSM
extract may have lost the element.
"""
from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import polygonize, unary_union

log = logging.getLogger("twinmodel.corrections")

SCHEMA = "0.1"
OSM_OPS = ("node.move", "node.add", "node.delete", "node.tags", "way.tags", "way.nodes", "way.add",
           "way.delete", "way.split")
LANEGRAPH_OPS = ("road.end", "junction.polygon")
SURFACE_OPS = ("drivable.add", "drivable.cut", "curb.line")
ALL_OPS = OSM_OPS + LANEGRAPH_OPS + SURFACE_OPS


@dataclass
class Corrections:
    name: str = ""
    ops: list[dict[str, Any]] = field(default_factory=list)
    path: Optional[Path] = None

    def active(self, family: Iterable[str] | None = None) -> list[dict[str, Any]]:
        fam = set(family) if family is not None else None
        return [o for o in self.ops if not o.get("disabled")
                and (fam is None or o.get("op") in fam)]

    def to_json(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "name": self.name, "ops": self.ops}

    def save(self, path: Path | str | None = None) -> Path:
        target = path or self.path
        if target is None:
            raise ValueError("Corrections.save: no path")
        p = Path(target)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_json(), indent=1, ensure_ascii=False) + "\n")
        self.path = p
        return p


def default_path(cache_dir: Path | str, name: str) -> Path:
    return Path(cache_dir) / "corrections" / f"{name}.json"


def load(path: Path | str) -> Corrections:
    p = Path(path)
    raw = json.loads(p.read_text())
    ops = raw.get("ops", [])
    problems = validate(ops)
    if problems:
        raise ValueError(f"{p}: " + "; ".join(problems))
    return Corrections(name=raw.get("name", ""), ops=ops, path=p)


def load_or_empty(path: Path | str | None, name: str = "") -> Corrections:
    if path and Path(path).exists():
        return load(path)
    return Corrections(name=name, path=Path(path) if path else None)


def validate(ops: list[dict[str, Any]]) -> list[str]:
    """Structural check; returns human-readable problems (empty = fine)."""
    out: list[str] = []
    need = {
        "node.move": ("node", "lat", "lon"), "node.add": ("node", "lat", "lon"), "node.delete": ("node",),
        "node.tags": ("node",), "way.tags": ("way",), "way.nodes": ("way", "nodes"),
        "way.add": ("way", "nodes"), "way.delete": ("way",), "way.split": ("way", "node", "new_way"),
        "road.end": ("way", "node", "shift_m"), "junction.polygon": ("nodes", "polygon"),
        "drivable.add": ("polygon",), "drivable.cut": ("polygon",), "curb.line": ("base", "line"),
    }
    for i, o in enumerate(ops):
        op = o.get("op")
        if op not in ALL_OPS:
            out.append(f"op {i}: unknown op {op!r}")
            continue
        for k in need[op]:
            if k not in o:
                out.append(f"op {i} ({op}): missing {k!r}")
        if "polygon" in o and (not isinstance(o["polygon"], list) or len(o["polygon"]) < 3):
            out.append(f"op {i} ({op}): polygon needs >= 3 [lon, lat] points")
        for k in ("base", "line"):
            if op == "curb.line" and k in o and (not isinstance(o[k], list) or len(o[k]) < 2):
                out.append(f"op {i} ({op}): {k} needs >= 2 [lon, lat] points")
        if op in ("node.add", "way.add") and int(o.get(op.split(".")[0], 0)) >= 0:
            out.append(f"op {i} ({op}): new elements need a negative id")
    return out


# --------------------------------------------------------------------------- stage A: OSM patch

def apply_osm(raw: dict[str, Any], ops: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Patch a raw Overpass JSON document (``{"elements": [...]}``). Returns (new_doc, report);
    the input is not modified."""
    doc = copy.deepcopy(raw)
    elements = doc.setdefault("elements", [])
    nodes: dict[int, dict[str, Any]] = {}
    ways: dict[int, dict[str, Any]] = {}
    for el in elements:
        if el.get("type") == "node":
            nodes[int(el["id"])] = el
        elif el.get("type") == "way":
            ways[int(el["id"])] = el
    report: dict[str, Any] = {"applied": [], "unmatched": [], "n_ops": 0}

    def miss(o: dict[str, Any], why: str) -> None:
        report["unmatched"].append({"id": o.get("id"), "op": o.get("op"), "why": why})
        log.warning("correction %s (%s) not applied: %s", o.get("id"), o.get("op"), why)

    def _tags(el: dict[str, Any], o: dict[str, Any]) -> None:
        tags = el.setdefault("tags", {})
        for k, v in (o.get("set") or {}).items():
            tags[str(k)] = str(v)
        for k in o.get("unset") or []:
            tags.pop(k, None)
        if not tags:
            el.pop("tags", None)

    for o in ops:
        if o.get("disabled"):
            continue
        op = o.get("op")
        if op not in OSM_OPS:
            continue
        report["n_ops"] += 1
        if op == "node.move":
            n = nodes.get(int(o["node"]))
            if n is None:
                miss(o, f"node {o['node']} not in extract")
                continue
            n["lat"], n["lon"] = float(o["lat"]), float(o["lon"])
        elif op == "node.add":
            nid = int(o["node"])
            if nid in nodes:
                nodes[nid]["lat"], nodes[nid]["lon"] = float(o["lat"]), float(o["lon"])
            else:
                el = {"type": "node", "id": nid, "lat": float(o["lat"]), "lon": float(o["lon"])}
                if o.get("tags"):
                    el["tags"] = {str(k): str(v) for k, v in o["tags"].items()}
                elements.append(el)
                nodes[nid] = el
        elif op == "node.delete":
            nid = int(o["node"])
            if nid not in nodes:
                miss(o, f"node {nid} not in extract")
                continue
            elements.remove(nodes.pop(nid))
            for w in ways.values():
                w["nodes"] = [x for x in w.get("nodes", []) if int(x) != nid]
        elif op == "node.tags":
            n = nodes.get(int(o["node"]))
            if n is None:
                miss(o, f"node {o['node']} not in extract")
                continue
            _tags(n, o)
        elif op == "way.tags":
            w = ways.get(int(o["way"]))
            if w is None:
                miss(o, f"way {o['way']} not in extract")
                continue
            _tags(w, o)
        elif op == "way.nodes":
            w = ways.get(int(o["way"]))
            if w is None:
                miss(o, f"way {o['way']} not in extract")
                continue
            missing = [int(x) for x in o["nodes"] if int(x) not in nodes]
            if missing:
                miss(o, f"nodes {missing} not in extract (add them first)")
                continue
            w["nodes"] = [int(x) for x in o["nodes"]]
        elif op == "way.add":
            wid = int(o["way"])
            missing = [int(x) for x in o["nodes"] if int(x) not in nodes]
            if missing:
                miss(o, f"nodes {missing} not in extract (add them first)")
                continue
            if wid in ways:
                ways[wid]["nodes"] = [int(x) for x in o["nodes"]]
                ways[wid]["tags"] = {str(k): str(v) for k, v in (o.get("tags") or {}).items()}
            else:
                el = {"type": "way", "id": wid, "nodes": [int(x) for x in o["nodes"]],
                      "tags": {str(k): str(v) for k, v in (o.get("tags") or {}).items()}}
                elements.append(el)
                ways[wid] = el
        elif op == "way.delete":
            wid = int(o["way"])
            if wid not in ways:
                miss(o, f"way {wid} not in extract")
                continue
            elements.remove(ways.pop(wid))
        elif op == "way.split":
            w = ways.get(int(o["way"]))
            nid = int(o["node"])
            if w is None:
                miss(o, f"way {o['way']} not in extract")
                continue
            nl = [int(x) for x in w.get("nodes", [])]
            if nid not in nl[1:-1]:
                miss(o, f"node {nid} is not an interior node of way {o['way']}")
                continue
            k = nl.index(nid)
            new_id = int(o["new_way"])
            el = {"type": "way", "id": new_id, "nodes": nl[k:], "tags": dict(w.get("tags") or {})}
            w["nodes"] = nl[:k + 1]
            if new_id in ways:
                elements.remove(ways[new_id])
            elements.append(el)
            ways[new_id] = el
        report["applied"].append({"id": o.get("id"), "op": op})
    return doc, report


# --------------------------------------------------------------------------- stage B: lane graph

def end_shifts(ops: list[dict[str, Any]]) -> dict[tuple[int, int], float]:
    """``{(osm way id, osm node id): shift_m}`` for ``lanegraph.build_lanegraph(end_shifts=...)``."""
    out: dict[tuple[int, int], float] = {}
    for o in ops:
        if o.get("disabled") or o.get("op") != "road.end":
            continue
        out[(int(o["way"]), int(o["node"]))] = float(o["shift_m"])
    return out


def lookup_end_shift(shifts: dict[tuple[int, int], float], way_ids: Iterable[int],
                     node_ids: Iterable[int]) -> Optional[float]:
    """The shift recorded for any (way, node) pair of a road end / junction cluster."""
    if not shifts:
        return None
    ws, ns = set(way_ids), set(node_ids)
    hits = [v for (w, n), v in shifts.items() if w in ws and n in ns]
    return hits[0] if hits else None


def _line_to_model(frame, pts: list[list[float]]) -> LineString:
    tf = frame._to_local()
    xs, ys = tf.transform([p[0] for p in pts], [p[1] for p in pts])
    return LineString(list(zip(xs, ys)))


def kerb_strips(op: dict[str, Any], frame, drivable) -> tuple[list[Polygon], list[Polygon]]:
    """A ``curb.line`` op -> (cut pieces, add pieces) in model space. The area between the kerb as
    the twin had it (``base``) and where the reviewer put it (``line``) is polygonised; a piece
    mostly inside ``drivable`` is a cut (the kerb moved into the road), the rest is an add."""
    base, line = _line_to_model(frame, op["base"]), _line_to_model(frame, op["line"])
    if base.length < 0.05 or line.length < 0.05:
        return [], []
    closers = [LineString([base.coords[0], line.coords[0]]), LineString([base.coords[-1], line.coords[-1]])]
    pieces = [g for g in polygonize(unary_union([base, line] + [c for c in closers if c.length > 1e-6]))
              if g.area > 0.02]
    cuts, adds = [], []
    for g in pieces:
        inside = g.intersection(drivable).area if drivable is not None and not drivable.is_empty else 0.0
        (cuts if inside > 0.5 * g.area else adds).append(g)
    return cuts, adds


def _poly_to_model(frame, ring: list[list[float]]) -> Polygon:
    tf = frame._to_local()
    xs, ys = tf.transform([p[0] for p in ring], [p[1] for p in ring])
    poly = Polygon(list(zip(xs, ys)))
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly


def apply_junctions(model, ops: list[dict[str, Any]], frame) -> dict[str, Any]:
    """``junction.polygon`` ops -> ``Junction.polygon`` (tagged ``polygon_source = "correction"``)."""
    report: dict[str, Any] = {"applied": [], "unmatched": []}
    for o in ops:
        if o.get("disabled") or o.get("op") != "junction.polygon":
            continue
        want = {int(n) for n in o["nodes"]}
        best, best_n = None, 0
        for j in model.junctions:
            n = len(want & set(j.osm_node_ids))
            if n > best_n:
                best, best_n = j, n
        if best is None:
            report["unmatched"].append({"id": o.get("id"), "op": "junction.polygon",
                                        "why": f"no junction contains nodes {sorted(want)}"})
            log.warning("correction %s: no junction contains nodes %s", o.get("id"), sorted(want))
            continue
        poly = _poly_to_model(frame, o["polygon"])
        if poly.is_empty or poly.area < 1.0:
            report["unmatched"].append({"id": o.get("id"), "op": "junction.polygon", "why": "degenerate polygon"})
            continue
        best.polygon = poly
        best.tags["polygon_source"] = "correction"
        best.tags["correction_id"] = o.get("id")
        report["applied"].append({"id": o.get("id"), "op": "junction.polygon", "junction": best.id})
    return report


# --------------------------------------------------------------------------- stage C: surfaces

def has_drivable_ops(ops: list[dict[str, Any]]) -> bool:
    return any(not o.get("disabled") and o.get("op") in SURFACE_OPS for o in ops)


def drivable_patch(ops: list[dict[str, Any]], frame, base):
    """Apply ``drivable.add`` / ``drivable.cut`` to ``base`` - a (Multi)Polygon or the
    ``{layer: polygon}`` dict ``refine.drivable_by_layer`` returns (the ground layer is patched).
    Returns an object of the same shape, or ``base`` unchanged when there is nothing to do."""
    adds = [_poly_to_model(frame, o["polygon"]) for o in ops
            if not o.get("disabled") and o.get("op") == "drivable.add"]
    cuts = [_poly_to_model(frame, o["polygon"]) for o in ops
            if not o.get("disabled") and o.get("op") == "drivable.cut"]
    kerbs = [o for o in ops if not o.get("disabled") and o.get("op") == "curb.line"]
    if not adds and not cuts and not kerbs:
        return base

    def patch(g):
        k_cuts, k_adds = [], []
        for o in kerbs:
            c, a = kerb_strips(o, frame, g)
            k_cuts += c
            k_adds += a
        if adds or k_adds:
            g = unary_union([g] + adds + k_adds)
        if cuts or k_cuts:
            g = g.difference(unary_union(cuts + k_cuts))
        g = g.buffer(0)
        if isinstance(g, (Polygon, MultiPolygon)):
            return g
        # GeometryCollection with slivers: keep the polygons
        return unary_union([p for p in getattr(g, "geoms", []) if isinstance(p, (Polygon, MultiPolygon))])

    if isinstance(base, dict):
        from .refine import ground_layer
        if not base:
            return base
        lay = ground_layer(base.keys())
        out = dict(base)
        out[lay] = patch(base[lay])
        return out
    return patch(base)


def raised_extras(ops: list[dict[str, Any]], frame, drivable=None) -> list[tuple[Polygon, str]]:
    """``drivable.cut`` areas and the cut strips of ``curb.line`` ops as raised surfaces for
    ``surfaces.build_surfaces(extra_raised=...)``: the op's ``as`` (default ``sidewalk``;
    ``median`` / ``verge`` / ``ground``) says what the un-paved area becomes; ``ground`` adds
    nothing (the ground fill takes it). ``drivable`` is the outline *before* the patch (used to
    tell a kerb's cut strips from its add strips)."""
    out: list[tuple[Polygon, str]] = []
    for o in ops:
        if o.get("disabled"):
            continue
        kind = str(o.get("as", "sidewalk"))
        if kind not in ("sidewalk", "median", "verge"):
            continue
        if o.get("op") == "drivable.cut":
            out.append((_poly_to_model(frame, o["polygon"]), kind))
        elif o.get("op") == "curb.line":
            cuts, _ = kerb_strips(o, frame, drivable)
            out.extend((g, kind) for g in cuts)
    return out


# --------------------------------------------------------------------------- helpers for tooling

def new_id(ops: list[dict[str, Any]], prefix: str = "c") -> str:
    used = {str(o.get("id")) for o in ops}
    n = len(ops) + 1
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"


def min_new_osm_id(raw_or_ops: Any) -> int:
    """Smallest (most negative) OSM id in use, so a tool can hand out the next one."""
    lo = 0
    if isinstance(raw_or_ops, dict):
        for el in raw_or_ops.get("elements", []):
            lo = min(lo, int(el.get("id", 0)))
    else:
        for o in raw_or_ops:
            for k in ("node", "way", "new_way"):
                if k in o:
                    lo = min(lo, int(o[k]))
            for n in o.get("nodes", []) or []:
                if isinstance(n, (int, float)):
                    lo = min(lo, int(n))
    return lo


def summary(reports: dict[str, dict[str, Any]]) -> str:
    parts = []
    for stage, r in reports.items():
        if not r:
            continue
        a, u = len(r.get("applied", [])), len(r.get("unmatched", []))
        parts.append(f"{stage}: {a} applied" + (f", {u} unmatched" if u else ""))
    return "; ".join(parts) if parts else "no corrections"


__all__ = ["Corrections", "SCHEMA", "ALL_OPS", "OSM_OPS", "LANEGRAPH_OPS", "SURFACE_OPS", "load",
           "load_or_empty", "default_path", "validate", "apply_osm", "end_shifts", "lookup_end_shift",
           "apply_junctions", "drivable_patch", "raised_extras", "kerb_strips", "has_drivable_ops", "new_id", "min_new_osm_id", "summary"]
