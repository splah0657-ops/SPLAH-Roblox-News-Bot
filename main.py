import os, re, sqlite3, asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from xml.etree import ElementTree as ET

import aiohttp
import discord
from dotenv import load_dotenv

try:
    from deep_translator import GoogleTranslator
except ImportError:
    GoogleTranslator = None

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", "1550746194219765873"))
INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "180"))
ACCOUNTS = ["Roblox", "Bloxy_News"]
FEED_URL = "https://fxtwitter.com/{}/feed.xml"
DB = Path("news.db")

client = discord.Client(intents=discord.Intents.default())
http = None
task = None

def db_init():
    with sqlite3.connect(DB) as c:
        c.execute("CREATE TABLE IF NOT EXISTS posted(id TEXT PRIMARY KEY, ts TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS sources(name TEXT PRIMARY KEY, initialized INTEGER)")
        c.commit()

def posted(pid):
    with sqlite3.connect(DB) as c:
        return c.execute("SELECT 1 FROM posted WHERE id=?", (pid,)).fetchone() is not None

def mark(pid):
    with sqlite3.connect(DB) as c:
        c.execute("INSERT OR IGNORE INTO posted VALUES (?,?)", (pid, datetime.now(timezone.utc).isoformat()))
        c.commit()

def initialized(name):
    with sqlite3.connect(DB) as c:
        r = c.execute("SELECT initialized FROM sources WHERE name=?", (name,)).fetchone()
        return bool(r and r[0])

def init_source(name):
    with sqlite3.connect(DB) as c:
        c.execute("INSERT OR REPLACE INTO sources VALUES (?,1)", (name,))
        c.commit()

def clean(s):
    s = unescape(s or "")
    s = re.sub(r"<br\\s*/?>", "\\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"https?://(?:t\\.co|x\\.com|twitter\\.com)/\\S+", "", s)
    return re.sub(r"\\s+", " ", s).strip()

def translate(s):
    s = clean(s)
    if not s:
        return "Новая публикация."
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", s)
    cyr = re.findall(r"[А-Яа-яЁё]", s)
    if letters and len(cyr) / len(letters) > .65:
        return s
    if not GoogleTranslator:
        return s
    try:
        return clean(GoogleTranslator(source="auto", target="ru").translate(s[:2500])) or s
    except Exception as e:
        print("[Перевод]", e)
        return s

def tag(x):
    return x.split("}", 1)[-1].lower()

def text_of(item, names):
    for n in list(item):
        if tag(n.tag) in names and n.text:
            return n.text.strip()
    return ""

def media_of(item):
    urls = []
    for n in item.iter():
        if tag(n.tag) in ("enclosure", "content", "thumbnail", "media"):
            u = n.attrib.get("url") or n.attrib.get("href")
            if u: urls.append(unescape(u))
        if n.text:
            urls += re.findall(r'https?://pbs\\.twimg\\.com/media/[A-Za-z0-9_.?=&/%:-]+', n.text)
    for u in urls:
        if "profile_images" not in u and "pbs.twimg.com/media/" in u:
            return u
    return None

def parse_feed(xml, username):
    root = ET.fromstring(xml)
    out = []
    for item in root.iter():
        if tag(item.tag) not in ("item", "entry"):
            continue
        link = ""
        for n in list(item):
            if tag(n.tag) == "link":
                link = n.attrib.get("href") or (n.text or "").strip()
                if link: break
        pid = text_of(item, {"guid","id"}) or link
        raw = text_of(item, {"description","summary","content","encoded","title"})
        if not pid or not raw: continue
        date = text_of(item, {"pubdate","published","updated"})
        try:
            dt = parsedate_to_datetime(date).astimezone(timezone.utc) if date else None
        except Exception:
            dt = None
        out.append({"id":pid, "text":clean(raw), "link":link, "author":text_of(item,{"creator","author"}) or username, "date":dt, "image":media_of(item)})
    seen=set(); unique=[]
    for p in out:
        if p["id"] not in seen:
            seen.add(p["id"]); unique.append(p)
    unique.sort(key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc))
    return unique

async def fetch(username):
    async with http.get(FEED_URL.format(username), headers={"User-Agent":"SPLAH-Roblox-News-Bot/2.0"}, timeout=25) as r:
        body = await r.text()
        if r.status != 200: raise RuntimeError(f"X feed HTTP {r.status}")
        return parse_feed(body, username)

def icon(s):
    t=s.lower()
    for words, em in [
        (("robux","economy","price","робук"),"💰"),
        (("avatar","ugc","accessory","аватар"),"👤"),
        (("update","new","launch","release","обнов"),"🆕"),
        (("event","ивент","событ"),"🎉"),
        (("game","experience","игр"),"🎮"),
        (("mobile","ios","android","phone","мобиль"),"📱"),
    ]:
        if any(w in t for w in words): return em
    return "📰"

async def publish(channel, p):
    ru=translate(p["text"])
    title=re.split(r"(?<=[.!?])\\s+", ru)[0][:120].strip() or "Новая публикация"
    embed=discord.Embed(title=f"{icon(p['text'])} {title.upper()}", description=ru[:1800], color=discord.Color.from_rgb(224,27,36))
    embed.set_footer(text=f"Источник: @{p['author']} • X")
    if p["image"]: embed.set_image(url=p["image"])
    view=discord.ui.View()
    if p["link"]:
        view.add_item(discord.ui.Button(label="Оригинальный пост", url=p["link"], style=discord.ButtonStyle.link))
    await channel.send(embed=embed, view=view)

async def check():
    channel=client.get_channel(CHANNEL_ID)
    if not channel:
        print("Канал не найден:", CHANNEL_ID); return
    for user in ACCOUNTS:
        try:
            posts=await fetch(user)
            if not initialized(user):
                for p in posts: mark(p["id"])
                init_source(user)
                print(f"@{user}: первый запуск, старые записи не публикуются.")
                continue
            for p in posts:
                if posted(p["id"]): continue
                await publish(channel,p)
                mark(p["id"])
                await asyncio.sleep(1)
            print(f"@{user}: проверено {len(posts)} записей.")
        except Exception as e:
            print(f"@{user}:", e)

async def loop():
    await asyncio.sleep(5)
    while not client.is_closed():
        await check()
        await asyncio.sleep(INTERVAL)

@client.event
async def on_ready():
    global task
    print("Бот запущен:", client.user)
    print("Источники:", ", ".join("@"+x for x in ACCOUNTS))
    print("Канал:", CHANNEL_ID, "Интервал:", INTERVAL, "сек.")
    if task is None or task.done(): task=asyncio.create_task(loop())

async def main():
    global http
    if not TOKEN: raise RuntimeError("DISCORD_TOKEN не задан в Railway Variables")
    db_init()
    http=aiohttp.ClientSession()
    try: await client.start(TOKEN)
    finally: await http.close()

if __name__ == "__main__":
    asyncio.run(main())
