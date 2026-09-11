// Sub-component selection: point removal reconnects; segment removal leaves a real gap.
let GEOM_PART=null,geometryPartHighlight=null;
function clearGeometryPart(){
  GEOM_PART=null;if(geometryPartHighlight){map.removeLayer(geometryPartHighlight);geometryPartHighlight=null;}
  if($('geometry-part'))$('geometry-part').hidden=true;
}
function selectGeometryPart(part){
  clearGeometryPart();GEOM_PART=part;
  if(isEditable(document.activeElement))document.activeElement.blur();
  map.getContainer().focus({preventScroll:true});
  const ring=part.layer instanceof L.Polygon?part.layer.getLatLngs()[0]:part.layer.getLatLngs();
  const style={...sceneOptions('selection'),color:'#ff754b',weight:5,opacity:1,interactive:false,pmIgnore:true};
  geometryPartHighlight=part.type==='point'?L.circleMarker(ring[part.index],{...style,radius:9,fillOpacity:.25}):L.polyline([ring[part.index],ring[(part.index+1)%ring.length]],{...style,dashArray:'5 4'});
  geometryPartHighlight.addTo(map);
  if(!$('geometry-part')){const box=document.createElement('div');box.id='geometry-part';box.className='bar';$('scene-active').after(box);}
  $('geometry-part').hidden=false;
  $('geometry-part').textContent=`${part.edge?part.edge+' boundary · ':''}${part.type} ${part.index+1} · Delete to remove`;
  if(part.edge)sceneActive(part.edge);
}
function clickInsideSelection(ev,at){
  const ll=map.mouseEventToLatLng(ev),pt=[ll.lng,ll.lat];
  let geometry=null;
  if(SEL.kind==='space')geometry=SEL.area.toGeoJSON().geometry;
  else if(SEL.kind==='control'){
    if(SEL.control.geometry.type==='Point')return SEL.edit.getElement()?.contains(ev.target)||at.distanceTo(map.latLngToContainerPoint(SEL.edit.getLatLng()))<=18;
    if(SEL.control.geometry.type==='Polygon')return SEL.edit.getLatLngs().some(ring=>pointInRing(pt,ring.map(p=>[p.lng,p.lat])));
    const strip=stripFromLine(SEL.edit.getLatLngs().map(p=>[p.lng,p.lat]),SEL.control.width_m);
    return pointInRing(pt,spaceRing(strip));
  }else if(SEL.edit?.toGeoJSON)geometry=SEL.edit.toGeoJSON().geometry;
  else if(SEL.f)geometry=SEL.f.geometry;
  if(!geometry)return false;
  if(geometry.type==='Point')return at.distanceTo(map.latLngToContainerPoint(llOf(geometry.coordinates)))<=12;
  const polygons=geometry.type==='Polygon'?[geometry.coordinates]:geometry.type==='MultiPolygon'?geometry.coordinates:[];
  return polygons.some(rings=>pointInRing(pt,rings[0])&&!rings.slice(1).some(hole=>pointInRing(pt,hole)));
}
map.getContainer().addEventListener('click',ev=>{
  if(!EDIT_MODE||ev.button!==0||!SEL||TOOL||!canSelect()||SPLIT_ARMED||spaceSplit||ev.altKey||ev.shiftKey)return;
  const at=map.mouseEventToContainerPoint(ev);
  const entries=SEL.kind==='space'?Object.entries(SEL.edges):[['',selectedLayer()]];
  let bestPoint=null,bestSegment=null;
  for(const [edge,layer] of entries){
    if(!layer||!layer.getLatLngs)continue;
    const polygon=layer instanceof L.Polygon,ring=polygon?layer.getLatLngs()[0]:layer.getLatLngs();
    ring.forEach((ll,index)=>{const d=at.distanceTo(map.latLngToContainerPoint(ll));if(d<9&&(!bestPoint||d<bestPoint.d))bestPoint={type:'point',layer,index,edge,d};});
    for(let index=0;index<(polygon?ring.length:ring.length-1);index++){
      const a=map.latLngToContainerPoint(ring[index]),b=map.latLngToContainerPoint(ring[(index+1)%ring.length]),d=L.LineUtil.pointToSegmentDistance(at,a,b);
      if(d<9&&(!bestSegment||d<bestSegment.d))bestSegment={type:'segment',layer,index,edge,d};
    }
  }
  const part=bestPoint||bestSegment;
  if(part){ev.preventDefault();ev.stopPropagation();if(SEL.kind==='space')SEL.edit=part.layer;selectGeometryPart(part);}
  else {
    clearGeometryPart();
    // Let Leaflet dispatch to the newly clicked object. Empty-map clicks deselect normally.

  }
},true);
async function deleteGeometryPart(){
  if(!EDIT_MODE||!GEOM_PART||!SEL||TOOL)return;
  const part=GEOM_PART,layer=part.layer,polygon=layer instanceof L.Polygon,ring=polygon?layer.getLatLngs()[0]:layer.getLatLngs();
  if(part.type==='point'){
    if(ring.length<=(polygon?3:2)){hint(`Keep at least ${polygon?3:2} points. Remove a segment or the whole component instead.`,'err');return;}
    const coordinates=ring.slice();coordinates.splice(part.index,1);clearGeometryPart();
    const options={...layer.pm.getOptions()};layer.pm.disable();layer.setLatLngs(polygon?[coordinates]:coordinates);layer.pm.enable(options);
    layer.fire('pm:vertexremoved',{layer,index:part.index,indexPath:polygon?[0,part.index]:[part.index]});
    return;
  }
  if(SEL.kind==='space'){
    const s=SEL.space,d=lineStations(s[part.edge]),length=d.at(-1);
    const a=d[part.index]/length,b=d[part.index+1]/length;
    if(b-a<1e-10){hint('This segment has duplicate points. Select one of those points and remove it.','err');return;}
    const intervals=[];if(a>1e-8)intervals.push([0,a]);if(b<1-1e-8)intervals.push([b,1]);
    const pieces=intervals.map(([start,end])=>({...s,left:lineSlice(s.left,start,end),right:lineSlice(s.right,start,end)}));
    await replaceAnnotationPieces(s,pieces,'space.set');
  }else if(SEL.kind==='control'&&SEL.control.geometry.type==='LineString'){
    const c=SEL.control,coords=c.geometry.coordinates;
    const pieces=[coords.slice(0,part.index+1),coords.slice(part.index+1)].filter(p=>p.length>=2).map(points=>({...c,geometry:{type:'LineString',coordinates:points}}));
    await replaceAnnotationPieces(c,pieces,'control.set');
  }else hint('This contour must stay continuous. Remove a point to reshape it.','err');
}
async function replaceAnnotationPieces(original,pieces,op){
  const before=JSON.stringify(OPS),selectionAtStart=SEL;
  if(original.id)removeOps(o=>o.id===original.id);
  // A deletion record suppresses an imported estimate and retains its source identity for export/undo.
  const replacements=(pieces.length?pieces:[{...original,deleted:true}]).map(piece=>{
    const data={...piece,op,estimated:false,replaces:original.replaces||original.key};delete data.id;delete data.key;return addOp(data);
  });
  try{
    const response=await(await fetch('/api/corrections',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({ops:OPS,save:false})})).json();
    if(!response.ok){OPS=JSON.parse(before);hint((response.problems||[response.error]).join('; '),'err');return;}
    const keepFocus=SEL===selectionAtStart;
    pushHistory();setDirty(response.dirty);if(keepFocus)deselect(true);renderOps();renderSpaces();renderControls();
    if(keepFocus&&pieces.length){if(op==='space.set')selectSpace(replacements[0]);else selectControl(replacements[0]);}
    hint('Segment removed. Remaining pieces kept; Ctrl+Z restores it.','ok');
  }catch(error){OPS=JSON.parse(before);hint('Could not remove segment: '+error,'err');}
}

async function deleteSelectedObject(){
  if(!EDIT_MODE||!SEL||TOOL||draggingGeometry)return;
  if(SEL.kind==='space')await replaceAnnotationPieces(SEL.space,[],'space.set');
  else if(SEL.kind==='control')await replaceAnnotationPieces(SEL.control,[],'control.set');
  else hint('Select an outline point to remove it. This generated contour cannot be deleted as a whole.','err');
}
