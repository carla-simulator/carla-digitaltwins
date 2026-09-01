"""Feed the twin's building footprints to CarlaTools' procedural building generator.

The generator is carla-digitaltwins' ``BP_BuildingGen`` (an EditorUtilityBlueprint that
reads ``FStreetMapBuilding`` footprints from a StreetMap-plugin asset and extrudes modular
facades: window/door/corner "atom" ISM instances picked from ``DT_BuildingsAtoms`` /
``PDA_ModuleWithGlass``). In ue58-dev it runs as *content*: a content-only plugin mounts
the digitaltwins assets at ``/CarlaDigitalTwinsTool/...`` and CoreRedirects map its native
classes onto stock CARLA (``UOpenDriveToMap``/``UBlueprintUtilFunctions``/
``UGenerationPathsHelper`` -> CarlaTools, ``UMapGenFunctionLibrary`` -> Carla, which
carries ported ``Add*StaticMeshComponentToActor``). ATagger labels the atom meshes
``static.building`` via its nearest-labelled-folder fallback (``.../Static/Building/...``).
(ue58-dev's own legacy copy of the BP is bit-rotted: compile-on-load pin errors.)

The generator's only input path is an ``.osm`` file, whose nodes the StreetMap importer
projects with a *spherical* transverse-mercator
(``UStreetMapFactory::GetTransversemercProjection``, R = 6 373 000 m, UE y = -north, cm).
The twin manifest's ``buildings`` rings are already in UE-frame cm, so this module writes a
buildings-only ``.osm`` whose node coordinates are the closed-form *inverse* of that exact
projection: after import, ``FStreetMapBuilding::BuildingPoints`` reproduce the manifest
rings to < 0.001 cm (roundtrip validated) -- no reliance on the raw OSM source, and the
generator inherits the same clip rules the baked slabs used.

Headless gotcha (verified): ``ImportStreetMap`` syncs the Content Browser after import,
which check-fails (SIGSEGV) in a commandlet -- and it is the ONLY setter of the factory's
static ``LatLonOrigin``. The working headless recipe is therefore: never call the blueprint
library; import with an automated ``AssetImportTask`` + ``StreetMapFactory`` (origin stays
at its (0, 0) default) and write this file's node coordinates inverse-projected around
``lat0 = lon0 = 0`` -- synthetic near-null-island lat/lons whose projection reproduces the
UE-frame cm exactly (roundtrip < 0.001 cm, trial 4).

Pure Python (no ``unreal``): usable from bake_level.py inside the editor and testable
outside it.
"""
import math

# UStreetMapFactory::GetTransversemercProjection constants (StreetMapFactory.cpp)
_R = 6373000.0  # spherical earth radius used by the plugin, metres


def inv_streetmap_tmerc(x_cm, y_cm, lat0, lon0):
    """Inverse of the StreetMap plugin's projection: UE-frame cm -> (lat, lon) degrees.

    Forward (plugin): x = R*asinh(sin dlon / sqrt(tan^2 lat + cos^2 dlon)) == standard
    spherical TM easting; y = R*atan(tan lat / cos dlon); result = (x, -(y - R*lat0)) * 100.
    """
    x = x_cm / 100.0
    y = _R * math.radians(lat0) - y_cm / 100.0
    dlon = math.atan2(math.sinh(x / _R), math.cos(y / _R))
    lat = math.asin(math.sin(y / _R) / math.cosh(x / _R))
    return math.degrees(lat), lon0 + math.degrees(dlon)


def buildings_osm_xml(manifest, use_raw=False):
    """Buildings-only OSM XML for the manifest's ``buildings`` array.

    One closed way per footprint ring, tagged ``building=<category|yes>``,
    ``height=<height_m>`` and (when known) ``building:levels``. By default the *clipped*
    rings are used -- the same footprint-minus-drivable-network the baked slabs use, so the
    generator cannot extrude a wall across a mapped road; ``use_raw=True`` switches to the
    raw OSM outlines.
    """
    # (0, 0), NOT the twin's real geographic origin: see the headless gotcha above.
    lat0 = lon0 = 0.0
    nodes, ways = [], []
    nid = 1
    for b in manifest.get("buildings", ()):
        rings = [b["raw_ring_ue"]] if use_raw and b.get("raw_ring_ue") else b["rings_ue"]
        for ring in rings:
            if not ring or len(ring) < 3:
                continue
            refs = []
            for x, y in ring:
                lat, lon = inv_streetmap_tmerc(x, y, lat0, lon0)
                nodes.append('<node id="%d" lat="%.9f" lon="%.9f" version="1"/>' % (nid, lat, lon))
                refs.append(nid)
                nid += 1
            refs.append(refs[0])  # close the way
            tags = ['<tag k="building" v="%s"/>' % (b.get("category") or "yes"),
                    '<tag k="height" v="%.1f"/>' % b["height_m"]]
            if b.get("levels"):
                tags.append('<tag k="building:levels" v="%d"/>' % b["levels"])
            ways.append('<way id="%d" version="1">%s%s</way>'
                        % (nid, "".join('<nd ref="%d"/>' % r for r in refs), "".join(tags)))
            nid += 1
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<osm version="0.6" generator="twinmodel-bake">\n%s\n%s\n</osm>\n'
            % ("\n".join(nodes), "\n".join(ways)))


def write_buildings_osm(manifest, path, use_raw=False):
    """Write ``buildings_osm_xml`` to ``path``; returns the number of ways written."""
    xml = buildings_osm_xml(manifest, use_raw=use_raw)
    with open(path, "w") as f:
        f.write(xml)
    return xml.count("<way ")
