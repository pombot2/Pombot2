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
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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

# ==========================================================
# CONFIG & AUTHENTICATION
# ==========================================================
ADMIN_USER = os.getenv("ADMIN_USER", "nagato")
DEFAULT_PASS = os.getenv("ADMIN_PASS", "nagato@123")
AUTH_COOKIE_NAME = "session_token"
AUTH_SECRET = "admin_authenticated_session_key_99"

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.join(os.getcwd(), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_NAME = os.path.join(DATA_DIR, "pomm_database.db")

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

# --- Keyboard Builders ---
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
        f"You can now send or forward **photos, videos, or documents** in bulk. "
        f"They will automatically attach to this button.\n\n"
        f"When you are finished, type `/done` to exit upload mode."
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

# --- Media Router: Bulk Admin Upload OR Screenshot Verification ---
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

# --- FastAPI Server Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    if cfg["token"]:
        await bot_manager.restart(cfg["token"])
    yield
    await bot_manager.stop()

app = FastAPI(lifespan=lifespan)

# --- Smooth CSS + Backdrop Layer ---
THEME_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Rajdhani:wght@500;600;700&display=swap');
    :root {
        --bg-dark: #070913;
        --card-bg: rgba(18, 17, 34, 0.78);
        --card-border: rgba(168, 85, 247, 0.25);
        --card-border-glow: rgba(168, 85, 247, 0.5);
        --cyan-glow: #00f0ff;
        --purple-glow: #c084fc;
        --magenta-btn: linear-gradient(135deg, #a855f7 0%, #06b6d4 100%);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
        font-family: 'Rajdhani', sans-serif;
        background-color: var(--bg-dark);
        color: #e2e8f0;
        min-height: 100vh;
        position: relative;
        overflow-x: hidden;
    }
    #particles-canvas {
        position: fixed;
        top: 0; left: 0; width: 100%; height: 100%;
        z-index: 0;
        pointer-events: none;
    }
    .main-wrapper {
        position: relative;
        z-index: 10;
        display: flex;
        flex-direction: column;
        min-height: 100vh;
    }

    #sidebar-backdrop {
        position: fixed;
        top: 0; left: 0; width: 100%; height: 100%;
        background: rgba(0, 0, 0, 0.7);
        backdrop-filter: blur(5px);
        -webkit-backdrop-filter: blur(5px);
        z-index: 90;
        opacity: 0;
        pointer-events: none;
        transition: opacity 0.32s cubic-bezier(0.25, 1, 0.5, 1);
    }
    #sidebar-backdrop.active {
        opacity: 1;
        pointer-events: auto;
    }

    .navbar {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 16px 24px;
        background: rgba(10, 11, 24, 0.85);
        backdrop-filter: blur(14px);
        border-bottom: 1px solid rgba(168, 85, 247, 0.2);
    }
    .nav-brand {
        display: flex;
        align-items: center;
        gap: 12px;
        font-family: 'Orbitron', sans-serif;
        font-size: 18px;
        color: #fff;
        text-shadow: 0 0 10px rgba(0, 240, 255, 0.5);
    }
    .nav-toggle-btn {
        background: transparent;
        border: 1px solid var(--card-border);
        color: #fff;
        font-size: 20px;
        padding: 6px 12px;
        border-radius: 6px;
        cursor: pointer;
        transition: all 0.25s ease;
    }
    .nav-toggle-btn:hover {
        border-color: var(--cyan-glow);
        box-shadow: 0 0 10px rgba(0, 240, 255, 0.4);
    }

    .status-badge-online {
        font-family: 'Orbitron', sans-serif;
        font-size: 11px;
        letter-spacing: 1px;
        padding: 5px 14px;
        border-radius: 20px;
        background: rgba(6, 182, 212, 0.15);
        color: var(--cyan-glow);
        border: 1px solid var(--cyan-glow);
        box-shadow: 0 0 12px rgba(0, 240, 255, 0.45);
        animation: pulseOnline 2.5s infinite ease-in-out;
    }
    .status-badge-offline {
        font-family: 'Orbitron', sans-serif;
        font-size: 11px;
        letter-spacing: 1px;
        padding: 5px 14px;
        border-radius: 20px;
        background: rgba(239, 68, 68, 0.15);
        color: #f87171;
        border: 1px solid #ef4444;
        box-shadow: 0 0 12px rgba(239, 68, 68, 0.4);
    }
    @keyframes pulseOnline {
        0%, 100% { box-shadow: 0 0 8px rgba(0, 240, 255, 0.3); }
        50% { box-shadow: 0 0 18px rgba(0, 240, 255, 0.7); }
    }

    .sidebar {
        position: fixed;
        top: 0; left: 0; width: 290px; height: 100%;
        background: rgba(12, 12, 27, 0.98);
        backdrop-filter: blur(24px);
        -webkit-backdrop-filter: blur(24px);
        border-right: 1px solid var(--card-border-glow);
        box-shadow: 20px 0 45px rgba(0,0,0,0.85);
        z-index: 100;
        transform: translate3d(-100%, 0, 0);
        transition: transform 0.32s cubic-bezier(0.25, 1, 0.5, 1);
        padding: 24px;
        display: flex;
        flex-direction: column;
    }
    .sidebar.active {
        transform: translate3d(0, 0, 0);
    }
    .sidebar-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 28px;
        font-family: 'Orbitron', sans-serif;
        color: var(--cyan-glow);
    }
    .sidebar-close {
        background: transparent;
        border: none;
        color: #94a3b8;
        font-size: 22px;
        cursor: pointer;
    }
    .sidebar-nav a {
        display: flex;
        align-items: center;
        gap: 10px;
        color: #fff;
        text-decoration: none;
        padding: 12px 16px;
        border-radius: 8px;
        margin-bottom: 10px;
        background: rgba(168, 85, 247, 0.12);
        border: 1px solid rgba(168, 85, 247, 0.25);
        font-weight: 600;
        font-size: 14px;
        transition: all 0.22s ease;
    }
    .sidebar-nav a:hover, .sidebar-nav a.active {
        background: rgba(0, 240, 255, 0.18);
        border-color: var(--cyan-glow);
        transform: translateX(4px);
        box-shadow: 0 0 15px rgba(0, 240, 255, 0.3);
    }
    .sidebar-footer { margin-top: auto; }
    .btn-signout {
        display: block;
        text-align: center;
        text-decoration: none;
        color: #f87171;
        padding: 10px;
        border: 1px solid rgba(239, 68, 68, 0.4);
        border-radius: 8px;
        font-weight: bold;
    }

    .dashboard-container {
        max-width: 1240px;
        margin: 20px auto;
        padding: 16px;
        width: 100%;
    }
    .stats-grid {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 16px;
        margin-bottom: 24px;
    }
    .stat-card {
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 14px;
        padding: 18px;
        box-shadow: 0 0 15px rgba(168, 85, 247, 0.15);
        transition: transform 0.2s ease;
    }
    .stat-card:hover { transform: translateY(-2px); }
    .stat-card h4 {
        font-size: 13px;
        color: #94a3b8;
        letter-spacing: 1px;
        text-transform: uppercase;
        margin-bottom: 8px;
    }
    .stat-card .val {
        font-family: 'Orbitron', sans-serif;
        font-size: 24px;
        color: #fff;
        text-shadow: 0 0 10px rgba(192, 132, 252, 0.6);
    }
    .grid-3-col {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 18px;
        margin-bottom: 20px;
    }
    @media (max-width: 1024px) {
        .grid-3-col { grid-template-columns: repeat(2, 1fr); }
        .stats-grid { grid-template-columns: repeat(2, 1fr); }
    }
    @media (max-width: 700px) {
        .grid-3-col { grid-template-columns: 1fr; }
        .stats-grid { grid-template-columns: 1fr; }
    }

    .cyber-card {
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 14px;
        padding: 20px;
        box-shadow: 0 0 20px rgba(168, 85, 247, 0.12);
        position: relative;
    }
    .cyber-card h3 {
        font-family: 'Orbitron', sans-serif;
        font-size: 15px;
        color: var(--cyan-glow);
        margin-bottom: 16px;
    }
    label {
        display: block;
        font-size: 13px;
        font-weight: 700;
        color: #cbd5e1;
        margin-bottom: 6px;
    }
    input[type="text"], input[type="password"], textarea {
        width: 100%;
        background: rgba(7, 9, 19, 0.85);
        border: 1px solid #3b4261;
        border-radius: 8px;
        padding: 10px 12px;
        color: #fff;
        font-family: 'Rajdhani', sans-serif;
        font-size: 14px;
        margin-bottom: 12px;
    }
    input:focus, textarea:focus {
        outline: none;
        border-color: var(--cyan-glow);
        box-shadow: 0 0 12px rgba(0, 240, 255, 0.35);
    }
    .btn-gradient {
        width: 100%;
        background: var(--magenta-btn);
        color: #fff;
        border: none;
        padding: 12px 18px;
        border-radius: 8px;
        font-family: 'Orbitron', sans-serif;
        font-size: 13px;
        font-weight: 700;
        cursor: pointer;
        box-shadow: 0 0 15px rgba(168, 85, 247, 0.5);
    }

    table {
        width: 100%;
        border-collapse: collapse;
        font-size: 14px;
    }
    th, td {
        padding: 14px 10px;
        text-align: left;
        border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        white-space: nowrap;
    }
    th {
        color: #94a3b8;
        font-family: 'Orbitron', sans-serif;
        font-size: 11px;
        letter-spacing: 0.5px;
    }
    
    .badge-pill {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 700;
        text-transform: capitalize;
        white-space: nowrap;
    }
    .badge-paid { background: rgba(34, 197, 94, 0.18); color: #4ade80; border: 1px solid #22c55e; }
    .badge-pending { background: rgba(234, 179, 8, 0.18); color: #facc15; border: 1px solid #eab308; }
    .badge-rejected { background: rgba(239, 68, 68, 0.18); color: #f87171; border: 1px solid #ef4444; }

    .action-cell {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        white-space: nowrap;
    }
    .action-btn-sm {
        display: inline-block;
        background: transparent;
        padding: 5px 12px;
        border-radius: 4px;
        font-size: 12px;
        font-weight: bold;
        cursor: pointer;
        text-decoration: none;
        transition: all 0.2s ease;
        text-align: center;
    }
    .btn-accept { border: 1px solid #22c55e; color: #4ade80; }
    .btn-accept:hover { background: rgba(34, 197, 94, 0.25); }
    .btn-reject { border: 1px solid #ef4444; color: #f87171; }
    .btn-reject:hover { background: rgba(239, 68, 68, 0.25); }

    .login-container {
        display: flex;
        justify-content: center;
        align-items: center;
        min-height: 85vh;
        padding: 20px;
    }
    .login-card {
        width: 100%;
        max-width: 400px;
        background: rgba(18, 17, 34, 0.88);
        border: 1px solid rgba(168, 85, 247, 0.4);
        border-radius: 20px;
        padding: 34px 28px;
        box-shadow: 0 0 35px rgba(168, 85, 247, 0.35);
        backdrop-filter: blur(16px);
        text-align: center;
    }
    .login-header { margin-bottom: 26px; }
    .login-header h2 { font-family: 'Orbitron', sans-serif; font-size: 22px; color: #fff; }
    .login-header p { font-family: 'Orbitron', sans-serif; font-size: 11px; color: var(--cyan-glow); letter-spacing: 2px; margin-top: 6px; }
    .contact-dev-btn {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        margin-top: 20px;
        color: var(--cyan-glow);
        text-decoration: none;
        font-size: 14px;
        font-weight: 600;
        transition: all 0.25s ease;
    }
    .contact-dev-btn:hover {
        color: #fff;
        text-shadow: 0 0 12px var(--cyan-glow);
    }
</style>
"""

# --- Canvas & Live Countdown Script ---
CANVAS_SCRIPT = """
<canvas id="particles-canvas"></canvas>
<div id="sidebar-backdrop" onclick="toggleSidebar()"></div>
<script>
    const canvas = document.getElementById('particles-canvas');
    const ctx = canvas.getContext('2d');
    let width = canvas.width = window.innerWidth;
    let height = canvas.height = window.innerHeight;

    window.addEventListener('resize', () => {
        width = canvas.width = window.innerWidth;
        height = canvas.height = window.innerHeight;
    });

    const particles = [];
    for (let i = 0; i < 48; i++) {
        particles.push({
            x: Math.random() * width,
            y: Math.random() * height,
            vx: (Math.random() - 0.5) * 0.35,
            vy: (Math.random() - 0.5) * 0.35,
            color: Math.random() > 0.5 ? '#00f0ff' : '#c084fc',
            radius: Math.random() * 2 + 1
        });
    }

    function animate() {
        ctx.clearRect(0, 0, width, height);
        for (let i = 0; i < particles.length; i++) {
            let p = particles[i];
            p.x += p.vx;
            p.y += p.vy;
            if (p.x < 0) p.x = width;
            if (p.x > width) p.x = 0;
            if (p.y < 0) p.y = height;
            if (p.y > height) p.y = 0;

            ctx.beginPath();
            ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
            ctx.fillStyle = p.color;
            ctx.shadowBlur = 8;
            ctx.shadowColor = p.color;
            ctx.fill();

            for (let j = i + 1; j < particles.length; j++) {
                let p2 = particles[j];
                let dist = Math.hypot(p.x - p2.x, p.y - p2.y);
                if (dist < 135) {
                    ctx.beginPath();
                    ctx.moveTo(p.x, p.y);
                    ctx.lineTo(p2.x, p2.y);
                    ctx.strokeStyle = `rgba(168, 85, 247, ${1 - dist / 135})`;
                    ctx.lineWidth = 0.55;
                    ctx.stroke();
                }
            }
        }
        requestAnimationFrame(animate);
    }
    animate();

    function toggleSidebar() {
        const sb = document.getElementById('sidebar');
        const bd = document.getElementById('sidebar-backdrop');
        sb.classList.toggle('active');
        bd.classList.toggle('active');
    }

    function updateCountdown() {
        const timerEl = document.getElementById('live-countdown');
        if (!timerEl) return;
        const expiryStr = timerEl.getAttribute('data-expiry');
        if (!expiryStr) return;

        const target = new Date(expiryStr.replace(' ', 'T')).getTime();
        const now = new Date().getTime();
        const diff = target - now;

        if (diff <= 0) {
            timerEl.innerHTML = "EXPIRED";
            timerEl.style.color = "#ef4444";
            return;
        }

        const d = Math.floor(diff / (1000 * 60 * 60 * 24));
        const h = Math.floor((diff % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
        const m = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
        const s = Math.floor((diff % (1000 * 60)) / 1000);

        timerEl.innerHTML = `${d}d ${h}h ${m}m ${s}s`;
    }
    setInterval(updateCountdown, 1000);
    updateCountdown();
</script>
"""

def render_navbar_badge():
    cfg = get_settings()
    if cfg["token"] and bot_manager.status == "Running":
        return '<div class="status-badge-online">● ONLINE</div>'
    return '<div class="status-badge-offline">○ OFFLINE</div>'

def render_sidebar(active_page="dashboard"):
    return f"""
    <div class="sidebar" id="sidebar">
        <div class="sidebar-header">
            <div>
                <div style="font-size: 16px;">🚀 Nagato Panel</div>
                <div style="font-size: 10px; color: #94a3b8;">POM POM BOT</div>
            </div>
            <button class="sidebar-close" onclick="toggleSidebar()">✕</button>
        </div>
        <div class="sidebar-nav">
            <a href="/admin" class="{'active' if active_page=='dashboard' else ''}">📊 Dashboard Overview</a>
            <a href="/admin/orders" class="{'active' if active_page=='orders' else ''}">📜 Customer Orders</a>
            <a href="/admin/bot-settings" class="{'active' if active_page=='bot-settings' else ''}">⚙️ Bot & Payment Settings</a>
            <a href="/admin/message-settings" class="{'active' if active_page=='message-settings' else ''}">💬 Messages & Buttons</a>
            <a href="/admin/packs" class="{'active' if active_page=='packs' else ''}">📦 Plan Buttons</a>
        </div>
        <div class="sidebar-footer">
            <a href="/logout" class="btn-signout">↪ Sign Out</a>
        </div>
    </div>
    """

def is_authenticated(request: Request) -> bool:
    return request.cookies.get(AUTH_COOKIE_NAME) == AUTH_SECRET

# --- Login & Authentication ---
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/admin", status_code=303)

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Admin Login — Nagato Panel</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        {THEME_CSS}
    </head>
    <body>
        {CANVAS_SCRIPT}
        <div class="main-wrapper">
            <div class="login-container">
                <div class="login-card">
                    <div class="login-header">
                        <h2>🚀 Nagato Panel</h2>
                        <p>ADMIN PANEL</p>
                    </div>
                    <form action="/login" method="post">
                        <div style="text-align: left;">
                            <label>Username</label>
                            <input type="text" name="username" placeholder="Username" required />
                            <label>Password</label>
                            <input type="password" name="password" placeholder="Password" required />
                        </div>
                        <button type="submit" class="btn-gradient">Sign in →</button>
                    </form>

                    <a href="https://t.me/NAGATOxOWNER" target="_blank" class="contact-dev-btn">
                        ✈️ Contact Developer
                    </a>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

@app.post("/login")
async def do_login(request: Request):
    form = await request.form()
    user = form.get("username", "").strip()
    pwd = form.get("password", "").strip()
    cfg = get_settings()

    if user == cfg["admin_user"] and pwd == cfg["admin_pass"]:
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie(key=AUTH_COOKIE_NAME, value=AUTH_SECRET, httponly=True)
        return response
    return RedirectResponse(url="/login?error=invalid", status_code=303)

@app.get("/logout")
async def do_logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response

# --- 1. Dashboard Overview ---
@app.get("/", response_class=HTMLResponse)
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)

    cfg = get_settings()
    total_users, paid_orders, revenue, recent_orders = get_stats()

    table_rows = ""
    if recent_orders:
        for ord in recent_orders:
            st = ord[6]
            badge_cls = "badge-paid" if st == "Paid" else ("badge-rejected" if st == "Rejected" else "badge-pending")
            table_rows += f"""
            <tr>
                <td style="color: var(--cyan-glow); font-family: 'Orbitron', monospace;">{ord[0]}</td>
                <td>{ord[1]}<br><span style="color:#94a3b8; font-size:12px;">@{ord[2]}</span></td>
                <td>{ord[3]}</td>
                <td>₹{ord[4]:.2f}</td>
                <td><span class="badge-pill {badge_cls}">{st}</span></td>
            </tr>
            """
    else:
        table_rows = "<tr><td colspan='5' style='text-align:center; color:#94a3b8;'>No orders recorded yet.</td></tr>"

    status_tag = render_navbar_badge()

    expiry_dt = datetime.strptime(cfg["license_expiry"], "%Y-%m-%d %H:%M:%S")
    time_left = expiry_dt - datetime.now()
    days = max(time_left.days, 0)
    hours, remainder = divmod(max(time_left.seconds, 0), 3600)
    minutes, seconds = divmod(remainder, 60)
    timer_initial = f"{days}d {hours}h {minutes}m {seconds}s"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Dashboard — Nagato Panel</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        {THEME_CSS}
    </head>
    <body>
        {CANVAS_SCRIPT}
        {render_sidebar("dashboard")}

        <div class="main-wrapper">
            <div class="navbar">
                <div class="nav-brand">
                    <button class="nav-toggle-btn" onclick="toggleSidebar()">☰</button>
                    <span>Dashboard Overview</span>
                </div>
                {status_tag}
            </div>

            <div class="dashboard-container">
                <div class="stats-grid">
                    <div class="stat-card">
                        <h4>Paid Orders</h4>
                        <div class="val">{paid_orders}</div>
                    </div>
                    <div class="stat-card">
                        <h4>Revenue</h4>
                        <div class="val">₹{revenue:.2f}</div>
                    </div>
                    <div class="stat-card">
                        <h4>Total Users</h4>
                        <div class="val">{total_users}</div>
                    </div>
                    <div class="stat-card" style="border: 1px solid rgba(168, 85, 247, 0.45);">
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <h4>Validity</h4>
                            <span class="status-badge-online" style="padding:2px 8px; font-size:9px;">ACTIVE</span>
                        </div>
                        <div class="val" id="live-countdown" data-expiry="{cfg['license_expiry']}" style="font-size: 19px; color: var(--cyan-glow); margin-top: 6px;">{timer_initial}</div>
                        <div style="font-size: 11px; color: #94a3b8; margin-top: 4px;">Valid till: {cfg['license_expiry']}</div>
                    </div>
                </div>

                <div class="cyber-card">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 12px;">
                        <span style="font-family:'Orbitron',sans-serif; color:var(--cyan-glow); font-size:14px;">Recent Orders</span>
                        <a href="/admin/orders" style="color:var(--purple-glow); font-size:13px; text-decoration:none; font-weight:bold;">View All Orders →</a>
                    </div>
                    <div style="overflow-x: auto;">
                        <table>
                            <thead>
                                <tr>
                                    <th>ORDER CODE</th>
                                    <th>TELEGRAM USER</th>
                                    <th>ITEM</th>
                                    <th>AMOUNT</th>
                                    <th>STATUS</th>
                                </tr>
                            </thead>
                            <tbody>
                                {table_rows}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

# --- 2. Customer Orders View Page ---
@app.get("/admin/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)

    orders = get_all_orders()
    order_rows = ""
    for ord in orders:
        st = ord[6]
        badge_cls = "badge-paid" if st == "Paid" else ("badge-rejected" if st == "Rejected" else "badge-pending")
        order_rows += f"""
        <tr>
            <td style="color: var(--cyan-glow); font-family: 'Orbitron', monospace; font-size:13px;">{ord[0]}</td>
            <td>{ord[1]}<br><span style="color:#94a3b8; font-size:12px;">@{ord[2]}</span></td>
            <td>🌽 {ord[3]}</td>
            <td>₹{ord[4]:.2f}</td>
            <td style="font-size:12px; color:#94a3b8;">{ord[5]}</td>
            <td>
                <span class="badge-pill {badge_cls}">{st}</span>
            </td>
            <td>
                <div class="action-cell">
                    <a href="/admin/order/update/{ord[7]}/Paid" class="action-btn-sm btn-accept">Paid</a>
                    <a href="/admin/order/update/{ord[7]}/Rejected" class="action-btn-sm btn-reject">Reject</a>
                </div>
            </td>
        </tr>
        """

    if not order_rows:
        order_rows = "<tr><td colspan='7' style='text-align:center; color:#94a3b8; padding:24px;'>No customer orders recorded yet.</td></tr>"

    status_tag = render_navbar_badge()

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Customer Orders — Nagato Panel</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        {THEME_CSS}
    </head>
    <body>
        {CANVAS_SCRIPT}
        {render_sidebar("orders")}

        <div class="main-wrapper">
            <div class="navbar">
                <div class="nav-brand">
                    <button class="nav-toggle-btn" onclick="toggleSidebar()">☰</button>
                    <span>Customer Orders</span>
                </div>
                {status_tag}
            </div>

            <div class="dashboard-container">
                <div class="cyber-card">
                    <div style="margin-bottom: 16px;">
                        <span style="font-size:12px; color:#94a3b8; text-transform:uppercase; letter-spacing:1px;">Nagato Panel Log</span>
                        <h2 style="font-family:'Orbitron',sans-serif; color:#fff; font-size:20px; margin-top:4px;">Customer Orders</h2>
                    </div>

                    <div style="overflow-x: auto;">
                        <table>
                            <thead>
                                <tr>
                                    <th>ORDER CODE</th>
                                    <th>TELEGRAM USER</th>
                                    <th>SUBSCRIPTION ITEM</th>
                                    <th>AMOUNT</th>
                                    <th>TIMESTAMP</th>
                                    <th>STATUS</th>
                                    <th>ACTION</th>
                                </tr>
                            </thead>
                            <tbody>
                                {order_rows}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

@app.get("/admin/order/update/{order_id}/{new_status}")
async def change_order_status(order_id: int, new_status: str, request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    if new_status in ["Paid", "Rejected", "Pending"]:
        update_order_status(order_id, new_status)
    return RedirectResponse(url="/admin/orders", status_code=303)

# --- 3. Category: Bot & Payment Gateway Settings ---
@app.get("/admin/bot-settings", response_class=HTMLResponse)
async def bot_settings_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cfg = get_settings()
    status_tag = render_navbar_badge()

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bot & Payment — Nagato Panel</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        {THEME_CSS}
    </head>
    <body>
        {CANVAS_SCRIPT}
        {render_sidebar("bot-settings")}

        <div class="main-wrapper">
            <div class="navbar">
                <div class="nav-brand">
                    <button class="nav-toggle-btn" onclick="toggleSidebar()">☰</button>
                    <span>⚙️ Bot & Payment Gateway</span>
                </div>
                {status_tag}
            </div>

            <div class="dashboard-container">
                <form action="/admin/save-bot-settings" method="post">
                    <div class="grid-3-col">
                        <div class="cyber-card">
                            <h3>1. Bot Token</h3>
                            <label>Token from @BotFather:</label>
                            <input type="text" name="token" value="{cfg['token']}" placeholder="123456:ABC-DEF..." required />
                            <p style="color: #94a3b8; font-size: 12px;">Engine updates immediately upon save.</p>
                        </div>

                        <div class="cyber-card">
                            <h3>2. UPI ID (VPA)</h3>
                            <label>Merchant UPI ID:</label>
                            <input type="text" name="upi_id" value="{cfg['upi_id']}" required />

                            <label>Payee Brand Name:</label>
                            <input type="text" name="payee_name" value="{cfg['payee_name']}" required />
                        </div>

                        <div class="cyber-card">
                            <h3>3. Admin Forwarding & Upload ID</h3>
                            <label>Admin Telegram Chat ID:</label>
                            <input type="text" name="admin_chat_id" value="{cfg['admin_chat_id']}" placeholder="e.g. 123456789" required />
                            <p style="color: var(--cyan-glow); font-size: 12px;">Required: Allows in-bot bulk uploading (`/upload &lt;1-17&gt;`).</p>
                        </div>
                    </div>

                    <button type="submit" class="btn-gradient" style="font-size: 16px; padding: 14px;">💾 Save Bot & UPI Settings</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """

# --- 4. Category: Messages & Buttons Settings ---
@app.get("/admin/message-settings", response_class=HTMLResponse)
async def message_settings_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cfg = get_settings()
    welcome_images_text = "\n".join(cfg["welcome_images"])
    status_tag = render_navbar_badge()

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Messages — Nagato Panel</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        {THEME_CSS}
    </head>
    <body>
        {CANVAS_SCRIPT}
        {render_sidebar("message-settings")}

        <div class="main-wrapper">
            <div class="navbar">
                <div class="nav-brand">
                    <button class="nav-toggle-btn" onclick="toggleSidebar()">☰</button>
                    <span>💬 Welcome & Action Buttons</span>
                </div>
                {status_tag}
            </div>

            <div class="dashboard-container">
                <form action="/admin/save-message-settings" method="post">
                    <div class="grid-3-col" style="grid-template-columns: 1fr 1fr;">
                        <div class="cyber-card">
                            <h3>1. Banner Images</h3>
                            <label>Image URLs (One per line):</label>
                            <textarea name="welcome_images" rows="5">{welcome_images_text}</textarea>
                        </div>
                        <div class="cyber-card">
                            <h3>2. Welcome Caption</h3>
                            <label>Caption Text:</label>
                            <textarea name="welcome_caption" rows="5">{cfg['welcome_caption']}</textarea>
                        </div>
                    </div>

                    <h3 style="font-family:'Orbitron',sans-serif; font-size:15px; color:var(--cyan-glow); margin-bottom: 12px;">🎨 Bottom 3 Design Action Buttons</h3>
                    <div class="grid-3-col">
                        <div class="cyber-card">
                            <label style="color: var(--cyan-glow);">🔵 Button 1 Label (Left):</label>
                            <input type="text" name="btn_how_to_use" value="{cfg['btn_how_to_use']}" required />
                            <label>Alert Popup Text:</label>
                            <input type="text" name="msg_how_to_use" value="{cfg['msg_how_to_use']}" required />
                        </div>
                        <div class="cyber-card">
                            <label style="color: #f87171;">🔴 Button 2 Label (Right):</label>
                            <input type="text" name="btn_report_issue" value="{cfg['btn_report_issue']}" required />
                            <label>Alert Popup Text:</label>
                            <input type="text" name="msg_report_issue" value="{cfg['msg_report_issue']}" required />
                        </div>
                        <div class="cyber-card">
                            <label style="color: var(--cyan-glow);">🔵 Button 3 Label (Bottom):</label>
                            <input type="text" name="btn_language" value="{cfg['btn_language']}" required />
                            <label>Alert Popup Text:</label>
                            <input type="text" name="msg_language" value="{cfg['msg_language']}" required />
                        </div>
                    </div>

                    <button type="submit" class="btn-gradient" style="font-size: 16px; padding: 14px;">💾 Save Message Settings</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """

# --- 5. Category: Plan Buttons ---
@app.get("/admin/packs", response_class=HTMLResponse)
async def packs_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cfg = get_settings()

    button_sections_html = ""
    for idx in range(17):
        b_name = cfg["buttons"][idx] if idx < len(cfg["buttons"]) else f"VIP Pack {idx+1}"
        v_list = cfg["button_videos"].get(str(idx), [])
        v_text = "\n".join(v_list)
        
        details = cfg["button_details"].get(str(idx), {
            "pack": f"VIP Pack {idx + 1}",
            "price": "299",
            "desc": "Exclusive HD streaming access."
        })

        button_sections_html += f"""
        <div style="background: rgba(7, 9, 19, 0.75); border: 1px solid rgba(168, 85, 247, 0.25); border-radius: 10px; padding: 14px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                <h4 style="color: var(--cyan-glow); margin: 0; font-family:'Orbitron',sans-serif; font-size:12px;">🟢 Button #{idx + 1}</h4>
                <span style="font-size:11px; color:#94a3b8;">{len(v_list)} media stored</span>
            </div>
            
            <label>Menu Button Label:</label>
            <input type="text" name="btn_label_{idx}" value="{b_name}" required style="border-left: 4px solid #22c55e;" />

            <div style="display: grid; grid-template-columns: 2fr 1fr; gap: 8px;">
                <div>
                    <label>🎀 Pack Name:</label>
                    <input type="text" name="pack_name_{idx}" value="{details.get('pack', '')}" required />
                </div>
                <div>
                    <label>💰 Price (₹):</label>
                    <input type="text" name="pack_price_{idx}" value="{details.get('price', '')}" required />
                </div>
            </div>

            <label>📄 Description (Shown on Pack Message):</label>
            <textarea name="pack_desc_{idx}" rows="2" placeholder="Custom pack description...">{details.get('desc', '')}</textarea>

            <label style="color: #94a3b8; font-size: 11px;">Media Items (One URL or file_id per line):</label>
            <textarea name="btn_videos_{idx}" rows="2" placeholder="Tip: You can also use /upload {idx + 1} in the bot!">{v_text}</textarea>
        </div>
        """

    status_tag = render_navbar_badge()

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Plan Buttons — Nagato Panel</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        {THEME_CSS}
    </head>
    <body>
        {CANVAS_SCRIPT}
        {render_sidebar("packs")}

        <div class="main-wrapper">
            <div class="navbar">
                <div class="nav-brand">
                    <button class="nav-toggle-btn" onclick="toggleSidebar()">☰</button>
                    <span>📦 Plan Buttons</span>
                </div>
                {status_tag}
            </div>

            <div class="dashboard-container">
                <div class="cyber-card" style="margin-bottom: 20px; border-left: 4px solid var(--cyan-glow);">
                    <h3 style="margin-bottom: 6px;">💡 How to Upload Media in Bulk:</h3>
                    <p style="font-size: 13px; color: #94a3b8; line-height: 1.5;">
                        You can paste links/file_ids below, <b>OR</b> simply open Telegram and send: <br>
                        <code>/upload 1</code> (or any number 1-17) to start bulk forwarding images & videos directly from Telegram!<br>
                        Type <code>/done</code> when you're finished.
                    </p>
                </div>

                <form action="/admin/save-packs" method="post">
                    <div class="grid-3-col">
                        {button_sections_html}
                    </div>

                    <button type="submit" class="btn-gradient" style="font-size: 16px; padding: 16px; margin-top: 10px;">💾 Save Plan Buttons</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """

# --- Save Handlers ---
@app.post("/admin/save-bot-settings")
async def save_bot_settings(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    token = form.get("token", "").strip()
    update_field("token", token)
    update_field("upi_id", form.get("upi_id", "").strip())
    update_field("payee_name", form.get("payee_name", "").strip())
    update_field("admin_chat_id", form.get("admin_chat_id", "").strip())

    await bot_manager.restart(token)
    return RedirectResponse(url="/admin/bot-settings", status_code=303)

@app.post("/admin/save-message-settings")
async def save_message_settings(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()

    raw_images = form.get("welcome_images", "").splitlines()
    images = [img.strip() for img in raw_images if img.strip()]
    update_field("welcome_images_json", json.dumps(images))

    update_field("welcome_caption", form.get("welcome_caption", "").strip())

    update_field("btn_how_to_use", form.get("btn_how_to_use", "").strip())
    update_field("btn_report_issue", form.get("btn_report_issue", "").strip())
    update_field("btn_language", form.get("btn_language", "").strip())
    update_field("msg_how_to_use", form.get("msg_how_to_use", "").strip())
    update_field("msg_report_issue", form.get("msg_report_issue", "").strip())
    update_field("msg_language", form.get("msg_language", "").strip())

    return RedirectResponse(url="/admin/message-settings", status_code=303)

@app.post("/admin/save-packs")
async def save_packs(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
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

    return RedirectResponse(url="/admin/packs", status_code=303)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("pomm:app", host="0.0.0.0", port=port)
