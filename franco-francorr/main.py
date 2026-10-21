# Francorr v1.0.12 - se este código estiver no seu repositório, o arquivo tem "def download_discord_attachment"
import asyncio
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import quote, urlparse

import discord
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "database.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
SELF_URL = os.getenv("SELF_URL", "http://franco-francorr_web_1:8090")
BUILD = "1.0.12"  # mesma versão do umbrel-app.yml; aparece no log e na página para conferir o que está rodando

# Pastas de download (a pasta /downloads do Umbrel é a mesma que o Sonarr enxerga)
DOWNLOAD_ROOT = os.getenv("DOWNLOAD_DIR", "/downloads")
FRANCORR_DIR = os.path.join(DOWNLOAD_ROOT, "francorr")
TORRENT_DIR = os.path.join(FRANCORR_DIR, "torrents")  # Sonarr grava os .torrent aqui
COMPLETE_DIR = os.path.join(FRANCORR_DIR, "complete")  # o app entrega os downloads prontos aqui
TMP_DIR = os.path.join(FRANCORR_DIR, ".tmp")
SONARR_UID = int(os.getenv("SONARR_UID", "1000"))
SONARR_GID = int(os.getenv("SONARR_GID", "1000"))

os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------- links
DRIVE_HOSTS = ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".webm", ".mov", ".ts", ".m2ts", ".wmv", ".flv", ".m4v"}


def drive_file_id(url):
    m = re.search(r"/d/([A-Za-z0-9_-]{10,})", url) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", url)
    return m.group(1) if m else None


def link_key(url):
    """Identifica o mesmo arquivo mesmo com variações do link (?usp=sharing, ?usp=drive_link...)."""
    fid = drive_file_id(url)
    if fid:
        return "drive:" + fid
    m = re.search(r"/folders/([A-Za-z0-9_-]{10,})", url)
    if m:
        return "folder:" + m.group(1)
    return url.split("#", 1)[0]


def host_of(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


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
        cols = {r[1] for r in conn.execute("PRAGMA table_info(links)")}
        for name, ddl in (
            ("extra", "TEXT"),  # texto do post, usado na busca (nome em inglês etc.)
            ("status", "TEXT DEFAULT 'ok'"),  # ok = visível ao Sonarr | review = aguardando revisão | ignored
            ("reason", "TEXT"),  # por que foi para revisão
            ("message_id", "TEXT"),  # mensagem do Discord de onde veio
            ("ukey", "TEXT"),  # chave do link (id do arquivo no Drive)
            ("edited", "INTEGER DEFAULT 0"),  # 1 = você mexeu à mão; o app não sobrescreve mais
            ("kind", "TEXT DEFAULT 'drive'"),  # drive | discord_attachment | other
        ):
            if name not in cols:
                conn.execute(f"ALTER TABLE links ADD COLUMN {name} {ddl}")
        conn.execute("UPDATE links SET status = 'ok' WHERE status IS NULL")
        conn.execute("UPDATE links SET edited = 0 WHERE edited IS NULL")
        for lid, url in conn.execute("SELECT id, url FROM links WHERE ukey IS NULL").fetchall():
            conn.execute("UPDATE links SET ukey = ? WHERE id = ?", (link_key(url or ""), lid))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                text TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        mcols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        if "attachments" not in mcols:  # JSON [{id, filename, url}] dos arquivos anexados ao post
            conn.execute("ALTER TABLE messages ADD COLUMN attachments TEXT DEFAULT '[]'")
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
        jcols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        if "kind" not in jcols:
            conn.execute("ALTER TABLE jobs ADD COLUMN kind TEXT DEFAULT 'drive'")


init_db()


# ---------------------------------------------------------------- parser
# Entende posts em vários formatos. Exemplos que reconhece (um post pode ter vários episódios/links):
#   Anime: **Nome - S02**            -> cabeçalho com nome e temporada
#   **Nome - S02E02 - Título [GRUPO]** | Nome 2x05 | Nome - Temporada 2 Episódio 5 | Episódio 05 | EP05
#   Download via [google-drive](https://drive.google.com/...)
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*<?(https?://[^)\s>]+)>?\s*\)")
URL_RE = re.compile(r"https?://[^\s<>()\[\]]+")
EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
MENTION_RE = re.compile(r"<(?:@[!&]?|#)\d+>")
LABEL_RE = re.compile(r"^(?:anime|s[eé]rie|series|show|dorama|filme|t[ií]tulo|title|nome|name)\s*[:：]\s*(.+)$", re.I)
PACK_RE = re.compile(r"\b(?:completa|completo|complete|batch|pack|temporada inteira|todos os epis[oó]dios)\b", re.I)

SE_RE = re.compile(r"\bS(\d{1,2})[\s._-]?E(\d{1,4})((?:[\s._-]?-?[\s._-]?E\d{1,4})*)(?!\d)", re.I)
WORD_EP = r"(?:epis[oó]dio|episode|ep|cap[ií]tulo|cap)\.?"
WORDS_RE = re.compile(
    r"\b(?:temporada|temp\.?|season)\s*[:#]?\s*(\d{1,2})\b[\s,;:.\-–|·]*" + WORD_EP + r"\s*#?\s*(\d{1,4})\b", re.I
)
ORD_RE = re.compile(
    r"\b(\d{1,2})\s*[ªº°]?\s*(?:temporada|season)\b[\s,;:.\-–|·]*" + WORD_EP + r"\s*#?\s*(\d{1,4})\b", re.I
)
NXM_RE = re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.I)
EPONLY_RE = re.compile(r"\b" + WORD_EP + r"\s*#?\s*(\d{1,4})\b|\bE(\d{1,3})\b", re.I)
WEAK_RE = re.compile(r"(?:^|\s)[-–—]\s*(\d{1,3})(?:v\d)?(?=\s|\[|\(|$)")

RES_RE = re.compile(r"\b(?:(2160|1440|1080|720|576|480)[pi]|(4k|uhd|fhd))\b", re.I)
SRC_RE = re.compile(r"\b(web[-. ]?dl|webrip|blu[-. ]?ray|bdrip|brrip|hdtv|dvdrip|hdrip|remux)\b", re.I)
SRC_NAMES = {
    "webdl": "WEB-DL", "webrip": "WEBRip", "bluray": "BluRay", "bdrip": "BDRip",
    "brrip": "BRRip", "hdtv": "HDTV", "dvdrip": "DVDRip", "hdrip": "HDRip", "remux": "Remux",
}


def clean_text(line):
    """Tira links, menções e marcações do Discord, devolvendo só o texto legível."""
    line = MD_LINK_RE.sub(" ", line or "")
    line = EMOJI_RE.sub(" ", line)
    line = MENTION_RE.sub(" ", line)
    line = URL_RE.sub(" ", line)
    line = re.sub(r"\*+|`+|~~|\|\||__", "", line)
    line = re.sub(r"^\s*(?:#{1,6}\s+|>\s*|[-•·▪►▶→]+\s+)", "", line)
    return re.sub(r"\s+", " ", line).strip(" -–—:|·")


def is_decorative(plain):
    """Linhas de enfeite: separadores, só emoji, ou letras espaçadas (E P I S O D I O)."""
    if not re.search(r"[A-Za-zÀ-ÿ0-9]", plain):
        return True
    toks = plain.split()
    return len(toks) >= 6 and sum(len(t) == 1 for t in toks) / len(toks) >= 0.7


def has_letters(text):
    return len(re.findall(r"[A-Za-zÀ-ÿ]", text or "")) >= 2


def season_from(text):
    """Acha 'S2', 'Temporada 2', '2ª Temporada', 'Season 2'. Devolve (temporada, texto sem a marcação)."""
    pats = (
        r"\bS(\d{1,2})\b(?![\s._-]?E\d)",
        r"\b(?:temporada|temp\.?|season)\s*[:#]?\s*(\d{1,2})\b",
        r"\b(\d{1,2})\s*[ªº°]?\s*(?:temporada|season)\b",
        r"\b(\d{1,2})(?:st|nd|rd|th)\s+season\b",
    )
    for p in pats:
        m = re.search(p, text or "", re.I)
        if m:
            rest = re.sub(r"\s+", " ", text[: m.start()] + " " + text[m.end():])
            return int(m.group(1)), rest.strip(" -–—:|·()[]")
    return None, text or ""


def parse_episode(text):
    """Procura o episódio num texto. Devolve dict ou None."""
    if not text:
        return None

    def mk(m, season, ep, explicit, more=(), weak=False, end=None):
        return {"season": season, "ep": ep, "more": list(more), "start": m.start(),
                "end": m.end() if end is None else end, "explicit": explicit, "weak": weak}

    m = SE_RE.search(text)
    if m:
        more = [int(n) for n in re.findall(r"E(\d{1,4})", m.group(3), re.I)]
        return mk(m, int(m.group(1)), int(m.group(2)), True, more)
    for rx in (WORDS_RE, ORD_RE, NXM_RE):
        m = rx.search(text)
        if m:
            return mk(m, int(m.group(1)), int(m.group(2)), True)
    m = EPONLY_RE.search(text)
    if m:
        return mk(m, None, int(m.group(1) or m.group(2)), False)
    m = WEAK_RE.search(text)
    if m:
        return mk(m, None, int(m.group(1)), False, weak=True)
    return None


def marker_str(season, ep, more=()):
    s = f"S{season:02d}E{ep:02d}"
    for n in more:
        s += f"-E{n:02d}"
    return s


def clean_name(text):
    text = re.sub(r"^[^\w\[(]+", "", text or "")  # emojis/símbolos no começo
    text = re.sub(r"^(?:download|baixar|assistir|link|dl)\s*[:\-–]\s*", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", PACK_RE.sub(" ", text))  # "completa", "batch"... não fazem parte do nome
    return text.strip(" -–—:|·")


def build_title(plain, ep, series_hdr, season_hdr):
    """Monta 'Série - SxxEyy - resto' a partir da linha do episódio. Devolve (título, [motivos de revisão])."""
    name = clean_name(plain[: ep["start"]])
    sn, name = season_from(name) if name else (None, "")
    if not has_letters(name):
        name = ""
    rest = plain[ep["end"]:].strip(" -–—:|·")
    reasons = []
    series = name or series_hdr or ""
    if not series:
        reasons.append("nome da série não encontrado")
    season = ep["season"] if ep["season"] is not None else (sn if sn is not None else season_hdr)
    if ep["weak"]:
        reasons.append("só o número do episódio foi encontrado (confirme)")
    if season is None:
        season = 1
        reasons.append("temporada não encontrada (usei S01)")
    title = " - ".join(p for p in (series, marker_str(season, ep["ep"], ep["more"]), rest) if p)
    return title, reasons


def find_quality(text):
    res = src = None
    m = RES_RE.search(text or "")
    if m:
        res = (m.group(1) + "p") if m.group(1) else {"4k": "2160p", "uhd": "2160p", "fhd": "1080p"}[m.group(2).lower()]
    m = SRC_RE.search(text or "")
    if m:
        src = SRC_NAMES[re.sub(r"[-. ]", "", m.group(1).lower())]
    return res, src


def apply_quality(title, text, tag):
    """Garante resolução/fonte no título: usa o que o post diz; se não diz nada, usa a etiqueta padrão."""
    t_res, t_src = find_quality(title)
    m_res, m_src = find_quality(text)
    add = []
    if not t_res and m_res:
        add.append(m_res)
    if not t_src and m_src:
        add.append(m_src)
    if not add and not (t_res or t_src) and tag:
        add.append(tag)
    return (title + " " + " ".join(add)).strip() if add else title


def attachment_plain(filename):
    """Transforma o nome de um arquivo anexado em texto legível, tipo uma linha de post."""
    name = os.path.splitext(filename or "")[0]
    name = re.sub(r"[._]+", " ", name)
    return clean_name(re.sub(r"\s+", " ", name).strip())


def fallback_title(rows):
    for r in rows:
        if not r["deco"] and len(re.findall(r"[A-Za-zÀ-ÿ0-9]", r["plain"])) >= 6:
            return r["plain"][:200]
    return "Untitled Release"


def message_text(message):
    parts = [message.content or ""]
    for e in message.embeds:
        parts += [e.title or "", e.description or ""]
        parts += [f"{f.name}\n{f.value}" for f in e.fields]
    for snap in getattr(message, "message_snapshots", None) or []:  # mensagens encaminhadas
        parts.append(getattr(snap, "content", "") or "")
    return "\n".join(p for p in parts if p)


def extract_releases(text, quality_tag="", attachments=None):
    """Devolve uma lista de dicts {title, url, extra, status, reason, kind}, um por link/anexo encontrado.
    status: 'ok' (entendi), 'review' (precisa de revisão) ou 'ignored' (link de serviço não suportado).
    attachments: lista opcional de (id, nome_do_arquivo, url) dos arquivos de vídeo anexados ao post."""
    rows = []
    for raw in (text or "").splitlines():
        links = [(m.group(2), clean_text(m.group(1))) for m in MD_LINK_RE.finditer(raw)]
        links += [(u, "") for u in URL_RE.findall(MD_LINK_RE.sub(" ", raw))]
        plain = clean_text(raw)
        rows.append({"plain": plain, "links": links, "deco": is_decorative(plain)})
    for att_id, filename, url in (attachments or []):
        # entra como se fosse mais uma linha do post, depois das demais: o resto do texto
        # (cabeçalho com série/temporada, linhas de cima) continua servindo de contexto para ela
        plain = attachment_plain(filename)
        rows.append({"plain": plain, "links": [(url, plain)], "deco": False, "att_id": att_id})
    if not any(r["links"] for r in rows):
        return []

    anchors = [a for r in rows for _u, a in r["links"] if a and parse_episode(a)]
    search_text = " ".join([r["plain"] for r in rows if not r["deco"]] + anchors)[:1500]
    eps = [parse_episode(r["plain"]) for r in rows]

    # cabeçalho: nome da série e temporada (linha "Anime: ...", ou linha só com a temporada)
    series_hdr = season_hdr = None
    for r, e in zip(rows, eps):
        if r["deco"] or e:
            continue
        m = LABEL_RE.match(r["plain"])
        if m:
            val = re.split(r"\s*[\(\[]", m.group(1), 1)[0]
            s, val = season_from(val)
            val = clean_name(val)
            if has_letters(val):
                series_hdr, season_hdr = val, s
                break
    if series_hdr is None:
        for r, e in zip(rows, eps):
            if r["deco"] or e or r["links"]:
                continue
            s, rem = season_from(r["plain"])
            rem = clean_name(rem)
            if s is not None and has_letters(rem):
                series_hdr, season_hdr = rem, s
                break
    if season_hdr is None:
        for r, e in zip(rows, eps):
            if r["deco"] or e or r["links"]:
                continue
            s, rem = season_from(r["plain"])
            if s is not None and not has_letters(rem):
                season_hdr = s
                break
    if series_hdr is None:  # usa o nome que aparece numa linha de episódio
        for r, e in zip(rows, eps):
            if e and not e["weak"]:
                n = clean_name(r["plain"][: e["start"]])
                _s, n = season_from(n)
                if has_letters(n):
                    series_hdr = n
                    break

    def usable(e, weak_ok):
        return e is not None and (weak_ok or not e["weak"])

    def pick(i, anchor, weak_ok):
        a = parse_episode(anchor) if anchor else None
        if usable(a, weak_ok):
            return anchor, a
        if usable(eps[i], weak_ok):
            return rows[i]["plain"], eps[i]
        for j in range(i - 1, max(-1, i - 9), -1):  # linhas de cima, a mais próxima primeiro
            if usable(eps[j], weak_ok):
                return rows[j]["plain"], eps[j]
        for j in range(i + 1, min(len(rows), i + 4)):  # título depois do link
            if usable(eps[j], weak_ok):
                return rows[j]["plain"], eps[j]
        cands = [(e["season"], e["ep"]) for e in eps if usable(e, weak_ok)]
        if len(set(cands)) == 1:  # o post inteiro fala de um único episódio
            j = next(k for k, e in enumerate(eps) if usable(e, weak_ok))
            return rows[j]["plain"], eps[j]
        return None

    pack_ok = bool(series_hdr and season_hdr is not None and PACK_RE.search(search_text))
    out = []
    for i, r in enumerate(rows):
        is_att = "att_id" in r
        for url, anchor in r["links"]:
            url = url.rstrip(".,;")
            host = host_of(url)
            rel = {"url": url, "extra": search_text}
            if is_att:
                rel["key"] = "att:" + r["att_id"]
                rel["kind"] = "discord_attachment"
            elif host in DRIVE_HOSTS:
                rel["key"] = link_key(url)
                rel["kind"] = "drive"
            else:
                rel.update(title=fallback_title(rows), status="ignored", kind="other", key=link_key(url),
                           reason=f"serviço não suportado ({host or 'link inválido'}) - só Google Drive e arquivos enviados no Discord são baixados")
                out.append(rel)
                continue
            reasons = []
            if pack_ok and "/folders/" in url:  # pasta do Drive + "temporada completa"
                title = f"{series_hdr} S{season_hdr:02d}"
            else:
                found = pick(i, anchor, False) or pick(i, anchor, True)
                if found:
                    title, reasons = build_title(found[0], found[1], series_hdr, season_hdr)
                elif pack_ok:
                    title = f"{series_hdr} S{season_hdr:02d}"
                else:
                    reasons.append("episódio não encontrado no post")
                    title = (series_hdr + (f" S{season_hdr:02d}" if season_hdr is not None else "")) if series_hdr else fallback_title(rows)
            rel["title"] = apply_quality(title, search_text, quality_tag)
            rel["status"] = "review" if reasons else "ok"
            rel["reason"] = "; ".join(reasons) or None
            out.append(rel)
    return out


def clean_title(title):
    title = re.sub(r"[\x00-\x1f]+", " ", str(title or ""))
    return re.sub(r"\s+", " ", title).strip()[:250]


def apply_message(conn, mid, text, created, qtag, attachments=None):
    """Aplica o parser a uma mensagem: cria/atualiza os links/anexos dela. Não mexe no que você corrigiu à mão."""
    new, keys = 0, []
    for rel in extract_releases(text, qtag, attachments):
        key = rel["key"]
        keys.append(key)
        row = conn.execute("SELECT id, edited FROM links WHERE ukey = ?", (key,)).fetchone()
        if row:
            if row[1]:
                conn.execute("UPDATE links SET message_id = ? WHERE id = ?", (mid, row[0]))
            else:
                conn.execute(
                    "UPDATE links SET title = ?, url = ?, extra = ?, status = ?, reason = ?, kind = ?, message_id = ? WHERE id = ?",
                    (rel["title"], rel["url"], rel["extra"], rel["status"], rel["reason"], rel["kind"], mid, row[0]),
                )
            continue
        conn.execute(
            "INSERT INTO links (title, url, extra, status, reason, kind, message_id, ukey, edited, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (rel["title"], rel["url"], rel["extra"], rel["status"], rel["reason"], rel["kind"], mid, key, created),
        )
        new += 1
        if rel["status"] == "ok":
            print(f"Novo link indexado: {rel['title']} -> {rel['url']}", flush=True)
        elif rel["status"] == "review":
            print(f"Link para revisar ({rel['reason']}): {rel['title']} -> {rel['url']}", flush=True)
    # links que sumiram da mensagem (editada) e que ninguém corrigiu à mão
    if keys:
        marks = ",".join("?" * len(keys))
        conn.execute(f"DELETE FROM links WHERE message_id = ? AND edited = 0 AND ukey NOT IN ({marks})", (mid, *keys))
    else:
        conn.execute("DELETE FROM links WHERE message_id = ? AND edited = 0", (mid,))
    return new


def process_message(mid, text, created, attachments=None):
    """Guarda o texto e os anexos originais da mensagem e (re)indexa os links/arquivos dela."""
    attachments = attachments or []
    qtag = load_config().get("quality_tag", "")
    with db() as conn:
        known = conn.execute("SELECT 1 FROM messages WHERE message_id = ?", (mid,)).fetchone()
        if not known and not attachments and not (URL_RE.search(text or "") or MD_LINK_RE.search(text or "")):
            return 0
        conn.execute(
            "INSERT OR REPLACE INTO messages (message_id, text, created_at, updated_at, attachments) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)",
            (mid, text, created, json.dumps(attachments)),
        )
        return apply_message(conn, mid, text, created, qtag, attachments)


def reprocess_all():
    """Reaplica o parser a todos os textos guardados (depois de melhorar o parser ou trocar a etiqueta)."""
    qtag = load_config().get("quality_tag", "")
    with db() as conn:
        rows = conn.execute("SELECT message_id, text, created_at, attachments FROM messages").fetchall()
        for mid, text, created, attachments in rows:
            try:
                atts = json.loads(attachments or "[]")
            except json.JSONDecodeError:
                atts = []
            apply_message(conn, mid, text, created, qtag, atts)
    return len(rows)


def index_message(message, self_id):
    """Indexa os links e arquivos anexados de uma mensagem do Discord; ignora o próprio bot."""
    if message.author.id == self_id:
        return 0
    created = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
    atts = [
        [str(a.id), a.filename, a.url]
        for a in message.attachments
        if os.path.splitext(a.filename or "")[1].lower() in VIDEO_EXTS
    ]
    return process_message(str(message.id), message_text(message), created, atts)


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


# ---------------------------------------------------------------- login do painel
# O painel (página inicial e suas ações) exige usuário e senha. Padrão: admin / admin (troque na própria página).
# A senha nunca é guardada em texto: só um hash PBKDF2-SHA256 com sal aleatório. As rotas /api e /download
# (Torznab e .torrent) continuam abertas, porque o Sonarr precisa delas sem login.
AUTH_PATH = os.path.join(DATA_DIR, "auth.json")
COOKIE = "francorr_session"
SESSION_SECONDS = 7 * 24 * 3600
PBKDF2_ROUNDS = 200_000
_fails = {"count": 0, "until": 0.0}


def hash_password(password, salt=None, rounds=PBKDF2_ROUNDS):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return {"salt": salt.hex(), "hash": digest.hex(), "rounds": rounds}


def load_auth():
    try:
        with open(AUTH_PATH) as f:
            auth = json.load(f)
        if auth.get("user") and auth.get("hash") and auth.get("salt") and auth.get("secret"):
            return auth
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    auth = {"user": "admin", "default": True, "secret": secrets.token_hex(32), **hash_password("admin")}
    save_auth(auth)
    return auth


def save_auth(auth):
    tmp = AUTH_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(auth, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_PATH)


def check_credentials(user, password):
    auth = load_auth()
    calc = hash_password(password or "", bytes.fromhex(auth["salt"]), auth.get("rounds", PBKDF2_ROUNDS))["hash"]
    ok_pass = hmac.compare_digest(calc, auth["hash"])
    ok_user = hmac.compare_digest((user or "").encode("utf-8"), auth["user"].encode("utf-8"))
    return ok_pass and ok_user


def session_sig(auth, expires):
    # trocar usuário/senha muda o hash e derruba as sessões antigas
    key = (auth["secret"] + auth["hash"]).encode()
    return hmac.new(key, f"{auth['user']}|{expires}".encode(), hashlib.sha256).hexdigest()


def make_session():
    auth = load_auth()
    expires = int(time.time()) + SESSION_SECONDS
    return f"{expires}.{session_sig(auth, expires)}"


def cookie_header(value, max_age=SESSION_SECONDS):
    return f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"


def is_logged_in(request):
    try:
        jar = SimpleCookie(request.headers.get("cookie", ""))
        value = jar[COOKIE].value
        expires, sig = value.split(".", 1)
        if int(expires) < time.time():
            return False
        return hmac.compare_digest(sig, session_sig(load_auth(), int(expires)))
    except (KeyError, ValueError, AttributeError, TypeError):
        return False


def guard(request):
    """Devolve uma resposta 401 se não estiver logado; None se pode seguir."""
    if is_logged_in(request):
        return None
    return JSONResponse(status_code=401, content={"error": "não autenticado"})


# ---------------------------------------------------------------- atualização do código
# O botão "Buscar atualização" baixa o main.py do GitHub, confere que é Python válido e mais novo que o atual,
# guarda em DATA_DIR/update/main.py e reinicia o app. O docker-compose usa essa cópia ao iniciar
# (FRANCORR_SELF_UPDATE=1 indica que o compose já foi ajustado para isso).
UPDATE_URL = os.getenv(
    "UPDATE_URL",
    "https://raw.githubusercontent.com/andrefranc0/franco-umbrel-app-store/master/franco-francorr/main.py",
)
UPDATE_DIR = os.path.join(DATA_DIR, "update")
UPDATE_FILE = os.path.join(UPDATE_DIR, "main.py")
SELF_UPDATE_READY = os.getenv("FRANCORR_SELF_UPDATE") == "1"


def vtuple(v):
    return tuple(int(n) for n in re.findall(r"\d+", v or "0"))


def fetch_remote_code():
    req = urllib.request.Request(UPDATE_URL, headers={"User-Agent": "francorr-updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8")


def self_update():
    """Devolve (mensagem, reiniciar?)."""
    try:
        code = fetch_remote_code()
    except Exception as e:  # noqa: BLE001
        return f"não consegui baixar do GitHub: {e}", False
    m = re.search(r'^BUILD = "([\d.]+)"', code, re.M)
    if not m or "APP = FastAPI" not in code:
        return "o arquivo baixado não parece ser o main.py do Francorr (nada foi alterado)", False
    try:
        compile(code, "main.py", "exec")
    except SyntaxError as e:
        return f"o arquivo baixado tem erro de sintaxe ({e}); nada foi alterado", False
    remote = m.group(1)
    if vtuple(remote) <= vtuple(BUILD):
        return f"você já está na versão mais nova (v{BUILD})", False
    os.makedirs(UPDATE_DIR, exist_ok=True)
    tmp = UPDATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(code)
    os.replace(tmp, UPDATE_FILE)
    if not SELF_UPDATE_READY:
        return (f"v{remote} baixada, mas o docker-compose ainda não está ajustado para usá-la "
                "(veja as instruções); nada vai mudar ao reiniciar"), False
    return f"v{remote} baixada. Reiniciando o app...", True


def schedule_restart():
    # sai com erro; o compose tem restart: on-failure e sobe o container de novo já com o código novo
    asyncio.get_running_loop().call_later(1.5, os._exit, 1)


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


def make_torrent(title, url, kind="drive"):
    return bencode({"comment": url, "kind": kind, "created by": "Francorr", "info": torrent_info(title, url)})


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
    """Devolve (url, nome, kind) de um .torrent do Francorr; tolera arquivos fora do padrão."""
    with open(path, "rb") as f:
        data = f.read()
    try:
        top, _ = bdecode(data)
        kind = top[b"kind"].decode() if b"kind" in top else "drive"
        return top[b"comment"].decode(), top[b"info"][b"name"].decode(), kind
    except Exception:  # noqa: BLE001
        pass
    # plano B: procura o link do Drive no texto bruto e usa o nome do arquivo como título
    m = re.search(rb"https?://[A-Za-z0-9\-._~:/?#@!$&'()*+,;=%]+", data)
    if m:
        url = re.sub(r"\d{1,3}:[a-z].*$", "", m.group(0).decode(errors="ignore"))
        return url, os.path.splitext(os.path.basename(path))[0], "drive"
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
                url, name, kind = read_torrent(path)
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
                    "INSERT INTO jobs (name, url, kind, status, updated_at) VALUES (?, ?, ?, 'queued', CURRENT_TIMESTAMP)",
                    (name, url, kind),
                )
                print(f"Download na fila: {name}", flush=True)
        try:
            os.remove(path)
        except OSError:
            pass


def next_job():
    with db() as conn:
        row = conn.execute(
            "SELECT id, name, url, kind FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE jobs SET status = 'downloading', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row[0],),
            )
    return row


def call_gdown(fn, **kwargs):
    """Chama o gdown só com os parâmetros que a versão instalada aceita (a API muda entre versões)."""
    import inspect

    params = inspect.signature(fn).parameters
    if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in params}
    return fn(**kwargs)


def refresh_attachment_urls(urls):
    """Pede ao Discord links novos para anexos cuja assinatura expirou (a parte fixa do link não muda)."""
    token = load_config().get("token", "").strip()
    if not token:
        raise RuntimeError("token do bot não configurado, não dá para renovar o link do anexo")
    body = json.dumps({"attachment_urls": list(urls)}).encode()
    req = urllib.request.Request(
        "https://discord.com/api/v10/attachments/refresh-urls",
        data=body,
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return {item["original"]: item["refreshed"] for item in data.get("refreshed_urls", [])}


def _http_download(url, dest_path):
    req = urllib.request.Request(url, headers={"User-Agent": "Francorr"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest_path, "wb") as f:
        shutil.copyfileobj(resp, f)


def download_discord_attachment(url, dest_path):
    """Baixa um arquivo anexado ao Discord; se o link expirou, pede um novo e tenta de novo."""
    try:
        _http_download(url, dest_path)
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403, 404):
            raise
        fresh = refresh_attachment_urls([url]).get(url)
        if not fresh:
            raise RuntimeError(f"link do anexo expirou e não consegui renovar ({e})") from e
        _http_download(fresh, dest_path)


def run_job(job):
    job_id, name, url, kind = job
    safe = safe_name(name)
    work = os.path.join(TMP_DIR, str(job_id))
    dl = os.path.join(work, "dl")
    try:
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(dl)
        print(f"Baixando: {name}", flush=True)
        if kind == "discord_attachment":
            fname = os.path.basename(urlparse(url).path) or (safe + ".mkv")
            download_discord_attachment(url, os.path.join(dl, fname))
        else:
            import gdown

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
 .item{border-top:1px solid #333;padding:8px 0}.item:first-child{border-top:0}
 .item input{margin:4px 0}.item button{width:auto;padding:6px 12px;margin:2px 4px 2px 0}
 .why{color:#e6b95b;font-size:12px}.src{color:#777;font-size:11px;word-break:break-all}
 code{background:#222;padding:2px 6px;border-radius:4px}
 .box{background:#1c1c1c;border-radius:10px;padding:14px;margin-bottom:18px}
</style></head><body>
<h1>Francorr</h1>
<div class="box"><b>Status do bot:</b> <span id="status">__STATUS__</span><br>
<b>Links indexados:</b> __COUNT__ (visíveis ao Sonarr) &middot; <b>Para revisar:</b> __REVIEW_COUNT__ &middot; <b>Ignorados:</b> __IGNORED_COUNT__</div>

<h3>Para revisar</h3>
<div class="box">__REVIEW__</div>

<h3>Qualidade e reprocessamento</h3>
<div class="box">
<label>Etiqueta de qualidade padrão (usada só quando o post não diz a resolução/fonte)</label>
<input id="qtag" value="__QTAG__" placeholder="ex.: 1080p WEB-DL (deixe vazio para não adicionar)">
<button onclick="saveSettings()">Salvar etiqueta</button>
<button onclick="reprocess()" style="background:#444">Reprocessar posts guardados (__MSG_COUNT__)</button>
<span id="msg" style="color:#8c8"></span></div>

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

<h3>Acesso ao painel</h3>
<div class="box">__AUTH_WARN__
<label>Usuário</label><input id="au" value="__AUTH_USER__" autocomplete="username">
<label>Senha atual</label><input id="ac" type="password" autocomplete="current-password">
<label>Nova senha (mínimo 6 caracteres)</label><input id="an" type="password" autocomplete="new-password">
<button onclick="saveAccount()">Salvar usuário e senha</button>
<button onclick="logout()" style="background:#555">Sair</button>
<span id="amsg" style="color:#8c8"></span></div>

<h3>Atualização do código</h3>
<div class="box">Versão atual: <b>v__BUILD__</b> __UPDATE_NOTE__<br>
<button onclick="updateCode()">Buscar atualização no GitHub</button>
<span id="umsg"></span></div>

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
async function post(path, body){
  const r = await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  if(r.status===401){location.reload()}
  return r.json();
}
async function saveSettings(){
  await post('settings',{quality_tag:document.getElementById('qtag').value});
  document.getElementById('msg').textContent = ' salvo (clique em Reprocessar para aplicar aos posts antigos)';
}
async function reprocess(){
  const j = await post('reprocess');
  document.getElementById('msg').textContent = ' ' + j.messages + ' post(s) reprocessado(s)';
  setTimeout(()=>location.reload(),800);
}
async function saveAccount(){
  const r = await fetch('account',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({user:document.getElementById('au').value,current:document.getElementById('ac').value,new_password:document.getElementById('an').value})});
  const j = await r.json();
  if(r.ok){location.reload()}else{document.getElementById('amsg').style.color='#e66';document.getElementById('amsg').textContent=' '+(j.error||'erro')}
}
async function updateCode(){
  const m = document.getElementById('umsg'); m.textContent = ' buscando...';
  const j = await post('update');
  m.textContent = ' ' + (j.message || j.error);
  if(j.restarting){ setTimeout(function w(){ fetch('./').then(r=>r.ok?location.reload():setTimeout(w,3000)).catch(()=>setTimeout(w,3000)); }, 15000); }
}
async function logout(){ await post('logout'); location.reload(); }
async function review(id, action){
  const t = document.getElementById('t'+id);
  await post('review/'+id,{action:action,title:t?t.value:''});
  location.reload();
}
</script></body></html>"""


LOGIN_PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Francorr - Entrar</title>
<style>
 body{font-family:system-ui,sans-serif;background:#111;color:#eee;max-width:360px;margin:12vh auto;padding:0 16px}
 input,button{width:100%;padding:10px;margin:6px 0 14px;border-radius:8px;border:1px solid #444;background:#1c1c1c;color:#eee;box-sizing:border-box}
 button{background:#5b8be6;border:0;cursor:pointer;font-weight:600}
 #err{color:#e66;min-height:1.2em}
</style></head><body>
<h1>Francorr</h1>
<label>Usuário</label><input id="u" autocomplete="username" autofocus>
<label>Senha</label><input id="p" type="password" autocomplete="current-password">
<button onclick="go()">Entrar</button><div id="err"></div>
<script>
document.getElementById('p').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
async function go(){
  const r = await fetch('login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({user:document.getElementById('u').value,password:document.getElementById('p').value})});
  const j = await r.json();
  if(r.ok){location.reload()}else{document.getElementById('err').textContent=j.error||'erro'}
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


def review_html():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, title, url, reason FROM links WHERE status = 'review' ORDER BY id DESC LIMIT 50"
        ).fetchall()
    if not rows:
        return "Nada para revisar."
    out = []
    for item_id, title, url, reason in rows:
        out.append(
            f'<div class="item"><div class="why">{html.escape(reason or "")}</div>'
            f'<input id="t{item_id}" value="{html.escape(title or "", quote=True)}">'
            f'<div class="src">{html.escape(url or "")}</div>'
            f'<button onclick="review({item_id},\'approve\')">Aprovar</button>'
            f'<button onclick="review({item_id},\'ignore\')" style="background:#555">Ignorar</button></div>'
        )
    return "".join(out)


def counts():
    with db() as conn:
        c = dict(conn.execute("SELECT status, COUNT(*) FROM links GROUP BY status").fetchall())
        msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return c.get("ok", 0), c.get("review", 0), c.get("ignored", 0), msgs


@APP.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not is_logged_in(request):
        return LOGIN_PAGE
    cfg = load_config()
    auth = load_auth()
    count, n_review, n_ignored, n_msgs = counts()
    values = {
        "__STATUS__": html.escape(bot_state["status"]),
        "__COUNT__": str(count),
        "__CHANNEL__": html.escape(str(cfg["channel_id"]), quote=True),
        "__TOKEN_HINT__": "(já salvo - deixe vazio para manter)" if cfg["token"] else "cole o token aqui",
        "__SELF_URL__": html.escape(SELF_URL),
        "__JOBS__": jobs_html(),
        "__BUILD__": BUILD,
        "__REVIEW__": review_html(),
        "__REVIEW_COUNT__": str(n_review),
        "__IGNORED_COUNT__": str(n_ignored),
        "__MSG_COUNT__": str(n_msgs),
        "__QTAG__": html.escape(cfg.get("quality_tag", ""), quote=True),
        "__AUTH_USER__": html.escape(auth["user"], quote=True),
        "__UPDATE_NOTE__": "" if SELF_UPDATE_READY else '<span style="color:#e6b95b">(docker-compose ainda sem o ajuste de auto-atualização)</span>',
        "__AUTH_WARN__": (
            '<p style="color:#e6b95b">Você está usando o acesso padrão (admin / admin). Troque abaixo.</p>'
            if auth.get("default") else ""
        ),
    }
    # substituição em uma passada só, para o conteúdo inserido nunca ser reinterpretado
    return re.sub(r"__(?:STATUS|COUNT|CHANNEL|TOKEN_HINT|SELF_URL|JOBS|BUILD|REVIEW_COUNT|IGNORED_COUNT|MSG_COUNT|REVIEW|QTAG|AUTH_USER|AUTH_WARN|UPDATE_NOTE)__", lambda m: values[m.group(0)], PAGE)


@APP.post("/config")
async def update_config(request: Request):
    if (denied := guard(request)):
        return denied
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


@APP.post("/login")
async def login(request: Request):
    now = time.time()
    if _fails["until"] > now:
        wait = int(_fails["until"] - now) + 1
        return JSONResponse(status_code=429, content={"error": f"muitas tentativas, aguarde {wait}s"})
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if check_credentials(str(body.get("user", "")), str(body.get("password", ""))):
        _fails["count"] = 0
        return JSONResponse(content={"ok": True}, headers={"Set-Cookie": cookie_header(make_session())})
    _fails["count"] += 1
    if _fails["count"] >= 5:  # trava por 30s a cada 5 erros seguidos
        _fails.update(count=0, until=now + 30)
    await asyncio.sleep(1)  # deixa tentativa em massa mais lenta
    return JSONResponse(status_code=401, content={"error": "usuário ou senha incorretos"})


@APP.post("/logout")
async def logout():
    return JSONResponse(content={"ok": True}, headers={"Set-Cookie": cookie_header("", 0)})


@APP.post("/account")
async def update_account(request: Request):
    if (denied := guard(request)):
        return denied
    body = await request.json()
    user = str(body.get("user", "")).strip()
    new = str(body.get("new_password", ""))
    if not check_credentials(load_auth()["user"], str(body.get("current", ""))):
        return JSONResponse(status_code=400, content={"error": "senha atual incorreta"})
    if not user or len(user) > 64:
        return JSONResponse(status_code=400, content={"error": "usuário inválido"})
    auth = load_auth()
    auth["user"] = user
    if new:
        if len(new) < 6 or new.lower() == "admin":
            return JSONResponse(status_code=400, content={"error": "senha nova muito fraca (mínimo 6, e não pode ser 'admin')"})
        auth.update(hash_password(new), default=False)
    elif auth.get("default"):
        return JSONResponse(status_code=400, content={"error": "defina uma senha nova"})
    save_auth(auth)
    # a assinatura da sessão depende do hash/usuário: renova o cookie para você continuar logado
    return JSONResponse(content={"ok": True}, headers={"Set-Cookie": cookie_header(make_session())})


@APP.post("/update")
async def update_code(request: Request):
    if (denied := guard(request)):
        return denied
    message, restart = await asyncio.to_thread(self_update)
    if restart:
        schedule_restart()
    return {"message": message, "restarting": restart}


@APP.post("/settings")
async def update_settings(request: Request):
    if (denied := guard(request)):
        return denied
    body = await request.json()
    cfg = load_config()
    cfg["quality_tag"] = re.sub(r"\s+", " ", str(body.get("quality_tag", ""))).strip()[:40]
    save_config(cfg)
    return {"quality_tag": cfg["quality_tag"]}


@APP.post("/reprocess")
def reprocess(request: Request):
    if (denied := guard(request)):
        return denied
    return {"messages": reprocess_all()}


@APP.post("/review/{item_id}")
async def review_item(item_id: int, request: Request):
    if (denied := guard(request)):
        return denied
    body = await request.json()
    action = body.get("action")
    with db() as conn:
        if action == "approve":
            title = clean_title(body.get("title"))
            if not title:
                return JSONResponse(status_code=400, content={"error": "título vazio"})
            conn.execute(
                "UPDATE links SET title = ?, status = 'ok', reason = NULL, edited = 1 WHERE id = ?", (title, item_id)
            )
        elif action == "ignore":
            conn.execute("UPDATE links SET status = 'ignored', edited = 1 WHERE id = ?", (item_id,))
        else:
            return JSONResponse(status_code=400, content={"error": "ação inválida"})
    return {"ok": True}


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
                "WHERE status = 'ok' AND (title || ' ' || COALESCE(extra, '')) LIKE ? ORDER BY id DESC LIMIT ?",
                (pattern, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, url, created_at FROM links WHERE status = 'ok' ORDER BY id DESC LIMIT ?", (limit,)
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
        row = conn.execute(
            "SELECT title, url, kind FROM links WHERE id = ? AND status = 'ok'", (id,)
        ).fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Link não encontrado"})
    title, url, kind = row
    if redirect:  # ?redirect=1 abre o link original (uso manual)
        return Response(status_code=302, headers={"Location": url})
    # Para o Sonarr: um .torrent válido que carrega o link (Drive ou anexo do Discord) no campo "comment"
    return Response(
        content=make_torrent(title, url, kind or "drive"),
        media_type="application/x-bittorrent",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(safe_name(title) + ".torrent")},
    )
