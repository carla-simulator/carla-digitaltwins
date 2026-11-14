"""Grouped street furniture against final surfaces; ENU geometry, CARLA transforms.

PCG is the renderer, not the placement authority. All group members pass together.
Connectivity is preserved in the clearance-eroded walking network on each layer.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import uuid
from collections import Counter
from pathlib import Path
import shapely
from shapely.geometry import Point, Polygon, box, mapping, shape
from shapely.affinity import affine_transform
from shapely.ops import unary_union, linemerge
from shapely.strtree import STRtree
from .project import _replace_retry
from .vegetation import _flatten_parts, _is_num, _parse_layer, stable_seed
from .furniture_assemblies import ASSEMBLIES, validate_anchors, anchor_pose

ROOT=Path(__file__).resolve().parent/'data/furniture'
CATALOG=json.loads((ROOT/'catalog.json').read_text(encoding='utf-8'))
SCHEMA='twin-furniture-plan/1'


def configuration(overrides=None):
    c=json.loads((ROOT/'defaults.json').read_text(encoding='utf-8'))
    unknown=set(overrides or {})-set(c)
    if unknown: raise ValueError(f'Unknown furniture settings: {sorted(unknown)}')
    c.update(copy.deepcopy(overrides or {}))
    for k in ('group_spacing_m','candidate_step_m','pedestrian_width_m','max_support_delta_m','max_candidates'):
        if not _is_num(c[k]) or c[k]<=0: raise ValueError(f'Invalid {k}')
    for k in ('curb_clearance_m','crossing_clearance_m','obstacle_clearance_m'):
        if not _is_num(c[k]) or c[k]<0: raise ValueError(f'Invalid {k}')
    if c['max_support_delta_m']>.05: raise ValueError('Support tolerance cannot exceed 5 cm')
    if not isinstance(c['seed'],int) or isinstance(c['seed'],bool): raise ValueError('Seed must be an integer')
    for g in c['exclusions']:
        p=shape(g)
        if p.is_empty or not p.is_valid or p.geom_type not in ('Polygon','MultiPolygon'): raise ValueError('Invalid exclusion polygon')
    for key,v in c['overrides'].items():
        if not isinstance(v,dict) or set(v)-{'position','disabled','yaw'}: raise ValueError(f'Unknown override for {key}')
        if 'position' in v and (len(v['position'])!=2 or not all(_is_num(x) for x in v['position'])): raise ValueError('Invalid position')
        if 'yaw' in v and not _is_num(v['yaw']): raise ValueError('Invalid yaw')
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


def _geom_dist_lt(tree: STRtree | None, parts: list, geom, thresh: float) -> bool:
    if tree is None or thresh <= 0.0:
        return False
    hits = tree.query(geom, predicate='dwithin', distance=thresh)
    if len(hits) == 0:
        return False
    return any(parts[int(i)].distance(geom) < thresh for i in hits)


def plan(model,config=None,poles=(),vegetation=None,other_plans=()):
    c=configuration(config);review=[];points=[];accepted=[]
    excl_parts=[shape(g) for g in c['exclusions']]
    exclusions=unary_union(excl_parts) if excl_parts else Polygon()
    bldg_parts=[b.footprint for b in model.buildings if not b.footprint.is_empty]
    buildings=unary_union(bldg_parts) if bldg_parts else Polygon()
    layers={}
    half_pw=c['pedestrian_width_m']/2
    for layer in sorted({_parse_layer(s.tags.get('layer')) for s in model.surfaces}):
        ss=[s for s in model.surfaces if _parse_layer(s.tags.get('layer'))==layer]
        def union(kinds, _ss=ss):
            geoms=[s.geometry for s in _ss if s.kind in kinds and not s.geometry.is_empty]
            return unary_union(geoms) if geoms else Polygon()
        walk=union({'sidewalk'});road=union({'drivable','parking','biking'});cross=union({'crossing'})
        shapely.prepare(walk)
        sidewalk_ss=[s for s in ss if s.kind=='sidewalk' and not s.geometry.is_empty]
        sidewalk_geoms=[s.geometry for s in sidewalk_ss]
        for sg in sidewalk_geoms:
            shapely.prepare(sg)
        obstacles=[Point(p['x'],-p['y']).buffer(.35+c['obstacle_clearance_m']) for p in poles if _parse_layer(p.get('layer',0))==layer]
        for p in (vegetation or {}).get('points',[]):
            if _parse_layer(p.get('layer',0))==layer:
                obstacles.append(Point(p['x'],-p['y']).buffer(p['pit_radius_m']+c['obstacle_clearance_m']))
        for other in other_plans:
            if other.get('schema')!=SCHEMA: raise ValueError('Invalid neighboring furniture plan')
            obstacles.extend(shape(g['occupied']) for g in other['review'] if g['status']=='accepted' and _parse_layer(g['layer'])==layer)
        occ_inputs=[o for o in obstacles+[buildings,exclusions] if not o.is_empty]
        occupied=unary_union(occ_inputs) if occ_inputs else Polygon()
        walk_cross=walk.union(cross)
        route=walk_cross.difference(occupied).buffer(-half_pw) if not walk_cross.is_empty else Polygon()
        route_polys=[p for p in polygons(route) if p.area>=.01]
        raw_curbs=[cb.geometry for cb in model.curbs if _parse_layer(cb.layer)==layer and cb.high_side_kind=='sidewalk' and not cb.geometry.is_empty]
        merged_curbs=[]
        if raw_curbs:
            merged=unary_union(raw_curbs)
            if merged.geom_type!='LineString':
                merged=linemerge(merged)
            lines=list(merged.geoms) if hasattr(merged,'geoms') else ([merged] if not merged.is_empty else [])
            merged_curbs=sorted((l.simplify(1e-8).normalize() for l in lines if not l.is_empty),key=lambda l:l.wkb_hex)
        road_parts=_flatten_parts(road)
        cross_parts=_flatten_parts(cross)
        occ_parts=_flatten_parts(occupied)
        layers[layer]=dict(
            surfaces=ss,
            sidewalk_ss=sidewalk_ss,
            sidewalk_geoms=sidewalk_geoms,
            sidewalk_tree=STRtree(sidewalk_geoms) if sidewalk_geoms else None,
            walk=walk,
            road=road,
            road_parts=road_parts,
            road_tree=STRtree(road_parts) if road_parts else None,
            cross=cross,
            cross_parts=cross_parts,
            cross_tree=STRtree(cross_parts) if cross_parts else None,
            walk_cross=walk_cross,
            base_occ_parts=occ_parts,
            base_occ_tree=STRtree(occ_parts) if occ_parts else None,
            accepted_envelopes=[],
            blocked=occupied,
            route_polys=route_polys,
            curbs=merged_curbs,
            curb_tree=STRtree(merged_curbs) if merged_curbs else None,
        )
    # Station merged canonical sidewalk curbs.
    candidates=[]
    for layer,g in layers.items():
        if not g['curbs'] or not c['infer_rest_groups']: continue
        for line in g['curbs']:
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
        layer=_parse_layer(anchor.get('layer',0))
        offsets=(0,2,-2,4,-4) if anchor['kind'].startswith('bus') else (0,)
        for offset in offsets:
            x,y,theta,error=anchor_pose({**anchor,'_station_offset':offset},layers.get(layer))
            candidates.append(('anchor:'+anchor['id'],x,y,theta,layer,{**anchor,'pose_error':error,'fit_station_offset_m':offset,
                'fit_displacement_m':Point(x,y).distance(Point(anchor['position']))}))
    if len(candidates)>c['max_candidates']: raise ValueError('Furniture candidate budget exceeded')
    completed_anchors=set()
    accepted_by_layer: dict[int, list[tuple[float, float]]] = {}
    group_spacing_r2=c['group_spacing_m']**2
    for key,x,y,angle,layer,anchor in sorted(candidates,key=lambda p:(p[5] is None,stable_seed(c['seed'],p[0]),p[0])):
        if key in completed_anchors: continue
        g=layers.get(layer);override=c['overrides'].get(key,{})
        has_manual_pos='position' in override
        original=[x,y]
        if has_manual_pos:
            x,y=override['position']
        if 'yaw' in override:
            angle=-override['yaw']
        elif has_manual_pos and g is not None and g['curbs'] and 'yaw' not in override and anchor and anchor['kind']!='banner':
            mp=Point(x,y)
            cb=g['curbs'][int(g['curb_tree'].nearest(mp))]
            d_cb=cb.project(mp)
            ca,cb_pt=cb.interpolate(max(0,d_cb-.3)),cb.interpolate(min(cb.length,d_cb+.3))
            angle=math.degrees(math.atan2(cb_pt.y-ca.y,cb_pt.x-ca.x))
        group_kind=anchor['kind'] if anchor else 'rest'
        if g is None:
            completed_anchors.add(key)
            review.append(dict(id=key,kind=group_kind,position=[x,y],original_position=original,layer=layer,
                               status='rejected',reason='no_surface_on_layer',members=[],occupied=mapping(Point(x,y).buffer(.1))))
            continue
        members=[];physical=[];functional=[]
        r_ang=math.radians(angle);cs_a,sn_a=math.cos(r_ang),math.sin(r_ang)
        for kind,dx,dy,parent,dz in ASSEMBLIES[group_kind]:
            spec=CATALOG[kind];lo,hi=spec['min_m'],spec['max_m']
            mx,my=x+dx*cs_a+dy*sn_a,y+dx*sn_a-dy*cs_a
            member_angle=angle
            footprint=placed(box(lo[0],lo[1],hi[0],hi[1]),mx,my,member_angle)
            access=placed(box(lo[0]-.1,lo[1]-.1,hi[0]+.1,hi[1]+spec['access_depth_m']),mx,my,member_angle)
            physical.append(footprint);functional.append(access)
            members.append(dict(id=key+'/'+kind,group_id=key,group_kind=group_kind,source=anchor['source'] if anchor else 'inferred',kind=kind,mesh=spec['path'],
                x=mx,y=-my,z=0.0,_dz=dz,_parent=parent,scale=1,yaw=-member_angle,seed=stable_seed(c['seed'],key+'/'+kind),layer=layer,
                status='accepted',parent_id=key+'/'+parent if parent else '',attachment_m=[dx,dy,dz] if parent else [0,0,0],footprint=mapping(footprint),_fp_geom=footprint,min_m=lo,max_m=hi,max_support_delta_m=c['max_support_delta_m']))
        footprint=unary_union(physical) if len(physical)>1 else physical[0]
        envelope=unary_union(functional) if len(functional)>1 else functional[0]
        env_bounds=envelope.bounds
        reason=None
        if override.get('disabled'):
            reason='disabled_by_user'
            if anchor: completed_anchors.add(key)
        elif anchor and anchor.get('pose_error') and not has_manual_pos:
            reason=anchor['pose_error']
        elif not g['walk'].covers(envelope):
            reason='sidewalk_or_access_width'
        elif _geom_dist_lt(g['road_tree'], g['road_parts'], footprint, c['curb_clearance_m']-.001):
            reason='road_clearance'
        elif _geom_dist_lt(g['cross_tree'], g['cross_parts'], envelope, c['crossing_clearance_m']):
            reason='crossing_access'
        elif (
            (g['base_occ_tree'] is not None and any(
                g['base_occ_parts'][int(i)].intersects(envelope)
                for i in g['base_occ_tree'].query(envelope, predicate='intersects')
            ))
            or any(
                not (eb[2] < env_bounds[0] or eb[0] > env_bounds[2] or eb[3] < env_bounds[1] or eb[1] > env_bounds[3])
                and eg.intersects(envelope)
                for eg, eb in g['accepted_envelopes']
            )
        ):
            reason='existing_obstacle_or_access'
        elif not anchor and any((qx-x)**2+(qy-y)**2<group_spacing_r2 for qx,qy in accepted_by_layer.get(layer, ())):
            reason='group_spacing'
        updated_route_polys = None
        route_obstacle = None
        if reason is None:
            # Keep every pre-existing clearance component connected and nonempty.
            # Crossing approach envelopes are protected separately above.
            route_obstacle=footprint if group_kind in ('bus_shelter','bus_stop') else envelope
            reach=half_pw+.001
            ex0,ey0,ex1,ey1=env_bounds
            near_indices=[]
            for idx, component in enumerate(g['route_polys']):
                cx0,cy0,cx1,cy1=component.bounds
                if cx1 < ex0 - reach or cx0 > ex1 + reach or cy1 < ey0 - reach or cy0 > ey1 + reach:
                    continue
                if component.distance(envelope) <= reach:
                    near_indices.append(idx)
            if near_indices:
                ux0=min(g['route_polys'][i].bounds[0] for i in near_indices)
                uy0=min(g['route_polys'][i].bounds[1] for i in near_indices)
                ux1=max(g['route_polys'][i].bounds[2] for i in near_indices)
                uy1=max(g['route_polys'][i].bounds[3] for i in near_indices)
                margin=c['pedestrian_width_m']+1.0
                clip=box(ux0-margin, uy0-margin, ux1+margin, uy1+margin)
                local_wc=g['walk_cross'].intersection(clip)
                local_blk=g['blocked'].intersection(clip).union(route_obstacle)
                local_new_route=local_wc.difference(local_blk).buffer(-half_pw)
                replacements={}
                for idx in near_indices:
                    component=g['route_polys'][idx]
                    remaining=component.intersection(local_new_route)
                    pieces=[p for p in polygons(remaining) if p.area>.01]
                    if len(pieces)!=1:
                        reason='pedestrian_network';break
                    replacements[idx]=pieces[0]
                if reason is None:
                    updated_route_polys=[replacements.get(i, comp) for i, comp in enumerate(g['route_polys'])]
        if reason is None:
            for m in members:
                if m['parent_id']: continue
                fp_geom=m['_fp_geom']
                heights=[float(model.sample_z(px,py,layer=layer)) for px,py in fp_geom.exterior.coords]
                if not all(math.isfinite(hz) for hz in heights):
                    reason='uneven_support';break
                if m['kind']=='bus_shelter':
                    coords=list(fp_geom.exterior.coords)[:4]
                    cx_m,cy_m=m['x'],-m['y']
                    z0=float(model.sample_z(cx_m,cy_m,layer=layer))
                    sx=(model.sample_z(cx_m+.5,cy_m,layer=layer)-model.sample_z(cx_m-.5,cy_m,layer=layer))
                    sy=(model.sample_z(cx_m,cy_m+.5,layer=layer)-model.sample_z(cx_m,cy_m-.5,layer=layer))
                    residual=max(abs(h-z0-sx*(px-cx_m)-sy*(py-cy_m)) for (px,py),h in zip(coords,heights))
                    if math.degrees(math.atan(math.hypot(sx,sy)))<=3 and residual<=c['max_support_delta_m']: continue
                if max(heights)-min(heights)>c['max_support_delta_m']:
                    reason='uneven_support';break
        # Populate z coordinates cleanly (with attached children inheriting parent base z).
        parent_base_z={}
        for m in members:
            dz=m.pop('_dz')
            parent_kind=m.pop('_parent')
            m.pop('_fp_geom')
            if parent_kind and parent_kind in parent_base_z:
                base_z=parent_base_z[parent_kind]
            else:
                mx_p,my_p=m['x'],-m['y']
                mpt=Point(mx_p,my_p)
                surface=None
                if g['sidewalk_tree'] is not None:
                    for si in sorted(int(k) for k in g['sidewalk_tree'].query(mpt, predicate='intersects')):
                        if g['sidewalk_geoms'][si].covers(mpt):
                            surface=g['sidewalk_ss'][si];break
                base_z=float(model.sample_z(mx_p,my_p,layer=layer))+(surface.z_offset if surface else 0.0)
                parent_base_z[m['kind']]=base_z
            m['z']=base_z+dz
        if has_manual_pos and anchor:
            completed_anchors.add(key)
        rec=dict(id=key,kind=group_kind,anchor=anchor,position=[x,y],original_position=original,layer=layer,yaw=-angle,
                 status='rejected' if reason else 'accepted',reason=reason,occupied=mapping(envelope),members=members)
        if anchor:
            previous=next((r for r in review if r['id']==key),None)
            if reason is None:
                if previous: review.remove(previous)
                review.append(rec)
            elif previous is None:
                review.append(rec)
        else:
            review.append(rec)
        if reason is None:
            if anchor: completed_anchors.add(key)
            points.extend(members)
            accepted.append((Point(x,y),layer))
            accepted_by_layer.setdefault(layer, []).append((x, y))
            g['accepted_envelopes'].append((envelope, env_bounds))
            g['blocked']=g['blocked'].union(route_obstacle)
            if updated_route_polys is not None:
                g['route_polys']=updated_route_polys
    return dict(schema=SCHEMA,coordinate_system='carla-metres',config=c,points=points,review=review,
        catalog=CATALOG,summary=dict(groups=len(accepted),accepted=len(points),rejected=len(review)-len(accepted),
        groups_by_kind=dict(Counter(r['kind'] for r in review if r['status']=='accepted')),
        reasons=dict(Counter(r['reason'] for r in review if r['reason']))))


def command(args):
    """Framework CLI entry; the interactive studio uses the same planner."""
    from .model import TwinModel
    load=lambda path: json.loads(Path(path).read_text(encoding='utf-8'))
    poles=[p.get('placement',p) for p in load(args.poles)]
    if any(p.get('status','ok')!='ok' for p in poles): raise ValueError('Unresolved pole placements')
    config=load(args.config) if args.config else {}
    if getattr(args,'anchors',None):config['anchors']=load(args.anchors)['anchors']
    result=plan(TwinModel.load(args.twin), config,
                poles,load(args.vegetation),[load(p) for p in args.occupied_plan])
    result['source_twin']=str(Path(args.twin).resolve())
    path=Path(args.out);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temp.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf-8',newline='\n')
        _replace_retry(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    print(json.dumps(result['summary']))
    return 0
