import sys
import argparse
import pathlib

import numpy as np
import geopandas as gpd
from shapely.geometry import Point, Polygon
from samgeo import tms_to_geotiff, raster_to_vector
from samgeo.text_sam import LangSAM

# Radius around each tree position in meters
TREE_RADIUS = 7

def hexagonal_tree_positions(polygon: Polygon, radius: float) -> list[Point]:
    """
    Fill polygon areas with fixed radii trees.
    Use hexagonal packing, close to optimal for circles and the simplest dense packing in 2D
    """
    
    minx, miny, maxx, maxy = polygon.bounds
    dx = radius * 2
    dy = radius * np.sqrt(3)

    points = []
    y = miny
    row = 0
    while y < maxy:
        x = minx + (radius if row % 2 else 0)
        while x < maxx:
            p = Point(x, y)
            if polygon.contains(p):
                points.append(p)
            x += dx
        y += dy * 0.5
        row += 1
    
    # Sample the centroid if the area is small and no positions were sampled
    if len(points)==0:
        centroid = polygon.centroid
        points.append(centroid)
    
    return points

def sample_tree_positions(gdf: gpd.GeoDataFrame, tree_radius: float) -> gpd.GeoDataFrame:

    all_tree_points = []

    for poly in gdf.geometry:
        all_tree_points.extend(hexagonal_tree_positions(poly, tree_radius))

    tree_gdf = gpd.GeoDataFrame(geometry=all_tree_points, crs=gdf.crs)

    return tree_gdf

def run_langsam(bbox: list[float],
                zoom: int,
                threshold: float = 0.2,
                tree_radius: float = TREE_RADIUS,
                plugin_path: str | None = None) -> None:
    """
    Run LangSam
    """

    if plugin_path is None:
        outpath = pathlib.Path.cwd() / "pyoutputs"
    else:
        outpath = pathlib.Path(plugin_path) / "pyoutputs"
    outpath.mkdir(parents=True, exist_ok=True)
    print("Python outputs path:",outpath)

    text_prompt = "tree"

    image_path = str(outpath / "satellite.tif")
    masktif_path = str(outpath / "masks.tif")
    maskgeojson_path = str(outpath / "masks.geojson")
    points_path = str(outpath / "points.csv")

    tms_to_geotiff(output=image_path, bbox=bbox, zoom=zoom, source="Satellite", overwrite=True)

    sam = LangSAM()

    sam.predict(image_path, text_prompt, output=masktif_path, box_threshold=threshold, text_threshold=threshold)
    raster_to_vector(masktif_path, maskgeojson_path)

    gdf = gpd.read_file(maskgeojson_path)

    polylines = []
    for idx, pol in gdf.to_crs(epsg=4326).iterrows():
        polylines.extend([[p[0], p[1]] for p in list(pol["geometry"].exterior.coords)])
    np.savetxt("polylines.csv", polylines, delimiter=",", fmt="%f, %f")

    tree_gdf = sample_tree_positions(gdf, tree_radius)

    # Reproject from Web Mercator (EPSG:3857) to latitude-longitude coordinates WGS84 (EPSG:4326)
    gdf_latlon = tree_gdf.to_crs(epsg=4326)

    points_list = [[pt.x, pt.y] for pt in gdf_latlon["geometry"]]
    np.savetxt(points_path, points_list, delimiter=",")

    print("Trees segmentation done.")

def main() -> None:

    print(sys.version)
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--lon_min', type=float)
    parser.add_argument('--lat_min', type=float)
    parser.add_argument('--lon_max', type=float)
    parser.add_argument('--lat_max', type=float)
    parser.add_argument('--zoom',default=20, type=int)
    parser.add_argument('--threshold',default=0.24, type=float)
    parser.add_argument('--tree_radius',default=TREE_RADIUS, type=float)
    parser.add_argument('--plugin_path', type=str)
    args = parser.parse_args()

    lon_min, lat_min, lon_max, lat_max = args.lon_min, args.lat_min, args.lon_max, args.lat_max
    zoom, threshold = args.zoom, args.threshold
    tree_radius = args.tree_radius
    plugin_path = args.plugin_path
    
    print("Args:", args)
    print("Coords:", lon_min, lat_min, lon_max, lat_max)
    print("Received sys.argv:", sys.argv)
    print("Types:", [type(x) for x in [lon_min, lat_min, lon_max, lat_max, zoom, threshold]])

    # Hardcoded values for initial tests
    # lat_min = 41.383
    # lon_min = 2.155
    # lat_max = 41.387
    # lon_max = 2.165

    bbox = [lon_min, lat_min, lon_max, lat_max]
    
    run_langsam(bbox=bbox,
                zoom=zoom,
                threshold=threshold,
                tree_radius=tree_radius,
                plugin_path=plugin_path
                )


if __name__ == '__main__':
    main()
