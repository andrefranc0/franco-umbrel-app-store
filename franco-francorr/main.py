# Francorr v1.0.8 - se este código estiver no seu repositório, o arquivo tem "def read_torrent" e "def call_gdown"
import asyncio
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import quote

import discord
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "database.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
SELF_URL = os.getenv("SELF_URL", "http://franco-francorr_web_1:8090")
BUILD = "1.0.8"  # mesma versão do umbrel-app.yml; aparece no log e na página para conferir o que está rodando

# Pastas de download (a pasta /downloads do Umbrel é a mesma que o Sonarr enxerga)
DOWNLOAD_ROOT = os.getenv("DOWNLOAD_DIR", "/downloads")
FRANCORR_DIR = os.path.join(DOWNLOAD_ROOT, "francorr")
TORRENT_DIR = os.path.join(FRANCORR_DIR, "torrents")  # Sonarr grava os .torrent aqui
COMPLETE_DIR = os.path.join(FRANCORR_DIR, "complete")  # o app entrega os downloads prontos aqui
TMP_DIR = os.path.join(FRANCORR_DIR, ".tmp")
SONARR_UID = int(os.getenv("SONARR_UID", "1000"))
SONARR_GID = int(os.getenv("SONARR_GID", "1000"))

os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------- parser
# Formato esperado (um post pode ter vários episódios/links):
#   Anime: **Nome - S02** (**Nome em inglês - S02**)
#   **Nome - S02E02 - Título do episódio [GRUPO] [pt-br]**
#   Download via [google-drive](https://drive.google.com/...)
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*<?(https?://[^)\s>]+)>?\s*\)")
URL_RE = re.compile(r"https?://[^\s<>()\[\]]+")
EP_RE = re.compile(r"\bS\d{1,2}\s?E\d{1,4}\b", re.I)


def clean_line(line):
    """Tira links e marcações do Discord, devolvendo só o texto legível."""
    line = MD_LINK_RE.sub(" ", line)
    line = URL_RE.sub(" ", line)
    line = re.sub(r"\*+|`|~~|\|\||__", "", line)
    line = re.sub(r"^\s*(#{1,6}\s|>\s|[-•]{1,2}\s)", "", line)
    return re.sub(r"\s+", " ", line).strip(" -–—:")


def message_text(message):
    parts = [message.content or ""]
    for e in message.embeds:
        parts += [e.title or "", e.description or ""]
        parts += [f"{f.name}\n{f.value}" for f in e.fields]
    for snap in getattr(message, "message_snapshots", None) or []:  # mensagens encaminhadas
        parts.append(getattr(snap, "content", "") or "")
    return "\n".join(p for p in parts if p)


def extract_releases(text):
    """Devolve [(titulo, url, texto_de_busca)] para cada link encontrado."""
    lines = text.splitlines()
    cleaned = [clean_line(l) for l in lines]
    search_text = " ".join(c for c in cleaned if c)
    fallback = next((c for c in cleaned if re.search(r"[A-Za-zÀ-ÿ0-9]{3,}", c)), "Untitled Release")[:200]

    releases, last_title = [], None
    for line, plain in zip(lines, cleaned):
        found = []  # (url, titulo_do_proprio_link)
        for m in MD_LINK_RE.finditer(line):
            anchor = clean_line(m.group(1))
            found.append((m.group(2), anchor if EP_RE.search(anchor) else None))
        found += [(u, None) for u in URL_RE.findall(MD_LINK_RE.sub(" ", line))]

        if not found:
            if plain and EP_RE.search(plain):
                last_title = plain
            continue
        for url, own_title in found:
            title = own_title or last_title or (plain if EP_RE.search(plain) else None) or fallback
            releases.append((title, url.rstrip(".,;"), search_text))
    return releases


def index_message(message, self_id):
    """Indexa os links de uma mensagem; ignora o próprio bot e links já vistos."""
    if message.author.id == self_id:
        return 0
    added = 0
    created = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
    with db() as conn:
        for title, url, extra in extract_releases(message_text(message)):
            if conn.execute("SELECT 1 FROM links WHERE url = ?", (url,)).fetchone():
                continue
            conn.execute(
                "INSERT INTO links (title, url, extra, created_at) VALUES (?, ?, ?, ?)",
                (title, url, extra, created),
            )
            added += 1
            print(f"Novo link indexado: {title} -> {url}", flush=True)
    return added


# ---------------------------------------------------------------- banco
def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cols = [r[1] for r in conn.execute("PRAGMA table_info(links)")]
        if "extra" not in cols:  # texto completo do post, usado na busca (nome em inglês etc.)
            conn.execute("ALTER TABLE links ADD COLUMN extra TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                url TEXT,
                status TEXT,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP
            )
            """
        )


init_db()


# ---------------------------------------------------------------- config
# No Umbrel não existe tela para preencher variáveis de ambiente, então o
# token e o canal são configurados pela página inicial do próprio app.
def load_config():
    cfg = {"token": os.getenv("DISCORD_TOKEN", ""), "channel_id": os.getenv("DISCORD_CHANNEL_ID", "")}
    try:
        with open(CONFIG_PATH) as f:
            cfg.update({k: v for k, v in json.load(f).items() if v})
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f)
    os.chmod(CONFIG_PATH, 0o600)


# ---------------------------------------------------------------- downloader
# Como o Sonarr recebe os downloads (cliente "Torrent Blackhole"):
#  1. o indexador entrega um .torrent válido, mas falso, com o link do Drive no campo "comment";
#  2. o Sonarr grava esse arquivo em TORRENT_DIR;
#  3. o app lê o arquivo, baixa o link com gdown e entrega a pasta pronta em COMPLETE_DIR;
#  4. o Sonarr monitora COMPLETE_DIR ("Watch Folder") e importa o episódio.
FAKE_SIZE = 1048576
PIECE_LEN = 262144


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name or "")
    return re.sub(r"\s+", " ", name).strip(" .")[:180] or "download"


def bencode(x):
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode()
    if isinstance(x, bytes):
        return b"%d:%s" % (len(x), x)
    if isinstance(x, list):
        return b"l" + b"".join(bencode(i) for i in x) + b"e"
    if isinstance(x, dict):
        items = sorted(((k.encode() if isinstance(k, str) else k), v) for k, v in x.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(type(x))


def bdecode(data, i=0):
    c = data[i : i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1 : j]), j + 1
    if c == b"l":
        i, out = i + 1, []
        while data[i : i + 1] != b"e":
            v, i = bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i, out = i + 1, {}
        while data[i : i + 1] != b"e":
            k, i = bdecode(data, i)
            out[k], i = bdecode(data, i)
        return out, i + 1
    j = data.index(b":", i)
    start = j + 1
    end = start + int(data[i:j])
    return data[start:end], end


def torrent_info(title, url):
    pieces = b"".join(
        hashlib.sha1(f"{url}#{n}".encode()).digest() for n in range(FAKE_SIZE // PIECE_LEN)
    )
    return {"length": FAKE_SIZE, "name": safe_name(title), "piece length": PIECE_LEN, "pieces": pieces}


def make_torrent(title, url):
    return bencode({"comment": url, "created by": "Francorr", "info": torrent_info(title, url)})


def info_hash(title, url):
    return hashlib.sha1(bencode(torrent_info(title, url))).hexdigest()


def setup_dirs():
    for d in (FRANCORR_DIR, TORRENT_DIR, COMPLETE_DIR, TMP_DIR):
        os.makedirs(d, exist_ok=True)
        try:
            os.chmod(d, 0o777)
            os.chown(d, SONARR_UID, SONARR_GID)
        except OSError:
            pass
    # arquivos rejeitados por uma versão anterior voltam para a fila de leitura
    for fname in os.listdir(TORRENT_DIR):
        if fname.endswith(".torrent.invalid"):
            try:
                os.replace(os.path.join(TORRENT_DIR, fname), os.path.join(TORRENT_DIR, fname[: -len(".invalid")]))
            except OSError:
                pass


def fix_perms(path):
    """Deixa o conteúdo legível/gravável pelo Sonarr (que roda com outro usuário)."""
    for root, _dirs, files in os.walk(path):
        targets = [(root, 0o777)] + [(os.path.join(root, f), 0o666) for f in files]
        for p, mode in targets:
            try:
                os.chmod(p, mode)
                os.chown(p, SONARR_UID, SONARR_GID)
            except OSError:
                pass


def set_job(job_id, status, error=None):
    with db() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, error, job_id),
        )


def read_torrent(path):
    """Devolve (url, nome) de um .torrent do Francorr; tolera arquivos fora do padrão."""
    with open(path, "rb") as f:
        data = f.read()
    try:
        top, _ = bdecode(data)
        return top[b"comment"].decode(), top[b"info"][b"name"].decode()
    except Exception:  # noqa: BLE001
        pass
    # plano B: procura o link do Drive no texto bruto e usa o nome do arquivo como título
    m = re.search(rb"https?://[A-Za-z0-9\-._~:/?#@!$&'()*+,;=%]+", data)
    if m:
        url = re.sub(r"\d{1,3}:[a-z].*$", "", m.group(0).decode(errors="ignore"))
        return url, os.path.splitext(os.path.basename(path))[0]
    raise ValueError(f"arquivo inválido ({len(data)} bytes, início: {data[:60]!r})")


_seen = {}  # caminho -> (tamanho, mtime) da varredura anterior


def scan_torrent_dir():
    """Transforma os .torrent gravados pelo Sonarr em downloads na fila."""
    try:
        names = os.listdir(TORRENT_DIR)
    except FileNotFoundError:
        return
    for fname in names:
        if not fname.lower().endswith(".torrent"):
            continue
        path = os.path.join(TORRENT_DIR, fname)
        try:
            st = os.stat(path)
            age = time.time() - st.st_mtime
            if age < 2:  # ainda sendo gravado
                continue
            # Em discos externos/de rede o conteúdo pode demorar a aparecer: só lê quando o
            # tamanho é maior que zero e igual ao da varredura anterior.
            sig = (st.st_size, st.st_mtime)
            if st.st_size == 0 or _seen.get(path) != sig:
                _seen[path] = sig
                if st.st_size == 0 and age > 60:
                    raise ValueError("arquivo vazio")
                continue
            try:
                url, name = read_torrent(path)
                if not url.startswith("http"):
                    raise ValueError("torrent sem link do Francorr")
            except Exception:  # noqa: BLE001
                if age < 60:  # pode ser leitura antes da hora: tenta de novo na próxima varredura
                    _seen.pop(path, None)
                    continue
                raise
        except Exception as e:  # noqa: BLE001
            print(f"Ignorando {fname}: {e}", flush=True)
            _seen.pop(path, None)
            try:
                os.replace(path, path + ".invalid")
            except OSError:
                pass
            continue
        _seen.pop(path, None)
        with db() as conn:
            last = conn.execute(
                "SELECT status FROM jobs WHERE url = ? ORDER BY id DESC LIMIT 1", (url,)
            ).fetchone()
            ready = os.path.isdir(os.path.join(COMPLETE_DIR, safe_name(name)))
            skip = last and (last[0] in ("queued", "downloading") or (last[0] == "done" and ready))
            if not skip:
                conn.execute(
                    "INSERT INTO jobs (name, url, status, updated_at) VALUES (?, ?, 'queued', CURRENT_TIMESTAMP)",
                    (name, url),
                )
                print(f"Download na fila: {name}", flush=True)
        try:
            os.remove(path)
        except OSError:
            pass


def next_job():
    with db() as conn:
        row = conn.execute(
            "SELECT id, name, url FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE jobs SET status = 'downloading', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row[0],),
            )
    return row


def drive_file_id(url):
    m = re.search(r"/d/([A-Za-z0-9_-]{10,})", url) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", url)
    return m.group(1) if m else None


def call_gdown(fn, **kwargs):
    """Chama o gdown só com os parâmetros que a versão instalada aceita (a API muda entre versões)."""
    import inspect

    params = inspect.signature(fn).parameters
    if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in params}
    return fn(**kwargs)


def run_job(job):
    job_id, name, url = job
    safe = safe_name(name)
    work = os.path.join(TMP_DIR, str(job_id))
    dl = os.path.join(work, "dl")
    try:
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(dl)
        import gdown

        print(f"Baixando: {name}", flush=True)
        if "/folders/" in url:
            call_gdown(gdown.download_folder, url=url, output=dl, quiet=True, remaining_ok=True)
        else:
            fid = drive_file_id(url)
            dl_url = f"https://drive.google.com/uc?id={fid}" if fid else url
            call_gdown(gdown.download, url=dl_url, output=dl + os.sep, quiet=True, fuzzy=True)
        files = [os.path.join(r, f) for r, _, fs in os.walk(dl) for f in fs]
        if not files:
            raise RuntimeError("nada foi baixado (link privado ou cota do Drive excedida?)")

        stage = os.path.join(work, safe)
        os.rename(dl, stage)
        if len(files) == 1:  # arquivo único: usa o título da release como nome (ajuda o Sonarr)
            old = os.path.join(stage, os.path.relpath(files[0], dl))
            new = os.path.join(stage, safe + os.path.splitext(old)[1])
            if old != new:
                os.rename(old, new)
            for root, dirs, _f in os.walk(stage, topdown=False):  # remove subpastas vazias
                for d in dirs:
                    try:
                        os.rmdir(os.path.join(root, d))
                    except OSError:
                        pass
        fix_perms(stage)
        final = os.path.join(COMPLETE_DIR, safe)
        shutil.rmtree(final, ignore_errors=True)
        os.rename(stage, final)  # renomeia a pasta inteira de uma vez: o Sonarr nunca vê pela metade
        set_job(job_id, "done")
        print(f"Download concluído: {final}", flush=True)
    except Exception as e:  # noqa: BLE001
        set_job(job_id, "failed", str(e)[:300])
        print(f"Download falhou ({name}): {e}", flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def downloader_loop():
    try:
        await asyncio.to_thread(setup_dirs)
        with db() as conn:
            conn.execute("UPDATE jobs SET status = 'queued' WHERE status = 'downloading'")
    except Exception as e:  # noqa: BLE001
        print(f"Erro ao preparar pastas de download: {e}", flush=True)
    while True:
        try:
            await asyncio.to_thread(scan_torrent_dir)
            job = await asyncio.to_thread(next_job)
            if job:
                await asyncio.to_thread(run_job, job)
                continue
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"Erro no downloader: {e}", flush=True)
        await asyncio.sleep(5)


# ---------------------------------------------------------------- bot
bot_state = {"client": None, "task": None, "status": "Bot não configurado"}


async def stop_bot():
    client, task = bot_state["client"], bot_state["task"]
    bot_state["client"] = bot_state["task"] = None
    if client:
        try:
            await client.close()
        except Exception:
            pass
    if task:
        task.cancel()


async def start_bot():
    await stop_bot()
    cfg = load_config()
    token = cfg["token"].strip()
    try:
        channel_id = int(str(cfg["channel_id"]).strip() or 0)
    except ValueError:
        channel_id = 0

    if not token or not channel_id:
        bot_state["status"] = "Bot não configurado (falta token ou ID do canal)"
        return

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        bot_state["status"] = f"Conectado como {client.user}"
        print(bot_state["status"], flush=True)
        # importa os últimos posts do canal (sem duplicar o que já foi indexado)
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            total = 0
            async for m in channel.history(limit=200, oldest_first=True):
                total += index_message(m, client.user.id)
            print(f"Histórico importado: {total} link(s) novo(s)", flush=True)
        except Exception as e:  # noqa: BLE001
            bot_state["status"] = f"Conectado como {client.user} (sem acesso ao histórico: {e})"

    @client.event
    async def on_message(message):
        # aceita posts de bots/webhooks (só ignora o próprio bot)
        if message.channel.id == channel_id:
            index_message(message, client.user.id)

    @client.event
    async def on_message_edit(_before, after):
        # embeds e links às vezes só aparecem depois de uma edição
        if after.channel.id == channel_id:
            index_message(after, client.user.id)

    async def runner():
        bot_state["status"] = "Conectando ao Discord..."
        try:
            await client.start(token)
        except discord.LoginFailure:
            bot_state["status"] = "Erro: token inválido"
        except discord.PrivilegedIntentsRequired:
            bot_state["status"] = (
                "Erro: ative 'Message Content Intent' no Discord Developer Portal"
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            bot_state["status"] = f"Erro: {e}"

    bot_state["client"] = client
    bot_state["task"] = asyncio.create_task(runner())


@asynccontextmanager
async def lifespan(_app):
    print(f"Francorr código v{BUILD} iniciado", flush=True)
    await start_bot()
    dl_task = asyncio.create_task(downloader_loop())
    yield
    dl_task.cancel()
    await stop_bot()


APP = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------- página inicial
PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Francorr</title>
<style>
 body{font-family:system-ui,sans-serif;background:#111;color:#eee;max-width:560px;margin:40px auto;padding:0 16px}
 input,button{width:100%;padding:10px;margin:6px 0 14px;border-radius:8px;border:1px solid #444;background:#1c1c1c;color:#eee;box-sizing:border-box}
 button{background:#5b8be6;border:0;cursor:pointer;font-weight:600}
 code{background:#222;padding:2px 6px;border-radius:4px}
 .box{background:#1c1c1c;border-radius:10px;padding:14px;margin-bottom:18px}
</style></head><body>
<h1>Francorr</h1>
<div class="box"><b>Status do bot:</b> <span id="status">__STATUS__</span><br>
<b>Links indexados:</b> __COUNT__</div>

<h3>Configuração do Discord</h3>
<label>Token do bot</label>
<input id="token" type="password" placeholder="__TOKEN_HINT__">
<label>ID do canal</label>
<input id="channel" value="__CHANNEL__" placeholder="123456789012345678">
<button onclick="save()">Salvar e reiniciar bot</button>

<h3>Configurar no Sonarr</h3>
<div class="box">Indexador (Torznab):<br><code>__SELF_URL__/api</code></div>
<div class="box">Cliente de download (Torrent Blackhole):<br>
Torrent Folder: <code>/downloads/francorr/torrents</code><br>
Watch Folder: <code>/downloads/francorr/complete</code></div>

<h3>Downloads</h3>
<div class="box">__JOBS__</div>
<p style="color:#777;font-size:12px">Código v__BUILD__</p>

<script>
async function save(){
  const r = await fetch('config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({token:document.getElementById('token').value,channel_id:document.getElementById('channel').value})});
  const j = await r.json();
  document.getElementById('status').textContent = j.status;
  document.getElementById('token').value = '';
}
</script></body></html>"""


def jobs_html():
    with db() as conn:
        rows = conn.execute("SELECT name, status, error FROM jobs ORDER BY id DESC LIMIT 8").fetchall()
    if not rows:
        return "Nenhum download ainda."
    label = {"queued": "na fila", "downloading": "baixando", "done": "concluído", "failed": "falhou"}
    out = []
    for name, status, error in rows:
        extra = f" - {html.escape(error)}" if error else ""
        out.append(f"<div>{label.get(status, status)}: {html.escape(name or '')}{extra}</div>")
    return "".join(out)


@APP.get("/", response_class=HTMLResponse)
def home():
    cfg = load_config()
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    values = {
        "__STATUS__": html.escape(bot_state["status"]),
        "__COUNT__": str(count),
        "__CHANNEL__": html.escape(str(cfg["channel_id"]), quote=True),
        "__TOKEN_HINT__": "(já salvo - deixe vazio para manter)" if cfg["token"] else "cole o token aqui",
        "__SELF_URL__": html.escape(SELF_URL),
        "__JOBS__": jobs_html(),
        "__BUILD__": BUILD,
    }
    # substituição em uma passada só, para o conteúdo inserido nunca ser reinterpretado
    return re.sub(r"__(?:STATUS|COUNT|CHANNEL|TOKEN_HINT|SELF_URL|JOBS|BUILD)__", lambda m: values[m.group(0)], PAGE)


@APP.post("/config")
async def update_config(request: Request):
    body = await request.json()
    cfg = load_config()
    if body.get("token", "").strip():
        cfg["token"] = body["token"].strip()
    if str(body.get("channel_id", "")).strip():
        cfg["channel_id"] = str(body["channel_id"]).strip()
    save_config(cfg)
    await start_bot()
    await asyncio.sleep(2)  # dá tempo de aparecer erro de login
    return {"status": bot_state["status"]}


# ---------------------------------------------------------------- Torznab
CAPS = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server title="Francorr" />
  <limits max="100" default="50" />
  <searching>
    <search available="yes" supportedParams="q" />
    <tv-search available="yes" supportedParams="q,season,ep" />
  </searching>
  <categories>
    <category id="5000" name="TV" />
  </categories>
</caps>"""


def to_rfc822(ts):
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        dt = datetime.now(timezone.utc)
    return format_datetime(dt)


@APP.get("/api")
def torznab_api(t: str = Query(None), q: str = Query(None), limit: int = Query(50)):
    if t == "caps":
        return Response(content=CAPS, media_type="application/xml")

    limit = max(1, min(limit, 100))
    with db() as conn:
        if q:
            # "Breaking Bad" casa com "Breaking.Bad.S01E01", "Breaking_Bad..." etc.
            pattern = "%" + "%".join(p for p in re.split(r"[\s._-]+", q.strip()) if p) + "%"
            rows = conn.execute(
                "SELECT id, title, url, created_at FROM links "
                "WHERE (title || ' ' || COALESCE(extra, '')) LIKE ? ORDER BY id DESC LIMIT ?",
                (pattern, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, url, created_at FROM links ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    items = ""
    for item_id, title, url, created_at in rows:
        dl = html.escape(f"{SELF_URL}/download?id={item_id}")
        items += f"""
    <item>
      <title>{html.escape(title)}</title>
      <guid>{item_id}</guid>
      <link>{dl}</link>
      <comments>{html.escape(url)}</comments>
      <pubDate>{to_rfc822(created_at)}</pubDate>
      <size>1048576000</size>
      <category>5000</category>
      <enclosure url="{dl}" length="1048576000" type="application/x-bittorrent" />
      <torznab:attr name="category" value="5000" />
      <torznab:attr name="size" value="1048576000" />
      <torznab:attr name="infohash" value="{info_hash(title, url)}" />
      <torznab:attr name="seeders" value="1" />
      <torznab:attr name="peers" value="1" />
    </item>"""

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Francorr Indexer</title>{items}
  </channel>
</rss>"""
    return Response(content=rss, media_type="application/xml")


@APP.get("/download")
def download(id: int, redirect: int = 0):
    with db() as conn:
        row = conn.execute("SELECT title, url FROM links WHERE id = ?", (id,)).fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Link não encontrado"})
    title, url = row
    if redirect:  # ?redirect=1 abre o link original do Drive (uso manual)
        return Response(status_code=302, headers={"Location": url})
    # Para o Sonarr: um .torrent válido que carrega o link do Drive no campo "comment"
    return Response(
        content=make_torrent(title, url),
        media_type="application/x-bittorrent",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(safe_name(title) + ".torrent")},
    )
