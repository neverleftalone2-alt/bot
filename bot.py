"""
Telegram bot -> channel list -> channel select -> latest YouTube video
-> TNT SMM panel par Like order.

Environment variables:
    BOT_TOKEN        @BotFather se
    SMM_API_URL      panel ka API URL (jaise https://<panel>/api/v2)
    SMM_API_KEY      panel ki API key
    LIKE_SERVICE_ID  panel ki services list se YouTube Likes ka ID
    ADMIN_IDS        comma separated Telegram user IDs (zaroori)
    DATA_DIR         (optional) jaha channels.json / orders.json save ho.
                     Railway par Volume lagao to ye path do, e.g. /data

Commands:
    /start                      channel list
    /channels                   saved channels dekho
    /addchannel <id|@handle|url> [naam]
    /removechannel              channel hatao
    /balance                    panel balance
    /status <order_id>          order status
"""
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smmbot")

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
SMM_API_URL = os.environ["SMM_API_URL"].strip()
SMM_API_KEY = os.environ["SMM_API_KEY"].strip()
LIKE_SERVICE_ID = os.environ["LIKE_SERVICE_ID"].strip()
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CHANNELS_FILE = DATA_DIR / "channels.json"
ORDERS_FILE = DATA_DIR / "orders.json"

QTY_OPTIONS = [100, 500, 1000, 2000]

# Pehli baar chalne par ye channels use hote hain (baad me /addchannel se badal sakte ho).
DEFAULT_CHANNELS = {
    "Channel Advik": "UC0gAdHRqvfgBhTpp_TIbnvQ",
    "Channel Divita": "UCA4XhK9qhBGeb403MH1uU6g",
    "Channel Careless": "UCjZ4BWgz7Mw0yHKErpmD-Gg",
    "Channel Modely": "UCqhWyz6sw7V7YbxRJMfa_uQ",
    "Channel POS": "UCv-3ZuJ6T60telXYgIrFAHA",
    "Channel PAPA": "UC6bg27SVxkBLB-LrvVmXsdg",
    "Channel Againthar": "UC1jSSmPwCE9i6_KaBGEVjuA",
    "Channel Shrija": "UCLhR0my_0QAmR2mLWXCm2eA",
}
# ----------------------------------------

NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
ID_RE = re.compile(r"UC[\w-]{22}")
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}


# ---------- storage ----------
def load_channels() -> dict:
    if CHANNELS_FILE.exists():
        return json.loads(CHANNELS_FILE.read_text())
    return dict(DEFAULT_CHANNELS)


def save_channels(ch: dict) -> None:
    CHANNELS_FILE.write_text(json.dumps(ch, indent=2, ensure_ascii=False))


def load_orders() -> dict:
    if ORDERS_FILE.exists():
        return json.loads(ORDERS_FILE.read_text())
    return {}


def save_order(video_id: str, qty: int, order_id) -> None:
    data = load_orders()
    data.setdefault(video_id, []).append({"qty": qty, "order_id": order_id})
    ORDERS_FILE.write_text(json.dumps(data, indent=2))


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)


# ---------- YouTube ----------
async def fetch_feed(channel_id: str) -> ET.Element:
    """Pehle channel feed, 404 aaye to uploads-playlist feed try karo."""
    urls = [
        f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
        f"https://www.youtube.com/feeds/videos.xml?playlist_id=UU{channel_id[2:]}",
    ]
    last_err = None
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=BROWSER_HEADERS) as c:
        for url in urls:
            try:
                r = await c.get(url)
                r.raise_for_status()
                return ET.fromstring(r.text)
            except Exception as e:  # noqa: BLE001
                last_err = e
    raise last_err


async def scrape_latest_video(channel_id: str) -> dict | None:
    """Last fallback: channel ke /videos page se latest video ID nikalo."""
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True, headers=BROWSER_HEADERS, cookies={"CONSENT": "YES+1"}
    ) as c:
        r = await c.get(url)
        r.raise_for_status()
    m = re.search(r'"videoId":"([\w-]{11})"', r.text)
    if not m:
        return None
    vid = m.group(1)
    t = re.search(r'"title":\{"runs":\[\{"text":"(.*?)"\}', r.text)
    title = t.group(1) if t else "Latest video"
    return {"id": vid, "title": title, "link": f"https://www.youtube.com/watch?v={vid}"}


async def get_latest_video(channel_id: str) -> dict | None:
    try:
        root = await fetch_feed(channel_id)
    except Exception:
        log.warning("RSS feeds fail hue, page scrape try kar raha hoon")
        return await scrape_latest_video(channel_id)
    entry = root.find("a:entry", NS)
    if entry is None:
        return await scrape_latest_video(channel_id)
    vid = entry.find("yt:videoId", NS).text
    return {
        "id": vid,
        "title": entry.find("a:title", NS).text,
        "link": f"https://www.youtube.com/watch?v={vid}",
    }


async def get_channel_title(channel_id: str) -> str | None:
    try:
        root = await fetch_feed(channel_id)
    except Exception:
        return None
    t = root.find("a:title", NS)
    return t.text if t is not None else None


async def resolve_channel_id(text: str) -> str | None:
    """UC ID, /channel/UC... URL, @handle ya channel URL se channel ID nikalo."""
    text = text.strip()
    m = ID_RE.search(text)
    if m:
        return m.group(0)
    if text.startswith("@"):
        url = f"https://www.youtube.com/{text}"
    elif text.startswith("http"):
        url = text
    else:
        return None
    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
        cookies={"CONSENT": "YES+1"},
    ) as c:
        r = await c.get(url)
    m = re.search(r'"channelId":"(UC[\w-]{22})"', r.text) or re.search(r"channel_id=(UC[\w-]{22})", r.text)
    return m.group(1) if m else None


# ---------- SMM panel ----------
async def smm_request(**params) -> dict:
    payload = {"key": SMM_API_KEY, **params}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(SMM_API_URL, data=payload)
        r.raise_for_status()
        return r.json()


# ---------- keyboards ----------
def channel_keyboard() -> InlineKeyboardMarkup:
    names = list(load_channels().keys())
    rows = [[InlineKeyboardButton(n, callback_data=f"ch:{i}")] for i, n in enumerate(names)]
    return InlineKeyboardMarkup(rows)


# ---------- handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text(
            f"⛔ Aap authorized nahi ho.\nAapki Telegram ID: {update.effective_user.id}"
        )
    if not load_channels():
        return await update.message.reply_text("Koi channel nahi hai. /addchannel se jodo.")
    await update.message.reply_text("📺 Channel select karo:", reply_markup=channel_keyboard())


async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    ch = load_channels()
    if not ch:
        return await update.message.reply_text("Koi channel nahi hai.")
    await update.message.reply_text("\n".join(f"• {n}: {cid}" for n, cid in ch.items()))


async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        return await update.message.reply_text(
            "Use: /addchannel <UC-ID | @handle | channel URL> [naam]\n"
            "Example: /addchannel @MrBeast Mera Channel"
        )
    try:
        cid = await resolve_channel_id(context.args[0])
        if not cid:
            return await update.message.reply_text(
                "❌ Channel ID nahi mili. Seedha UC... wali ID bhejo."
            )
        name = " ".join(context.args[1:]).strip() or await get_channel_title(cid) or cid
    except Exception as e:
        log.exception("add_channel error")
        return await update.message.reply_text(f"❌ Error: {e}")

    ch = load_channels()
    ch[name] = cid
    save_channels(ch)
    await update.message.reply_text(f"✅ Added: {name}\n🆔 {cid}")


async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    names = list(load_channels().keys())
    if not names:
        return await update.message.reply_text("Koi channel nahi hai.")
    rows = [[InlineKeyboardButton(f"🗑 {n}", callback_data=f"del:{i}")] for i, n in enumerate(names)]
    await update.message.reply_text("Kaunsa channel hatana hai?", reply_markup=InlineKeyboardMarkup(rows))


async def on_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update):
        return
    idx = int(q.data.split(":")[1])
    ch = load_channels()
    names = list(ch.keys())
    if idx >= len(names):
        return await q.edit_message_text("❌ Channel list badal chuki hai, dobara try karo.")
    removed = names[idx]
    del ch[removed]
    save_channels(ch)
    await q.edit_message_text(f"🗑 Removed: {removed}")


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    try:
        res = await smm_request(action="balance")
    except Exception as e:
        return await update.message.reply_text(f"❌ API error: {e}")
    await update.message.reply_text(f"💰 Balance: {res.get('balance')} {res.get('currency', '')}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        return await update.message.reply_text("Use: /status <order_id>")
    try:
        res = await smm_request(action="status", order=context.args[0])
    except Exception as e:
        return await update.message.reply_text(f"❌ API error: {e}")
    text = "\n".join(f"{k}: {v}" for k, v in res.items()) if isinstance(res, dict) else str(res)
    await update.message.reply_text(text)


async def on_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update):
        return
    ch = load_channels()
    names = list(ch.keys())
    idx = int(q.data.split(":")[1])
    if idx >= len(names):
        return await q.edit_message_text("❌ Channel list badal chuki hai, /start dobara bhejo.")
    name = names[idx]
    try:
        video = await get_latest_video(ch[name])
    except Exception as e:
        log.exception("RSS error")
        return await q.edit_message_text(f"❌ Video fetch nahi hua: {e}")
    if not video:
        return await q.edit_message_text("❌ Is channel par koi video nahi mili.")

    already = load_orders().get(video["id"])
    note = f"\n⚠️ Is video par pehle {len(already)} order lag chuka hai." if already else ""

    kb = [[InlineKeyboardButton(f"👍 {n}", callback_data=f"qty:{video['id']}:{n}")] for n in QTY_OPTIONS]
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    await q.edit_message_text(
        f"📺 {name}\n🎬 {video['title']}\n🔗 {video['link']}{note}\n\nKitne likes chahiye?",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def on_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update):
        return
    _, vid, qty = q.data.split(":")
    kb = [[
        InlineKeyboardButton("✅ Confirm", callback_data=f"ok:{vid}:{qty}"),
        InlineKeyboardButton("❌ Cancel", callback_data="back"),
    ]]
    await q.edit_message_text(
        f"Confirm karo:\n🔗 https://www.youtube.com/watch?v={vid}\n👍 Likes: {qty}",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update):
        return
    _, vid, qty = q.data.split(":")
    link = f"https://www.youtube.com/watch?v={vid}"
    await q.edit_message_text("⏳ Order laga raha hoon...")
    try:
        res = await smm_request(action="add", service=LIKE_SERVICE_ID, link=link, quantity=qty)
    except Exception as e:
        log.exception("SMM error")
        return await q.edit_message_text(f"❌ API error: {e}")

    if isinstance(res, dict) and "order" in res:
        save_order(vid, int(qty), res["order"])
        await q.edit_message_text(
            f"✅ Order placed!\n🆔 Order ID: {res['order']}\n👍 {qty} likes\n🔗 {link}\n\n"
            f"Status ke liye: /status {res['order']}"
        )
    else:
        err = res.get("error", res) if isinstance(res, dict) else res
        await q.edit_message_text(f"❌ Panel error: {err}")


async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update):
        return
    await q.edit_message_text("📺 Channel select karo:", reply_markup=channel_keyboard())


def main():
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS khali hai - koi bhi bot use nahi kar payega. ADMIN_IDS set karo.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("channels", list_channels))
    app.add_handler(CommandHandler("addchannel", add_channel))
    app.add_handler(CommandHandler("removechannel", remove_channel))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(on_channel, pattern=r"^ch:"))
    app.add_handler(CallbackQueryHandler(on_qty, pattern=r"^qty:"))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^ok:"))
    app.add_handler(CallbackQueryHandler(on_delete, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(on_back, pattern=r"^back$"))
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
