// Contextual creation: no persistent drawing toolbar and no mutations until the shape is finished.
let createMenu=null,creation=null,creationPreview=null,creationShape=null;
const CREATE_TYPES={...SPACE_TYPES,crosswalk:'Crosswalk',stop_line:'Stop line',traffic_light:'Traffic light',traffic_sign:'Traffic sign'};
const creationIsPoint=k=>['traffic_light','traffic_sign'].includes(k);
const creationIsArea=k=>['crosswalk','bike_crosswalk'].includes(k);
function closeCreateMenu(){createMenu?.remove();createMenu=null;if(TOOL==='create-menu')TOOL=null;}
function cancelCreation(force=false){
  if(creation?.saving&&!force)return;
  closeCreateMenu();creation=null;TOOL=null;
  if(creationPreview){map.removeLayer(creationPreview);creationPreview=null;}
  $('create-progress')?.remove();map.getContainer().focus({preventScroll:true});
}
function openCreateMenu(ev){
  if(!EDIT_MODE||!FRAME||draggingGeometry||creation?.saving)return;
  ev.preventDefault();ev.stopPropagation();cancelCreation();TOOL='create-menu';
  const anchor=map.mouseEventToLatLng(ev),menu=document.createElement('div');createMenu=menu;
  menu.id='create-menu';menu.className='creation-card';menu.setAttribute('role','dialog');menu.setAttribute('aria-label','Create object');
  menu.innerHTML=`<strong>Create object</strong><label>Type<select id="create-kind">${Object.entries(CREATE_TYPES).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></label><label>Name<input id="create-name" placeholder="Optional"></label><div id="create-options"></div><div class="creation-actions"><button id="create-cancel">Cancel</button><button id="create-start">Draw</button></div>`;
  document.body.appendChild(menu);L.DomEvent.disableClickPropagation(menu);L.DomEvent.disableScrollPropagation(menu);
  const options=()=>{const k=$('create-kind').value,point=creationIsPoint(k),area=creationIsArea(k);
    $('create-options').innerHTML=(CONTROL_TYPES[k]?`<label>Style<select id="create-subtype">${Object.entries(CONTROL_TYPES[k]).map(([t,v])=>`<option value="${t}">${v}</option>`).join('')}</select></label>`:'')+
      (point?'<label>Facing (° from north)<input id="create-bearing" type="number" required min="0" max="359" value="0"></label><label id="create-speed-row" hidden>Speed (km/h)<input id="create-speed" type="number" required min="1" max="200" value="30"></label>':area?'':`<label>Width (m)<input id="create-width" type="number" required min="0.1" max="30" step="0.05" value="${k==='stop_line'?.3:k==='sidewalk'||k==='biking'?2:3.25}"></label>`);
    if($('create-subtype'))$('create-subtype').onchange=()=>{if($('create-speed-row'))$('create-speed-row').hidden=$('create-subtype').value!=='speed_limit';};
    $('create-start').textContent=point?'Place here':'Draw';
  };options();$('create-kind').onchange=options;$('create-cancel').onclick=cancelCreation;
  menu.style.left=Math.max(8,Math.min(ev.clientX,innerWidth-260))+'px';menu.style.top=Math.max(8,Math.min(ev.clientY,innerHeight-menu.offsetHeight-8))+'px';
  $('create-start').onclick=async()=>{
    const k=$('create-kind').value,width=Number($('create-width')?.value),bearing=Number($('create-bearing')?.value),type=$('create-subtype')?.value;
    if($('create-width')&&!$('create-width').reportValidity()||$('create-bearing')&&!$('create-bearing').reportValidity()||type==='speed_limit'&&!$('create-speed').reportValidity())return;
    const draft={kind:k,name:$('create-name').value.trim(),type,width,bearing,value:Number($('create-speed')?.value),points:[[anchor.lng,anchor.lat]]};
    closeCreateMenu();deselect(true);sceneHidden.delete(k);storeSceneOrder();renderLayerStack();
    if(SPACE_ALPHA===0)setSpaceAlpha(.5);
    if(creationIsPoint(k)){await saveControl({kind:k,type,label:draft.name||(type==='custom'?'Custom sign':undefined),geometry:{type:'Point',coordinates:draft.points[0]},bearing,value:type==='speed_limit'?draft.value:undefined,provenance:{basis:'drawn by reviewer'}});return;}
    creation=draft;TOOL='create';map.getContainer().focus({preventScroll:true});
    const bar=document.createElement('div');bar.id='create-progress';bar.className='creation-card';bar.innerHTML=`<strong>New ${esc(CREATE_TYPES[k].toLowerCase())}</strong><span>${creationIsArea(k)?'Click the outline corners':'Click points along the centerline'}. Drag any point to adjust it. Enter or double-click finishes. Esc cancels.</span><div id="create-error" role="alert" hidden></div><div class="creation-actions"><button id="create-abort">Cancel</button><button id="create-finish">Finish</button></div>`;document.body.appendChild(bar);
    L.DomEvent.disableClickPropagation(bar);$('create-abort').onclick=cancelCreation;$('create-finish').onclick=finishCreation;renderCreation();
  };
  $('create-kind').focus();
}
function updateCreationShape(){
  if(creationShape){creationPreview.removeLayer(creationShape);creationShape=null;}
  const d=creation,lls=d.points.map(llOf),opts={...sceneOptions('selection'),color:sceneColour(d.kind),weight:3,pmIgnore:true,interactive:false};
  if(lls.length>1){
    if(creationIsArea(d.kind))creationShape=L.polygon(lls,{...opts,fillOpacity:.4});
    else if(d.kind==='stop_line')creationShape=L.polyline(lls,opts);
    else creationShape=L.polygon(spaceRing(stripFromLine(d.points,d.width)).map(llOf),{...opts,fillOpacity:.4});
    creationPreview.addLayer(creationShape);
  }
  $('create-finish').disabled=!!d.saving||lls.length<(creationIsArea(d.kind)?3:2);
}
function clearCreationError(){if($('create-error'))$('create-error').hidden=true;$('hint').hidden=true;}
function renderCreation(){
  if(creationPreview)map.removeLayer(creationPreview);creationPreview=L.featureGroup().addTo(map);creationShape=null;
  const d=creation;updateCreationShape();
  d.points.forEach((point,index)=>{
    const marker=L.marker(llOf(point),{pane:'markerPane',pmIgnore:true,draggable:true,icon:L.divIcon({className:'creation-vertex',html:'',iconSize:[16,16],iconAnchor:[8,8]})});
    marker.on('dragstart',()=>{if(d.saving)return;d.selected=index;geometryDragStart();clearCreationError();});
    marker.on('drag',()=>{if(d.saving)return;const ll=marker.getLatLng();d.points[index]=[ll.lng,ll.lat];updateCreationShape();});
    marker.on('dragend',()=>{geometryDragEnd();map.getContainer().focus({preventScroll:true});});
    marker.on('click',()=>{d.selected=index;map.getContainer().focus({preventScroll:true});});
    creationPreview.addLayer(marker);marker.getElement().dataset.draftIndex=String(index);
  });
}
async function finishCreation(){
  const d=creation;if(!d||d.saving||draggingGeometry||d.points.length<(creationIsArea(d.kind)?3:2))return;
  d.saving=true;updateCreationShape();$('create-abort').disabled=true;
  creationPreview.eachLayer(l=>l.dragging?.disable());
  const before=JSON.stringify(OPS);let ok=false;
  try{
    if(creationIsArea(d.kind))ok=await saveControl({kind:d.kind,type:d.type||(d.kind==='bike_crosswalk'?'marked':'zebra'),label:d.name||CREATE_TYPES[d.kind],geometry:{type:'Polygon',coordinates:[[...d.points,d.points[0]]]},provenance:{basis:'drawn by reviewer'}});
    else if(d.kind==='stop_line')ok=await saveControl({kind:d.kind,type:d.type,label:d.name||'Stop line',geometry:{type:'LineString',coordinates:d.points},width_m:d.width,provenance:{basis:'drawn by reviewer'}});
    else ok=await saveSpace({kind:d.kind,name:d.name||CREATE_TYPES[d.kind],...stripFromLine(d.points,d.width),provenance:{basis:'drawn by reviewer'}});
  }catch(e){OPS=JSON.parse(before);hint('Could not save this shape: '+e,'err');}
  if(ok){cancelCreation(true);return;}
  d.saving=false;$('create-abort').disabled=false;renderCreation();
  const error=$('create-error'),message=$('hint').textContent;
  error.textContent=/cross itself|collapse|crossed|invalid polygon/i.test(message)?'The outline crosses itself or has no area. Drag its points into a simple outline, then Finish again.':message;
  error.hidden=false;$('hint').hidden=true;map.getContainer().focus({preventScroll:true});
}
function handleCreationKey(ev){
  if(creation?.saving){ev.preventDefault();return true;}
  if(ev.key==='Escape'&&(creation||createMenu)){ev.preventDefault();cancelCreation();return true;}
  if(creation&&!isEditable(ev.target)){
    if(['Delete','Backspace'].includes(ev.key)&&creation.selected!==undefined){ev.preventDefault();creation.points.splice(creation.selected,1);delete creation.selected;clearCreationError();renderCreation();return true;}
    if(ev.key==='Enter'){ev.preventDefault();finishCreation();return true;}
    if((ev.ctrlKey||ev.metaKey)&&['z','y'].includes(ev.key.toLowerCase())){ev.preventDefault();if(ev.key.toLowerCase()==='z'&&creation.points.length>1){creation.points.pop();renderCreation();}return true;}
  }
  return false;
}
map.getContainer().addEventListener('contextmenu',openCreateMenu,true);
map.getContainer().addEventListener('click',ev=>{
  if(createMenu){closeCreateMenu();ev.preventDefault();ev.stopImmediatePropagation();return;}
  if(!creation||!EDIT_MODE)return;ev.preventDefault();ev.stopImmediatePropagation();
  if(creation.saving||draggingGeometry||performance.now()<ignoreSelectionUntil)return;
  const vertex=ev.target.closest('.creation-vertex');if(vertex){creation.selected=Number(vertex.dataset.draftIndex);map.getContainer().focus({preventScroll:true});return;}
  const ll=map.mouseEventToLatLng(ev),last=creation.points.length?llOf(creation.points.at(-1)):null;
  if(!last||map.latLngToContainerPoint(ll).distanceTo(map.latLngToContainerPoint(last))>3){creation.points.push([ll.lng,ll.lat]);clearCreationError();renderCreation();}
},true);
map.getContainer().addEventListener('dblclick',ev=>{if(creation){ev.preventDefault();ev.stopImmediatePropagation();finishCreation();}},true);
