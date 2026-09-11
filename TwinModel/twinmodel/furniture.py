"""Grouped street furniture against final surfaces; ENU geometry, CARLA transforms.

PCG is the renderer, not the placement authority. All group members pass together.
Connectivity is preserved in the clearance-eroded walking network on each layer.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from shapely.geometry import Point, box, mapping, shape
from shapely.affinity import affine_transform
from shapely.ops import unary_union, linemerge
from .vegetation import stable_seed
from .furniture_assemblies import ASSEMBLIES, validate_anchors, anchor_pose

ROOT=Path(__file__).resolve().parent/'data/furniture'
CATALOG=json.loads((ROOT/'catalog.json').read_text())
SCHEMA='twin-furniture-plan/1'


def configuration(overrides=None):
    c=json.loads((ROOT/'defaults.json').read_text())
    unknown=set(overrides or {})-set(c)
    if unknown: raise ValueError(f'Unknown furniture settings: {sorted(unknown)}')
    c.update(copy.deepcopy(overrides or {}))
    for k in ('group_spacing_m','candidate_step_m','pedestrian_width_m','max_support_delta_m','max_candidates'):
        if not isinstance(c[k],(int,float)) or not math.isfinite(c[k]) or c[k]<=0: raise ValueError(f'Invalid {k}')
    for k in ('curb_clearance_m','crossing_clearance_m','obstacle_clearance_m'):
        if not math.isfinite(c[k]) or c[k]<0: raise ValueError(f'Invalid {k}')
    if c['max_support_delta_m']>.05: raise ValueError('Support tolerance cannot exceed 5 cm')
    if not isinstance(c['seed'],int): raise ValueError('Seed must be an integer')
    for g in c['exclusions']:
        p=shape(g)
        if p.is_empty or not p.is_valid or p.geom_type not in ('Polygon','MultiPolygon'): raise ValueError('Invalid exclusion polygon')
    for key,v in c['overrides'].items():
        if set(v)-{'position','disabled','yaw'}: raise ValueError(f'Unknown override for {key}')
        if 'position' in v and (len(v['position'])!=2 or not all(math.isfinite(x) for x in v['position'])): raise ValueError('Invalid position')
        if 'yaw' in v and not math.isfinite(v['yaw']): raise ValueError('Invalid yaw')
    validate_anchors(c['anchors'])
    if not isinstance(c['infer_rest_groups'],bool): raise ValueError('infer_rest_groups must be boolean')
    return c


def polygons(g):
    if g.is_empty: return []
    if g.geom_type=='Polygon': return [g]
    return [p for part in getattr(g,'geoms',()) for p in polygons(part)]


def placed(local,x,y,angle):
    a=math.radians(angle);cs,sn=math.cos(a),math.sin(a)
    # Mesh local +Y is reflected into ENU before rotation; output yaw=-angle.
    return affine_transform(local,[cs,sn,sn,-cs,x,y])


def plan(model,config=None,poles=(),vegetation=None,other_plans=()):
    c=configuration(config);review=[];points=[];accepted=[]
    exclusions=unary_union([shape(g) for g in c['exclusions']])
    buildings=unary_union([b.footprint for b in model.buildings])
    layers={}
    for layer in sorted({int(s.tags.get('layer') or 0) for s in model.surfaces}):
        ss=[s for s in model.surfaces if int(s.tags.get('layer') or 0)==layer]
        union=lambda kinds: unary_union([s.geometry for s in ss if s.kind in kinds])
        walk=union({'sidewalk'});road=union({'drivable','parking','biking'});cross=union({'crossing'})
        obstacles=[Point(p['x'],-p['y']).buffer(.35+c['obstacle_clearance_m']) for p in poles if int(p.get('layer') or 0)==layer]
        for p in (vegetation or {}).get('points',[]):
            if p.get('layer',0)==layer:
                obstacles.append(Point(p['x'],-p['y']).buffer(p['pit_radius_m']+c['obstacle_clearance_m']))
        for other in other_plans:
            if other.get('schema')!=SCHEMA: raise ValueError('Invalid neighboring furniture plan')
            obstacles.extend(shape(g['occupied']) for g in other['review'] if g['status']=='accepted' and g['layer']==layer)
        occupied=unary_union(obstacles+[buildings,exclusions])
        route=walk.union(cross).difference(occupied).buffer(-c['pedestrian_width_m']/2)
        layers[layer]=dict(surfaces=ss,walk=walk,road=road,cross=cross,occupied=occupied,blocked=occupied,route=route,
                           curbs=[cb.geometry for cb in model.curbs if int(cb.layer or 0)==layer and cb.high_side_kind=='sidewalk'])
    # Merge curb fragments and canonicalize direction before stationing.
    candidates=[]
    for layer,g in layers.items():
        raw=[cb.geometry for cb in model.curbs if int(cb.layer or 0)==layer and cb.high_side_kind=='sidewalk']
        if not raw or not c['infer_rest_groups']: continue
        merged=unary_union(raw)
        if merged.geom_type!='LineString': merged=linemerge(merged)
        lines=list(merged.geoms) if hasattr(merged,'geoms') else [merged]
        for line in sorted((l.simplify(1e-8).normalize() for l in lines),key=lambda l:l.wkb_hex):
            identity=hashlib.sha256(line.wkb).hexdigest()[:12]
            for station in range(1,math.ceil(line.length/c['candidate_step_m'])):
                d=station*c['candidate_step_m']
                if d>=line.length: continue
                p=line.interpolate(d);a=line.interpolate(max(0,d-.3));b=line.interpolate(min(line.length,d+.3))
                angle=math.degrees(math.atan2(b.y-a.y,b.x-a.x))
                inset=c['curb_clearance_m']+.4
                for side in (1,-1):
                    # local +Y (front) points toward the interior of the sidewalk.
                    theta=angle if side==1 else angle+180
                    r=math.radians(theta);x=p.x+math.sin(r)*inset;y=p.y-math.cos(r)*inset
                    if g['walk'].covers(Point(x,y)):
                        key=f'rest:{layer}:{identity}:{station}'
                        candidates.append((key,x,y,theta,layer,None));break
    for anchor in c['anchors']:
        layer=anchor.get('layer',0)
        offsets=(0,2,-2,4,-4) if anchor['kind'].startswith('bus') else (0,)
        for offset in offsets:
            x,y,theta,error=anchor_pose({**anchor,'_station_offset':offset},layers.get(layer))
            candidates.append(('anchor:'+anchor['id'],x,y,theta,layer,{**anchor,'pose_error':error,'fit_station_offset_m':offset,
                'fit_displacement_m':Point(x,y).distance(Point(anchor['position']))}))
    if len(candidates)>c['max_candidates']: raise ValueError('Furniture candidate budget exceeded')
    completed_anchors=set()
    for key,x,y,angle,layer,anchor in sorted(candidates,key=lambda p:(p[5] is None,stable_seed(c['seed'],p[0]),p[0])):
        if key in completed_anchors: continue
        g=layers.get(layer);override=c['overrides'].get(key,{})
        original=[x,y];x,y=override.get('position',[x,y]);angle=-override.get('yaw',-angle)
        group_kind=anchor['kind'] if anchor else 'rest'
        if g is None:
            completed_anchors.add(key)
            review.append(dict(id=key,kind=group_kind,position=[x,y],original_position=original,layer=layer,
                               status='rejected',reason='no_surface_on_layer',members=[],occupied=mapping(Point(x,y).buffer(.1))))
            continue
        members=[];physical=[];functional=[]
        for kind,dx,dy,parent,dz in ASSEMBLIES[group_kind]:
            spec=CATALOG[kind];lo,hi=spec['min_m'],spec['max_m'];r=math.radians(angle)
            mx,my=x+dx*math.cos(r)+dy*math.sin(r),y+dx*math.sin(r)-dy*math.cos(r)
            member_angle=angle
            footprint=placed(box(lo[0],lo[1],hi[0],hi[1]),mx,my,member_angle)
            access=placed(box(lo[0]-.1,lo[1]-.1,hi[0]+.1,hi[1]+spec['access_depth_m']),mx,my,member_angle)
            physical.append(footprint);functional.append(access)
            surface=next((s for s in g['surfaces'] if s.kind=='sidewalk' and s.geometry.covers(Point(mx,my))),None)
            z=float(model.sample_z(mx,my,layer=layer))+(surface.z_offset if surface else 0)
            members.append(dict(id=key+'/'+kind,group_id=key,group_kind=group_kind,source=anchor['source'] if anchor else 'inferred',kind=kind,mesh=spec['path'],
                x=mx,y=-my,z=z+dz,scale=1,yaw=-member_angle,seed=stable_seed(c['seed'],key+'/'+kind),layer=layer,
                status='accepted',parent_id=key+'/'+parent if parent else '',attachment_m=[dx,dy,dz] if parent else [0,0,0],footprint=mapping(footprint),min_m=lo,max_m=hi,max_support_delta_m=c['max_support_delta_m']))
        footprint=unary_union(physical);envelope=unary_union(functional)
        reason=None
        if anchor and anchor.get('pose_error'): reason=anchor['pose_error']
        elif override.get('disabled'): reason='disabled_by_user'
        elif not g['walk'].covers(envelope): reason='sidewalk_or_access_width'
        elif footprint.distance(g['road'])<c['curb_clearance_m']-.001: reason='road_clearance'
        elif not g['cross'].is_empty and envelope.distance(g['cross'])<c['crossing_clearance_m']: reason='crossing_access'
        elif envelope.intersects(g['occupied']): reason='existing_obstacle_or_access'
        elif not anchor and any(l==layer and Point(x,y).distance(p)<c['group_spacing_m'] for p,l in accepted): reason='group_spacing'
        if reason is None:
            # Keep every pre-existing clearance component connected and nonempty.
            # Crossing approach envelopes are protected separately above.
            route_obstacle=footprint if group_kind in ('bus_shelter','bus_stop') else envelope
            new_route=g['walk'].union(g['cross']).difference(g['blocked'].union(route_obstacle)).buffer(-c['pedestrian_width_m']/2)
            for component in polygons(g['route']):
                if component.area<.01 or component.distance(envelope)>c['pedestrian_width_m']/2+.001: continue
                remaining=component.intersection(new_route)
                pieces=[p for p in polygons(remaining) if p.area>.01]
                if len(pieces)!=1:
                    reason='pedestrian_network';break
        if reason is None:
            for m in members:
                if m['parent_id']: continue
                heights=[float(model.sample_z(px,py,layer=layer)) for px,py in shape(m['footprint']).exterior.coords]
                if not all(math.isfinite(z) for z in heights):
                    reason='uneven_support';break
                if m['kind']=='bus_shelter':
                    coords=list(shape(m['footprint']).exterior.coords)[:4]
                    center=Point(m['x'],-m['y']);z0=float(model.sample_z(center.x,center.y,layer=layer))
                    sx=(model.sample_z(center.x+.5,center.y,layer=layer)-model.sample_z(center.x-.5,center.y,layer=layer))
                    sy=(model.sample_z(center.x,center.y+.5,layer=layer)-model.sample_z(center.x,center.y-.5,layer=layer))
                    residual=max(abs(h-z0-sx*(px-center.x)-sy*(py-center.y)) for (px,py),h in zip(coords,heights))
                    if math.degrees(math.atan(math.hypot(sx,sy)))<=3 and residual<=c['max_support_delta_m']: continue
                if max(heights)-min(heights)>c['max_support_delta_m']:
                    reason='uneven_support';break
        rec=dict(id=key,kind=group_kind,anchor=anchor,position=[x,y],original_position=original,layer=layer,yaw=-angle,
                 status='rejected' if reason else 'accepted',reason=reason,occupied=mapping(envelope),members=members)
        if anchor:
            previous=next((r for r in review if r['id']==key),None)
            if previous: review.remove(previous)
        review.append(rec)
        if reason is None:
            if anchor: completed_anchors.add(key)
            points.extend(members);accepted.append((Point(x,y),layer));g['occupied']=g['occupied'].union(envelope);g['blocked']=g['blocked'].union(route_obstacle);g['route']=new_route
    return dict(schema=SCHEMA,coordinate_system='carla-metres',config=c,points=points,review=review,
        catalog=CATALOG,summary=dict(groups=len(accepted),accepted=len(points),rejected=len(review)-len(accepted),
        groups_by_kind=dict(Counter(r['kind'] for r in review if r['status']=='accepted')),
        reasons=dict(Counter(r['reason'] for r in review if r['reason']))))


def command(args):
    """Framework CLI entry; the interactive studio uses the same planner."""
    from .model import TwinModel
    load=lambda path: json.loads(Path(path).read_text())
    poles=[p.get('placement',p) for p in load(args.poles)]
    if any(p.get('status','ok')!='ok' for p in poles): raise ValueError('Unresolved pole placements')
    config=load(args.config) if args.config else {}
    if getattr(args,'anchors',None):config['anchors']=load(args.anchors)['anchors']
    result=plan(TwinModel.load(args.twin), config,
                poles,load(args.vegetation),[load(p) for p in args.occupied_plan])
    result['source_twin']=str(Path(args.twin).resolve())
    path=Path(args.out);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n');temp.replace(path)
    print(json.dumps(result['summary']))
    return 0
