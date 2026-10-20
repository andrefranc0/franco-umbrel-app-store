// Painel de configuração do LiveKit para Umbrel. Sem dependências externas.
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const PORT = process.env.PORT || 3000;
const APP_ID = process.env.APP_ID || 'custom-livekit';
const CONFIG_DIR = '/config';
const CONFIG_FILE = path.join(CONFIG_DIR, 'livekit.yaml');
const STATE_FILE = path.join(CONFIG_DIR, 'panel-state.json');

// ---------- helpers ----------
const rand = (chars, n) => {
  let out = '';
  const bytes = crypto.randomBytes(n);
  for (let i = 0; i < n; i++) out += chars[bytes[i] % chars.length];
  return out;
};
const ALNUM = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
const generatePair = () => ({ key: 'API' + rand(ALNUM, 12), secret: rand(ALNUM, 43) });

const defaultState = () => ({
  keys: [generatePair()],
  logLevel: 'info',
  useExternalIp: true,
});

function loadState() {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
  } catch {
    return null;
  }
}

function validate(state) {
  if (!state || !Array.isArray(state.keys) || state.keys.length === 0)
    return 'Informe pelo menos uma chave de API.';
  const seen = new Set();
  for (const k of state.keys) {
    if (!/^[A-Za-z0-9_-]{3,64}$/.test(k.key || ''))
      return `API key inválida: "${k.key}". Use 3-64 caracteres (letras, números, _ ou -).`;
    if (typeof k.secret !== 'string' || k.secret.length < 32 || /[\s"'\\]/.test(k.secret))
      return `API secret da chave "${k.key}" precisa ter ao menos 32 caracteres, sem espaços ou aspas.`;
    if (seen.has(k.key)) return `API key duplicada: "${k.key}".`;
    seen.add(k.key);
  }
  if (!['debug', 'info', 'warn', 'error'].includes(state.logLevel))
    return 'Nível de log inválido.';
  return null;
}

function toYaml(state) {
  const q = (s) => JSON.stringify(String(s)); // JSON string é YAML válido
  const lines = [
    '# Gerado pelo painel do Umbrel. Edite pelo painel, não à mão.',
    'port: 7880',
    `log_level: ${state.logLevel}`,
    'rtc:',
    '  tcp_port: 7881',
    '  udp_port: 7882',
    `  use_external_ip: ${state.useExternalIp ? 'true' : 'false'}`,
    'keys:',
    ...state.keys.map((k) => `  ${q(k.key)}: ${q(k.secret)}`),
    '',
  ];
  return lines.join('\n');
}

function persist(state) {
  fs.mkdirSync(CONFIG_DIR, { recursive: true });
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
  fs.writeFileSync(CONFIG_FILE, toYaml(state));
}

// ---------- docker ----------
function docker(method, p) {
  return new Promise((resolve, reject) => {
    const req = http.request({ socketPath: '/var/run/docker.sock', path: p, method }, (res) => {
      let data = '';
      res.on('data', (c) => (data += c));
      res.on('end', () => resolve({ status: res.statusCode, body: data }));
    });
    req.on('error', reject);
    req.end();
  });
}

async function findLivekit() {
  const filters = encodeURIComponent(JSON.stringify({ label: ['com.docker.compose.service=livekit'] }));
  const r = await docker('GET', `/containers/json?all=1&filters=${filters}`);
  if (r.status !== 200) throw new Error('Docker respondeu ' + r.status);
  const list = JSON.parse(r.body);
  return (
    list.find((c) => c.Names.some((n) => n.includes(APP_ID)) || (c.Labels || {})['com.docker.compose.project'] === APP_ID) ||
    null
  );
}

async function status() {
  try {
    const c = await findLivekit();
    return c ? c.State : 'não encontrado';
  } catch (e) {
    return 'indisponível';
  }
}

async function restartLivekit() {
  const c = await findLivekit();
  if (!c) throw new Error('Container do LiveKit não encontrado.');
  const r = await docker('POST', `/containers/${c.Id}/restart?t=5`);
  if (r.status !== 204) throw new Error('Falha ao reiniciar (HTTP ' + r.status + ').');
}

// ---------- boot ----------
let state = loadState();
if (!state) {
  state = defaultState();
  persist(state);
} else if (!fs.existsSync(CONFIG_FILE)) {
  persist(state);
}

// ---------- http ----------
const send = (res, code, obj) => {
  res.writeHead(code, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(obj));
};
const readBody = (req) =>
  new Promise((resolve, reject) => {
    let d = '';
    req.on('data', (c) => {
      d += c;
      if (d.length > 1e5) reject(new Error('Corpo muito grande'));
    });
    req.on('end', () => {
      try {
        resolve(JSON.parse(d || '{}'));
      } catch (e) {
        reject(e);
      }
    });
  });

http
  .createServer(async (req, res) => {
    try {
      if (req.method === 'GET' && (req.url === '/' || req.url === '/index.html')) {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        return res.end(fs.readFileSync(path.join(__dirname, 'index.html')));
      }
      if (req.method === 'GET' && req.url === '/api/state') {
        return send(res, 200, { ...loadState(), status: await status() });
      }
      if (req.method === 'GET' && req.url === '/api/generate') {
        return send(res, 200, generatePair());
      }
      if (req.method === 'PUT' && req.url === '/api/config') {
        const body = await readBody(req);
        const next = {
          keys: (body.keys || []).map((k) => ({ key: String(k.key || '').trim(), secret: String(k.secret || '').trim() })),
          logLevel: body.logLevel || 'info',
          useExternalIp: !!body.useExternalIp,
        };
        const err = validate(next);
        if (err) return send(res, 400, { error: err });
        persist(next);
        let restarted = true;
        let warning;
        try {
          await restartLivekit();
        } catch (e) {
          restarted = false;
          warning = e.message;
        }
        return send(res, 200, { ok: true, restarted, warning });
      }
      if (req.method === 'POST' && req.url === '/api/restart') {
        await restartLivekit();
        return send(res, 200, { ok: true });
      }
      send(res, 404, { error: 'Não encontrado' });
    } catch (e) {
      send(res, 500, { error: e.message });
    }
  })
  .listen(PORT, '0.0.0.0', () => console.log('Painel LiveKit na porta ' + PORT));
