/* Demo device writes stay in this page's memory, just like demo pairing. Only the static fixture is fetched. */
let demoBareStore = null;
async function demoBare(method, path, body = {}) {
  if (!demoBareStore) {
    const r = await fetch('/static/demo/bare.json');
    if (!r.ok) throw new Error(`demo fixture bare.json: ${r.status} ${r.statusText}`);
    const data = await r.json();
    demoBareStore = { data, next: 0, template: JSON.parse(JSON.stringify(data.devices[0])) };
  }
  const { data, template } = demoBareStore;
  const route = path.split('?')[0];
  if (method === 'GET' && route === '/api/bare') return JSON.parse(JSON.stringify(data));
  const sessionRoute = /^\/api\/bare\/([^/]+)\/sessions(?:\/(read)|\/([^/]+)\/stop)?$/.exec(route);
  if (sessionRoute && method === 'POST') {
    const device = data.devices.find((d) => d.ts_id === decodeURIComponent(sessionRoute[1]));
    if (!device || !device.check.ok) throw demoError(400, 'Add and check your device first.');
    device.sessions ||= [];
    if (sessionRoute[2]) {
      device.sessions = device.sessions.filter((s) => s.state !== 'stopping');
      return { sessions: JSON.parse(JSON.stringify(device.sessions)) };
    }
    if (sessionRoute[3]) {
      const row = device.sessions.find((s) => s.tmux_name === decodeURIComponent(sessionRoute[3]));
      if (!row || !body.confirm) throw demoError(400, 'Confirm Stop with a second tap.');
      row.state = 'stopping';
      return { status: 'stopping' };
    }
    const agent = body.agent || 'claude';
    if (device.sessions.length >= 20) throw demoError(409, 'This device already has 20 open sessions; stop some first.');
    const modes = { claude: ['default', 'manual', 'acceptEdits', 'plan'], codex: ['workspace-write', 'read-only'] };
    if (!device.check.repos.includes(body.repo) || !['claude', 'codex', 'shell'].includes(agent) ||
        (agent !== 'shell' && (!device.check.rows.some((r) => r.key === agent && r.ok) || !modes[agent].includes(body.mode === undefined ? modes[agent][0] : body.mode))))
      throw demoError(400, 'Choose a checked repository, agent and permission mode.');
    const row = { tmux_name: 'bare-demo-' + (++demoBareStore.next), host: device.ts_id, agent, repo: body.repo, state: 'alive' };
    device.sessions.push(row);
    return { session: { ...row }, status: 'alive' };
  }
  const match = /^\/api\/bare\/([^/]+)(?:\/(check|forget-host-key))?$/.exec(route);
  const id = match ? decodeURIComponent(match[1]) : body.ts_id;
  const saved = data.devices.find((d) => d.ts_id === id);
  const candidate = data.candidates.find((d) => d.ts_id === id);
  if (!candidate) throw demoError(404, 'No such demo device.');
  if (method === 'DELETE' && match && !match[2]) {
    if (body.discard_sessions && body.confirm !== true) throw demoError(400, 'Confirm removing this device and forgetting its sessions here with a second tap.');
    if ((saved && saved.sessions || []).length && !(body.confirm === true && body.discard_sessions === true))
      throw demoError(409, 'Stop its sessions first, or refresh the list, or remove it and forget its sessions here (they keep running on the device).');
    data.devices = data.devices.filter((d) => d.ts_id !== id);
    return { ok: true };
  }
  if (method === 'POST' && match && match[2] === 'forget-host-key') {
    if (saved) saved.check = { ok: false, rows: [{ key: 'ssh', ok: false,
      message: "Host key forgotten. Check again to pin the device's current key.", fix: 'Check again.' }], repos: [], login_url: null };
    return { ok: true };
  }
  if (method !== 'POST' || (route !== '/api/bare' && (!match || match[2] !== 'check'))) throw demoError(404, 'No such demo action.');
  const user = saved ? saved.user : body.user || 'ccagent', folder = saved ? saved.folder : body.folder || 'projects';
  if (!/^[a-z_][a-z0-9_-]{0,31}$/.test(user) || user === 'root') throw demoError(400, 'Choose a dedicated non-root user, such as ccagent.');
  const check = JSON.parse(JSON.stringify(template.check));
  const wording = (s) => s.replaceAll(template.name, candidate.name).replaceAll(template.user, user).replaceAll('~/projects', `~/${folder}`).replaceAll(template.os === 'Linux' ? 'Linux' : 'Darwin (macOS)', candidate.os === 'Linux' ? 'Linux' : 'Darwin (macOS)');
  check.rows = check.rows.map((row) => ({ ...row, message: wording(row.message), fix: wording(row.fix) }));
  const device = { ...candidate, user, folder, owner: template.owner, check, sessions: saved ? saved.sessions || [] : [] };
  data.devices = [...data.devices.filter((d) => d.ts_id !== id), device];
  return { added: true, device, check };
}

