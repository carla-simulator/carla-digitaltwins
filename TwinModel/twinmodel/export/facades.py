"""Facade styles, wall/roof UVs and synthetic facade textures for 3DBAG buildings.

3DBAG (LoD 2.2) gives every Dutch building its real footprint, eaves and roof shape, but no
appearance. This module turns the BAG attributes into a look:

* ``classify`` picks a facade style from the construction year (BAG ``oorspronkelijkbouwjaar``),
  the dominant use of the building's units (BAG ``gebruiksdoel``, weighted by floor area),
  its height and footprint: pre-war brick, post-war brick, modern brick or render for homes,
  concrete or glass for offices, metal cladding for industry, a shopfront ground floor where
  the building has shops or cafes, a plain wall for party walls and sheds.
* ``wall_uv`` maps a wall polygon so the texture's window grid lines up with the building:
  a whole number of bays across each wall and a whole number of storeys up to the eaves.
* ``synth_facade`` draws the tiling textures (base colour, normal, AO/roughness/metallic) of a
  style with numpy, so the pipeline needs no texture download: 2 bays x 2 storeys a tile.
* ``roof_uv`` and ``ortho_tile`` texture the roofs with the aerial photo (PDOK Luchtfoto,
  CC BY 4.0, the twin's ortho): one texture per World Partition tile, planar from the top.

Pure numpy (PIL only to write PNGs); unit tested in tests/test_facades.py.
"""
from __future__ import annotations

import math
import zlib
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

TEX_BAYS = 2      # one facade texture covers 2 bays ...
TEX_STOREYS = 2   # ... by 2 storeys
TEX_PX = 1024
SHOP_FLOOR_M = 4.0   # shopfront ground floor height
PARTY_TOL_M = 0.3    # a wall whose foot lies this close to another footprint is a party wall


@dataclass(frozen=True)
class Style:
    name: str
    bay_m: float                # module width
    storey_m: float             # nominal storey height (the UVs fit a whole number to the eaves)
    wall: str                   # brick | render | concrete | metal | glass
    colours: tuple              # wall colour per variant (sRGB 0-255)
    window: tuple = (0.25, 0.75, 0.30, 0.85)   # x0, x1 (of the bay), y0, y1 (of the storey, from the floor)
    frame: tuple = (235, 235, 230)
    frame_m: float = 0.06
    mullions: int = 0           # vertical glazing bars per window
    transom: bool = False       # horizontal bar at 2/3 height
    sill: bool = True
    lintel: bool = False        # darker soldier course above the window (brick)
    glass: tuple = (52, 62, 72)
    mortar: tuple = (190, 185, 175)
    brick_m: tuple = (0.22, 0.0625)   # brick length incl. joint, course height
    shop: bool = False          # shopfront: sign band, door
    blank: bool = False         # no windows at all


STYLES = {s.name: s for s in (
    # pre-war Dutch brick: tall windows, white frames, transom, darker lintels
    Style("brick_old", 2.4, 3.2, "brick", ((122, 52, 38), (98, 46, 36)),
          window=(0.28, 0.72, 0.22, 0.86), frame=(236, 234, 226), mullions=1, transom=True, lintel=True),
    # post-war reconstruction (most of the Eindhoven centre): wide windows, darker frames
    Style("brick_postwar", 3.0, 2.9, "brick", ((150, 98, 66), (128, 82, 58)),
          window=(0.12, 0.88, 0.30, 0.82), frame=(70, 60, 52), mullions=2),
    Style("brick_modern", 2.8, 2.9, "brick", ((140, 64, 44), (88, 70, 62)),
          window=(0.18, 0.82, 0.18, 0.86), frame=(60, 62, 64), mullions=1),
    Style("render_modern", 3.0, 3.0, "render", ((228, 224, 214), (205, 200, 190)),
          window=(0.15, 0.85, 0.15, 0.88), frame=(55, 57, 60), mullions=1, sill=False),
    # offices: ribbon windows between concrete spandrels / a curtain wall
    Style("office_concrete", 3.6, 3.4, "concrete", ((172, 168, 160),),
          window=(0.04, 0.96, 0.32, 0.86), frame=(80, 82, 85), mullions=2, sill=False),
    Style("office_glass", 1.8, 3.6, "glass", ((70, 82, 92),),
          window=(0.03, 0.97, 0.08, 0.97), frame=(150, 155, 160), frame_m=0.05, sill=False,
          glass=(58, 76, 90)),
    # industry and warehouses: profiled metal sheet, one strip of high windows
    Style("industrial", 6.0, 4.5, "metal", ((168, 174, 178), (120, 128, 132)),
          window=(0.10, 0.90, 0.70, 0.86), frame=(90, 92, 95), mullions=3, sill=False),
    # ground floor of a building with shops or cafes
    Style("shopfront", 4.0, SHOP_FLOOR_M, "render", ((60, 60, 62), (110, 95, 80)),
          window=(0.06, 0.94, 0.03, 0.74), frame=(40, 40, 42), frame_m=0.08, mullions=1, sill=False,
          glass=(48, 58, 66), shop=True),
    # party walls, narrow walls, sheds: plain wall at true brick scale
    Style("blank_brick", 2.5, 3.0, "brick", ((132, 60, 44), (110, 80, 62)), blank=True, sill=False),
)}

# style -> the plain wall its party walls get
BLANK_OF = {"render_modern": "blank_brick", "office_concrete": "blank_brick", "office_glass": "blank_brick",
            "industrial": "industrial", "shopfront": "blank_brick"}


def _crc(*parts) -> int:
    return zlib.crc32("|".join(str(p) for p in parts).encode("utf-8"))


def material_key(style: str, variant: int) -> str:
    return "facade_%s_%d" % (style, variant)


def variant_of(style: str, building_id: str) -> int:
    return _crc(style, building_id) % len(STYLES[style].colours)


# --------------------------------------------------------------------------- classification

OFFICE_USES = {"kantoorfunctie", "onderwijsfunctie", "gezondheidszorgfunctie", "logiesfunctie", "celfunctie"}
SHOP_USES = {"winkelfunctie", "bijeenkomstfunctie"}
INDUSTRY_USES = {"industriefunctie"}
OSM_INDUSTRY = {"industrial", "warehouse", "hangar", "factory", "manufacture"}
OSM_SHED = {"shed", "garage", "garages", "carport", "hut", "roof"}
OSM_OFFICE = {"office", "commercial", "public", "civic", "school", "university", "college", "hospital", "hotel"}


@dataclass
class BuildingInfo:
    """What classify needs to know about a building."""
    id: str
    year: Optional[int] = None
    uses: dict = field(default_factory=dict)   # gebruiksdoel -> floor area m2
    height_m: float = 0.0                      # ground to eaves
    area_m2: float = 0.0                       # footprint
    osm_category: Optional[str] = None


def dominant_use(uses: dict) -> Optional[str]:
    if not uses:
        return None
    return max(sorted(uses), key=lambda k: uses[k])


def classify(b: BuildingInfo) -> tuple[str, bool]:
    """(facade style, shopfront ground floor) for a building."""
    use = dominant_use(b.uses)
    shops = any(u in SHOP_USES for u in b.uses)
    year = b.year or 0
    cat = (b.osm_category or "").lower()
    small = b.area_m2 < 40.0 and b.height_m < 4.0
    if small or (not b.uses and cat in OSM_SHED):
        return "blank_brick", False
    if use in INDUSTRY_USES or (not b.uses and cat in OSM_INDUSTRY) or \
            (use is None and b.area_m2 > 1500.0 and b.height_m < 10.0):
        return "industrial", False
    shop_floor = shops and b.height_m >= SHOP_FLOOR_M + 2.5
    if use in OFFICE_USES or (use is None and cat in OSM_OFFICE) or \
            (use in SHOP_USES and b.height_m >= 12.0):
        if year >= 1985 or (year == 0 and b.height_m >= 25.0):
            return "office_glass", shop_floor
        return "office_concrete", shop_floor
    if use in SHOP_USES and b.height_m < SHOP_FLOOR_M + 2.5:
        return "shopfront", False   # a one-storey shop: shopfront all the way up
    # homes and mixed blocks
    if 0 < year < 1940:
        return "brick_old", shop_floor
    if year < 1980:
        return "brick_postwar", shop_floor
    if year >= 1995 and _crc("render", b.id) % 3 == 0:
        return "render_modern", shop_floor
    return "brick_modern", shop_floor


# --buildings procedural (bake_level.py): carla-digitaltwins' modular atoms need door + window + top
# rows (Skysc21: 9 m and more), and they are apartment / office kits, not sheds or warehouses
PROCEDURAL_MIN_HEIGHT_M = 9.5
PROCEDURAL_STYLES = {"brick_old", "brick_postwar", "brick_modern", "render_modern", "office_concrete", "office_glass"}


def procedural_ok(style: str, height_m: float, min_height_m: float = PROCEDURAL_MIN_HEIGHT_M) -> bool:
    return style in PROCEDURAL_STYLES and height_m >= min_height_m


def procedural_family(style: str) -> str:
    """bake_level.FAMILIES member for a style: the glass office kit for offices, else the apartment kit."""
    return "Skysc19" if style in ("office_glass", "office_concrete") else "Skysc21"


def storeys_for(height_m: float, storey_m: float) -> tuple[int, float]:
    """A whole number of storeys up to the eaves: (n, actual storey height)."""
    if height_m <= 0:
        return 1, storey_m
    n = max(1, int(round(height_m / storey_m)))
    h = height_m / n
    if h < 0.75 * storey_m or h > 1.35 * storey_m:   # an odd wall: keep the nominal storey
        return n, storey_m
    return n, h


# --------------------------------------------------------------------------- UVs

def wall_frame(normal: np.ndarray) -> np.ndarray:
    """Unit horizontal axis along a vertical wall (left to right seen from outside)."""
    nx, ny = float(normal[0]), float(normal[1])
    h = math.hypot(nx, ny)
    if h < 1e-9:
        return np.array([1.0, 0.0, 0.0])
    return np.array([-ny / h, nx / h, 0.0])


def wall_uv(verts: np.ndarray, normal: np.ndarray, base_z: float, storey_h: float, bay_m: float,
            ring: Optional[np.ndarray] = None, z0: float = 0.0) -> np.ndarray:
    """glTF UVs (v grows downwards) for points of a wall polygon.

    u: a whole number of bays across the wall's horizontal extent (taken from ``ring``, the
    polygon's own vertices, so every piece of a clipped wall shares the mapping);
    v: storeys from ``base_z + z0`` (the floor of the first storey this texture covers).
    One texture tile is TEX_BAYS x TEX_STOREYS modules."""
    along = wall_frame(normal)
    ref = verts if ring is None else ring
    a_ref = ref @ along
    a0, a1 = float(a_ref.min()), float(a_ref.max())
    width = max(a1 - a0, 1e-6)
    bays = max(1, int(round(width / bay_m)))
    bay = width / bays
    u = (verts @ along - a0) / bay / TEX_BAYS
    v = -(verts[:, 2] - (base_z + z0)) / storey_h / TEX_STOREYS
    return np.column_stack([u, v])


def blank_uv(verts: np.ndarray, normal: np.ndarray, style: Style) -> np.ndarray:
    """Plain walls: metric, so the bricks keep their size whatever the wall."""
    along = wall_frame(normal)
    return np.column_stack([verts @ along / (TEX_BAYS * style.bay_m),
                            -verts[:, 2] / (TEX_STOREYS * style.storey_m)])


def roof_uv(verts: np.ndarray, tile: tuple[int, int], tile_m: float) -> np.ndarray:
    """Planar top-down UVs into the tile's ortho texture (image row 0 = north edge)."""
    x0, y1 = tile[0] * tile_m, (tile[1] + 1) * tile_m
    return np.column_stack([(verts[:, 0] - x0) / tile_m, (y1 - verts[:, 1]) / tile_m])


# --------------------------------------------------------------------------- clipping

def clip_triangles(verts: np.ndarray, tris: np.ndarray, z: float):
    """Split a triangle mesh at the plane Z = z. Returns ((verts, tris) below, (verts, tris) above);
    new vertices are interpolated on the cut, winding is preserved."""
    out = {True: ([], []), False: ([], [])}
    for t in tris:
        p = verts[t]
        side = p[:, 2] < z
        if side.all() or (~side).all():
            vs, fs = out[bool(side[0])]
            base = sum(len(a) for a in vs)
            vs.append(p)
            fs.append(np.array([[0, 1, 2]]) + base)
            continue
        poly = {True: [], False: []}
        for i in range(3):
            a, b = p[i], p[(i + 1) % 3]
            sa, sb = bool(a[2] < z), bool(b[2] < z)
            poly[sa].append(a)
            if sa != sb:
                f = (z - a[2]) / (b[2] - a[2])
                c = a + f * (b - a)
                poly[True].append(c)
                poly[False].append(c)
        for s in (True, False):
            pts = poly[s]
            if len(pts) < 3:
                continue
            vs, fs = out[s]
            base = sum(len(a) for a in vs)
            vs.append(np.asarray(pts))
            fs.append(np.array([[0, i, i + 1] for i in range(1, len(pts) - 1)]) + base)

    def pack(s):
        vs, fs = out[s]
        if not vs:
            return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
        return np.vstack(vs), np.vstack(fs).astype(np.int64)
    return pack(True), pack(False)


# --------------------------------------------------------------------------- textures

def _periodic_noise(shape, scale_px: float, rng) -> np.ndarray:
    """Tileable smooth noise in [-1, 1] (white noise low-passed in the Fourier domain)."""
    h, w = shape
    n = rng.standard_normal((h, w))
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.fftfreq(w)[None, :]
    f = np.sqrt(fx ** 2 + fy ** 2)
    filt = np.exp(-(f * scale_px) ** 2 * 4.0)
    out = np.real(np.fft.ifft2(np.fft.fft2(n) * filt))
    out -= out.mean()
    m = np.abs(out).max()
    return out / m if m > 0 else out


def _brick_layer(h, w, mw, mh, style: Style, colour, rng):
    """Running-bond brick wall over a (mw x mh metre) tile: colour image and height map."""
    bl, ch = style.brick_m
    nb = max(4, int(round(mw / bl)))
    nc = max(4, int(round(mh / ch)))
    nc += nc % 2   # even number of courses so the half-brick offset tiles
    ys, xs = np.mgrid[0:h, 0:w]
    cy = ys * nc / h
    course = np.floor(cy).astype(int)
    cx = xs * nb / w + 0.5 * (course % 2)
    brick = np.floor(cx).astype(int) % nb
    fy, fx = cy - course, cx - np.floor(cx)
    jy = 0.010 / ch      # 1 cm joints
    jx = 0.010 / bl
    mortar = (fy < jy) | (fx < jx)
    tone = rng.normal(0.0, 0.09, size=(nc, nb))
    shade = 1.0 + tone[course % nc, brick]
    col = np.asarray(colour, float)[None, None, :] * shade[..., None]
    col[mortar] = style.mortar
    height = np.where(mortar, -0.35, 0.0)
    return col, height


def synth_facade(style: Style, variant: int = 0, px: int = TEX_PX, seed: int = 0):
    """(base colour uint8 HxWx3, normal uint8 HxWx3, ORM uint8 HxWx4) for one tile of a style:
    TEX_BAYS x TEX_STOREYS modules, seamless in both directions, row 0 = top."""
    rng = np.random.default_rng(_crc(style.name, variant, seed))
    h = w = px
    mw, mh = TEX_BAYS * style.bay_m, TEX_STOREYS * style.storey_m
    colour = style.colours[variant % len(style.colours)]
    if style.wall == "brick":
        col, height = _brick_layer(h, w, mw, mh, style, colour, rng)
    else:
        col = np.ones((h, w, 3)) * np.asarray(colour, float)
        height = np.zeros((h, w))
        if style.wall == "concrete":   # panel joints at every bay and storey
            ys, xs = np.mgrid[0:h, 0:w]
            jx = (xs * TEX_BAYS / w) % 1.0
            jy = (ys * TEX_STOREYS / h) % 1.0
            seam = (jx < 0.012 / style.bay_m * 2) | (jy < 0.012 / style.storey_m * 2)
            col[seam] *= 0.7
            height[seam] = -0.4
        elif style.wall == "metal":    # trapezoidal sheet, ribs every 20 cm
            xs = np.arange(w)[None, :] * mw / w
            rib = (xs % 0.2) / 0.2
            prof = np.clip(np.abs(rib - 0.5) * 4 - 0.6, 0, 1)
            height = np.repeat(prof, h, axis=0) * 0.8
            col = col * (0.92 + 0.08 * np.repeat(prof, h, axis=0))[..., None]
    # dirt / tone: tileable low-frequency variation
    col *= (1.0 + 0.06 * _periodic_noise((h, w), 48, rng))[..., None]
    col *= (1.0 + 0.03 * _periodic_noise((h, w), 6, rng))[..., None]
    rough = np.full((h, w), 0.88 if style.wall in ("brick", "render", "concrete") else 0.45)
    metal = np.full((h, w), 0.5 if style.wall == "metal" else 0.0)
    ao = np.ones((h, w))
    if not style.blank:
        bw, sh = w / TEX_BAYS, h / TEX_STOREYS
        fpx = max(2, int(round(style.frame_m / style.bay_m * bw)))
        x0, x1, y0, y1 = style.window
        for sy in range(TEX_STOREYS):
            for bx in range(TEX_BAYS):
                # storey floor at the bottom of its band (row grows downwards)
                floor = (sy + 1) * sh
                wx0, wx1 = int(bx * bw + x0 * bw), int(bx * bw + x1 * bw)
                wy0, wy1 = int(floor - y1 * sh), int(floor - y0 * sh)
                door = style.shop and bx == (sy + variant) % TEX_BAYS
                if door:
                    wy1 = int(floor - 0.01 * sh)
                # frame (outer rectangle) then glass inset
                col[wy0:wy1, wx0:wx1] = style.frame
                height[wy0:wy1, wx0:wx1] = -0.3
                rough[wy0:wy1, wx0:wx1] = 0.5
                metal[wy0:wy1, wx0:wx1] = 0.0
                gy0, gy1, gx0, gx1 = wy0 + fpx, wy1 - fpx, wx0 + fpx, wx1 - fpx
                if gy1 <= gy0 or gx1 <= gx0:
                    continue
                gh = gy1 - gy0
                grad = np.linspace(1.25, 0.8, gh)[:, None, None]   # sky reflection: brighter at the top
                g = np.asarray(style.glass, float)[None, None, :] * grad
                tint = 1.0 + 0.12 * rng.standard_normal()
                col[gy0:gy1, gx0:gx1] = g * tint
                height[gy0:gy1, gx0:gx1] = -1.0
                rough[gy0:gy1, gx0:gx1] = 0.06
                ao[gy0:gy1, gx0:gx1] = 0.85
                if not style.shop and rng.random() < 0.35:   # curtains in some windows
                    cw = int((gx1 - gx0) * rng.uniform(0.15, 0.35))
                    curtain = (225, 218, 200) if rng.random() < 0.7 else (150, 140, 125)
                    col[gy0:gy1, gx0:gx0 + cw] = curtain
                    col[gy0:gy1, gx1 - cw:gx1] = curtain
                    rough[gy0:gy1, gx0:gx0 + cw] = 0.9
                    rough[gy0:gy1, gx1 - cw:gx1] = 0.9
                bar = max(1, fpx // 2)
                for m in range(1, style.mullions + 1):
                    xm = gx0 + (gx1 - gx0) * m // (style.mullions + 1)
                    col[gy0:gy1, xm - bar:xm + bar] = style.frame
                    height[gy0:gy1, xm - bar:xm + bar] = -0.3
                if style.transom:
                    yt = gy0 + gh // 3
                    col[yt - bar:yt + bar, gx0:gx1] = style.frame
                    height[yt - bar:yt + bar, gx0:gx1] = -0.3
                if style.sill and not door:
                    s0 = wy1
                    s1 = min(int(floor), wy1 + max(2, int(0.07 / style.storey_m * sh)))
                    col[s0:s1, wx0 - fpx:wx1 + fpx] = (200, 198, 190)
                    height[s0:s1, wx0 - fpx:wx1 + fpx] = 0.3
                if style.lintel:
                    l1 = wy0
                    l0 = max(0, wy0 - int(0.22 / style.storey_m * sh))
                    col[l0:l1, wx0:wx1] *= 0.8
                if style.shop:   # sign band above the shop window
                    b0 = max(0, int(floor - 0.97 * sh))
                    b1 = max(b0 + 1, int(floor - 0.80 * sh))
                    col[b0:b1, int(bx * bw):int((bx + 1) * bw)] = (30, 32, 36) if variant == 0 else (120, 30, 28)
                    rough[b0:b1, int(bx * bw):int((bx + 1) * bw)] = 0.4
    albedo = np.clip(col, 0, 255).astype(np.uint8)
    normal = height_to_normal(height, strength=3.0)
    orm = np.stack([ao, rough, metal, np.zeros_like(ao)], axis=-1)
    orm = np.clip(orm * 255.0, 0, 255).astype(np.uint8)
    return albedo, normal, orm


def height_to_normal(height: np.ndarray, strength: float = 3.0) -> np.ndarray:
    """Tangent-space normal map (DirectX green, as UE expects) from a height map, wrapped."""
    dx = (np.roll(height, -1, axis=1) - np.roll(height, 1, axis=1)) * 0.5 * strength
    dy = (np.roll(height, -1, axis=0) - np.roll(height, 1, axis=0)) * 0.5 * strength
    n = np.stack([-dx, dy, np.ones_like(height)], axis=-1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    return np.clip(np.round((n * 0.5 + 0.5) * 255.0), 0, 255).astype(np.uint8)


def flat_normal(px: int = 4) -> np.ndarray:
    return np.full((px, px, 3), (128, 128, 255), dtype=np.uint8)


def constant_orm(roughness: float, px: int = 4) -> np.ndarray:
    o = np.zeros((px, px, 4), dtype=np.uint8)
    o[..., 0] = 255
    o[..., 1] = int(round(roughness * 255))
    return o


# --------------------------------------------------------------------------- aerial photo

def ortho_tile(img: np.ndarray, transform, tile: tuple[int, int], tile_m: float, px: int) -> Optional[np.ndarray]:
    """The tile's square of a north-up ortho (HxWx3, affine ``transform`` = (a, b, c, d, e, f) as in
    rasterio: x = a*col + c, y = e*row + f), resampled to px x px (nearest of a box average).
    None when the tile is outside the photo."""
    a, _, c, _, e, f = transform[:6]
    x0, x1 = tile[0] * tile_m, (tile[0] + 1) * tile_m
    y0, y1 = tile[1] * tile_m, (tile[1] + 1) * tile_m
    h, w = img.shape[:2]
    # pixel centres of the output grid
    xs = x0 + (np.arange(px) + 0.5) * (x1 - x0) / px
    ys = y1 - (np.arange(px) + 0.5) * (y1 - y0) / px
    cols = np.floor((xs - c) / a).astype(int)
    rows = np.floor((ys - f) / e).astype(int)
    inside_c = (cols >= 0) & (cols < w)
    inside_r = (rows >= 0) & (rows < h)
    if not inside_c.any() or not inside_r.any():
        return None
    out = np.zeros((px, px, 3), dtype=np.uint8)
    mean = img.reshape(-1, img.shape[2])[:, :3].mean(axis=0).astype(np.uint8)
    out[:] = mean
    rr, cc = np.meshgrid(np.clip(rows, 0, h - 1), np.clip(cols, 0, w - 1), indexing="ij")
    ok = inside_r[:, None] & inside_c[None, :]
    out[ok] = img[rr[ok], cc[ok], :3]
    return out


def mean_colour(img: np.ndarray, transform, ring_xy: np.ndarray) -> Optional[tuple]:
    """Average ortho colour inside a footprint ring (model metres), or None."""
    a, _, c, _, e, f = transform[:6]
    h, w = img.shape[:2]
    cols = (ring_xy[:, 0] - c) / a
    rows = (ring_xy[:, 1] - f) / e
    r0, r1 = int(max(0, math.floor(rows.min()))), int(min(h, math.ceil(rows.max()) + 1))
    c0, c1 = int(max(0, math.floor(cols.min()))), int(min(w, math.ceil(cols.max()) + 1))
    if r1 <= r0 or c1 <= c0:
        return None
    rr, cc = np.mgrid[r0:r1, c0:c1]
    inside = _points_in_ring(cc.ravel() + 0.5, rr.ravel() + 0.5, cols, rows)
    if not inside.any():
        return None
    px = img[r0:r1, c0:c1, :3].reshape(-1, 3)[inside]
    return tuple(int(v) for v in np.median(px, axis=0))


def _points_in_ring(x, y, rx, ry) -> np.ndarray:
    inside = np.zeros(len(x), dtype=bool)
    n = len(rx)
    j = n - 1
    for i in range(n):
        cond = ((ry[i] > y) != (ry[j] > y))
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (rx[j] - rx[i]) * (y - ry[i]) / (ry[j] - ry[i]) + rx[i]
        inside ^= cond & (x < xint)
        j = i
    return inside


def save_png(path, arr: np.ndarray) -> None:
    from PIL import Image
    mode = {3: "RGB", 4: "RGBA"}[arr.shape[2]]
    Image.fromarray(arr, mode).save(str(path), optimize=True)
