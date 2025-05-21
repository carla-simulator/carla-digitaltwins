import argparse
import sys

def main():

    print(sys.version)
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--lon_min')
    parser.add_argument('--lat_min')
    parser.add_argument('--lon_max')
    parser.add_argument('--lat_max')
    args = parser.parse_args()

    lon_min, lat_min, lon_max, lat_max = args.lon_min, args.lat_min, args.lon_max, args.lat_max
    
    print("Args:", args)
    print("Coords:", lon_min, lat_min, lon_max, lat_max)
    print("Received sys.argv:", sys.argv)

if __name__ == '__main__':
    main()
