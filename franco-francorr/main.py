import os
import sqlite3
import re
from fastapi import FastAPI, Query, Response
from fastapi.responses import JSONResponse
import discord
from discord.ext import commands
import asyncio
import threading

APP = FastAPI()
DB_PATH = "/app/data/database.db"

def init_db():
    os.makedirs("/app/data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Bot conectado como {bot.user}")

@bot.event
async def on_message(message):
    if message.channel.id == DISCORD_CHANNEL_ID and not message.author.bot:
        urls = re.findall(r'https?://[^\s]+', message.content)
        if urls:
            title = re.sub(r'https?://[^\s]+', '', message.content).strip() or "Untitled Release"
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            for url in urls:
                cursor.execute("INSERT INTO links (title, url) VALUES (?, ?)", (title, url))
            conn.commit()
            conn.close()
            print(f"Novo link indexado: {title} -> {urls[0]}")

def run_bot():
    if DISCORD_TOKEN:
        bot.run(DISCORD_TOKEN)

threading.Thread(target=run_bot, daemon=True).start()

# Rota raiz para o Healthcheck do Umbrel OS
@APP.get("/")
def healthcheck():
    return {"status": "ok", "app": "Francorr"}

# Rota API Torznab para o Sonarr
@APP.get("/api")
def torznab_api(t: str = Query(None), q: str = Query(None)):
    if t == "caps":
        xml_caps = """<?xml version="1.0" encoding="UTF-8"?>
        <caps>
            <server title="Francorr" />
            <searching>
                <search available="yes" supportedParams="q" />
            </searching>
            <categories>
                <category id="5000" name="TV" />
            </categories>
        </caps>"""
        return Response(content=xml_caps, media_type="application/xml")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if q:
        cursor.execute("SELECT id, title, url, created_at FROM links WHERE title LIKE ? ORDER BY id DESC", (f"%{q}%",))
    else:
        cursor.execute("SELECT id, title, url, created_at FROM links ORDER BY id DESC LIMIT 50")
    
    rows = cursor.fetchall()
    conn.close()

    items_xml = ""
    for row in rows:
        item_id, title, url, created_at = row
        download_url = f"http://franco-francorr_web_1:8090/download?id={item_id}"
        items_xml += f"""
        <item>
            <title>{title}</title>
            <guid>{item_id}</guid>
            <link>{download_url}</link>
            <comments>{url}</comments>
            <pubDate>{created_at}</pubDate>
            <size>1048576000</size>
            <enclosure url="{download_url}" length="1048576000" type="application/x-bittorrent" />
        </item>"""

    rss_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
        <channel>
            <title>Francorr Indexer</title>
            {items_xml}
        </channel>
    </rss>"""
    return Response(content=rss_xml, media_type="application/xml")

@APP.get("/download")
def download(id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT url FROM links WHERE id = ?", (id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return JSONResponse(status_code=404, content={"error": "Link não encontrado"})
    
    return Response(status_code=302, headers={"Location": row[0]})
