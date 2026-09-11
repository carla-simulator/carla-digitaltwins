// Scene inspector and traffic-control authoring. Display preferences never become corrections.
const CONTROL_TYPES={stop_line:{stop:'Stop',yield:'Give way'},bike_crosswalk:{marked:'Marked',unmarked:'Unmarked',signalized:'Signalized'},crosswalk:{zebra:'Zebra',unmarked:'Unmarked',signalized:'Signalized'},traffic_light:{vehicle:'Vehicle',pedestrian:'Pedestrian',left_arrow:'Left arrow',right_arrow:'Right arrow',directional:'Directional head'},traffic_sign:{stop:'Stop',yield:'Give way',speed_limit:'Speed limit',no_entry:'No entry',priority_road:'Priority road',custom:'Custom sign'}};
const SCENE_LABELS={driving:'Lanes',biking:'Cycle lanes',bus:'Bus lanes',taxi:'Taxi lanes',bike_crosswalk:'Bike crosswalks',parking:'Parking',sidewalk:'Sidewalks',intersections:'Junctions',crosswalk:'Crosswalks',stop_line:'Stop lines',traffic_light:'Traffic lights',traffic_sign:'Traffic signs',shoulder:'Shoulders',median:'Medians',verge:'Verges'};
let sceneHidden=new Set(), sceneRenderers={}, controlLayer=null,sceneContext=null,controlCandidates=[];
let objectOrder=[],sceneColours={...SPACE_COLOURS,intersections:'#b08be0',crosswalk:'#61d5d0',stop_line:'#f2f2ed',traffic_light:'#e99b64',traffic_sign:'#e16f81'};
try{const p=JSON.parse(localStorage.getItem('twin-spaces-display')||'null');if(p){sceneHidden=new Set(p.hidden||[]);objectOrder=p.objects||[];for(const [k,v] of Object.entries(p.colours||{}))if(k in SCENE_LABELS&&/^#[0-9a-f]{6}$/i.test(v))sceneColours[k]=v;}}catch(e){}
Object.assign(SPACE_COLOURS,sceneColours);
function sceneColour(key){return sceneColours[key]||'#b08be0';}
// A single parent composites all space fills, symbols, selections and edit handles together.
const overlayRoot=map.createPane('spaces-overlay');overlayRoot.style.zIndex='400';
for(const key of ['overlayPane','markerPane','shadowPane','tooltipPane'])overlayRoot.appendChild(map.getPane(key));
function sceneOptions(key){
  const pane=key==='selection'?'scene-selection':'scene-objects';
  if(!map.getPane(pane))map.createPane(pane,overlayRoot);
  if(!sceneRenderers[pane])sceneRenderers[pane]=L.svg({pane});
  return {pane,renderer:sceneRenderers[pane]};
}
function objectOptions(category,key){return {...sceneOptions(category),sceneCategory:category,sceneKey:String(key)};}
function sceneSelectionCategory(){if(!SEL)return '';return SEL.kind==='space'?SEL.space.kind:SEL.kind==='control'?SEL.control.kind:SEL.kind==='junction'?'intersections':'crosswalk';}
function sceneSelectionId(){return SEL?.space?.id||SEL?.space?.key||SEL?.control?.id||SEL?.control?.key||SEL?.f?.properties.id;}
function applySceneOrder(){
  sceneOptions('objects');sceneOptions('selection');
  map.getPane('scene-objects').style.zIndex='420';map.getPane('scene-selection').style.zIndex='590';
  overlayRoot.style.opacity=String(SPACE_ALPHA);
  overlayRoot.style.visibility=SPACE_ALPHA===0?'hidden':'';
  const ranked=new Map(objectOrder.map((k,i)=>[k,i])),layers=[];
  map.eachLayer(l=>{if(l.options.sceneCategory){const el=l.getElement?.();if(el)el.style.display=sceneHidden.has(l.options.sceneCategory)?'none':'';layers.push(l);}});
  layers.sort((a,b)=>(ranked.get(b.options.sceneKey)??1e9)-(ranked.get(a.options.sceneKey)??1e9));
  for(const l of layers)l.bringToFront?.();
}
function storeSceneOrder(){try{localStorage.setItem('twin-spaces-display',JSON.stringify({hidden:[...sceneHidden],objects:objectOrder,colours:sceneColours}));}catch(e){}}
function renderLayerStack(){
  $('scene-stack').innerHTML=Object.entries(SCENE_LABELS).map(([k,v])=>`<div class="type-row"><input type="color" value="${sceneColour(k)}" data-colour="${k}" aria-label="${v} color"><span>${v}</span><input type="checkbox" data-visible="${k}" aria-label="Show ${v}" ${sceneHidden.has(k)?'':'checked'}></div>`).join('');
  $('scene-stack').querySelectorAll('[data-visible]').forEach(b=>b.onchange=()=>{const k=b.dataset.visible;b.checked?sceneHidden.delete(k):sceneHidden.add(k);if(SEL&&sceneSelectionCategory()===k&&!b.checked)deselect(true);storeSceneOrder();applySceneOrder();renderLayerStack();renderSceneList();});
  $('scene-stack').querySelectorAll('[data-colour]').forEach(b=>b.oninput=()=>{const k=b.dataset.colour;sceneColours[k]=b.value;SPACE_COLOURS[k]=b.value;storeSceneOrder();refreshSceneDisplay();});
  $('scene-all').checked=sceneHidden.size===0;$('scene-all').indeterminate=sceneHidden.size>0&&sceneHidden.size<Object.keys(SCENE_LABELS).length;
}
function refreshSceneDisplay(){
  const selection=SEL,edge=SEL?.activeEdge;deselect(true);
  if(selection?.kind==='space')selectSpace(selection.space);
  else if(selection?.kind==='control')selectControl(selection.control);
  else if(selection?.kind==='junction')selectJunction(selection.f);
  else if(selection?.kind==='feature')inspectSceneFeature(selection.f);
  if(edge)sceneActive(edge);renderSceneList();
}
function sceneActive(edge){
  if(edge&&SEL)SEL.activeEdge=edge;
  if(!edge)applySpaceFocus();
  if(!SEL){$('scene-active').innerHTML='<small>Selected object</small><strong>Nothing selected</strong><span>Click a space to inspect it</span>';return;}
  let name,type;
  if(SEL.kind==='space'){name=SEL.space.name||'Unnamed space';type=SPACE_TYPES[SEL.space.kind];}
  else if(SEL.kind==='control'){name=SEL.control.label||CONTROL_TYPES[SEL.control.kind][SEL.control.type];type=SCENE_LABELS[SEL.control.kind];}
  else{name=SEL.f?.properties.name||SEL.f?.properties.id||'Unnamed object';type=SEL.kind==='junction'?'Junction':'Crosswalk';}
  $('scene-active').innerHTML=`<small>Selected object</small><strong>${esc(name)}</strong><span><i style="background:${sceneColour(sceneSelectionCategory())}"></i>${EDIT_MODE&&SEL.kind==='space'?`<select id="active-space-type" aria-label="Space type">${Object.entries(SPACE_TYPES).map(([k,v])=>`<option value="${k}" ${k===SEL.space.kind?'selected':''}>${v}</option>`).join('')}</select>`:esc(type)}${EDIT_MODE&&SEL.activeEdge?' · '+esc(SEL.activeEdge)+' boundary':''}</span>`;
  if($('active-space-type'))$('active-space-type').onchange=async ev=>{const select=ev.target;select.disabled=true;try{await saveSpace({...SEL.space,kind:select.value});}finally{select.disabled=false;}};
  if(EDIT_MODE&&SEL.kind==='control'&&['crosswalk','bike_crosswalk'].includes(SEL.control.kind)){
    const select=document.createElement('select');select.id='active-crosswalk-type';select.setAttribute('aria-label','Crosswalk type');
    select.innerHTML=['crosswalk','bike_crosswalk'].map(k=>`<option value="${k}" ${k===SEL.control.kind?'selected':''}>${SCENE_LABELS[k]}</option>`).join('');
    $('scene-active').appendChild(select);select.onchange=async()=>{const c=SEL.control,kind=select.value;select.disabled=true;try{await saveControl({...c,kind,type:c.type==='zebra'?'marked':c.type==='marked'?'zebra':c.type});}finally{select.disabled=false;}};
  }
}
function setInteractionMode(edit){
  if(EDIT_MODE===edit||creation?.saving)return;
  cancelCreation();
  if(GEOMAN_OK)map.pm.disableDraw();TOOL=null;EDIT_MODE=edit;
  refreshSceneDisplay();map.dragging.enable();
  $('mode-browse').setAttribute('aria-pressed',String(!edit));$('mode-edit').setAttribute('aria-pressed',String(edit));
  map.getContainer().classList.toggle('editing-spaces',edit);if(edit)map.doubleClickZoom.disable();else map.doubleClickZoom.enable();
}
function initScenePanel(){
  const toggle=$('scene-collapse');
  toggle.onclick=()=>{const collapsed=$('tools').classList.toggle('scene-collapsed');$('scene-panel-body').hidden=collapsed;toggle.textContent=collapsed?'›':'‹';toggle.setAttribute('aria-expanded',String(!collapsed));toggle.setAttribute('aria-label',collapsed?'Expand panel':'Collapse panel');};
  $('mode-browse').onclick=()=>setInteractionMode(false);$('mode-edit').onclick=()=>setInteractionMode(true);
  $('scene-all').onchange=()=>{sceneHidden=$('scene-all').checked?new Set():new Set(Object.keys(SCENE_LABELS));if(SEL&&sceneHidden.has(sceneSelectionCategory()))deselect(true);storeSceneOrder();applySceneOrder();renderLayerStack();renderSceneList();};
  $('scene-search').oninput=renderSceneList;
  L.DomEvent.disableClickPropagation($('tools'));L.DomEvent.disableScrollPropagation($('tools'));
  sceneActive();renderLayerStack();
}
function visibleSpaces(){const corrected=OPS.filter(o=>o.op==='space.set'&&!o.disabled),replaced=new Set(corrected.map(o=>o.replaces));return [...spaceCandidates.filter(s=>!replaced.has(s.key)),...corrected.filter(o=>!o.deleted)];}
function crossingKey(f){return 'crossing:'+(f.properties.id||TWIN.features.indexOf(f));}
function crossingReplaced(f){return OPS.some(o=>o.op==='control.set'&&!o.disabled&&o.replaces===crossingKey(f));}
function allSceneComponents(){
  if(!TWIN)return [];
  return [...visibleSpaces().map(s=>({category:s.kind,key:s.id||s.key,name:s.name||'Unnamed section',geometry:{type:'Polygon',coordinates:[spaceRing(s)]},select:()=>selectSpace(s)})),
    ...allControls().map(c=>({category:c.kind,key:c.id||c.key,name:c.label||CONTROL_TYPES[c.kind][c.type],geometry:c.geometry,select:()=>selectControl(c)})),
    ...TWIN.features.filter(f=>f.properties.layer==='signals'&&f.properties.kind==='crosswalk'||f.properties.layer==='surfaces'&&f.properties.kind==='crossing').filter(f=>!crossingReplaced(f)).map((f,i)=>({category:'crosswalk',key:crossingKey(f),name:'Generated crosswalk',geometry:f.geometry,select:()=>inspectSceneFeature(f)})),
    ...TWIN.features.filter(f=>f.properties.layer==='junctions').map(f=>({category:'intersections',key:f.properties.id,name:f.properties.name||f.properties.id,geometry:f.geometry,select:()=>selectJunction(f)}))];
}
function componentBounds(g){const bounds=L.latLngBounds([]);const visit=c=>{if(typeof c[0]==='number')bounds.extend(llOf(c));else c.forEach(visit);};visit(g.coordinates);return bounds;}
function renderSceneList(){
  if(!TWIN)return;
  const all=allSceneComponents(),keys=new Set(all.map(c=>String(c.key)));
  objectOrder=objectOrder.filter(k=>keys.has(k));const known=new Set(objectOrder);
  for(const c of all)if(!known.has(String(c.key))){objectOrder.push(String(c.key));known.add(String(c.key));}
  const ranked=new Map(objectOrder.map((k,i)=>[k,i])),q=$('scene-search').value.toLowerCase();
  const items=all.filter(c=>`${c.name} ${c.key} ${SCENE_LABELS[c.category]}`.toLowerCase().includes(q)).sort((a,b)=>ranked.get(String(a.key))-ranked.get(String(b.key)));
  $('scene-count').textContent=String(all.length);const host=$('scene-components'),scroll=host.scrollTop;host.replaceChildren();
  const fragment=document.createDocumentFragment();
  for(const c of items){
    const row=document.createElement('div');row.className='object-row';row.dataset.key=String(c.key);
    row.classList.toggle('type-hidden',sceneHidden.has(c.category));
    const index=ranked.get(String(c.key));
    row.innerHTML=`<button class="object-pick" title="${esc(c.key)}"><i style="background:${sceneColour(c.category)}"></i><span>${esc(c.name)}<small>${esc(SCENE_LABELS[c.category])}</small></span></button><button data-shift="-1" aria-label="Move ${esc(c.name)} forward" ${index===0?'disabled':''}>↑</button><button data-shift="1" aria-label="Move ${esc(c.name)} backward" ${index===objectOrder.length-1?'disabled':''}>↓</button>`;
    row.querySelector('.object-pick').onclick=()=>{sceneHidden.delete(c.category);renderLayerStack();storeSceneOrder();const bounds=componentBounds(c.geometry);if(!map.getBounds().contains(bounds))map.fitBounds(bounds,{maxZoom:21,paddingTopLeft:[370,60],paddingBottomRight:[60,60]});c.select();sceneActive();};
    row.querySelectorAll('[data-shift]').forEach(b=>b.onclick=()=>{const i=objectOrder.indexOf(String(c.key)),j=i+Number(b.dataset.shift);if(j<0||j>=objectOrder.length)return;[objectOrder[i],objectOrder[j]]=[objectOrder[j],objectOrder[i]];storeSceneOrder();applySceneOrder();renderSceneList();});fragment.appendChild(row);
  }
  host.appendChild(fragment);host.scrollTop=scroll;applySceneOrder();
}
function inspectSceneFeature(f){
  if(EDIT_MODE&&f.geometry.type==='Polygon'){selectControl({key:crossingKey(f),kind:'crosswalk',type:'zebra',label:f.properties.name||'Crosswalk',geometry:JSON.parse(JSON.stringify(f.geometry)),provenance:{source_id:f.properties.id}});return;}
  deselect(true);SEL={kind:'feature',f};selectionChanged();
  track(L.geoJSON(f,{...sceneOptions('selection'),interactive:false,style:{interactive:false,color:'#ffe14d',weight:4,fillColor:sceneColour('crosswalk'),fillOpacity:0},pointToLayer:(f,ll)=>L.circleMarker(ll,{...sceneOptions('selection'),color:'#ffe14d',radius:10})}).addTo(map));
  sceneActive();
}
function buildControlCandidates(){
  controlCandidates=[];if(!TWIN)return;
  for(const f of TWIN.features){const p=f.properties;if(p.layer!=='signals'||f.geometry.type!=='Point')continue;
    const kind=p.kind.startsWith('traffic_light')?'traffic_light':['stop','yield','speed_limit','priority_road'].includes(p.kind)?'traffic_sign':null;if(!kind)continue;
    const type=kind==='traffic_light'?(p.kind==='traffic_light_ped'?'pedestrian':p.kind==='traffic_light_arrow'?'directional':'vehicle'):p.kind;
    controlCandidates.push({key:'signal:'+p.id,kind,type,label:p.id,geometry:f.geometry,bearing:((90-Number(p.heading||0)*180/Math.PI)%360+360)%360,target:{road_id:p.road_id},value:p.kind==='speed_limit'?Number(p.value)*3.6:undefined,provenance:{signal_id:p.id,basis:'generated signal'}});
  }
}
function allControls(){const ops=OPS.filter(o=>o.op==='control.set'&&!o.disabled),replaced=new Set(ops.map(o=>o.replaces));return [...controlCandidates.filter(c=>!replaced.has(c.key)),...ops.filter(o=>!o.deleted)];}
function controlIcon(c){
  let symbol=c.type==='stop'?'STOP':c.type==='yield'?'▽':c.type==='speed_limit'?String(c.value):c.type==='no_entry'?'━':c.type==='priority_road'?'◆':'!';
  if(c.kind==='traffic_light')symbol=c.type==='pedestrian'?'🚶':c.type==='left_arrow'?'←':c.type==='right_arrow'?'→':c.type==='directional'?'↗':'●';
  return L.divIcon({className:'scene-control-icon',iconSize:[36,44],iconAnchor:[18,22],html:`<div style="background:${sceneColour(c.kind)}" class="control-face control-${c.kind} sign-${c.type}">${esc(symbol)}</div><div class="control-bearing" style="transform:rotate(${Number(c.bearing)||0}deg)">↑</div>`});
}
function makeControlLayer(c,selected=false){const opts=selected&&!EDIT_MODE?sceneOptions('selection'):objectOptions(c.kind,c.id||c.key),g=c.geometry;
  if(g.type==='Point'&&(!selected||!EDIT_MODE))return L.circleMarker(llOf(g.coordinates),{...opts,radius:selected?10:6,color:selected?'#ffe14d':sceneColour(c.kind),fillColor:sceneColour(c.kind),fillOpacity:selected?0:1,weight:2,pmIgnore:true,interactive:!selected});
  if(g.type==='Point')return L.marker(llOf(g.coordinates),{...opts,icon:controlIcon(c),draggable:selected&&EDIT_MODE,pmIgnore:true});
  if(g.type==='Polygon')return L.polygon(g.coordinates.map(ring=>ring.map(llOf)),{...opts,color:selected?'#ffe14d':sceneColour(c.kind),fillColor:sceneColour(c.kind),fillOpacity:selected&&!EDIT_MODE?0:1,weight:selected?3:1,pmIgnore:!selected});
  return L.polyline(g.coordinates.map(llOf),{...opts,color:selected?'#ffe14d':sceneColour(c.kind),weight:selected?5:4,dashArray:['crosswalk','bike_crosswalk'].includes(c.kind)?'8 5':null,pmIgnore:!selected});
}
function controlFootprint(c,selected=false){
  if(c.geometry.type!=='LineString')return null;
  const strip=stripFromLine(c.geometry.coordinates,c.width_m);
  return L.polygon(spaceRing(strip).map(llOf),{...(selected&&!EDIT_MODE?sceneOptions('selection'):objectOptions(c.kind,c.id||c.key)),color:sceneColour(c.kind),weight:1,fillColor:sceneColour(c.kind),fillOpacity:selected&&!EDIT_MODE?0:1,interactive:false,pmIgnore:true});
}
function renderControls(){
  if(!FRAME)return;if(!controlLayer)controlLayer=L.featureGroup().addTo(map);controlLayer.clearLayers();
  if(!SPACES_MODE)return;
  for(const c of allControls()){
    if(EDIT_MODE&&SEL&&SEL.kind==='control'&&(SEL.control===c||c.id&&SEL.control.id===c.id))continue;
    const footprint=controlFootprint(c);if(footprint)controlLayer.addLayer(footprint);
    const l=makeControlLayer(c);l.on('click',ev=>{if(TOOL)return;L.DomEvent.stop(ev);if(canSelect())selectControl(c);});l.bindTooltip(`${SCENE_LABELS[c.kind]} · ${CONTROL_TYPES[c.kind][c.type]}`);controlLayer.addLayer(l);
  }
  applySpaceFocus();
}
function renderSceneContext(){
  if(!TWIN)return;if(!sceneContext)sceneContext=L.featureGroup().addTo(map);sceneContext.clearLayers();
  for(const f of TWIN.features){
    const p=f.properties,category=p.layer==='junctions'?'intersections':p.layer==='signals'&&p.kind==='crosswalk'||p.layer==='surfaces'&&p.kind==='crossing'?'crosswalk':null;
    if(!category||EDIT_MODE&&SEL?.f===f||category==='crosswalk'&&(crossingReplaced(f)||EDIT_MODE&&SEL?.control?.key===crossingKey(f)))continue;
    const nodes=parseJ(p.osm_node_ids)||[],correction=category==='intersections'?OPS.find(o=>o.op==='junction.polygon'&&!o.disabled&&(o.nodes||[]).some(n=>nodes.includes(n))):null;
    const feature=correction?{...f,geometry:{type:'Polygon',coordinates:[correction.polygon]}}:f;
    const opts=objectOptions(category,p.id||'crossing:'+TWIN.features.indexOf(f));
    const layer=L.geoJSON(feature,{...opts,style:()=>({...opts,color:sceneColour(category),fillColor:sceneColour(category),weight:1,fillOpacity:1,opacity:1,pmIgnore:true})});
    layer.on('click',ev=>{L.DomEvent.stop(ev);if(canSelect()){category==='intersections'?selectJunction(f):inspectSceneFeature(f);sceneActive();}});sceneContext.addLayer(layer);
  }
  applySceneOrder();
}
function selectControl(c){
  if(EDIT_MODE&&SEL&&SEL.kind==='control'&&SEL.control===c)return;
  deselect(true);SEL={kind:'control',control:c,op:c.id?c:null,edit:null};selectionChanged();
  const footprint=controlFootprint(c,true);if(footprint)track(footprint.addTo(map));
  const l=track(makeControlLayer(c,true).addTo(map));SEL.edit=l;
  if(c.geometry.type==='Point'){l.on('dragstart',geometryDragStart);l.on('dragend',()=>{geometryDragEnd();saveControl({...c,geometry:{type:'Point',coordinates:[l.getLatLng().lng,l.getLatLng().lat]}});});}
  else if(GEOMAN_OK&&EDIT_MODE){l.pm.enable({allowSelfIntersection:false,snappable:false,removeVertexOn:'disabled',hideMiddleMarkers:true});enableDirectLine(l);l.on('pm:markerdragstart',geometryDragStart);l.on('pm:markerdragend',geometryDragEnd);l.on('pm:markerdragend pm:vertexadded pm:vertexremoved pm:dragend',()=>saveControl({...c,geometry:l.toGeoJSON(false).geometry}));}
  renderControlPanel();renderControls();renderSceneContext();sceneActive();
}
function renderControlPanel(){sceneActive();}
async function saveControl(c){
  const original=SEL&&SEL.kind==='control'?SEL.control:null;
  const rollback=()=>{OPS=JSON.parse(before);if(original){deselect(true);selectControl(original);}};
  const before=JSON.stringify(OPS),data={...c,op:'control.set',replaces:c.replaces||c.key};delete data.key;
  if(data.id){const i=OPS.findIndex(o=>o.id===data.id);if(i>=0)OPS[i]=data;}else addOp(data);
  try{const r=await(await fetch('/api/corrections',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({ops:OPS,save:false})})).json();if(!r.ok){rollback();hint((r.problems||[r.error]).join('; '),'err');return false;}pushHistory();setDirty(r.dirty);renderOps();selectControl(data);hint('Control annotation recorded. Ctrl+Z to undo.','ok');return true;}
  catch(e){rollback();hint('Could not save control: '+e,'err');return false;}
}
window.sceneReady=true;initScenePanel();applySceneOrder();
