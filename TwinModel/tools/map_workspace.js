// Unified authoring on the canonical map. All planner positions are local ENU;
// only exported Unreal instances use the flipped Y axis.
(() => {
  const style = document.createElement('style');
  style.textContent = `
    #tools{width:350px}#tools header strong{font-size:16px}
    #workspace-tabs{display:flex;padding:6px 10px;border-bottom:1px solid #ffffff20;gap:3px}
    #workspace-tabs button{flex:1}#tools #workspace-tabs [aria-selected=true]{background:#cee0ff;color:#172c46}
    #workspace-layers{display:flex;gap:12px;padding:9px 14px;font-size:12px;border-bottom:1px solid #ffffff16}
    #planning-panel{overflow:auto;padding:14px;min-height:0;flex:1}#planning-panel[hidden]{display:none}
    #planning-panel p{line-height:1.5;color:#b9c8d5}#planning-panel label{display:block;margin:10px 0}
    #planning-panel input[type=number],#planning-panel select,#planning-panel textarea,#planning-search{width:100%;background:#151b22;color:#edf3fa;border:1px solid #526073;padding:7px;border-radius:5px;font:inherit}
    #planning-panel textarea{height:200px;font:11px monospace}#planning-panel button{border:1px solid #526073;margin:3px 2px 3px 0}
    #planning-panel .primary{background:#cee0ff;color:#172c46}#planning-panel [aria-pressed=true]{background:#44654b}
    #planning-counts{padding:10px;background:#2a3540;border-radius:7px;line-height:1.6}
    #planning-selection,#planning-status,#planning-error{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}
    #planning-error{color:#ffb09e}#planning-list{max-height:170px;overflow:auto;margin-top:8px}
    #planning-list button{display:block;width:100%;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .planning-active .leaflet-scene-objects-pane *{pointer-events:none!important}
    #workspace-notice{padding:8px 14px;color:#f3ce91;font-size:12px;white-space:pre-wrap}
    #workspace-notice:empty{display:none}#tools [hidden]{display:none!important}
    .scene-collapsed #workspace-tabs,.scene-collapsed #workspace-layers,.scene-collapsed #planning-panel,.scene-collapsed #workspace-notice{display:none!important}
    @media(max-width:640px){#tools{width:290px;max-height:70vh;bottom:auto}}
  `;
  document.head.appendChild(style);
  const tools = $('tools');
  tools.setAttribute('aria-label', 'Digital twin workspace');
  tools.querySelector('header strong').textContent = 'Digital twin';
  const tabs = document.createElement('nav'); tabs.id = 'workspace-tabs'; tabs.setAttribute('aria-label', 'Authoring tools');
  tabs.innerHTML = '<button data-tab="layout" aria-selected="true">Layout</button><button data-tab="vegetation" aria-selected="false">Trees & plants</button><button data-tab="furniture" aria-selected="false">Furniture</button>';
  const layers = document.createElement('div'); layers.id = 'workspace-layers';
  layers.innerHTML = ['layout','vegetation','furniture'].map(k=>`<label><input type="checkbox" data-layer="${k}" checked> ${k==='vegetation'?'Plants':k[0].toUpperCase()+k.slice(1)}</label>`).join('');
  const notice = document.createElement('div'); notice.id='workspace-notice'; notice.setAttribute('role','status');
  const panel = document.createElement('section');panel.id='planning-panel';panel.hidden=true;
  tools.querySelector('header').after(tabs);tabs.after(layers);layers.after(notice);notice.after(panel);
  let active='layout', data=null, selected=null, selectedArea=null, mode='inspect', draft=[], busy=false;
  const histories={vegetation:[],furniture:[]}, visible={layout:true,vegetation:true,furniture:true};
  const groups={vegetation:L.layerGroup().addTo(map),furniture:L.layerGroup().addTo(map)}, sketches=L.layerGroup().addTo(map);
  const pane=map.createPane('planning');pane.style.zIndex='610';
  const renderer=L.canvas({pane:'planning',padding:.5});
  const ll=p=>FRAME.toWGS(p[0],p[1]);
  const toGeo=g=>({type:g.type,coordinates:convert(g.coordinates)});
  const convert=c=>typeof c[0]==='number'?ll(c).reverse():c.map(convert);
  const current=()=>data?.tools[active];
  const chosen=()=>current()?.plan.review.find(g=>g.id===selected);
  const options={pane:'planning',renderer,bubblingMouseEvents:false};
  async function api(path, body) {
    const r=await fetch(path,body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json();if(!r.ok||j.ok===false)throw Error(j.error||j.problems?.join('\n')||'Request failed');return j;
  }
  function error(e){const el=$('planning-error');if(el)el.textContent=e.message||String(e);else hint(String(e),'err');}
  function updateNotice(){
    if(!data)return;
    const stale=Object.entries(data.tools).filter(([,s])=>s.stale).map(([k])=>k);
    notice.textContent=data.layout_pending?'Layout changed. Rebuild the planning model, then regenerate plants and furniture.':stale.length?`Regenerate ${stale.join(' and ')}: planning inputs changed.`:'';
    if(data.busy)notice.textContent='Unreal bake in progress. Editing is paused until it finishes.';
  }
  function setMode(value){mode=value;draft=[];sketches.clearLayers();panel.querySelectorAll('[data-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.mode===mode)));const el=$('planning-mode');if(el)el.textContent=mode==='inspect'?'Click an object to inspect it.':mode==='move'?'Click the new position for the selected object.':['zone','exclude'].includes(mode)?'Click polygon corners, then Finish polygon.':`Click the map to place ${mode.replaceAll('_',' ')}.`;}
  function setTab(name){
    if(busy||creation?.saving)return;
    cancelCreation();setInteractionMode(false);deselect(true);active=name;selected=null;selectedArea=null;setMode('inspect');
    $('scene-collapse').onclick=()=>{const collapsed=tools.classList.toggle('scene-collapsed');$('scene-panel-body').hidden=collapsed||active!=='layout';panel.hidden=collapsed||active==='layout';const b=$('scene-collapse');b.textContent=collapsed?'›':'‹';b.setAttribute('aria-expanded',String(!collapsed));};
    TOOL=name==='layout'?null:'planning';map.getContainer().classList.toggle('planning-active',name!=='layout');
    $('scene-panel-body').hidden=name!=='layout';panel.hidden=name==='layout';$('b-save').hidden=name!=='layout';
    tabs.querySelectorAll('button').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===name)));
    renderPanel();draw();
  }
  tabs.querySelectorAll('button').forEach(b=>b.onclick=()=>setTab(b.dataset.tab));
  layers.querySelectorAll('input').forEach(b=>b.onchange=()=>{visible[b.dataset.layer]=b.checked;if(b.dataset.layer==='layout')overlayRoot.style.display=b.checked?'':'none';draw();});
  function onFeature(ev,id,area=null){
    if(active==='layout'||busy)return;
    if(mode!=='inspect'){mapClick(ev);return;}
    selected=id;selectedArea=area;selectionText();draw();
  }
  function geometry(g, colour, group, id, area=null, fill=.18){
    if(!g)return;
    L.geoJSON(toGeo(g),{...options,interactive:group===groups[active],style:{...options,interactive:group===groups[active],color:colour,weight:id===selected?3:1,fillOpacity:fill},onEachFeature:(_,l)=>l.on('click',e=>onFeature(e,id,area))}).addTo(group);
  }
  function draw(){
    if(!FRAME||!data)return;
    for(const name of ['vegetation','furniture']){
      const group=groups[name];group.clearLayers();if(!visible[name]||!data.tools[name])continue;
      const s=data.tools[name],isActive=name===active,rejected=isActive&&$('planning-rejected')?.checked;
      for(const g of s.plan.review){
        if(g.status!=='accepted'&&!rejected&&g.id!==selected)continue;
        const colour=g.id===selected&&isActive?'#ffffff':g.status!=='accepted'?'#f1a369':name==='vegetation'?'#61d69a':'#57cce3';
        if(name==='furniture'&&g.occupied){geometry(g.occupied,colour,group,g.id);for(const m of g.members||[])geometry(m.footprint,colour,group,g.id,null,.7);}
        const marker=L.circleMarker(ll(g.position),{...options,radius:g.id===selected&&isActive?7:name==='vegetation'?3.5:4,color:colour,weight:1,fillColor:colour,fillOpacity:g.status==='accepted'?.95:.15,interactive:isActive});
        marker.bindTooltip(`${g.kind||g.preset||name} · ${g.status}`);marker.on('click',e=>onFeature(e,g.id));marker.addTo(group);
      }
      if(isActive){
        for(const z of s.config.zones||[])geometry(z.geometry,'#91ccff',group,null,{kind:'zone',id:z.id},.04);
        (s.config.exclusions||[]).forEach((g,i)=>geometry(g,'#f29285',group,null,{kind:'exclusion',index:i}));
        for(const a of s.config.anchors||[]){
          L.circleMarker(ll(a.position),{...options,radius:3,color:'#89c9ff',fillOpacity:1}).on('click',e=>onFeature(e,'anchor:'+a.id)).addTo(group);
          const g=s.plan.review.find(g=>g.id==='anchor:'+a.id);
          if(g)L.polyline([ll(a.position),ll(g.position)],{...options,color:'#89c9ff',weight:1,interactive:false}).addTo(group);
        }
      }
    }
  }
  function selectionText(){
    const g=chosen(),el=$('planning-selection');if(!el)return;
    el.textContent=g?`${g.id}\n${g.kind||g.preset||''} · ${g.anchor?.name||g.source||'inferred'}\n${g.status}: ${g.ground_error||g.reason||'valid placement'}${g.footing?' · sidewalk surround':''}${g.anchor?.fit_displacement_m!==undefined?'\n'+g.anchor.fit_displacement_m.toFixed(2)+' m from source':''}`:selectedArea?`${selectedArea.kind}: ${selectedArea.id||selectedArea.index+1}`:'Nothing selected.';
    for(const id of ['disable','restore','move','remove-anchor']){const b=$('planning-'+id);if(b)b.disabled=!g||busy||data.busy;}
  }
  function renderPanel(){
    updateNotice();if(active==='layout')return;
    const s=current();if(!s){panel.innerHTML='<h2>'+ (active==='vegetation'?'Trees & plants':'Furniture')+'</h2><p>'+esc(data?.errors[active]||'Loading…')+'</p>';return;}
    const veg=active==='vegetation',c=s.config;
    panel.innerHTML=`<div id="planning-counts"></div>
      <p>${veg?'Trees, planting zones, and sidewalk surrounds.':'Rest groups, bus stops, shelters, and supported banners.'}</p>
      <label>Variation seed<input id="planning-seed" type="number" value="${c.seed}"></label>
      <label>${veg?'Row':'Group'} spacing (m)<input id="planning-spacing" type="number" step=".5" value="${veg?c.row_spacing_m:c.group_spacing_m}"></label>
      ${veg?'<label>Street tree preset<select id="planning-tree-preset"></select></label>':'<label>Clear walk width (m)<input id="planning-width" type="number" step=".1" value="'+c.pedestrian_width_m+'"></label>'}
      <label><input id="planning-infer" type="checkbox" ${(veg?c.street_rows:c.infer_rest_groups)?'checked':''}> ${veg?'Fill street-tree rows':'Infer rest groups'}</label>
      <button id="planning-generate" class="primary">Regenerate & save</button><button id="planning-undo">Undo</button>
      <hr>${veg?'<label>New plant / zone preset<select id="planning-preset"></select></label><label>Zone density<input id="planning-density" type="number" min="0" max="1" step=".1" value="1"></label>':'<label>Banner yaw (degrees)<input id="planning-yaw" type="number" value="0"></label>'}
      <label>Placement layer<input id="planning-layer" type="number" value="0" step="1"></label>
      <div>${(veg?['inspect','tree','zone','exclude','move']:['inspect','bus_stop','bus_shelter','banner','exclude','move']).map(m=>`<button data-mode="${m}" id="planning-${m}" aria-pressed="${mode===m}">${({tree:'Add plant',zone:'Draw planting zone',exclude:'Draw exclusion',move:'Move selected',bus_stop:'Add stop pole',bus_shelter:'Add shelter',banner:'Add banner',inspect:'Inspect'})[m]}</button>`).join('')}</div>
      <p id="planning-mode"></p><button id="planning-finish">Finish polygon</button><button id="planning-cancel">Cancel</button>
      <p id="planning-selection"></p><button id="planning-disable">Disable selected</button><button id="planning-restore">Reset selected</button><button id="planning-delete-area">Delete selected area</button>
      ${veg?'':'<button id="planning-remove-anchor">Remove anchor</button><label>Import OSM anchors<input id="planning-import" type="file" accept=".json"></label>'}
      <hr><label><input id="planning-rejected" type="checkbox"> Show rejected candidates</label><input id="planning-search" type="search" placeholder="Find an object…" aria-label="Find a placement"><div id="planning-list"></div>
      <details><summary>Configuration</summary><p>Full planner settings, including exclusions and overrides.</p><textarea id="planning-config" aria-label="Planner configuration"></textarea><button id="planning-apply">Apply configuration</button><button id="planning-export">Export configuration</button></details>
      <hr><button id="planning-bake" class="primary">Bake ${veg?'vegetation':'furniture'} into Unreal</button><button id="planning-rebuild">Rebuild planning model</button><p id="planning-status" role="status"></p><p id="planning-error" role="alert"></p><p>Planning saves are immediate. Baking updates the saved Unreal map; reload CARLA to see it. A model rebuild does not bake map geometry.</p>`;
    if(veg){for(const [key,p] of Object.entries(c.presets)){ $('planning-preset').add(new Option(key+' ('+p.kind+')',key));if(p.kind==='tree')$('planning-tree-preset').add(new Option(key,key));}$('planning-tree-preset').value=c.tree_preset;}
    $('planning-config').value=JSON.stringify(c,null,2);
    panel.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>setMode(b.dataset.mode));
    $('planning-cancel').onclick=()=>setMode('inspect');
    $('planning-generate').onclick=()=>edit(cfg=>{cfg.seed=+$('planning-seed').value;cfg[veg?'row_spacing_m':'group_spacing_m']=+$('planning-spacing').value;cfg[veg?'street_rows':'infer_rest_groups']=$('planning-infer').checked;if(veg)cfg.tree_preset=$('planning-tree-preset').value;else cfg.pedestrian_width_m=+$('planning-width').value;});
    $('planning-undo').onclick=async()=>{const history=histories[active];if(history.length&&await submit(history.at(-1),false))history.pop();updateStatus();};
    $('planning-finish').onclick=()=>{if(draft.length<3||!['zone','exclude'].includes(mode))return;const geometry={type:'Polygon',coordinates:[[...draft,draft[0]]]};const zone=mode==='zone';edit(cfg=>{if(zone)cfg.zones.push({id:'zone-'+crypto.randomUUID(),preset:$('planning-preset').value,density:+$('planning-density').value,layer:+$('planning-layer').value,geometry});else cfg.exclusions.push(geometry);});};
    $('planning-disable').onclick=()=>edit(cfg=>{if(selected)cfg.overrides[selected]={...cfg.overrides[selected],disabled:true};});
    $('planning-restore').onclick=()=>edit(cfg=>{if(!selected)return;if(veg&&selected.startsWith('manual:'))cfg.overrides[selected].disabled=false;else delete cfg.overrides[selected];});
    $('planning-delete-area').onclick=()=>{if(!selectedArea)return;edit(cfg=>{if(selectedArea.kind==='zone')cfg.zones=cfg.zones.filter(z=>z.id!==selectedArea.id);else cfg.exclusions.splice(selectedArea.index,1);});};
    if(!veg){$('planning-remove-anchor').onclick=()=>{if(!selected?.startsWith('anchor:'))return;edit(cfg=>{cfg.anchors=cfg.anchors.filter(a=>'anchor:'+a.id!==selected);delete cfg.overrides[selected];});};$('planning-import').onchange=async e=>{try{const j=JSON.parse(await e.target.files[0].text());if(!Array.isArray(j.anchors))throw Error('Expected an anchors document');edit(cfg=>{cfg.anchors=j.anchors;});}catch(e){error(e);}};}
    $('planning-rejected').onchange=()=>{draw();renderList();};$('planning-search').oninput=renderList;
    $('planning-apply').onclick=()=>{try{submit(JSON.parse($('planning-config').value));}catch(e){error(e);}};
    $('planning-export').onclick=()=>{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(current().config,null,2)],{type:'application/json'}));a.download=active+'-config.json';a.click();URL.revokeObjectURL(a.href);};
    $('planning-bake').onclick=()=>run(async()=>{data=await api('/api/planning/'+active+'/bake',{});});
    $('planning-rebuild').onclick=()=>run(async()=>{await save();if(DIRTY)throw Error('Save layout edits before rebuilding.');await api('/api/rebuild',{});await refreshTwin();await refreshOsm();data=await api('/api/planning');});
    updateStatus();selectionText();renderList();setMode(mode);
  }
  function renderList(){const host=$('planning-list');if(!host)return;host.replaceChildren();const q=$('planning-search').value.toLowerCase();for(const g of current().plan.review.filter(g=>(g.status==='accepted'||$('planning-rejected').checked)&&`${g.id} ${g.kind||g.preset||''} ${g.anchor?.name||''}`.toLowerCase().includes(q))){const b=document.createElement('button');b.textContent=`${g.anchor?.name||g.id} · ${g.status}`;b.onclick=()=>{selected=g.id;selectedArea=null;map.setView(ll(g.position),Math.max(map.getZoom(),21),{animate:false});selectionText();draw();};host.appendChild(b);}}
  function updateStatus(){
    updateNotice();const s=current();if(!s||active==='layout')return;
    const summary=s.plan.summary;$('planning-counts').textContent=active==='vegetation'?`${summary.accepted} plants · ${summary.footings||0} surrounds · ${summary.rejected} rejected`:`${summary.groups} groups · ${summary.accepted} props · ${summary.rejected} rejected`;
    const b=s.bake;$('planning-status').textContent=b.running?'Baking in Unreal…':b.error?'Bake failed: '+b.error:b.exit_code?'Bake failed. Inspect bake.log.':s.stale?'Preview is out of date. Regenerate before baking.':b.report?`Saved map: ${b.report.status||'bake complete'}`:'Placement preview saved.';
    panel.querySelectorAll('button,input,select,textarea').forEach(el=>el.disabled=busy||data.busy);
    $('planning-bake').disabled=busy||data.busy||!b.enabled||s.stale||data.layout_pending;
    $('planning-generate').disabled=busy||data.busy||data.layout_pending;
    $('planning-undo').disabled=busy||data.busy||!histories[active].length;
    tabs.querySelectorAll('button').forEach(el=>el.disabled=busy);
    selectionText();
  }
  async function run(fn){if(busy||data?.busy)return;busy=true;updateStatus();try{$('planning-error').textContent='';await fn();return true;}catch(e){error(e);return false;}finally{busy=false;updateStatus();draw();}}
  async function submit(cfg,checkpoint=true){const name=active,previous=structuredClone(current().config);const ok=await run(async()=>{data=await api('/api/planning/'+name+'/preview',cfg);});if(ok){if(checkpoint){histories[name].push(previous);if(histories[name].length>30)histories[name].shift();}setMode('inspect');renderPanel();draw();}return ok;}
  function edit(fn){try{const cfg=structuredClone(current().config);fn(cfg);return submit(cfg);}catch(e){error(e);}}
  function mapClick(ev){
    if(active==='layout'||!current()||busy||data.busy)return;
    const p=FRAME.toLocal(ev.latlng.lat,ev.latlng.lng);
    if(['zone','exclude'].includes(mode)){draft.push(p);sketches.clearLayers();L.polyline(draft.map(ll),{...options,color:'#fff',weight:2,interactive:false}).addTo(sketches);return;}
    if(mode==='tree'){edit(cfg=>{cfg.overrides['manual:'+crypto.randomUUID()]={position:p,preset:$('planning-preset').value,layer:+$('planning-layer').value};});return;}
    if(['bus_stop','bus_shelter','banner'].includes(mode)){edit(cfg=>cfg.anchors.push({id:'manual:'+crypto.randomUUID(),kind:mode,position:p,source:'manual',layer:+$('planning-layer').value,yaw:+$('planning-yaw').value}));return;}
    if(mode==='move'&&selected){edit(cfg=>{cfg.overrides[selected]={...cfg.overrides[selected],position:p};});return;}
    selected=null;selectedArea=null;selectionText();draw();
  }
  map.on('click',mapClick);
  document.addEventListener('keydown',ev=>{if(active==='layout'||isEditable(ev.target))return;if(ev.key==='Escape'){ev.preventDefault();ev.stopImmediatePropagation();setMode('inspect');}if((ev.ctrlKey||ev.metaKey)&&['s','z'].includes(ev.key.toLowerCase())){ev.preventDefault();ev.stopImmediatePropagation();if(ev.key.toLowerCase()==='z')$('planning-undo')?.click();else $('planning-generate')?.click();}},true);
  async function load(){try{data=await api('/api/planning');updateNotice();if(active!=='layout')renderPanel();draw();}catch(e){error(e);}}
  load();
  const ready=setInterval(()=>{if(FRAME&&data){clearInterval(ready);draw();}},200);
  setInterval(async()=>{if(busy)return;try{const next=await api('/api/planning');const changed=data?.busy!==next.busy||data?.layout_pending!==next.layout_pending||Object.keys(next.tools).some(k=>data?.tools[k]?.stale!==next.tools[k].stale);data=next;if(changed){updateStatus();draw();}}catch(e){error(e);}},3000);
})();
