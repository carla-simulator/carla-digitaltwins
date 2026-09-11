"""Import observed stop anchors, preserving source coordinates and shelter tags.

The OSM map API is deliberately a separate explicit download, never a side effect
of planning or baking. Previously cached JSON can be imported fully offline.
"""
import json
from pathlib import Path
from pyproj import Transformer


def import_osm(model,data):
    transformer=Transformer.from_crs('EPSG:4326',model.geo_reference.replace(' +geoidgrids=egm96_15.gtx',''),always_xy=True)
    south,west,north,east=model.bbox_wgs84
    anchors=[];review=[];seen=set()
    for node in data.get('elements',[]):
        tags=node.get('tags',{})
        if node.get('type')!='node' or tags.get('highway')!='bus_stop' or node['id'] in seen:continue
        seen.add(node['id'])
        if not south<=node['lat']<=north or not west<=node['lon']<=east:continue
        x,y=transformer.transform(node['lon'],node['lat'])
        item=dict(id='osm:'+str(node['id']),position=[x,y],layer=int(tags.get('layer','0')),
                  source='osm',source_url=f"https://www.openstreetmap.org/node/{node['id']}",
                  name=tags.get('name','Bus stop'),tags=tags)
        shelter=tags.get('shelter')
        if shelter not in ('yes','no'):
            review.append({**item,'reason':'unknown_shelter_type'});continue
        anchors.append({**item,'kind':'bus_shelter' if shelter=='yes' else 'bus_stop'})
    return {'anchors':sorted(anchors,key=lambda a:a['id']),'review':review,'attribution':'© OpenStreetMap contributors, ODbL 1.0'}


def main():
    import argparse
    from .model import TwinModel
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--twin',required=True);ap.add_argument('--osm-json',required=True)
    ap.add_argument('--out',required=True);ap.add_argument('--fetch',action='store_true')
    a=ap.parse_args();model=TwinModel.load(a.twin);source=Path(a.osm_json)
    if a.fetch:
        import requests
        s,w,n,e=model.bbox_wgs84
        response=requests.get('https://api.openstreetmap.org/api/0.6/map.json',params={'bbox':f'{w},{s},{e},{n}'},
                              headers={'User-Agent':'TwinModel/0.1 furniture planning'},timeout=45)
        response.raise_for_status();data=response.json()
        if 'elements' not in data:raise ValueError('Invalid OSM map response')
        source.parent.mkdir(parents=True,exist_ok=True);source.write_text(json.dumps(data))
    result=import_osm(model,json.loads(source.read_text()))
    output=Path(a.out);output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,indent=2)+'\n')
    print(f"Imported {len(result['anchors'])} stops; {len(result['review'])} require review")

if __name__=='__main__':main()
