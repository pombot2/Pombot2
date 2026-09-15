import asyncio
import json
import logging
import os
import random
import sqlite3
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Template
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ==========================================
# CONFIG & AUTHENTICATION
# ==========================================
ADMIN_USER = os.getenv("ADMIN_USER", "nagato")
DEFAULT_PASS = os.getenv("ADMIN_PASS", "nagato@123")
AUTH_COOKIE_NAME = "session_token"
AUTH_SECRET = "admin_authenticated_session_key_99"

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.join(os.getcwd(), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_NAME = os.path.join(DATA_DIR, "nagato_database.db")

INITIAL_BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEFAULT_BACK_BUTTON = "🔙 Back"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

pending_verifications = {}
active_admin_uploads = {}

# --- Database Setup & Migration ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            token TEXT,
            welcome_images_json TEXT,
            welcome_caption TEXT,
            buttons_json TEXT,
            button_videos_json TEXT,
            button_details_json TEXT,
            upi_id TEXT,
            payee_name TEXT,
            admin_chat_id TEXT,
            btn_how_to_use TEXT,
            btn_report_issue TEXT,
            btn_language TEXT,
            msg_how_to_use TEXT,
            msg_report_issue TEXT,
            msg_language TEXT,
            admin_user TEXT,
            admin_pass TEXT,
            license_expiry TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            txn_id TEXT,
            chat_id INTEGER,
            username TEXT,
            pack_name TEXT,
            amount REAL,
            status TEXT DEFAULT 'Pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    try:
        c.execute("ALTER TABLE settings ADD COLUMN license_expiry TEXT")
    except sqlite3.OperationalError:
        pass

    c.execute("SELECT COUNT(*) FROM settings")
    if c.fetchone()[0] == 0:
        default_buttons = [f"VIP Pack {i}" for i in range(1, 18)]
        default_images = [
            "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=800"
        ]
        default_button_videos = {str(i): [] for i in range(17)}
        default_details = {
            str(i): {
                "pack": "HOUSEWIFE 😱" if i == 0 else f"VIP Pack {i + 1}",
                "price": "49" if i == 0 else "299",
                "desc": "HOUSEWIFE IN HOME WITH HUSBAND OR HIS BROTHER" if i == 0 else "Instant streaming bundle."
            } for i in range(17)
        }
        thirty_days_later = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

        c.execute("""
            INSERT INTO settings (
                id, token, welcome_images_json, welcome_caption, buttons_json,
                button_videos_json, button_details_json,
                upi_id, payee_name, admin_chat_id,
                btn_how_to_use, btn_report_issue, btn_language,
                msg_how_to_use, msg_report_issue, msg_language,
                admin_user, admin_pass, license_expiry
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            INITIAL_BOT_TOKEN,
            json.dumps(default_images),
            "✨ *Welcome to our Exclusive Hub!*\n\nSelect an option below to preview content:",
            json.dumps(default_buttons),
            json.dumps(default_button_videos),
            json.dumps(default_details),
            "thesalesgod@nyes",
            "Trusted Seller",
            "",
            "📖 How To Use",
            "🚨 Report Issue",
            "🌐 Language",
            "Select any package to preview videos, then complete payment via UPI QR code.",
            "For help or support, contact support directly: @YourSupportHandle",
            "🌐 English is active by default.",
            ADMIN_USER,
            DEFAULT_PASS,
            thirty_days_later
        ))
        conn.commit()
    conn.close()

def get_settings():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM settings WHERE id = 1")
    row = c.fetchone()
    conn.close()

    expiry = row[18] if len(row) > 18 and row[18] else (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    button_details = {}
    if row[6]:
        try:
            button_details = json.loads(row[6])
        except Exception:
            pass

    button_videos = {}
    if row[5]:
        try:
            button_videos = json.loads(row[5])
        except Exception:
            pass

    return {
        "token": row[1] or "",
        "welcome_images": json.loads(row[2]) if row[2] else [],
        "welcome_caption": row[3] or "",
        "buttons": json.loads(row[4]) if row[4] else [],
        "button_videos": button_videos,
        "button_details": button_details,
        "upi_id": row[7] or "",
        "payee_name": row[8] or "Merchant",
        "admin_chat_id": row[9] or "",
        "btn_how_to_use": row[10] or "📖 How To Use",
        "btn_report_issue": row[11] or "🚨 Report Issue",
        "btn_language": row[12] or "🌐 Language",
        "msg_how_to_use": row[13] or "Select any pack to proceed.",
        "msg_report_issue": row[14] or "Contact support.",
        "msg_language": row[15] or "Current language: English.",
        "admin_user": row[16] if len(row) > 16 and row[16] else ADMIN_USER,
        "admin_pass": row[17] if len(row) > 17 and row[17] else DEFAULT_PASS,
        "license_expiry": expiry
    }

def update_field(field_name: str, value: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(f"UPDATE settings SET {field_name} = ? WHERE id = 1", (value,))
    conn.commit()
    conn.close()

def register_user(chat_id: int, username: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (chat_id, username) VALUES (?, ?)", (chat_id, username or "N/A"))
    conn.commit()
    conn.close()

def log_order(txn_id: str, chat_id: int, username: str, pack_name: str, amount: float):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        INSERT INTO orders (txn_id, chat_id, username, pack_name, amount)
        VALUES (?, ?, ?, ?, ?)
    """, (txn_id, chat_id, username or "N/A", pack_name, amount))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0.0) FROM orders WHERE status = 'Paid'")
    paid_row = c.fetchone()
    paid_orders = paid_row[0] or 0
    revenue = paid_row[1] or 0.0

    c.execute("SELECT txn_id, chat_id, username, pack_name, amount, created_at, status, id FROM orders ORDER BY id DESC LIMIT 10")
    recent_orders = c.fetchall()
    conn.close()
    return total_users, paid_orders, revenue, recent_orders

def get_all_orders():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT txn_id, chat_id, username, pack_name, amount, created_at, status, id FROM orders ORDER BY id DESC")
    orders = c.fetchall()
    conn.close()
    return orders

def update_order_status(order_id: int, status: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()
    conn.close()

init_db()

# --- Dynamic UPI Helpers ---
def make_upi_uri(upi_id: str, payee_name: str, amount: str, note: str) -> str:
    clean_amount = "".join(c for c in str(amount) if c.isdigit() or c == '.') or "0"
    params = {
        "pa": upi_id.strip(),
        "pn": payee_name.strip() or "Merchant",
        "am": clean_amount,
        "cu": "INR",
        "tn": note[:50]
    }
    return f"upi://pay?{urllib.parse.urlencode(params)}"

def generate_upi_qr_url(upi_uri: str) -> str:
    encoded = urllib.parse.quote(upi_uri)
    return f"https://api.qrserver.com/v1/create-qr-code/?size=500x500&data={encoded}"

def generate_txn_id(pack_name: str) -> str:
    slug = "".join(c for c in pack_name if c.isalnum()).upper()[:8] or "PACK"
    date_str = time.strftime("%y%m%d")
    rnd = f"{random.randint(1000, 99999):05d}"
    return f"TXN-{date_str}-{slug}-{rnd}"

# --- Bot Lifecycle Manager ---
class BotManager:
    def __init__(self):
        self.app: Application | None = None
        self.task: asyncio.Task | None = None
        self.status = "Stopped"

    async def _run_bot(self, token: str):
        try:
            self.app = ApplicationBuilder().token(token).build()
            self.app.add_handler(CommandHandler("start", handle_start))
            self.app.add_handler(CommandHandler("cancel", handle_cancel))
            self.app.add_handler(CommandHandler("upload", handle_admin_upload_command))
            self.app.add_handler(CommandHandler("clear", handle_admin_clear_command))
            self.app.add_handler(CommandHandler("done", handle_admin_done_command))
            self.app.add_handler(CallbackQueryHandler(handle_callback))
            self.app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION, handle_incoming_media))

            await self.app.initialize()
            await self.app.updater.start_polling()
            await self.app.start()
            self.status = "Running"
            logger.info("Bot is running.")

            while self.status == "Running":
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Bot runtime error: {e}")
            self.status = f"Error: {e}"
        finally:
            if self.app:
                if self.app.updater and self.app.updater.running:
                    await self.app.updater.stop()
                if self.app.running:
                    await self.app.stop()
                await self.app.shutdown()
            self.status = "Stopped"

    async def stop(self):
        if self.task and not self.task.done():
            self.status = "Stopping"
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.status = "Stopped"

    async def restart(self, token: str):
        await self.stop()
        if token.strip():
            self.task = asyncio.create_task(self._run_bot(token.strip()))

bot_manager = BotManager()

# --- Keyboard Builders with Price on Buttons ---
def build_main_keyboard(cfg):
    keyboard = []
    for idx in range(17):
        b_name = cfg["buttons"][idx] if idx < len(cfg["buttons"]) else f"Category {idx+1}"
        details = cfg["button_details"].get(str(idx), {"price": "299"})
        price = details.get("price", "299")

        display_label = f"🟢 {b_name} - ₹{price}"

        btn = InlineKeyboardButton(
            text=display_label,
            callback_data=f"btn_cat_{idx}",
            api_kwargs={"style": "success"}
        )
        keyboard.append([btn])

    row_actions = [
        InlineKeyboardButton(
            text=cfg["btn_how_to_use"],
            callback_data="act_how_to_use",
            api_kwargs={"style": "primary"}
        ),
        InlineKeyboardButton(
            text=cfg["btn_report_issue"],
            callback_data="act_report_issue",
            api_kwargs={"style": "danger"}
        )
    ]
    keyboard.append(row_actions)

    btn_lang = InlineKeyboardButton(
        text=cfg["btn_language"],
        callback_data="act_language",
        api_kwargs={"style": "primary"}
    )
    keyboard.append([btn_lang])
    return InlineKeyboardMarkup(keyboard)

async def send_media_in_chunks(context: ContextTypes.DEFAULT_TYPE, chat_id: int, media_list: list):
    for i in range(0, len(media_list), 10):
        chunk = media_list[i:i + 10]
        if len(chunk) == 1:
            item = chunk[0]
            if isinstance(item, InputMediaPhoto):
                await context.bot.send_photo(chat_id=chat_id, photo=item.media)
            else:
                await context.bot.send_video(chat_id=chat_id, video=item.media, supports_streaming=True)
        else:
            await context.bot.send_media_group(chat_id=chat_id, media=chunk)

# --- Admin In-Bot Media Upload Commands ---
def is_admin(chat_id: int) -> bool:
    cfg = get_settings()
    admin_id = cfg.get("admin_chat_id", "").strip()
    return str(chat_id) == admin_id

async def handle_admin_upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ You are not registered as the Admin. Set your Chat ID in Admin Panel.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ Usage: `/upload <number 1-17>`\nExample: `/upload 1`", parse_mode="Markdown")
        return

    btn_num = int(context.args[0])
    if not (1 <= btn_num <= 17):
        await update.message.reply_text("⚠️ Button number must be between 1 and 17.")
        return

    btn_idx = btn_num - 1
    active_admin_uploads[chat_id] = btn_idx

    cfg = get_settings()
    pack_name = cfg["button_details"].get(str(btn_idx), {}).get("pack", f"Button {btn_num}")

    msg = (
        f"📥 *Bulk Media Upload Mode Activated!*\n\n"
        f"🎯 **Target Button:** #{btn_num} ({pack_name})\n\n"
        f"Send or forward photos, videos, or documents in bulk.\n"
        f"Type `/done` to finish upload mode."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_admin_clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ Usage: `/clear <1-17>`")
        return

    btn_idx = int(context.args[0]) - 1
    if 0 <= btn_idx < 17:
        cfg = get_settings()
        vids = cfg["button_videos"]
        vids[str(btn_idx)] = []
        update_field("button_videos_json", json.dumps(vids))
        await update.message.reply_text(f"🗑️ Cleared all media for Button #{btn_idx + 1}.")

async def handle_admin_done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_admin_uploads:
        btn_idx = active_admin_uploads.pop(chat_id)
        cfg = get_settings()
        count = len(cfg["button_videos"].get(str(btn_idx), []))
        await update.message.reply_text(f"✅ *Upload Finished!*\nSaved **{count}** media items to Button #{btn_idx + 1}.", parse_mode="Markdown")
    else:
        await update.message.reply_text("No active upload session found.")

# --- Telegram Handlers ---
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_user(chat_id, update.effective_user.username or "")
    pending_verifications.pop(chat_id, None)
    cfg = get_settings()

    valid_images = [img.strip() for img in cfg["welcome_images"] if img.strip()]
    if valid_images:
        media_group = [InputMediaPhoto(media=url) for url in valid_images]
        try:
            await send_media_in_chunks(context, chat_id, media_group)
        except Exception as e:
            logger.error(f"Failed to send images: {e}")

    caption_text = cfg["welcome_caption"].strip() if cfg["welcome_caption"] else "✨ Select an option below to continue:"

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption_text,
            reply_markup=build_main_keyboard(cfg),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Markdown error in welcome caption ({e}). Sending plain text...")
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption_text,
            reply_markup=build_main_keyboard(cfg)
        )

async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pending_verifications.pop(chat_id, None)
    active_admin_uploads.pop(chat_id, None)
    await update.message.reply_text("❌ Action cancelled.")
    await handle_start(update, context)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id
    cfg = get_settings()

    if data == "btn_home":
        pending_verifications.pop(chat_id, None)
        await query.answer()
        await handle_start(update, context)

    elif data == "act_how_to_use":
        await query.answer(cfg["msg_how_to_use"], show_alert=True)
    elif data == "act_report_issue":
        await query.answer(cfg["msg_report_issue"], show_alert=True)
    elif data == "act_language":
        await query.answer(cfg["msg_language"], show_alert=True)

    # Step 1: Click Green Button -> Deliver Videos/Photos as an Album + Pack Details Card
    elif data.startswith("btn_cat_"):
        await query.answer()
        btn_idx = data.replace("btn_cat_", "")

        button_videos = cfg["button_videos"].get(btn_idx, [])
        valid_media = [v.strip() for v in button_videos if v.strip()]

        album = []
        fallback_urls = []

        for item in valid_media:
            if item.startswith("http://") or item.startswith("https://"):
                if any(item.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                    album.append(InputMediaPhoto(media=item))
                elif any(item.lower().endswith(ext) for ext in [".mp4", ".mov", ".m4v"]):
                    album.append(InputMediaVideo(media=item, supports_streaming=True))
                else:
                    fallback_urls.append(item)
            else:
                album.append(InputMediaVideo(media=item, supports_streaming=True))

        if album:
            for i in range(0, len(album), 10):
                chunk = album[i:i + 10]
                if len(chunk) == 1:
                    single = chunk[0]
                    try:
                        if isinstance(single, InputMediaPhoto):
                            await context.bot.send_photo(chat_id=chat_id, photo=single.media)
                        else:
                            await context.bot.send_video(chat_id=chat_id, video=single.media, supports_streaming=True)
                    except Exception:
                        try:
                            await context.bot.send_photo(chat_id=chat_id, photo=single.media)
                        except Exception as e:
                            logger.warning(f"Failed single item send: {e}")
                else:
                    try:
                        await context.bot.send_media_group(chat_id=chat_id, media=chunk)
                    except Exception as e:
                        logger.warning(f"send_media_group error ({e}), delivering individually...")
                        for m_item in chunk:
                            try:
                                await context.bot.send_video(chat_id=chat_id, video=m_item.media, supports_streaming=True)
                            except Exception:
                                try:
                                    await context.bot.send_photo(chat_id=chat_id, photo=m_item.media)
                                except Exception:
                                    pass

        if fallback_urls:
            watch_keyboard = [
                [InlineKeyboardButton(f"▶️ Watch Preview Clip {i}", url=link)]
                for i, link in enumerate(fallback_urls, 1)
            ]
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="🎬 *Sample Preview Clips:*",
                    reply_markup=InlineKeyboardMarkup(watch_keyboard),
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        details = cfg["button_details"].get(btn_idx, {
            "pack": f"VIP Pack {int(btn_idx) + 1}",
            "price": "299",
            "desc": "Full premium access."
        })

        pack_name = details.get("pack", f"Pack {int(btn_idx) + 1}")
        price = details.get("price", "0")
        desc = details.get("desc", "Instant access after payment.")

        preview_text = (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎀 *Pack*\n"
            f"{pack_name}\n\n"
            f"💰 *Price*\n"
            f"₹{price}\n\n"
            f"📄 *Description*\n"
            f"{desc}\n"
            f"━━━━━━━━━━━━━━━━━━"
        )

        preview_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💳 Buy Now",
                    callback_data=f"action_proceed_pay_{btn_idx}",
                    api_kwargs={"style": "success"}
                )
            ],
            [
                InlineKeyboardButton(
                    DEFAULT_BACK_BUTTON,
                    callback_data="btn_home",
                    api_kwargs={"style": "primary"}
                )
            ]
        ])

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=preview_text,
                reply_markup=preview_markup,
                parse_mode="Markdown"
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text=preview_text.replace("*", ""),
                reply_markup=preview_markup
            )

    # Step 2: Click "Buy Now" -> Show Auto-Generated Payment QR Page
    elif data.startswith("action_proceed_pay_"):
        await query.answer()
        btn_idx = data.replace("action_proceed_pay_", "")

        details = cfg["button_details"].get(btn_idx, {
            "pack": f"VIP Pack {int(btn_idx) + 1}",
            "price": "299",
            "desc": ""
        })

        pack_name = details.get("pack", f"Pack {int(btn_idx) + 1}")
        price = details.get("price", "0")
        txn_id = generate_txn_id(pack_name)

        try:
            amt_val = float("".join(c for c in str(price) if c.isdigit() or c == '.') or "0")
        except ValueError:
            amt_val = 0.0

        log_order(txn_id, chat_id, update.effective_user.username or "", pack_name, amt_val)

        pending_verifications[chat_id] = {
            "txn_id": txn_id,
            "price": price,
            "pack": pack_name
        }

        upi_link = make_upi_uri(
            upi_id=cfg["upi_id"],
            payee_name=cfg["payee_name"],
            amount=price,
            note=txn_id
        )
        qr_url = generate_upi_qr_url(upi_link)

        payment_text = (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💎 *Payment*\n\n"
            f"🎀 *{pack_name}*\n"
            f"💰 *Amount : ₹{price}*\n"
            f"🪪 *UPI :* `{cfg['upi_id']}`\n"
            f"🧾 *Txn :* `{txn_id}`\n\n"
            f"Scan the QR or copy the UPI ID above and pay the exact amount. "
            f"Then tap 📸 Send Payment Screenshot and upload the payment receipt.\n\n"
            f"📲 [Open UPI App]({upi_link})\n"
            f"━━━━━━━━━━━━━━━━━━"
        )

        payment_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📸 Send Payment Screenshot",
                    callback_data=f"btn_send_ss_{btn_idx}",
                    api_kwargs={"style": "success"}
                )
            ],
            [
                InlineKeyboardButton(
                    DEFAULT_BACK_BUTTON,
                    callback_data=f"btn_cat_{btn_idx}",
                    api_kwargs={"style": "primary"}
                )
            ]
        ])

        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=qr_url,
                caption=payment_text,
                reply_markup=payment_markup,
                parse_mode="Markdown"
            )
        except Exception:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=qr_url,
                caption=payment_text.replace("*", "").replace("`", ""),
                reply_markup=payment_markup
            )

    elif data.startswith("btn_send_ss_"):
        await query.answer()
        session = pending_verifications.get(chat_id)
        if not session:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Session expired. Please choose a package again.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(DEFAULT_BACK_BUTTON, callback_data="btn_home", api_kwargs={"style": "primary"})
                ]])
            )
            return

        prompt_msg = (
            f"💎 *Send Payment Screenshot*\n\n"
            f"🧾 `{session['txn_id']}`\n"
            f"💰 *₹{session['price']}*\n\n"
            f"Upload the screenshot of your successful payment here as a photo.\n\n"
            f"Send /cancel to abort."
        )

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=prompt_msg,
                parse_mode="Markdown"
            )
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text=prompt_msg.replace("*", "").replace("`", "")
            )

# --- Media Router ---
async def handle_incoming_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = update.effective_chat.id

    if chat_id in active_admin_uploads and is_admin(chat_id):
        btn_idx = active_admin_uploads[chat_id]
        f_id = None
        m_type = "file"

        if msg.video:
            f_id = msg.video.file_id
            m_type = "Video"
        elif msg.photo:
            f_id = msg.photo[-1].file_id
            m_type = "Photo"
        elif msg.document:
            f_id = msg.document.file_id
            m_type = "Document"
        elif msg.animation:
            f_id = msg.animation.file_id
            m_type = "GIF"

        if f_id:
            cfg = get_settings()
            vids = cfg["button_videos"]
            if str(btn_idx) not in vids:
                vids[str(btn_idx)] = []
            
            vids[str(btn_idx)].append(f_id)
            update_field("button_videos_json", json.dumps(vids))

            total = len(vids[str(btn_idx)])
            await msg.reply_text(f"📥 Saved **{m_type}** to Button #{btn_idx + 1} (Total: {total}).\nSend next or `/done` to finish.", parse_mode="Markdown")
            return

    session = pending_verifications.get(chat_id)
    if session and msg.photo:
        photo_file = msg.photo[-1]
        cfg = get_settings()

        await update.message.reply_text(
            f"✅ *Screenshot Received!*\n\n"
            f"🧾 *Txn:* `{session['txn_id']}`\n"
            f"📦 *Pack:* {session['pack']}\n\n"
            f"Your transaction is currently being verified. You will receive your access link here shortly.",
            parse_mode="Markdown"
        )

        if cfg["admin_chat_id"]:
            try:
                admin_caption = (
                    f"🚨 *New Payment Screenshot Received!*\n\n"
                    f"👤 *User:* @{update.effective_user.username or 'N/A'} (`{chat_id}`)\n"
                    f"📦 *Pack:* {session['pack']}\n"
                    f"💰 *Amount:* ₹{session['price']}\n"
                    f"🧾 *Txn ID:* `{session['txn_id']}`"
                )
                await context.bot.send_photo(
                    chat_id=int(cfg["admin_chat_id"]),
                    photo=photo_file.file_id,
                    caption=admin_caption,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed forwarding screenshot: {e}")

        del pending_verifications[chat_id]
        return

    if is_admin(chat_id):
        f_id = None
        if msg.video: f_id = msg.video.file_id
        elif msg.photo: f_id = msg.photo[-1].file_id
        elif msg.document: f_id = msg.document.file_id
        if f_id:
            await msg.reply_text(f"📹 **File ID:**\n`{f_id}`\n\n💡 Tip: Use `/upload <1-17>` to assign media automatically in bulk.", parse_mode="Markdown")

# --- FastAPI Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    if cfg["token"]:
        await bot_manager.restart(cfg["token"])
    yield
    await bot_manager.stop()

app = FastAPI(lifespan=lifespan)

# ==========================================
# TEMPLATES (EXACT NOISY/NAGATO LOGIN & CYBERPUNK PANEL)
# ==========================================
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Login &mdash; Nagato Panel</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;700;800;900&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Plus Jakarta Sans', sans-serif; background-color: #030108; }
        .font-tech { font-family: 'Orbitron', monospace; }
        .exact-login-card {
            background: linear-gradient(180deg, rgba(16, 12, 34, 0.94) 0%, rgba(10, 8, 22, 0.96) 100%);
            border: 1px solid rgba(168, 85, 247, 0.5);
            box-shadow: 0 0 28px rgba(168, 85, 247, 0.35), 0 0 70px rgba(168, 85, 247, 0.15);
            border-radius: 26px;
        }
        .custom-input {
            background-color: #080613;
            border: 1px solid rgba(147, 51, 234, 0.25);
            transition: all 0.2s ease;
        }
        .custom-input:focus {
            outline: none;
            border-color: #38bdf8;
            box-shadow: 0 0 12px rgba(56, 189, 248, 0.3);
        }
        #cyberCanvas {
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            z-index: 1;
            pointer-events: none;
        }
    </style>
</head>
<body class="text-slate-100 min-h-screen flex items-center justify-center p-4 relative overflow-hidden">
    <canvas id="cyberCanvas"></canvas>

    <div class="w-full max-w-[370px] relative z-10">
        <div class="exact-login-card p-8 space-y-6">
            
            <div class="space-y-1">
                <div class="flex items-center gap-2.5">
                    <svg class="w-6 h-6 text-fuchsia-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/>
                        <path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/>
                        <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/>
                        <path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>
                    </svg>
                    <h1 class="text-xl font-bold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-purple-300 via-fuchsia-300 to-cyan-300">
                        Nagato Panel
                    </h1>
                </div>
                <div class="font-tech text-[10px] tracking-[0.25em] text-cyan-400/90 font-bold uppercase pl-8">
                    ADMIN PANEL
                </div>
            </div>

            {% if error %}
            <div class="p-3 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-400 text-xs font-mono">
                {{ error }}
            </div>
            {% endif %}

            <form method="POST" action="/login" class="space-y-4 pt-1">
                <div>
                    <label class="block text-xs font-medium text-slate-300 mb-2">Username</label>
                    <input type="text" name="username" required autofocus placeholder=""
                           class="custom-input w-full h-11 rounded-xl px-4 text-sm text-white">
                </div>

                <div>
                    <label class="block text-xs font-medium text-slate-300 mb-2">Password</label>
                    <input type="password" name="password" required placeholder=""
                           class="custom-input w-full h-11 rounded-xl px-4 text-sm text-white">
                </div>

                <button type="submit"
                        class="w-full h-11 mt-3 bg-gradient-to-r from-purple-500 via-fuchsia-500 to-cyan-400 hover:opacity-95 text-white font-semibold rounded-xl text-sm transition shadow-lg shadow-purple-600/30">
                    Sign in &rarr;
                </button>
            </form>

            <div class="pt-2 text-center">
                <a href="https://t.me/NAGATOxOWNER" target="_blank" rel="noopener noreferrer"
                   class="inline-flex items-center gap-1.5 text-xs text-cyan-400/80 hover:text-cyan-300 transition font-mono">
                    <svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24">
                        <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69a.2.2 0 00-.05-.18c-.06-.05-.14-.03-.21-.02-.09.02-1.49.95-4.22 2.79-.4.27-.76.41-1.08.4-.36-.01-1.04-.2-1.55-.37-.63-.2-1.12-.31-1.08-.66.02-.18.27-.36.74-.55 2.92-1.27 4.86-2.11 5.83-2.51 2.78-1.16 3.35-1.36 3.73-1.36.08 0 .27.02.39.12.1.08.13.19.14.27-.01.06.01.24 0 .38z"/>
                    </svg>
                    Contact Developer
                </a>
            </div>

        </div>
    </div>

    <script>
        const canvas = document.getElementById('cyberCanvas');
        const ctx = canvas.getContext('2d');
        let width = canvas.width = window.innerWidth;
        let height = canvas.height = window.innerHeight;

        window.addEventListener('resize', () => {
            width = canvas.width = window.innerWidth;
            height = canvas.height = window.innerHeight;
        });

        const particles = [];
        const count = 38;
        const colors = ['#a855f7', '#00f0ff', '#ff007f'];

        for (let i = 0; i < count; i++) {
            particles.push({
                x: Math.random() * width,
                y: Math.random() * height,
                vx: (Math.random() - 0.5) * 0.5,
                vy: (Math.random() - 0.5) * 0.5,
                radius: Math.random() * 1.8 + 0.8,
                color: colors[Math.floor(Math.random() * colors.length)]
            });
        }

        function render() {
            ctx.clearRect(0, 0, width, height);

            for (let i = 0; i < count; i++) {
                let p = particles[i];
                p.x += p.vx;
                p.y += p.vy;

                if (p.x < 0 || p.x > width) p.vx *= -1;
                if (p.y < 0 || p.y > height) p.vy *= -1;

                ctx.beginPath();
                ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
                ctx.fillStyle = p.color;
                ctx.shadowBlur = 6;
                ctx.shadowColor = p.color;
                ctx.fill();
                ctx.shadowBlur = 0;

                for (let j = i + 1; j < count; j++) {
                    let p2 = particles[j];
                    let dist = Math.hypot(p.x - p2.x, p.y - p2.y);
                    if (dist < 110) {
                        ctx.beginPath();
                        ctx.moveTo(p.x, p.y);
                        ctx.lineTo(p2.x, p2.y);
                        ctx.strokeStyle = p.color;
                        ctx.globalAlpha = 1 - (dist / 110);
                        ctx.lineWidth = 0.4;
                        ctx.stroke();
                        ctx.globalAlpha = 1;
                    }
                }
            }
            requestAnimationFrame(render);
        }
        render();
    </script>
</body>
</html>"""

DASHBOARD_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard &mdash; Nagato Panel</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Plus Jakarta Sans', sans-serif; background-color: #06040c; }
        .font-tech { font-family: 'Orbitron', monospace; }
        .glass-card {
            background: linear-gradient(135deg, rgba(22, 12, 42, 0.75) 0%, rgba(13, 8, 25, 0.85) 100%);
            backdrop-filter: blur(16px);
            border: 1px solid rgba(139, 92, 246, 0.25);
        }
        .neon-border-pink {
            box-shadow: 0 0 15px rgba(255, 0, 127, 0.2), inset 0 0 15px rgba(255, 0, 127, 0.05);
            border-color: rgba(255, 0, 127, 0.4);
        }
        .neon-border-cyan {
            box-shadow: 0 0 15px rgba(0, 240, 255, 0.2), inset 0 0 15px rgba(0, 240, 255, 0.05);
            border-color: rgba(0, 240, 255, 0.4);
        }
        #cyberCanvas {
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            z-index: 0;
            pointer-events: none;
        }
    </style>
</head>
<body class="text-slate-100 min-h-screen flex overflow-x-hidden relative">
    <canvas id="cyberCanvas"></canvas>

    <div id="sidebarBackdrop" onclick="toggleSidebar()" class="fixed inset-0 bg-black/70 z-30 backdrop-blur-sm hidden md:hidden"></div>

    <aside id="sidebar" class="fixed inset-y-0 left-0 z-40 w-64 bg-[#090614] border-r border-purple-900/40 p-5 flex flex-col justify-between -translate-x-full md:translate-x-0 transition-transform duration-200 ease-in-out md:static md:h-screen">
        <div class="space-y-6">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-2.5">
                    <span class="text-lg">🚀</span>
                    <div>
                        <span class="font-tech font-bold text-sm text-transparent bg-clip-text bg-gradient-to-r from-fuchsia-400 to-cyan-300 tracking-wider block">Nagato Panel</span>
                        <span class="font-tech text-[10px] tracking-wider text-cyan-300 uppercase block">Pom Pom Bot</span>
                    </div>
                </div>
                <button onclick="toggleSidebar()" class="md:hidden text-purple-400 hover:text-white p-1">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
                </button>
            </div>

            <nav class="space-y-1 text-xs">
                <button onclick="switchTab('tab-dashboard')" id="nav-tab-dashboard" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-semibold bg-purple-900/40 border border-fuchsia-500/30 text-cyan-400 shadow-[0_0_12px_rgba(0,240,255,0.15)] transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/></svg>
                    Dashboard
                </button>
                <button onclick="switchTab('tab-orders')" id="nav-tab-orders" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-purple-900/30 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/></svg>
                    Customer Orders
                </button>
                <button onclick="switchTab('tab-users')" id="nav-tab-users" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-purple-900/30 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"/></svg>
                    Manage Users
                </button>
                <button onclick="switchTab('tab-settings')" id="nav-tab-settings" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-purple-900/30 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                    Setting &amp; UPI
                </button>
                <button onclick="switchTab('tab-bot')" id="nav-tab-bot" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-purple-900/30 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z"/></svg>
                    Bot Token Config
                </button>
                <button onclick="switchTab('tab-media')" id="nav-tab-media" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-purple-900/30 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
                    Media &amp; Greetings
                </button>
                <button onclick="switchTab('tab-plans')" id="nav-tab-plans" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-purple-900/30 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                    Plan Buttons
                </button>
            </nav>
        </div>

        <div class="pt-4 border-t border-purple-900/40 space-y-3">
            <a href="/logout" class="w-full flex items-center justify-center gap-2 py-2 rounded-xl text-xs font-mono font-semibold text-rose-400 hover:bg-rose-500/10 transition">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/></svg>
                Sign Out
            </a>
        </div>
    </aside>

    <main class="flex-1 flex flex-col min-w-0 h-screen overflow-y-auto relative z-10">
        <header class="sticky top-0 z-20 bg-[#06040c]/90 backdrop-blur-md border-b border-purple-900/40 px-4 md:px-8 py-3.5 flex items-center justify-between">
            <div class="flex items-center gap-3">
                <button onclick="toggleSidebar()" class="p-2 rounded-lg bg-[#120b22] border border-purple-900/40 text-purple-300 hover:text-white">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"/></svg>
                </button>
                <h2 id="sectionTitle" class="font-tech text-base md:text-lg font-bold text-white tracking-wider">Dashboard</h2>
            </div>
            
            <div class="flex items-center gap-3">
                <span class="text-xs text-purple-300 hidden sm:inline font-mono flex items-center gap-1">
                    Nagato
                </span>
                <span class="flex items-center gap-1.5 px-3 py-1 rounded-full bg-[#120b22] border border-cyan-500/40 text-xs font-mono text-cyan-300 shadow-[0_0_10px_rgba(0,240,255,0.2)]">
                    <span class="w-2 h-2 rounded-full {{ 'bg-cyan-400' if is_online else 'bg-rose-500' }} animate-pulse inline-block"></span>
                    {{ 'ONLINE' if is_online else 'OFFLINE' }}
                </span>
            </div>
        </header>

        <div class="p-4 md:p-8 max-w-5xl w-full mx-auto space-y-6">
            {% if message %}
            <div class="p-4 rounded-xl bg-cyan-500/10 border border-cyan-500/40 text-cyan-300 text-xs font-mono flex items-center gap-2 shadow-[0_0_15px_rgba(0,240,255,0.15)]">
                <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
                <span>{{ message }}</span>
            </div>
            {% endif %}

            <!-- TAB: DASHBOARD OVERVIEW -->
            <div id="tab-dashboard" class="tab-content space-y-6">
                <div class="grid grid-cols-2 lg:grid-cols-4 gap-3 md:gap-4">
                    <div class="glass-card rounded-2xl p-4 flex flex-col justify-between hover:border-cyan-500/40 transition">
                        <div class="flex items-center justify-between mb-2">
                            <span class="p-2 rounded-xl bg-cyan-500/10 text-cyan-400 border border-cyan-500/30">
                                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                            </span>
                        </div>
                        <div>
                            <span class="font-tech text-2xl md:text-3xl font-extrabold text-white tracking-tight">{{ paid_orders }}</span>
                            <p class="text-[11px] text-purple-300/80 font-mono mt-0.5 uppercase">Paid orders</p>
                        </div>
                    </div>

                    <div class="glass-card rounded-2xl p-4 flex flex-col justify-between hover:border-fuchsia-500/40 transition">
                        <div class="flex items-center justify-between mb-2">
                            <span class="p-2 rounded-xl bg-fuchsia-500/10 text-fuchsia-400 border border-fuchsia-500/30">
                                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 9V7a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2m2 4h10a2 2 0 002-2v-6a2 2 0 00-2-2H9a2 2 0 00-2 2v6a2 2 0 002 2zm7-5a2 2 0 11-4 0 2 2 0 014 0z"/></svg>
                            </span>
                            <form method="POST" action="/admin/revenue/reset" onsubmit="return confirm('Reset all revenue counters?');">
                                <button type="submit" class="text-[10px] text-purple-400 hover:text-fuchsia-300 flex items-center gap-1 font-mono">
                                    Reset
                                </button>
                            </form>
                        </div>
                        <div>
                            <span class="font-tech text-2xl md:text-3xl font-extrabold text-white tracking-tight">&#8377;{{ "%.2f"|format(revenue) }}</span>
                            <p class="text-[11px] text-purple-300/80 font-mono mt-0.5 uppercase">Revenue</p>
                        </div>
                    </div>

                    <div class="glass-card rounded-2xl p-4 flex flex-col justify-between hover:border-purple-500/40 transition">
                        <div class="flex items-center justify-between mb-2">
                            <span class="p-2 rounded-xl bg-purple-500/10 text-purple-300 border border-purple-500/30">
                                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"/></svg>
                            </span>
                        </div>
                        <div>
                            <span class="font-tech text-2xl md:text-3xl font-extrabold text-white tracking-tight">{{ total_users }}</span>
                            <p class="text-[11px] text-purple-300/80 font-mono mt-0.5 uppercase">Total users</p>
                        </div>
                    </div>

                    <div class="glass-card rounded-2xl p-4 flex flex-col justify-between neon-border-pink relative overflow-hidden">
                        <div class="absolute -right-6 -bottom-6 w-24 h-24 bg-fuchsia-500/20 rounded-full blur-xl pointer-events-none"></div>
                        <div class="flex items-center justify-between mb-2">
                            <span class="p-2 rounded-xl bg-fuchsia-500/10 text-fuchsia-400 border border-fuchsia-500/30">
                                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                            </span>
                            <span id="expiryBadge" class="text-[10px] font-tech px-2 py-0.5 rounded-md bg-fuchsia-500/20 text-fuchsia-300 border border-fuchsia-500/40">
                                ACTIVE
                            </span>
                        </div>
                        <div>
                            <span id="countdownTimer" class="font-tech text-lg md:text-xl font-black text-transparent bg-clip-text bg-gradient-to-r from-fuchsia-400 to-cyan-300">
                                Calculating...
                            </span>
                            <p class="text-[11px] text-purple-400 mt-1 font-mono">
                                Valid till: <span class="text-slate-300">{{ license_expiry }}</span>
                            </p>
                        </div>
                    </div>
                </div>

                <div class="glass-card rounded-2xl p-5 space-y-4">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-purple-900/40 pb-3">
                        <div>
                            <span class="text-xs text-cyan-400 font-mono">GATEWAY: DIRECT UPI QR</span>
                            <h3 class="font-tech text-base font-bold text-white tracking-wide">Recent orders</h3>
                        </div>
                        <button onclick="switchTab('tab-orders')" class="text-xs text-fuchsia-400 hover:text-fuchsia-300 font-mono">View All Orders &rarr;</button>
                    </div>

                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs font-mono">
                            <thead class="text-purple-400 uppercase text-[10px] border-b border-purple-900/40">
                                <tr>
                                    <th class="py-3 px-3">Code</th>
                                    <th class="py-3 px-3">UID</th>
                                    <th class="py-3 px-3">Item</th>
                                    <th class="py-3 px-3">Amount</th>
                                    <th class="py-3 px-3 text-right">Status</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-purple-900/30 text-slate-300">
                                {% for txn_id, cid, uname, item, amt, dt, st, oid in recent_orders %}
                                <tr class="hover:bg-purple-900/20 transition">
                                    <td class="py-3 px-3 text-cyan-400">{{ txn_id }}</td>
                                    <td class="py-3 px-3">
                                        {{ cid }}
                                        <span class="block text-[10px] text-purple-400">@{{ uname }}</span>
                                    </td>
                                    <td class="py-3 px-3 text-white font-sans">{{ item }}</td>
                                    <td class="py-3 px-3 font-semibold text-white">&#8377;{{ "%.2f"|format(amt) }}</td>
                                    <td class="py-3 px-3 text-right">
                                        {% if st == 'Paid' %}
                                        <span class="px-2.5 py-1 rounded-md text-[10px] font-tech bg-emerald-500/10 border border-emerald-500/40 text-emerald-400">PAID</span>
                                        {% elif st == 'Pending' %}
                                        <span class="px-2.5 py-1 rounded-md text-[10px] font-tech bg-amber-500/10 border border-amber-500/40 text-amber-400">PENDING</span>
                                        {% else %}
                                        <span class="px-2.5 py-1 rounded-md text-[10px] font-tech bg-rose-500/10 border border-rose-500/40 text-rose-400">REJECTED</span>
                                        {% endif %}
                                    </td>
                                </tr>
                                {% else %}
                                <tr>
                                    <td colspan="5" class="py-8 text-center text-purple-400 text-xs">No orders recorded yet.</td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- TAB: CUSTOMER ORDERS -->
            <div id="tab-orders" class="tab-content space-y-6 hidden">
                <div class="glass-card rounded-2xl p-5 space-y-4">
                    <div class="border-b border-purple-900/40 pb-3">
                        <span class="text-xs text-cyan-400 font-mono">NAGATO TRANSACTION ARCHIVE</span>
                        <h2 class="font-tech text-base font-bold text-white tracking-wide">Customer Orders</h2>
                    </div>

                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs font-mono">
                            <thead class="text-purple-400 uppercase text-[10px] border-b border-purple-900/40">
                                <tr>
                                    <th class="py-3 px-3">Order Code</th>
                                    <th class="py-3 px-3">Telegram User</th>
                                    <th class="py-3 px-3">Subscription Item</th>
                                    <th class="py-3 px-3">Amount</th>
                                    <th class="py-3 px-3">Timestamp</th>
                                    <th class="py-3 px-3">Status</th>
                                    <th class="py-3 px-3 text-right">Actions</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-purple-900/30 text-slate-300">
                                {% for txn_id, cid, uname, item, amt, dt, st, oid in all_orders %}
                                <tr class="hover:bg-purple-900/20 transition">
                                    <td class="py-3 px-3 text-cyan-400">{{ txn_id }}</td>
                                    <td class="py-3 px-3">
                                        {{ cid }}
                                        <span class="block text-[10px] text-purple-400">@{{ uname }}</span>
                                    </td>
                                    <td class="py-3 px-3 text-white font-sans">🌽 {{ item }}</td>
                                    <td class="py-3 px-3 font-semibold text-white">&#8377;{{ "%.2f"|format(amt) }}</td>
                                    <td class="py-3 px-3 text-purple-300/80 text-[11px]">{{ dt }}</td>
                                    <td class="py-3 px-3">
                                        {% if st == 'Paid' %}
                                        <span class="px-2.5 py-1 rounded-md text-[10px] font-tech bg-emerald-500/10 border border-emerald-500/40 text-emerald-400">PAID</span>
                                        {% elif st == 'Pending' %}
                                        <span class="px-2.5 py-1 rounded-md text-[10px] font-tech bg-amber-500/10 border border-amber-500/40 text-amber-400">PENDING</span>
                                        {% else %}
                                        <span class="px-2.5 py-1 rounded-md text-[10px] font-tech bg-rose-500/10 border border-rose-500/40 text-rose-400">REJECTED</span>
                                        {% endif %}
                                    </td>
                                    <td class="py-3 px-3 text-right whitespace-nowrap">
                                        <a href="/admin/order/update/{{ oid }}/Paid" class="bg-emerald-500/10 border border-emerald-500/40 hover:bg-emerald-500 text-emerald-400 hover:text-white px-2.5 py-1 rounded-lg text-[10px] font-tech transition mr-1">Paid</a>
                                        <a href="/admin/order/update/{{ oid }}/Rejected" class="bg-rose-500/10 border border-rose-500/40 hover:bg-rose-500 text-rose-400 hover:text-white px-2.5 py-1 rounded-lg text-[10px] font-tech transition">Reject</a>
                                    </td>
                                </tr>
                                {% else %}
                                <tr>
                                    <td colspan="7" class="py-12 text-center text-purple-400 text-xs">No orders recorded in database.</td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- TAB: MANAGE USERS -->
            <div id="tab-users" class="tab-content space-y-6 hidden">
                <div class="glass-card rounded-2xl p-5 space-y-4">
                    <div class="border-b border-purple-900/40 pb-3">
                        <span class="text-xs text-cyan-400 font-mono">DATABASE DIRECTORY</span>
                        <h3 class="font-tech text-base font-bold text-white tracking-wide">Registered Users</h3>
                    </div>

                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs font-mono">
                            <thead class="text-purple-400 uppercase text-[10px] border-b border-purple-900/40">
                                <tr>
                                    <th class="py-3 px-3">User &amp; Chat ID</th>
                                    <th class="py-3 px-3">Username</th>
                                    <th class="py-3 px-3">Joined Date</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-purple-900/30 text-slate-300">
                                {% for u_id, u_uname, u_joined in users_list %}
                                <tr class="hover:bg-purple-900/20 transition">
                                    <td class="py-3 px-3 text-cyan-400 font-mono">{{ u_id }}</td>
                                    <td class="py-3 px-3">
                                        {% if u_uname != 'N/A' %}
                                        <a href="https://t.me/{{ u_uname }}" target="_blank" class="text-fuchsia-400 hover:underline">@{{ u_uname }}</a>
                                        {% else %}
                                        <span class="text-purple-400/60">None</span>
                                        {% endif %}
                                    </td>
                                    <td class="py-3 px-3 text-purple-300/80 text-[11px]">{{ u_joined }}</td>
                                </tr>
                                {% else %}
                                <tr>
                                    <td colspan="3" class="py-12 text-center text-purple-400 text-xs">No registered users in database yet.</td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- TAB: SETTINGS & UPI -->
            <div id="tab-settings" class="tab-content space-y-6 hidden">
                <form method="POST" action="/admin/save-bot-settings" class="glass-card rounded-2xl p-6 space-y-5">
                    <div class="flex items-center gap-3 border-b border-purple-900/40 pb-4">
                        <div class="w-9 h-9 rounded-xl bg-cyan-500/15 text-cyan-400 border border-cyan-500/30 flex items-center justify-center">
                            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 10h18M7 15h1m4 0h1m-7 4h12a3 3 0 003-3V8a3 3 0 00-3-3H6a3 3 0 00-3 3v8a3 3 0 003 3z"/></svg>
                        </div>
                        <div>
                            <h3 class="font-tech text-base font-bold text-white tracking-wide">UPI Payment Configuration</h3>
                            <p class="text-xs text-purple-400/80">Configure payout UPI ID and merchant details.</p>
                        </div>
                    </div>

                    <input type="hidden" name="token" value="{{ token }}">
                    <div class="space-y-4">
                        <div>
                            <label class="block text-xs font-semibold text-purple-300 font-mono uppercase mb-2">UPI ID *</label>
                            <input type="text" name="upi_id" value="{{ upi_id }}" required placeholder="thesalesgod@nyes"
                                   class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-3 text-sm text-white font-mono focus:outline-none focus:border-cyan-400 transition">
                        </div>

                        <div>
                            <label class="block text-xs font-semibold text-purple-300 mb-2 font-mono uppercase">Payee Registered Name</label>
                            <input type="text" name="payee_name" value="{{ payee_name }}" required placeholder="Trusted Seller"
                                   class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-cyan-400 transition">
                        </div>

                        <div>
                            <label class="block text-xs font-semibold text-purple-300 mb-2 font-mono uppercase">Admin Telegram Chat ID</label>
                            <input type="text" name="admin_chat_id" value="{{ admin_chat_id }}" placeholder="e.g. 6528792525"
                                   class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-2.5 text-sm text-white font-mono focus:outline-none focus:border-cyan-400 transition">
                            <p class="text-[11px] text-purple-400/80 mt-1 font-mono">Screenshots and /upload bulk media commands are tied to this chat ID.</p>
                        </div>
                    </div>

                    <button type="submit" class="bg-gradient-to-r from-fuchsia-600 to-purple-600 hover:from-fuchsia-500 hover:to-purple-500 text-white font-tech font-bold px-5 py-2.5 rounded-xl text-xs uppercase tracking-wider shadow-lg shadow-fuchsia-600/30 transition">
                        Save UPI &amp; Payout Settings
                    </button>
                </form>
            </div>

            <!-- TAB: BOT TOKEN CONFIG -->
            <div id="tab-bot" class="tab-content space-y-5 hidden">
                <form method="POST" action="/admin/save-bot-settings" class="glass-card rounded-2xl p-6 space-y-5">
                    <h3 class="font-tech text-base font-bold text-white tracking-wide">Telegram Bot API Engine</h3>
                    <input type="hidden" name="upi_id" value="{{ upi_id }}">
                    <input type="hidden" name="payee_name" value="{{ payee_name }}">
                    <input type="hidden" name="admin_chat_id" value="{{ admin_chat_id }}">
                    <div>
                        <label class="block text-xs font-semibold text-purple-300 uppercase tracking-wider mb-2 font-mono">Bot Token</label>
                        <input type="text" name="token" value="{{ token }}" placeholder="123456:ABC-DEF1234..." required
                               class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-3 text-sm text-white font-mono focus:outline-none focus:border-cyan-400 transition">
                        <p class="text-xs text-purple-400/80 mt-2 font-mono">Updating your bot token restarts the engine and resets Webhook polling safely.</p>
                    </div>
                    <button type="submit" class="bg-gradient-to-r from-cyan-600 to-blue-600 hover:from-cyan-500 hover:to-blue-500 text-white font-tech font-bold px-5 py-2.5 rounded-xl text-xs uppercase tracking-wider transition">
                        Update &amp; Restart Bot
                    </button>
                </form>
            </div>

            <!-- TAB: MEDIA & GREETINGS -->
            <div id="tab-media" class="tab-content space-y-5 hidden">
                <form method="POST" action="/admin/save-message-settings" class="glass-card rounded-2xl p-6 space-y-5">
                    <h3 class="font-tech text-base font-bold text-white tracking-wide">Interface Templates &amp; Action Buttons</h3>
                    
                    <div>
                        <label class="block text-xs font-semibold text-purple-300 uppercase tracking-wider mb-2 font-mono">Welcome Caption</label>
                        <textarea name="welcome_caption" rows="4" required
                                  class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-cyan-400 transition">{{ welcome_caption }}</textarea>
                    </div>

                    <div>
                        <label class="block text-xs font-semibold text-purple-300 uppercase tracking-wider mb-2 font-mono">Banner Image URLs (One URL per line)</label>
                        <textarea name="welcome_images" rows="3"
                                  class="w-full bg-[#070410]/90 border border-purple-900/60 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-cyan-400 transition">{{ welcome_images_text }}</textarea>
                    </div>

                    <div class="border-t border-purple-900/40 pt-4 space-y-4">
                        <h4 class="font-tech text-xs font-bold text-cyan-400 tracking-wider">Bottom 3 Action Buttons</h4>
                        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                            <div class="p-3 bg-[#070410]/60 border border-purple-900/40 rounded-xl space-y-2">
                                <label class="block text-[11px] text-cyan-300 font-mono">Button 1 (Left)</label>
                                <input type="text" name="btn_how_to_use" value="{{ btn_how_to_use }}" required class="w-full bg-black/50 border border-purple-900/60 rounded-lg px-3 py-1.5 text-xs text-white">
                                <input type="text" name="msg_how_to_use" value="{{ msg_how_to_use }}" required placeholder="Popup Alert Message" class="w-full bg-black/50 border border-purple-900/60 rounded-lg px-3 py-1.5 text-xs text-slate-300">
                            </div>
                            <div class="p-3 bg-[#070410]/60 border border-purple-900/40 rounded-xl space-y-2">
                                <label class="block text-[11px] text-rose-400 font-mono">Button 2 (Right)</label>
                                <input type="text" name="btn_report_issue" value="{{ btn_report_issue }}" required class="w-full bg-black/50 border border-purple-900/60 rounded-lg px-3 py-1.5 text-xs text-white">
                                <input type="text" name="msg_report_issue" value="{{ msg_report_issue }}" required placeholder="Popup Alert Message" class="w-full bg-black/50 border border-purple-900/60 rounded-lg px-3 py-1.5 text-xs text-slate-300">
                            </div>
                        </div>

                        <div class="p-3 bg-[#070410]/60 border border-purple-900/40 rounded-xl space-y-2">
                            <label class="block text-[11px] text-cyan-300 font-mono">Button 3 (Full Width)</label>
                            <input type="text" name="btn_language" value="{{ btn_language }}" required class="w-full bg-black/50 border border-purple-900/60 rounded-lg px-3 py-1.5 text-xs text-white">
                            <input type="text" name="msg_language" value="{{ msg_language }}" required placeholder="Popup Alert Message" class="w-full bg-black/50 border border-purple-900/60 rounded-lg px-3 py-1.5 text-xs text-slate-300">
                        </div>
                    </div>

                    <button type="submit" class="w-full bg-gradient-to-r from-fuchsia-600 via-purple-600 to-cyan-600 text-white font-tech font-bold py-3.5 rounded-xl shadow-lg shadow-fuchsia-500/25 tracking-wider uppercase transition">
                        Save Interface Parameters
                    </button>
                </form>
            </div>

            <!-- TAB: 17 PLAN BUTTONS -->
            <div id="tab-plans" class="tab-content space-y-5 hidden">
                <div class="glass-card rounded-2xl p-5 space-y-2 border-l-4 border-cyan-400">
                    <h3 class="font-tech text-xs text-white font-bold tracking-wider">💡 Bulk In-Bot Media Uploading</h3>
                    <p class="text-xs text-purple-300/80 font-mono">
                        Send <code class="text-cyan-400">/upload 1</code> (or any button 1-17) inside Telegram to forward media directly. Send <code class="text-cyan-400">/done</code> when finished.
                    </p>
                </div>

                <form method="POST" action="/admin/save-packs" class="space-y-4">
                    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        {% for item in pack_cards %}
                        <div class="glass-card rounded-2xl p-4 flex flex-col justify-between space-y-3 border border-purple-900/40">
                            <div class="flex items-center justify-between border-b border-purple-900/30 pb-2">
                                <span class="text-xs font-tech text-cyan-400 font-bold">🟢 BUTTON #{{ item.index + 1 }}</span>
                                <span class="text-[10px] font-mono text-purple-400">{{ item.media_count }} items</span>
                            </div>

                            <div class="space-y-2 text-xs">
                                <div>
                                    <label class="block text-[10px] font-mono text-purple-300 uppercase">Button Label</label>
                                    <input type="text" name="btn_label_{{ item.index }}" value="{{ item.label }}" required
                                           class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white focus:outline-none focus:border-emerald-400 font-medium">
                                </div>

                                <div class="grid grid-cols-2 gap-2">
                                    <div>
                                        <label class="block text-[10px] font-mono text-purple-300 uppercase">Pack Name</label>
                                        <input type="text" name="pack_name_{{ item.index }}" value="{{ item.pack_name }}" required
                                               class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white">
                                    </div>
                                    <div>
                                        <label class="block text-[10px] font-mono text-purple-300 uppercase">Price (&#8377;)</label>
                                        <input type="text" name="pack_price_{{ item.index }}" value="{{ item.price }}" required
                                               class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white">
                                    </div>
                                </div>

                                <div>
                                    <label class="block text-[10px] font-mono text-purple-300 uppercase">Description (Card Detail)</label>
                                    <textarea name="pack_desc_{{ item.index }}" rows="2"
                                              class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white text-[11px]">{{ item.desc }}</textarea>
                                </div>

                                <div>
                                    <label class="block text-[10px] font-mono text-purple-300 uppercase">Stored Media (file_id or URL)</label>
                                    <textarea name="btn_videos_{{ item.index }}" rows="2" placeholder="One per line"
                                              class="w-full bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white text-[10px] font-mono">{{ item.videos }}</textarea>
                                </div>
                            </div>
                        </div>
                        {% endfor %}
                    </div>

                    <button type="submit" class="w-full bg-gradient-to-r from-emerald-600 via-teal-600 to-cyan-600 text-white font-tech font-bold py-3.5 rounded-xl shadow-lg shadow-emerald-500/25 tracking-wider uppercase transition">
                        Save All Plan Buttons
                    </button>
                </form>
            </div>

        </div>
    </main>

    <script>
        const canvas = document.getElementById('cyberCanvas');
        const ctx = canvas.getContext('2d');
        let width = canvas.width = window.innerWidth;
        let height = canvas.height = window.innerHeight;

        window.addEventListener('resize', () => {
            width = canvas.width = window.innerWidth;
            height = canvas.height = window.innerHeight;
        });

        const particles = [];
        const count = 45;
        const colors = ['#ff007f', '#00f0ff', '#a855f7'];

        for (let i = 0; i < count; i++) {
            particles.push({
                x: Math.random() * width,
                y: Math.random() * height,
                vx: (Math.random() - 0.5) * 0.7,
                vy: (Math.random() - 0.5) * 0.7,
                radius: Math.random() * 2 + 1,
                color: colors[Math.floor(Math.random() * colors.length)]
            });
        }

        function render() {
            ctx.clearRect(0, 0, width, height);

            for (let i = 0; i < count; i++) {
                let p = particles[i];
                p.x += p.vx;
                p.y += p.vy;

                if (p.x < 0 || p.x > width) p.vx *= -1;
                if (p.y < 0 || p.y > height) p.vy *= -1;

                ctx.beginPath();
                ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
                ctx.fillStyle = p.color;
                ctx.shadowBlur = 8;
                ctx.shadowColor = p.color;
                ctx.fill();
                ctx.shadowBlur = 0;

                for (let j = i + 1; j < count; j++) {
                    let p2 = particles[j];
                    let dist = Math.hypot(p.x - p2.x, p.y - p2.y);
                    if (dist < 130) {
                        ctx.beginPath();
                        ctx.moveTo(p.x, p.y);
                        ctx.lineTo(p2.x, p2.y);
                        ctx.strokeStyle = p.color;
                        ctx.globalAlpha = 1 - (dist / 130);
                        ctx.lineWidth = 0.5;
                        ctx.stroke();
                        ctx.globalAlpha = 1;
                    }
                }
            }
            requestAnimationFrame(render);
        }
        render();

        function toggleSidebar() {
            const sidebar = document.getElementById('sidebar');
            const backdrop = document.getElementById('sidebarBackdrop');
            sidebar.classList.toggle('-translate-x-full');
            backdrop.classList.toggle('hidden');
        }

        const titles = {
            'tab-dashboard': 'Dashboard',
            'tab-orders': 'Customer Orders',
            'tab-users': 'Manage Users',
            'tab-settings': 'Setting & UPI',
            'tab-bot': 'Bot Token Config',
            'tab-media': 'Media & Greetings',
            'tab-plans': 'Plan Buttons'
        };

        function switchTab(tabId) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
            
            const target = document.getElementById(tabId);
            if (target) target.classList.remove('hidden');

            document.getElementById('sectionTitle').innerText = titles[tabId] || 'Nagato Panel';

            document.querySelectorAll('.nav-btn').forEach(btn => {
                btn.classList.remove('bg-purple-900/40', 'border', 'border-fuchsia-500/30', 'text-cyan-400', 'shadow-[0_0_12px_rgba(0,240,255,0.15)]');
                btn.classList.add('text-slate-400');
            });
            const activeNav = document.getElementById('nav-' + tabId);
            if (activeNav) {
                activeNav.classList.add('bg-purple-900/40', 'border', 'border-fuchsia-500/30', 'text-cyan-400', 'shadow-[0_0_12px_rgba(0,240,255,0.15)]');
                activeNav.classList.remove('text-slate-400');
            }

            if (window.innerWidth < 768) {
                const sidebar = document.getElementById('sidebar');
                if (!sidebar.classList.contains('-translate-x-full')) {
                    toggleSidebar();
                }
            }
        }

        const urlParams = new URLSearchParams(window.location.search);
        const requestedTab = urlParams.get('tab');
        if (requestedTab && titles[requestedTab]) {
            switchTab(requestedTab);
        }

        const expiryDateStr = "{{ license_expiry }}";
        const expiryDate = new Date(expiryDateStr.replace(' ', 'T')).getTime();

        function updateCountdown() {
            const now = new Date().getTime();
            const distance = expiryDate - now;

            const timerEl = document.getElementById("countdownTimer");
            const badgeEl = document.getElementById("expiryBadge");

            if (!timerEl || !badgeEl) return;

            if (isNaN(distance) || distance <= 0) {
                timerEl.innerText = "EXPIRED";
                timerEl.className = "font-tech text-lg md:text-xl font-extrabold text-rose-400 tracking-tight";
                badgeEl.innerText = "EXPIRED";
                badgeEl.className = "text-[10px] font-tech px-2 py-0.5 rounded-md bg-rose-500/20 text-rose-400 border border-rose-500/40";
                return;
            }

            const days = Math.floor(distance / (1000 * 60 * 60 * 24));
            const hours = Math.floor((distance % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
            const minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
            const seconds = Math.floor((distance % (1000 * 60)) / 1000);

            timerEl.innerText = days + "d " + hours + "h " + minutes + "m " + seconds + "s";
        }

        setInterval(updateCountdown, 1000);
        updateCountdown();
    </script>
</body>
</html>"""

# ==========================================
# AUTHENTICATION & FASTAPI ROUTES
# ==========================================
async def require_admin(request: Request):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token != AUTH_SECRET:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return True

@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str | None = None):
    if request.cookies.get(AUTH_COOKIE_NAME) == AUTH_SECRET:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    tmpl = Template(LOGIN_PAGE)
    return HTMLResponse(content=tmpl.render(error=error))

@app.post("/login")
async def process_login(username: str = Form(...), password: str = Form(...)):
    cfg = get_settings()
    if username == cfg["admin_user"] and password == cfg["admin_pass"]:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key=AUTH_COOKIE_NAME, value=AUTH_SECRET, httponly=True, max_age=86400 * 7)
        return response
    tmpl = Template(LOGIN_PAGE)
    return HTMLResponse(content=tmpl.render(error="Invalid administrator credentials."), status_code=status.HTTP_401_UNAUTHORIZED)

@app.get("/logout")
async def logout_admin():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=AUTH_COOKIE_NAME)
    return response

@app.get("/", response_class=HTMLResponse)
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard_view(request: Request, message: str | None = None, is_auth: bool = Depends(require_admin)):
    cfg = get_settings()
    total_users, paid_orders, revenue, recent_orders = get_stats()
    all_orders = get_all_orders()

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT chat_id, username, joined_at FROM users ORDER BY joined_at DESC")
    users_list = c.fetchall()
    conn.close()

    pack_cards = []
    for idx in range(17):
        b_name = cfg["buttons"][idx] if idx < len(cfg["buttons"]) else f"VIP Pack {idx+1}"
        v_list = cfg["button_videos"].get(str(idx), [])
        details = cfg["button_details"].get(str(idx), {
            "pack": f"VIP Pack {idx + 1}",
            "price": "299",
            "desc": "Full HD streaming bundle."
        })
        pack_cards.append({
            "index": idx,
            "label": b_name,
            "pack_name": details.get("pack", ""),
            "price": details.get("price", "299"),
            "desc": details.get("desc", ""),
            "videos": "\n".join(v_list),
            "media_count": len(v_list)
        })

    tmpl = Template(DASHBOARD_PAGE)
    html = tmpl.render(
        message=message,
        is_online=(bot_manager.status == "Running"),
        paid_orders=paid_orders,
        revenue=revenue,
        total_users=total_users,
        recent_orders=recent_orders,
        all_orders=all_orders,
        users_list=users_list,
        license_expiry=cfg["license_expiry"],
        token=cfg["token"],
        upi_id=cfg["upi_id"],
        payee_name=cfg["payee_name"],
        admin_chat_id=cfg["admin_chat_id"],
        welcome_caption=cfg["welcome_caption"],
        welcome_images_text="\n".join(cfg["welcome_images"]),
        btn_how_to_use=cfg["btn_how_to_use"],
        msg_how_to_use=cfg["msg_how_to_use"],
        btn_report_issue=cfg["btn_report_issue"],
        msg_report_issue=cfg["msg_report_issue"],
        btn_language=cfg["btn_language"],
        msg_language=cfg["msg_language"],
        pack_cards=pack_cards
    )
    return HTMLResponse(content=html)

@app.get("/admin/order/update/{order_id}/{new_status}")
async def change_order_status(order_id: int, new_status: str, is_auth: bool = Depends(require_admin)):
    if new_status in ["Paid", "Rejected", "Pending"]:
        update_order_status(order_id, new_status)
    return RedirectResponse(url="/?tab=tab-orders&message=Order+status+updated", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/save-bot-settings")
async def save_bot_settings(
    token: str = Form(...),
    upi_id: str = Form(...),
    payee_name: str = Form(...),
    admin_chat_id: str = Form(""),
    is_auth: bool = Depends(require_admin),
):
    cleaned_token = token.strip()
    update_field("token", cleaned_token)
    update_field("upi_id", upi_id.strip())
    update_field("payee_name", payee_name.strip())
    update_field("admin_chat_id", admin_chat_id.strip())

    await bot_manager.restart(cleaned_token)
    return RedirectResponse(url="/?tab=tab-settings&message=Settings+and+bot+engine+updated", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/save-message-settings")
async def save_message_settings(
    welcome_caption: str = Form(...),
    welcome_images: str = Form(""),
    btn_how_to_use: str = Form(...),
    msg_how_to_use: str = Form(...),
    btn_report_issue: str = Form(...),
    msg_report_issue: str = Form(...),
    btn_language: str = Form(...),
    msg_language: str = Form(...),
    is_auth: bool = Depends(require_admin),
):
    raw_images = [img.strip() for img in welcome_images.splitlines() if img.strip()]
    update_field("welcome_images_json", json.dumps(raw_images))
    update_field("welcome_caption", welcome_caption.strip())
    update_field("btn_how_to_use", btn_how_to_use.strip())
    update_field("btn_report_issue", btn_report_issue.strip())
    update_field("btn_language", btn_language.strip())
    update_field("msg_how_to_use", msg_how_to_use.strip())
    update_field("msg_report_issue", msg_report_issue.strip())
    update_field("msg_language", msg_language.strip())

    return RedirectResponse(url="/?tab=tab-media&message=Messages+and+actions+saved", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/save-packs")
async def save_packs_settings(request: Request, is_auth: bool = Depends(require_admin)):
    form = await request.form()
    buttons = []
    button_videos = {}
    button_details = {}

    for idx in range(17):
        btn_label = form.get(f"btn_label_{idx}", f"VIP Pack {idx+1}").strip()
        buttons.append(btn_label)

        raw_vids = form.get(f"btn_videos_{idx}", "").splitlines()
        button_videos[str(idx)] = [v.strip() for v in raw_vids if v.strip()]

        button_details[str(idx)] = {
            "pack": form.get(f"pack_name_{idx}", f"VIP Pack {idx+1}").strip(),
            "price": form.get(f"pack_price_{idx}", "0").strip(),
            "desc": form.get(f"pack_desc_{idx}", "").strip(),
        }

    update_field("buttons_json", json.dumps(buttons))
    update_field("button_videos_json", json.dumps(button_videos))
    update_field("button_details_json", json.dumps(button_details))

    return RedirectResponse(url="/?tab=tab-plans&message=All+17+plan+buttons+saved", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/revenue/reset")
async def reset_revenue_stats(is_auth: bool = Depends(require_admin)):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM orders")
    conn.commit()
    conn.close()
    return RedirectResponse(url="/?tab=tab-dashboard&message=Revenue+counters+reset", status_code=status.HTTP_303_SEE_OTHER)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("pomm:app", host="0.0.0.0", port=port)
