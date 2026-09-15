import asyncio
import json
import logging
import os
import random
import sqlite3
import time
import urllib.parse
from contextlib import asynccontextmanager
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.join(os.getcwd(), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "bot_config.db")

pending_verifications = {}

# --- Database Setup & Migration ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
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
            back_button_text TEXT,
            upi_id TEXT,
            payee_name TEXT,
            admin_chat_id TEXT,
            btn_how_to_use TEXT,
            btn_report_issue TEXT,
            btn_language TEXT,
            msg_how_to_use TEXT,
            msg_report_issue TEXT,
            msg_language TEXT
        )
    """)

    # Columns migration
    cols = [
        "button_details_json TEXT", "back_button_text TEXT", "upi_id TEXT",
        "payee_name TEXT", "admin_chat_id TEXT", "btn_how_to_use TEXT",
        "btn_report_issue TEXT", "btn_language TEXT", "msg_how_to_use TEXT",
        "msg_report_issue TEXT", "msg_language TEXT"
    ]
    for col in cols:
        try:
            c.execute(f"ALTER TABLE settings ADD COLUMN {col}")
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
                "pack": f"HOUSEWIFE 🔥" if i == 0 else f"VIP Pack {i + 1}",
                "price": "49" if i == 0 else "299",
                "desc": "Instant access."
            } for i in range(17)
        }

        c.execute("""
            INSERT INTO settings (
                id, token, welcome_images_json, welcome_caption, buttons_json,
                button_videos_json, button_details_json, back_button_text,
                upi_id, payee_name, admin_chat_id,
                btn_how_to_use, btn_report_issue, btn_language,
                msg_how_to_use, msg_report_issue, msg_language
            ) VALUES (1, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            json.dumps(default_images),
            "✨ *Welcome to our Exclusive Hub!*\n\nSelect an option below to preview content:",
            json.dumps(default_buttons),
            json.dumps(default_button_videos),
            json.dumps(default_details),
            "🔙 Back",
            "thesalesgod@nyes",
            "Trusted Seller",
            "",
            "📖 How To Use",
            "🚨 Report Issue",
            "🌐 Language",
            "ℹ️ Select any package to preview videos, then complete payment via UPI QR code.",
            "📩 For help or support, contact our support admin directly: @YourSupportHandle",
            "🌐 English is selected by default."
        ))
        conn.commit()
    conn.close()

def get_settings():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM settings WHERE id = 1")
    row = c.fetchone()
    conn.close()

    # Column Mapping
    return {
        "token": row[1] or "",
        "welcome_images": json.loads(row[2]) if row[2] else [],
        "welcome_caption": row[3] or "",
        "buttons": json.loads(row[4]) if row[4] else [],
        "button_videos": json.loads(row[5]) if row[5] else {},
        "button_details": json.loads(row[6]) if row[6] else {},
        "back_button_text": row[7] or "🔙 Back",
        "upi_id": row[8] or "",
        "payee_name": row[9] or "Merchant",
        "admin_chat_id": row[10] or "",
        "btn_how_to_use": row[11] or "📖 How To Use",
        "btn_report_issue": row[12] or "🚨 Report Issue",
        "btn_language": row[13] or "🌐 Language",
        "msg_how_to_use": row[14] or "Select any pack to proceed.",
        "msg_report_issue": row[15] or "Contact support.",
        "msg_language": row[16] or "Current language: English."
    }

def update_field(field_name: str, value: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"UPDATE settings SET {field_name} = ? WHERE id = 1", (value,))
    conn.commit()
    conn.close()

init_db()

# --- UPI URI Helper ---
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

# --- Bot Process Manager ---
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
            self.app.add_handler(CallbackQueryHandler(handle_callback))
            self.app.add_handler(MessageHandler(filters.PHOTO, handle_screenshot_upload))

            await self.app.initialize()
            await self.app.updater.start_polling()
            await self.app.start()
            self.status = "Running"
            logger.info("Bot started successfully.")

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

# --- Main Menu Keyboard Builder ---
def build_main_keyboard(cfg):
    keyboard = []
    
    # 1. 17 Full-Width Green Buttons
    for idx, text in enumerate(cfg["buttons"][:17]):
        btn = InlineKeyboardButton(
            text=text,
            callback_data=f"btn_cat_{idx}",
            api_kwargs={"style": "success"}
        )
        keyboard.append([btn])

    # 2. Row of 2 Buttons: Blue (How to Use) & Red (Report Issue)
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

    # 3. Bottom Row: 1 Full-Width Blue Button (Language)
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

# --- Handlers ---
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pending_verifications.pop(chat_id, None)
    cfg = get_settings()

    # Message 1: Image Album
    valid_images = [img.strip() for img in cfg["welcome_images"] if img.strip()]
    if valid_images:
        media_group = [InputMediaPhoto(media=url) for url in valid_images]
        try:
            await send_media_in_chunks(context, chat_id, media_group)
        except Exception as e:
            logger.error(f"Failed to send images: {e}")

    # Message 2: Welcome caption + 17 Green Buttons + Bottom 3 Design Buttons
    await context.bot.send_message(
        chat_id=chat_id,
        text=cfg["welcome_caption"],
        reply_markup=build_main_keyboard(cfg),
        parse_mode="Markdown"
    )

async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in pending_verifications:
        del pending_verifications[chat_id]
        await update.message.reply_text("❌ Payment submission cancelled.", parse_mode="Markdown")
        await handle_start(update, context)
    else:
        await update.message.reply_text("No active payment session to cancel.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id
    cfg = get_settings()

    if data == "btn_home":
        pending_verifications.pop(chat_id, None)
        await query.answer()
        await handle_start(update, context)

    # Click actions for the bottom design buttons
    elif data == "act_how_to_use":
        await query.answer(cfg["msg_how_to_use"], show_alert=True)
    elif data == "act_report_issue":
        await query.answer(cfg["msg_report_issue"], show_alert=True)
    elif data == "act_language":
        await query.answer(cfg["msg_language"], show_alert=True)

    elif data.startswith("btn_cat_"):
        await query.answer()
        btn_idx = data.replace("btn_cat_", "")

        # Send videos
        button_videos = cfg["button_videos"].get(btn_idx, [])
        valid_videos = [v.strip() for v in button_videos if v.strip()]
        if valid_videos:
            media_group = [InputMediaVideo(media=url, supports_streaming=True) for url in valid_videos]
            try:
                await send_media_in_chunks(context, chat_id, media_group)
            except Exception as e:
                logger.error(f"Error sending videos: {e}")

        # Details
        details = cfg["button_details"].get(btn_idx, {
            "pack": f"VIP Pack {int(btn_idx) + 1}",
            "price": "299",
            "desc": ""
        })

        pack_name = details.get("pack", f"Pack {int(btn_idx) + 1}")
        price = details.get("price", "0")
        txn_id = generate_txn_id(pack_name)

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
            f"🔹 *Payment*\n\n"
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
                    cfg["back_button_text"],
                    callback_data="btn_home",
                    api_kwargs={"style": "primary"}
                )
            ]
        ])

        await context.bot.send_photo(
            chat_id=chat_id,
            photo=qr_url,
            caption=payment_text,
            reply_markup=payment_markup,
            parse_mode="Markdown"
        )

    elif data.startswith("btn_send_ss_"):
        await query.answer()
        session = pending_verifications.get(chat_id)
        if not session:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Session expired. Please choose a package again.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back", callback_data="btn_home", api_kwargs={"style": "primary"})
                ]])
            )
            return

        prompt_msg = (
            f"🔹 *Send Payment Screenshot*\n\n"
            f"🧾 `{session['txn_id']}`\n"
            f"💰 *₹{session['price']}*\n\n"
            f"Upload the screenshot of your successful payment here as a photo.\n\n"
            f"Send /cancel to abort."
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=prompt_msg,
            parse_mode="Markdown"
        )

async def handle_screenshot_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = pending_verifications.get(chat_id)

    if not session:
        return

    photo_file = update.message.photo[-1]
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

# --- FastAPI Server ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    if cfg["token"]:
        await bot_manager.restart(cfg["token"])
    yield
    await bot_manager.stop()

app = FastAPI(lifespan=lifespan)

# --- Web Admin UI ---
@app.get("/", response_class=HTMLResponse)
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    cfg = get_settings()
    welcome_images_text = "\n".join(cfg["welcome_images"])

    button_sections_html = ""
    for idx in range(17):
        b_name = cfg["buttons"][idx] if idx < len(cfg["buttons"]) else f"VIP Pack {idx+1}"
        v_list = cfg["button_videos"].get(str(idx), [])
        v_text = "\n".join(v_list)
        
        details = cfg["button_details"].get(str(idx), {
            "pack": f"VIP Pack {idx + 1}",
            "price": "299",
            "desc": ""
        })

        button_sections_html += f"""
        <div style="background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 16px; margin-bottom: 20px;">
            <h4 style="color: #4ade80; margin: 0 0 12px 0;">🟢 Green Button #{idx + 1}</h4>
            
            <label>Menu Button Label:</label>
            <input type="text" name="btn_label_{idx}" value="{b_name}" required style="border-left: 5px solid #22c55e;" />

            <div style="display: grid; grid-template-columns: 2fr 1fr; gap: 12px;">
                <div>
                    <label>🎀 Pack Name:</label>
                    <input type="text" name="pack_name_{idx}" value="{details.get('pack', '')}" required />
                </div>
                <div>
                    <label>💰 Price (₹):</label>
                    <input type="text" name="pack_price_{idx}" value="{details.get('price', '')}" required />
                </div>
            </div>

            <label style="color: #94a3b8; font-size: 13px;">Bulk Video URLs (One per line):</label>
            <textarea name="btn_videos_{idx}" rows="2" placeholder="https://domain.com/video.mp4">{v_text}</textarea>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Telegram Bot Management</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0b0f19; color: #e2e8f0; margin: 0; padding: 20px; }}
            .container {{ max-width: 850px; margin: auto; background: #111827; border-radius: 12px; padding: 24px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
            h1 {{ margin-top: 0; font-size: 22px; color: #38bdf8; border-bottom: 1px solid #374151; padding-bottom: 12px; }}
            .card {{ background: #1f2937; border: 1px solid #374151; border-radius: 8px; padding: 18px; margin-bottom: 18px; }}
            .badge {{ display: inline-block; padding: 4px 10px; border-radius: 20px; font-weight: bold; font-size: 12px; }}
            .badge.Running {{ background: #166534; color: #4ade80; }}
            .badge.Stopped {{ background: #991b1b; color: #fca5a5; }}
            label {{ display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }}
            input[type="text"], textarea {{ width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #4b5563; border-radius: 6px; background: #111827; color: #fff; margin-bottom: 10px; font-size: 14px; }}
            input:focus, textarea:focus {{ border-color: #38bdf8; outline: none; }}
            button {{ background: #2563eb; color: #fff; font-weight: bold; border: none; padding: 10px 18px; border-radius: 6px; cursor: pointer; }}
            button:hover {{ background: #1d4ed8; }}
            .btn-danger {{ background: #dc2626; }}
            .btn-danger:hover {{ background: #b91c1c; }}
            .btn-save {{ background: #16a34a; width: 100%; font-size: 16px; padding: 14px; }}
            .btn-save:hover {{ background: #15803d; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>⚙️ Telegram Bot Console</h1>
            <p>Engine Status: <span class="badge {bot_manager.status}">{bot_manager.status}</span></p>

            <div class="card">
                <h3>1. Telegram Bot Token</h3>
                <form action="/admin/save-token" method="post">
                    <input type="text" name="token" value="{cfg['token']}" placeholder="123456:ABC-DEF1234..." required />
                    <button type="submit">Update & Restart Bot</button>
                </form>
                <form action="/admin/stop-bot" method="post" style="margin-top: 10px;">
                    <button type="submit" class="btn-danger">Stop Bot</button>
                </form>
            </div>

            <form action="/admin/save-content" method="post">
                <div class="card" style="border: 1px solid #38bdf8;">
                    <h3 style="color: #38bdf8; margin-top: 0;">⚡ UPI Settings</h3>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
                        <div>
                            <label>UPI ID (VPA):</label>
                            <input type="text" name="upi_id" value="{cfg['upi_id']}" placeholder="name@upi" required style="border-left: 5px solid #38bdf8;" />
                        </div>
                        <div>
                            <label>Payee Name:</label>
                            <input type="text" name="payee_name" value="{cfg['payee_name']}" required />
                        </div>
                    </div>
                    <label style="color: #fbbf24;">Admin Telegram Chat ID (Screenshots will be sent here):</label>
                    <input type="text" name="admin_chat_id" value="{cfg['admin_chat_id']}" placeholder="e.g. 123456789" />
                </div>

                <div class="card">
                    <h3>2. Start Message Setup</h3>
                    <label>1st Message: Images (One URL per line):</label>
                    <textarea name="welcome_images" rows="3">{welcome_images_text}</textarea>

                    <label>2nd Message: Caption:</label>
                    <textarea name="welcome_caption" rows="3">{cfg['welcome_caption']}</textarea>

                    <label style="color: #38bdf8;">🔵 Payment Page Back Button Text:</label>
                    <input type="text" name="back_button_text" value="{cfg['back_button_text']}" required />
                </div>

                <!-- Bottom 3 Buttons Configuration -->
                <div class="card" style="border: 1px solid #6366f1;">
                    <h3 style="color: #818cf8; margin-top: 0;">🎨 Bottom 3 Design Buttons</h3>
                    
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
                        <div>
                            <label style="color: #38bdf8;">🔵 Button 1 Label (Left):</label>
                            <input type="text" name="btn_how_to_use" value="{cfg['btn_how_to_use']}" required />
                            <label>Alert Popup Text:</label>
                            <input type="text" name="msg_how_to_use" value="{cfg['msg_how_to_use']}" required />
                        </div>
                        <div>
                            <label style="color: #f87171;">🔴 Button 2 Label (Right):</label>
                            <input type="text" name="btn_report_issue" value="{cfg['btn_report_issue']}" required />
                            <label>Alert Popup Text:</label>
                            <input type="text" name="msg_report_issue" value="{cfg['msg_report_issue']}" required />
                        </div>
                    </div>

                    <div style="margin-top: 10px;">
                        <label style="color: #38bdf8;">🔵 Button 3 Label (Bottom Full-Width):</label>
                        <input type="text" name="btn_language" value="{cfg['btn_language']}" required />
                        <label>Alert Popup Text:</label>
                        <input type="text" name="msg_language" value="{cfg['msg_language']}" required />
                    </div>
                </div>

                <div class="card">
                    <h3>3. 17 Green Buttons Configuration</h3>
                    {button_sections_html}
                </div>

                <button type="submit" class="btn-save">💾 Save All Changes</button>
            </form>
        </div>
    </body>
    </html>
    """

@app.post("/admin/save-token")
async def save_token_endpoint(request: Request):
    form = await request.form()
    token = form.get("token", "").strip()
    update_field("token", token)
    await bot_manager.restart(token)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/stop-bot")
async def stop_bot_endpoint():
    await bot_manager.stop()
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/save-content")
async def save_content_endpoint(request: Request):
    form = await request.form()

    # 1. Bulk Images
    raw_images = form.get("welcome_images", "").splitlines()
    images = [img.strip() for img in raw_images if img.strip()]
    update_field("welcome_images_json", json.dumps(images))

    # 2. Captions & Bottom Buttons
    update_field("welcome_caption", form.get("welcome_caption", "").strip())
    update_field("back_button_text", form.get("back_button_text", "🔙 Back").strip())
    update_field("upi_id", form.get("upi_id", "").strip())
    update_field("payee_name", form.get("payee_name", "").strip())
    update_field("admin_chat_id", form.get("admin_chat_id", "").strip())

    # Bottom 3 buttons text & popups
    update_field("btn_how_to_use", form.get("btn_how_to_use", "").strip())
    update_field("btn_report_issue", form.get("btn_report_issue", "").strip())
    update_field("btn_language", form.get("btn_language", "").strip())
    update_field("msg_how_to_use", form.get("msg_how_to_use", "").strip())
    update_field("msg_report_issue", form.get("msg_report_issue", "").strip())
    update_field("msg_language", form.get("msg_language", "").strip())

    # 3. 17 Buttons details & videos
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
            "desc": ""
        }

    update_field("buttons_json", json.dumps(buttons))
    update_field("button_videos_json", json.dumps(button_videos))
    update_field("button_details_json", json.dumps(button_details))

    return RedirectResponse(url="/admin", status_code=303)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
