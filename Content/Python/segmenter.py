import sys
import argparse
import pathlib

from samgeo import tms_to_geotiff, raster_to_vector
from samgeo.text_sam import LangSAM


def run_langsam(bbox, zoom, threshold=0.24, plugin_path=None):

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

    tms_to_geotiff(output=image_path, bbox=bbox, zoom=zoom, source="Satellite", overwrite=True)

    sam = LangSAM()

    sam.predict(image_path, text_prompt, output=masktif_path, box_threshold=threshold, text_threshold=threshold)
    raster_to_vector(masktif_path, maskgeojson_path)

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
    