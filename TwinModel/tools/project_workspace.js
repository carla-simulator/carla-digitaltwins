// Project transport is installed before the canonical editor initializes.
(() => {
  const originalFetch = window.fetch.bind(window);
  let token;
  const ready = originalFetch('/api/project').then(async response => {
    const value = await response.json(); token = value.token; return value;
  });
  window.fetch = async (input, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    const mutation = typeof input === 'string' && input.startsWith('/api/') && ['POST', 'PUT', 'DELETE'].includes(method);
    if (mutation) {
      await ready;
      const headers = new Headers(options.headers); headers.set('If-Match', token);
      options = {...options, headers};
    }
    const response = await originalFetch(input, options);
    if (mutation && response.ok) token = response.headers.get('X-Project-Revision') || token;
    return response;
  };
  document.addEventListener('DOMContentLoaded', async () => {
    const panel = document.createElement('details'); panel.id = 'project-panel';
    panel.style.cssText = 'position:absolute;z-index:1100;top:16px;right:16px;background:#182432;color:#eef5ff;border:1px solid #64778c;border-radius:8px;padding:14px;width:360px;max-width:85vw;max-height:75vh;overflow:auto;font:13px/1.5 sans-serif';
    panel.innerHTML = `<summary>Project build &amp; apply</summary><p id="project-state"></p>
      <button id="project-build">Build model &amp; exports</button>
      <button id="project-review">Review apply stages</button>
      <button id="project-apply">Apply to Unreal</button>
      <button id="project-reload">Reload saved map in CARLA</button>
      <a href="/api/project/export" download="map.twinproject">Export project</a>
      <pre id="project-stages" style="white-space:pre-wrap"></pre>
      <details><summary>Diagnostics and build log</summary><pre id="project-log" style="white-space:pre-wrap"></pre></details>`;
    document.body.appendChild(panel);
    for (const button of panel.querySelectorAll('button')) button.style.cssText = 'background:#cee0ff;color:#172c46;border:0;border-radius:5px;padding:8px;margin:4px 4px 4px 0;cursor:pointer';
    panel.querySelector('a').style.color = '#cee0ff';
    const state = document.getElementById('project-state');
    const log = document.getElementById('project-log');
    const stages = document.getElementById('project-stages');
    async function refresh() {
      try {
        const response = await originalFetch('/api/project');
        const value = await response.json();
        state.textContent = `${value.project} → ${value.target_level?.split('/').pop() || value.target} · ${value.job.running ? 'Running' : value.dirty ? 'Unsaved layout edits' : 'Saved authoring inputs'}`;
        panel.querySelector('a').download = value.project + '.twinproject';
        const labels = {'model':'Map model','export':'Geometry exports','validate.model':'Model checks','unreal.geometry':'Unreal map geometry','unreal.traffic':'Traffic controls','occupancy.export':'Physical obstacles','vegetation.plan':'Plant placement','vegetation.bake':'Save vegetation','furniture.plan':'Furniture placement','furniture.bake':'Save furniture','validate.target':'Saved map checks'};
        stages.textContent = value.stages.map(s => `${s.state === 'current' ? '✓' : '○'} ${labels[s.stage] || s.stage} · ${s.state}`).join('\n');
        log.textContent = value.diagnostics.map(d => `${(d.ids || []).join(', ')}: ${d.message}`).join('\n') + '\n' + (value.job.log || '');
        for (const id of ['project-build','project-apply','project-reload']) document.getElementById(id).disabled = value.job.running || value.dirty;
      } catch (error) { log.textContent = error.message; }
    }
    async function start(action) {
      const response = await fetch('/api/project/run', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action})});
      const value = await response.json();
      if (!response.ok) { log.textContent = value.error; return; }
      await refresh();
    }
    document.getElementById('project-build').onclick = () => start('build');
    document.getElementById('project-apply').onclick = () => start('apply');
    document.getElementById('project-reload').onclick = () => start('runtime');
    document.getElementById('project-review').onclick = refresh;
    await ready; await refresh(); setInterval(refresh, 3000);
  });
})();
