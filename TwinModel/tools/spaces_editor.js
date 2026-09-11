// Street spaces: persistent boundary pairs. Imported geometry is an unconfirmed estimate.
let SPACES_MODE = true, spaceLayer = null, spaceCandidates = [], spaceSplit = false;
let SPACE_ALPHA = .30;
try {const saved=localStorage.getItem('twin-space-alpha');if(saved!==null&&Number.isFinite(Number(saved)))SPACE_ALPHA=Math.max(0,Math.min(1,Number(saved)));}catch(e){}
function setSpaceAlpha(value){
  SPACE_ALPHA=Math.max(0,Math.min(1,value));
  $('space-alpha').value=Math.round(SPACE_ALPHA*100);$('space-alpha-value').textContent=`${Math.round(SPACE_ALPHA*100)}%`;
  applySpaceFocus();
  try{localStorage.setItem('twin-space-alpha',String(SPACE_ALPHA));}catch(e){}
}
// Keep the selected strip above overlapping estimates, with its own two edges unmistakable.
function applySpaceFocus(){
  // Compositing the overlay as one group makes 0% truly basemap-only, including edit handles.
  if(window.sceneReady)applySceneOrder();
}
$('space-alpha').oninput=ev=>setSpaceAlpha(Number(ev.target.value)/100);
setSpaceAlpha(SPACE_ALPHA);
const SPACE_TYPES = {driving:'Driving lane',parking:'Parking strip',biking:'Cycle lane',bus:'Bus lane',taxi:'Taxi lane',bike_crosswalk:'Bike crosswalk',sidewalk:'Sidewalk',shoulder:'Shoulder',median:'Median',verge:'Verge'};
const SPACE_COLOURS = {driving:'#719bda',parking:'#dba452',biking:'#57bd85',bus:'#de8766',taxi:'#e3cc55',bike_crosswalk:'#2ab8a5',sidewalk:'#c5b8aa',shoulder:'#a4acb6',median:'#d6bb69',verge:'#8cab50'};
const spaceRing = s => [...s.left,...s.right.slice().reverse()];
const spaceXY = c => FRAME.toLocal(c[1],c[0]);
const spaceWGS = p => {const a=FRAME.toWGS(...p);return [a[1],a[0]];};
const mixPoint = (a,b,t) => [a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t];
function lineStations(c) {const d=[0];for(let i=1;i<c.length;i++){const a=spaceXY(c[i-1]),b=spaceXY(c[i]);d.push(d[i-1]+Math.hypot(b[0]-a[0],b[1]-a[1]));}return d;}
function lineAt(c,t) {const d=lineStations(c),s=t*d.at(-1);for(let i=1;i<c.length;i++)if(d[i]>=s){return mixPoint(c[i-1],c[i],(s-d[i-1])/(d[i]-d[i-1]||1));}return c.at(-1).slice();}
function lineSlice(c,start,end) {const d=lineStations(c),n=d.at(-1);return [lineAt(c,start),...c.filter((p,i)=>d[i]>n*start+1e-6&&d[i]<n*end-1e-6),lineAt(c,end)];}
function stripFromLine(coords,width) {
  const xy=coords.map(spaceXY), left=[],right=[];
  for(let i=0;i<xy.length;i++){
    const tangent=(a,b)=>{const dx=b[0]-a[0],dy=b[1]-a[1],n=Math.hypot(dx,dy)||1;return [-dy/n,dx/n];};
    const a=tangent(xy[Math.max(0,i-1)],xy[i===0?1:i]),b=tangent(xy[i===xy.length-1?i-1:i],xy[Math.min(xy.length-1,i+1)]);
    const n=[a[0]+b[0],a[1]+b[1]],len=Math.hypot(...n)||1;n[0]/=len;n[1]/=len;
    const scale=width/2/Math.max(.5,n[0]*b[0]+n[1]*b[1]);
    left.push(spaceWGS([xy[i][0]+n[0]*scale,xy[i][1]+n[1]*scale]));right.push(spaceWGS([xy[i][0]-n[0]*scale,xy[i][1]-n[1]*scale]));
  }return {left,right};
}
function buildSpaceCandidates() {
  spaceCandidates=[];if(!TWIN||!OSM)return;
  const append=(s)=>{
    const length=(lineStations(s.left).at(-1)+lineStations(s.right).at(-1))/2;
    const n=Math.max(1,Math.ceil(length/40));
    for(let i=0;i<n;i++)spaceCandidates.push({...s,key:`${s.source}:${i}`,left:lineSlice(s.left,i/n,(i+1)/n),right:lineSlice(s.right,i/n,(i+1)/n),estimated:true});
  };
  const edges=TWIN.features.filter(f=>f.properties.layer==='lane_boundaries');
  for(const a of edges.filter(f=>f.properties.boundary==='inner')){
    const p=a.properties;if(p.junction_id||!SPACE_TYPES[p.type])continue;
    const b=edges.find(f=>f.properties.boundary==='outer'&&f.properties.road_id===p.road_id&&Number(f.properties.lane_id)===Number(p.lane_id));if(!b)continue;
    const positive=Number(p.lane_id)>0;
    append({source:`lane:${p.road_id}:${p.lane_id}`,name:p.name||'Unnamed street',kind:p.type,
      left:(positive?b:a).geometry.coordinates,right:(positive?a:b).geometry.coordinates,
      provenance:{road_id:p.road_id,lane_id:p.lane_id,osm_way_ids:parseJ(p.osm_way_ids)||[],basis:'generated width estimate'}});
  }
  for(const f of OSM.features){
    const p=f.properties;if(p.layer!=='osm_highway'||!['cycleway','footway','pedestrian'].includes(p['tag:highway']))continue;
    const c=f.geometry.coordinates;if(c.length<2)continue;
    const parsed=Number(p['tag:width']),width=parsed>0&&parsed<30?parsed: p['tag:highway']==='cycleway'?1.5:2;
    append({source:`osm:${p.osm_id}`,name:p['tag:name']||'Unnamed path',kind:p['tag:highway']==='cycleway'?(p['tag:cycleway']==='crossing'?'bike_crosswalk':'biking'):'sidewalk',...stripFromLine(c,width),
      provenance:{osm_way_ids:[p.osm_id],basis:parsed>0?'OSM centreline and tagged width':'OSM centreline and assumed width',initial_width_m:width}});
  }
}
function renderSpaces() {
  if(!FRAME)return;if(!spaceLayer)spaceLayer=L.featureGroup().addTo(map);spaceLayer.clearLayers();
  if(!SPACES_MODE)return;
  const corrected=OPS.filter(o=>o.op==='space.set'&&!o.disabled),replaced=new Set(corrected.map(o=>o.replaces));
  for(const s of [...spaceCandidates.filter(s=>!replaced.has(s.key)),...corrected.filter(o=>!o.deleted)]){
    if(EDIT_MODE&&SEL&&SEL.kind==='space'&&(SEL.space===s||SEL.space.id&&SEL.space.id===s.id))continue;
    const colour=SPACE_COLOURS[s.kind],poly=L.polygon(spaceRing(s).map(llOf),{...objectOptions(s.kind,s.id||s.key),color:colour,weight:1,fillColor:colour,fillOpacity:1,opacity:1,pmIgnore:true});
    poly.on('click',ev=>{if(TOOL)return;L.DomEvent.stop(ev);if(!canSelect())return;selectSpace(s,ev.latlng);});
    spaceLayer.addLayer(poly);
  }
  spaceLayer.bringToFront();applySpaceFocus();
}
function setSpacesMode() { SPACES_MODE=true; }
function spaceWidth(s){return [0,.25,.5,.75,1].reduce((sum,t)=>{const a=spaceXY(lineAt(s.left,t)),b=spaceXY(lineAt(s.right,t));return sum+Math.hypot(a[0]-b[0],a[1]-b[1]);},0)/5;}
function selectSpace(s,at) {
  if(SEL&&SEL.kind==='space'&&SEL.space===s){if(spaceSplit&&at)splitSpaceAt(at);return;}
  deselect(true);SEL={kind:'space',space:s,op:s.id?s:null,edges:{},edit:null};selectionChanged();
  const poly=track(L.polygon(spaceRing(s).map(llOf),{...(EDIT_MODE?objectOptions(s.kind,s.id||s.key):sceneOptions('selection')),interactive:EDIT_MODE,color:'#ffe14d',weight:2,fillColor:SPACE_COLOURS[s.kind],fillOpacity:EDIT_MODE?1:0,opacity:1}).addTo(map));SEL.area=poly;
  poly.on('click',ev=>{if(TOOL)return;L.DomEvent.stop(ev);if(spaceSplit)splitSpaceAt(ev.latlng);});
  if(!EDIT_MODE){renderSpaces();sceneActive();return;}
  poly.on('mousedown',beginSpaceMove);
  for(const [edge,colour,label] of [['left','#36d9ee','Left edge'],['right','#e7a3ff','Right edge']]){
    const l=track(L.polyline(s[edge].map(llOf),{...sceneOptions("selection"),color:colour,weight:4,opacity:1}).addTo(map));SEL.edges[edge]=l;

    l.on('mousedown',()=>{if(SEL&&SEL.kind==='space'){SEL.edit=l;sceneActive(edge);}});
    l.on('click',ev=>{if(TOOL)return;L.DomEvent.stop(ev);if(spaceSplit)splitSpaceAt(ev.latlng);});
    if(GEOMAN_OK){l.pm.enable({allowSelfIntersection:false,snappable:false,removeVertexOn:'disabled',hideMiddleMarkers:true});enableDirectLine(l);
      l.on('pm:markerdragstart',()=>{geometryDragStart();sceneActive(edge);});l.on('pm:vertexclick',()=>sceneActive(edge));l.on('pm:markerdragend',geometryDragEnd);
      l.on('pm:markerdrag pm:drag',()=>{if(SEL&&SEL.kind==='space')poly.setLatLngs([...SEL.edges.left.getLatLngs(),...SEL.edges.right.getLatLngs().slice().reverse()]);});
      l.on('pm:markerdragend pm:vertexadded pm:vertexremoved pm:dragend',()=>saveSpaceEdges());}
  }
  renderSpacePanel();renderSpaces();sceneActive();

}
function renderSpacePanel(){sceneActive();}
async function saveSpaceEdges(){if(!SEL||SEL.kind!=='space')return;await saveSpace({...SEL.space,left:SEL.edges.left.getLatLngs().map(p=>[p.lng,p.lat]),right:SEL.edges.right.getLatLngs().map(p=>[p.lng,p.lat])});}
async function saveSpace(next) {
  const before=JSON.stringify(OPS),original=SEL&&SEL.space;
  const data={...next,op:'space.set',estimated:false};delete data.key;
  // Freeze the generated footprint on the first edit, before moving any vertices.
  // Existing legacy edits deliberately stay unresolved: their original bounds are unknown.
  if(!data.source_geometry&&original&&original.estimated){
    data.source_geometry={type:'Polygon',coordinates:[JSON.parse(JSON.stringify([...original.left,...original.right.slice().reverse(),original.left[0]]))]};
  }
  if(!data.id){data.replaces=next.replaces||next.key;addOp(data);}else {const i=OPS.findIndex(o=>o.id===data.id);if(i>=0)OPS[i]=data;}
  // Validate remotely before adding history. Invalid edits revert to the last accepted boundaries.
  const r=await (await fetch('/api/corrections',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({ops:OPS,save:false})})).json();
  if(!r.ok){OPS=JSON.parse(before);deselect(true);if(original)selectSpace(original);hint((r.problems||[r.error]).join('; '),'err');return false;}
  pushHistory();setDirty(r.dirty);renderOps();const activeEdge=SEL&&SEL.activeEdge;selectSpace(data);sceneActive(activeEdge);hint(`${SPACE_TYPES[data.kind]} boundaries recorded. Ctrl+Z to undo.`,'ok');return true;
}
function beginSpaceMove(ev){
  if(!EDIT_MODE||TOOL||spaceSplit||!SEL||SEL.kind!=='space'||ev.originalEvent.button!==0)return;
  const s=SEL.space,origin=map.mouseEventToContainerPoint(ev.originalEvent),projected={left:s.left.map(c=>map.latLngToContainerPoint(llOf(c))),right:s.right.map(c=>map.latLngToContainerPoint(llOf(c)))};
  const pan=map.dragging.enabled();let moved=false;map.dragging.disable();L.DomEvent.stop(ev.originalEvent);
  const clean=()=>{document.removeEventListener('mousemove',move);document.removeEventListener('mouseup',end);cancelLineDrag=null;if(pan)map.dragging.enable();};
  const move=e=>{const d=map.mouseEventToContainerPoint(e).subtract(origin);if(!moved&&d.distanceTo(L.point(0,0))<3)return;
    if(!moved){geometryDragStart();moved=true;Object.values(SEL.edges).forEach(l=>l.pm.disable());}
    for(const edge of ['left','right'])SEL.edges[edge].setLatLngs(projected[edge].map(p=>map.containerPointToLatLng(p.add(d))));
    SEL.area.setLatLngs([...SEL.edges.left.getLatLngs(),...SEL.edges.right.getLatLngs().slice().reverse()]);};
  const end=()=>{clean();if(moved){geometryDragEnd();saveSpaceEdges();}};
  cancelLineDrag=()=>{clean();if(moved){geometryDragEnd();for(const edge of ['left','right'])SEL.edges[edge].setLatLngs(s[edge].map(llOf));SEL.area.setLatLngs(spaceRing(s).map(llOf));}};
  document.addEventListener('mousemove',move);document.addEventListener('mouseup',end);
}
async function splitSpaceAt(ll){
  const s=SEL.space,pt=spaceXY([ll.lng,ll.lat]);let best={d:Infinity,t:0};
  const fractions=[...new Set([0,1,...['left','right'].flatMap(e=>{const d=lineStations(s[e]);return d.map(v=>v/d.at(-1));})])].sort((a,b)=>a-b);
  const centre=t=>spaceXY(mixPoint(lineAt(s.left,t),lineAt(s.right,t),.5));
  for(let i=1;i<fractions.length;i++){
    const a=centre(fractions[i-1]),b=centre(fractions[i]),dx=b[0]-a[0],dy=b[1]-a[1],n=dx*dx+dy*dy;if(!n)continue;
    const u=Math.max(0,Math.min(1,((pt[0]-a[0])*dx+(pt[1]-a[1])*dy)/n)),d=Math.hypot(pt[0]-a[0]-u*dx,pt[1]-a[1]-u*dy);
    if(d<best.d)best={d,t:fractions[i-1]+u*(fractions[i]-fractions[i-1])};
  }
  if(best.t<=1e-5||best.t>=1-1e-5){hint('Choose a split inside the section, away from its ends.','err');return;}
  const t=best.t;spaceSplit=false;const before=JSON.stringify(OPS);
  if(s.id)removeOps(o=>o.id===s.id);
  const pieces=[[0,t],[t,1]].map(([a,b])=>{const o={...s,op:'space.set',estimated:false,replaces:s.replaces||s.key,left:lineSlice(s.left,a,b),right:lineSlice(s.right,a,b)};delete o.id;delete o.key;return addOp(o);});
  const r=await (await fetch('/api/corrections',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({ops:OPS,save:false})})).json();
  if(!r.ok){OPS=JSON.parse(before);hint(r.problems.join('; '),'err');return;}
  pushHistory();setDirty(r.dirty);renderOps();selectSpace(pieces[0]);hint('Section split. Each part now has its own type and boundaries.','ok');
}
