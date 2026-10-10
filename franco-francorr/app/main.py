import asyncio
import os
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
import discord
from fastapi import FastAPI, Response, BackgroundTasks
import uvicorn
import gdown

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/downloads")
DB_PATH = "/app/data/index.db"

os.makedirs("/app/data", exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS releases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        link TEXT,
        pub_date TEXT
    )
""")
conn.commit()

# --- BOT DISCORD ---
class DiscordClient(discord.Client):
    async def on_ready(self):
        print(f'[Francorr] Bot conectado como {self.user}')

    async def on_message(self, message):
        if message.channel.id == DISCORD_CHANNEL_ID:
            for word in message.content.split():
                if word.startswith("http://") or word.startswith("https://"):
                    clean_title = message.content.replace(word, "").strip() or "Media Release"
                    pub_date = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
                    
                    cursor.execute(
                        "INSERT INTO releases (title, link, pub_date) VALUES (?, ?, ?)",
                        (clean_title, word, pub_date)
                    )
                    conn.commit()
                    print(f"[Francorr] Novo link indexado: {clean_title} -> {word}")

intents = discord.Intents.default()
intents.message_content = True
discord_bot = DiscordClient(intents=intents)

# --- API FASTAPI (TORZNAB COMPATIBLE) ---
app = FastAPI(title="Francorr")

def download_file(url: str):
    try:
        print(f"[Francorr Downloader] Iniciando download: {url}")
        gdown.download(url, output=f"{DOWNLOAD_DIR}/", quiet=False, fuzzy=True)
        print(f"[Francorr Downloader] Download concluído com sucesso!")
    except Exception as e:
        print(f"[Francorr Downloader] Erro no download: {e}")

@app.get("/api")
def torznab_api(t: str = "search", q: str = ""):
    if t == "caps":
        caps_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <caps>
            <server title="Francorr" />
            <searching>
                <search available="yes" supportedParams="q" />
                <tv-search available="yes" supportedParams="q" />
            </searching>
            <categories>
                <category id="5000" name="TV" />
            </categories>
        </caps>"""
        return Response(content=caps_xml, media_type="application/xml")

    root = ET.Element("rss", version="2.0", attrib={"xmlns:torznab": "http://torznab.com/schemas/2010/feed/1.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = "Francorr Torznab Feed"

    cursor.execute("SELECT id, title, link, pub_date FROM releases WHERE title LIKE ?", (f"%{q}%",))
    rows = cursor.fetchall()

    for row in rows:
        item_id, title, link, pub_date = row
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = title
        # Aponta para o novo nome do container interno do Umbrel
        download_url = f"http://franco-francorr_web_1:8090/download?id={item_id}"
        ET.SubElement(item, "link").text = download_url
        ET.SubElement(item, "guid").text = str(item_id)
        ET.SubElement(item, "pubDate").text = pub_date
        
        ET.SubElement(item, "torznab:attr", name="category", value="5000")
        ET.SubElement(item, "torznab:attr", name="size", value="1073741824")

    xml_data = ET.tostring(root, encoding="utf-8", method="xml")
    return Response(content=xml_data, media_type="application/xml")

@app.get("/download")
def trigger_download(id: int, background_tasks: BackgroundTasks):
    cursor.execute("SELECT link FROM releases WHERE id = ?", (id,))
    row = cursor.fetchone()
    if row:
        link = row[0]
        background_tasks.add_task(download_file, link)
        return {"status": "download_started", "link": link}
    return {"error": "not_found"}

# --- EXECUTOR DUAL ---
async def main():
    config = uvicorn.Config(app, host="0.0.0.0", port=8090, log_level="info")
    server = uvicorn.Server(config)
    
    await asyncio.gather(
        server.serve(),
        discord_bot.start(DISCORD_TOKEN)
    )

if __name__ == "__main__":
    asyncio.run(main())