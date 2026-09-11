"""Reviewed assemblies and anchor orientation. Offsets are mesh-local metres."""
import math
from shapely.geometry import Point
from shapely.ops import nearest_points

# root-first; child offsets preserve authored glass pivots and actual mounting.
ASSEMBLIES={
    'rest':[('bench',0,0,None,0),('bin',1.75,0,None,0)],
    'bus_shelter':[('bus_shelter',0,0,None,0),('bus_glass',0,0,'bus_shelter',0)],
    'bus_stop':[('bus_pole',0,0,None,0),('bus_panel',0,.025,'bus_pole',2.05)],
    'banner':[('banner_pole',0,0,None,0),('banner',0,0,'banner_pole',4.47)],
}


def validate_anchors(anchors):
    seen=set()
    for a in anchors:
        if not isinstance(a.get('id'),str) or not a['id'] or a['id'] in seen:raise ValueError('Anchor IDs must be unique nonempty strings')
        seen.add(a['id'])
        if a.get('kind') not in ('bus_shelter','bus_stop','banner'):raise ValueError('Unknown anchor kind')
        if len(a.get('position',[]))!=2 or not all(math.isfinite(v) for v in a['position']):raise ValueError('Invalid anchor position')
        if not isinstance(a.get('layer',0),int):raise ValueError('Anchor layer must be an integer')
        if a.get('source') not in ('osm','manual'):raise ValueError('Anchor requires observed or manual provenance')
        if a['kind']=='banner' and (a.get('source')!='manual' or not math.isfinite(a.get('yaw',float('nan')))):
            raise ValueError('Banner requires an authored orientation and standalone support anchor')
        if a['kind'].startswith('bus') and a.get('source')=='osm':
            expected='yes' if a['kind']=='bus_shelter' else 'no'
            if a.get('tags',{}).get('shelter')!=expected:raise ValueError('Observed stop type contradicts source shelter tag')


def anchor_pose(anchor,geometry):
    x,y=anchor['position'];p=Point(x,y)
    if geometry is None:return x,y,0,'no_surface_on_layer'
    if anchor['kind']=='banner':return x,y,-anchor['yaw'],None
    curbs=geometry.get('curbs',[])
    if not curbs:return x,y,0,'stop_has_no_curb'
    curb=min(curbs,key=lambda c:c.distance(p))
    if curb.distance(p)>6:return x,y,0,'stop_curb_too_far'
    d=max(0,min(curb.length,curb.project(p)+anchor.get('_station_offset',0)));edge=curb.interpolate(d)
    a=curb.interpolate(max(0,d-.3));b=curb.interpolate(min(curb.length,d+.3))
    tangent=math.atan2(b.y-a.y,b.x-a.x)
    # The observed stop stays in anchor.position. This is a derived furniture
    # center on the same nearby curb, with the displacement recorded for review.
    for theta in (tangent,tangent+math.pi):
        nx,ny=math.sin(theta),-math.cos(theta)
        if geometry['walk'].covers(Point(edge.x-nx*1.2,edge.y-ny*1.2)):
            inset=2.92 if anchor['kind']=='bus_shelter' else .85
            cx,cy=edge.x-nx*inset,edge.y-ny*inset
            if Point(cx,cy).distance(p)>6:return x,y,0,'stop_fit_exceeds_6m'
            return cx,cy,math.degrees(theta),None
    return x,y,0,'stop_sidewalk_side_ambiguous'
