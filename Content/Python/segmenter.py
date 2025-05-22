import sys
import argparse
import pathlib

import numpy as np
import geopandas as gpd
from shapely.geometry import Point
from samgeo import tms_to_geotiff, raster_to_vector
from samgeo.text_sam import LangSAM


def sample_points_in_polygons(gdf, min_area_per_point=100):
    """
    Sample points inside each polygon in a GeoDataFrame.

    Args:
        - gdf: GeoDataFrame with Polygon or MultiPolygon geometries
        - min_area_per_point: Area threshold (in geometry units) for additional points beyond the centroid

    Returns:
        - GeoDataFrame of sampled Points with reference to original index
    """
    sampled_points = []

    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom.is_empty or geom.area == 0:
            continue

        # Always sample the centroid
        centroid = geom.centroid
        sampled_points.append({'geometry': centroid, 'source_index': idx})

        # Estimate how many extra points to sample based on area
        n_extra = int(geom.area // min_area_per_point)

        if n_extra > 0:
            # Create a uniform grid inside the bounding box
            minx, miny, maxx, maxy = geom.bounds
            side = int(np.ceil(np.sqrt(n_extra * 2)))  # oversample slightly for spacing

            xs = np.linspace(minx, maxx, side)
            ys = np.linspace(miny, maxy, side)
            grid_points = [Point(x, y) for x in xs for y in ys]

            # Keep only points inside the polygon and not the centroid
            for pt in grid_points:
                if geom.contains(pt) and pt != centroid:
                    sampled_points.append({'geometry': pt, 'source_index': idx})
                    if len(sampled_points) - 1 >= n_extra:  # -1 for the centroid already added
                        break

    return gpd.GeoDataFrame(sampled_points, geometry='geometry', crs=gdf.crs)

def run_langsam(bbox, zoom, threshold=0.24, plugin_path=None):
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

    # Reproject from Web Mercator (EPSG:3857) to latitude-longitude coordinates WGS84 (EPSG:4326)
    gdf_latlon = gdf.to_crs(epsg=4326)

    sampled_points = sample_points_in_polygons(gdf_latlon, min_area_per_point=100)

    points_list = [[pt.x, pt.y] for pt in sampled_points["geometry"]]
    np.savetxt(points_path, points_list, delimiter=",")

    print("Trees segmentation done.")

def main():

    print(sys.version)
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--lon_min', type=float)
    parser.add_argument('--lat_min', type=float)
    parser.add_argument('--lon_max', type=float)
    parser.add_argument('--lat_max', type=float)
    parser.add_argument('--zoom',default=20, type=int)
    parser.add_argument('--threshold',default=0.24, type=float)
    parser.add_argument('--plugin_path', type=str)
    args = parser.parse_args()

    lon_min, lat_min, lon_max, lat_max = args.lon_min, args.lat_min, args.lon_max, args.lat_max
    zoom, threshold = args.zoom, args.threshold
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
                plugin_path=plugin_path
                )


if __name__ == '__main__':
    main()
