import asyncio
import html
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import format_datetime

import discord
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "database.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
SELF_URL = os.getenv("SELF_URL", "http://franco-francorr_web_1:8090")
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
    await start_bot()
    yield
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

<script>
async function save(){
  const r = await fetch('config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({token:document.getElementById('token').value,channel_id:document.getElementById('channel').value})});
  const j = await r.json();
  document.getElementById('status').textContent = j.status;
  document.getElementById('token').value = '';
}
</script></body></html>"""


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
    }
    page = PAGE
    for key, val in values.items():
        page = page.replace(key, val)
    return page


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
    </item>"""

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Francorr Indexer</title>{items}
  </channel>
</rss>"""
    return Response(content=rss, media_type="application/xml")


@APP.get("/download")
def download(id: int):
    with db() as conn:
        row = conn.execute("SELECT url FROM links WHERE id = ?", (id,)).fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Link não encontrado"})
    return Response(status_code=302, headers={"Location": row[0]})
