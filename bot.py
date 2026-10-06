
import json
import logging
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smmbot")

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
SMM_API_URL = os.environ["SMM_API_URL"]          # e.g. https://<panel-domain>/api/v2
SMM_API_KEY = os.environ["SMM_API_KEY"]
LIKE_SERVICE_ID = os.environ["LIKE_SERVICE_ID"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

QTY_OPTIONS = [100, 500, 1000, 2000]

# Yaha apne YouTube channels daalo: "Display name": "UC... channel ID"
CHANNELS = {
    "Channel Model": "UCqhWyz6sw7V7YbxRJMfa_uQ",
    "Channel Two": "UCyyyyyyyyyyyyyyyyyyyyyy",
}
CHANNEL_NAMES = list(CHANNELS.keys())

ORDERS_FILE = Path("orders.json")  # duplicate order se bachne ke liye log
# ----------------------------------------


def load_orders() -> dict:
    if ORDERS_FILE.exists():
        return json.loads(ORDERS_FILE.read_text())
    return {}


def save_order(video_id: str, qty: int, order_id) -> None:
    data = load_orders()
    data.setdefault(video_id, []).append({"qty": qty, "order_id": order_id})
    ORDERS_FILE.write_text(json.dumps(data, indent=2))


def is_admin(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return not ADMIN_IDS or uid in ADMIN_IDS


async def get_latest_video(channel_id: str) -> dict | None:
    """YouTube RSS feed se latest video (API key ki zarurat nahi)."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
    ns = {
        "a": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }
    entry = ET.fromstring(r.text).find("a:entry", ns)
    if entry is None:
        return None
    vid = entry.find("yt:videoId", ns).text
    return {
        "id": vid,
        "title": entry.find("a:title", ns).text,
        "link": f"https://www.youtube.com/watch?v={vid}",
        "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
    }


async def smm_request(**params) -> dict:
    payload = {"key": SMM_API_KEY, **params}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(SMM_API_URL, data=payload)
        r.raise_for_status()
        return r.json()


# ---------------- HANDLERS ----------------
def channel_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(n, callback_data=f"ch:{i}")] for i, n in enumerate(CHANNEL_NAMES)]
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await update.message.reply_text("⛔ Aap authorized nahi ho.")
    await update.message.reply_text("📺 Channel select karo:", reply_markup=channel_keyboard())


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    res = await smm_request(action="balance")
    await update.message.reply_text(f"💰 Balance: {res.get('balance')} {res.get('currency', '')}")


async def on_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update):
        return
    name = CHANNEL_NAMES[int(q.data.split(":")[1])]
    try:
        video = await get_latest_video(CHANNELS[name])
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
        f"📺 *{name}*\n🎬 {video['title']}\n🔗 {video['link']}{note}\n\nKitne likes chahiye?",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
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

    if "order" in res:
        save_order(vid, int(qty), res["order"])
        await q.edit_message_text(
            f"✅ Order placed!\n🆔 Order ID: {res['order']}\n👍 {qty} likes\n🔗 {link}\n\n"
            f"Status ke liye: /status {res['order']}"
        )
    else:
        await q.edit_message_text(f"❌ Panel error: {res.get('error', res)}")


async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("📺 Channel select karo:", reply_markup=channel_keyboard())


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        return await update.message.reply_text("Use: /status <order_id>")
    res = await smm_request(action="status", order=context.args[0])
    await update.message.reply_text(
        "\n".join(f"{k}: {v}" for k, v in res.items()) if isinstance(res, dict) else str(res)
    )


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(on_channel, pattern=r"^ch:"))
    app.add_handler(CallbackQueryHandler(on_qty, pattern=r"^qty:"))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^ok:"))
    app.add_handler(CallbackQueryHandler(on_back, pattern=r"^back$"))
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
