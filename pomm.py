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

# In-memory tracking for users waiting to upload screenshots: {chat_id: {txn_id, price, pack_name}}
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
            home_button_text TEXT,
            back_button_text TEXT,
            upi_id TEXT,
            payee_name TEXT,
            admin_chat_id TEXT
        )
    """)

    for col in [
        "button_details_json TEXT",
        "back_button_text TEXT",
        "upi_id TEXT",
        "payee_name TEXT",
        "admin_chat_id TEXT"
    ]:
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
                "desc": "Instant download access."
            } for i in range(17)
        }

        c.execute("""
            INSERT INTO settings (
                id, token, welcome_images_json, welcome_caption, buttons_json,
                button_videos_json, button_details_json, home_button_text, back_button_text, 
                upi_id, payee_name, admin_chat_id
            ) VALUES (1, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
        """, (
            json.dumps(default_images),
            "✨ *Welcome to our Exclusive Hub!*\n\nSelect an option below to preview content:",
            json.dumps(default_buttons),
            json.dumps(default_button_videos),
            json.dumps(default_details),
            "🏠 Home",
            "🔙 Back",
            "thesalesgod@nyes",
            "Trusted Seller",
        ))
        conn.commit()
    conn.close()

def get_settings():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT token, welcome_images_json, welcome_caption, buttons_json, 
               button_videos_json, button_details_json, home_button_text, 
               back_button_text, upi_id, payee_name, admin_chat_id 
        FROM settings WHERE id = 1
    """)
    row = c.fetchone()
    conn.close()

    button_details = {}
    if row and row[5]:
        try:
            button_details = json.loads(row[5])
        except Exception:
            pass

    return {
        "token": row[0] if row and row[0] else "",
        "welcome_images": json.loads(row[1]) if row and row[1] else [],
        "welcome_caption": row[2] if row and row[2] else "",
        "buttons": json.loads(row[3]) if row and row[3] else [],
        "button_videos": json.loads(row[4]) if row and row[4] else {},
        "button_details": button_details,
        "home_button_text": (row[6] if row and row[6] else "🏠 Home"),
        "back_button_text": (row[7] if row and row[7] else "🔙 Back"),
        "upi_id": (row[8] if row and row[8] else ""),
        "payee_name": (row[9] if row and row[9] else "Merchant"),
        "admin_chat_id": (row[10] if row and len(row) > 10 and row[10] else "")
    }

def update_field(field_name: str, value: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"UPDATE settings SET {field_name} = ? WHERE id = 1", (value,))
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

# --- Keyboard Builders ---
def build_main_keyboard(cfg):
    keyboard = []
    for idx, text in enumerate(cfg["buttons"][:17]):
        btn = InlineKeyboardButton(
            text=text,
            callback_data=f"btn_cat_{idx}",
            api_kwargs={"style": "success"}
        )
        keyboard.append([btn])

    home_btn = InlineKeyboardButton(
        text=cfg["home_button_text"],
        callback_data="btn_home",
        api_kwargs={"style": "primary"}
    )
    keyboard.append([home_btn])
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

# --- Telegram Handlers ---
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pending_verifications.pop(chat_id, None)
    cfg = get_settings()

    valid_images = [img.strip() for img in cfg["welcome_images"] if img.strip()]
    if valid_images:
        media_group = [InputMediaPhoto(media=url) for url in valid_images]
        try:
            await send_media_in_chunks(context, chat_id, media_group)
        except Exception as e:
            logger.error(f"Failed to send images: {e}")

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
    await query.answer()
    chat_id = update.effective_chat.id
    cfg = get_settings()

    if query.data == "btn_home":
        pending_verifications.pop(chat_id, None)
        await handle_start(update, context)

    elif query.data.startswith("btn_cat_"):
        btn_idx = query.data.replace("btn_cat_", "")

        # 1. Send bulk videos for this category
        button_videos = cfg["button_videos"].get(btn_idx, [])
        valid_videos = [v.strip() for v in button_videos if v.strip()]
        if valid_videos:
            media_group = [InputMediaVideo(media=url, supports_streaming=True) for url in valid_videos]
            try:
                await send_media_in_chunks(context, chat_id, media_group)
            except Exception as e:
                logger.error(f"Error sending videos: {e}")

        # 2. Package & Payment info
        details = cfg["button_details"].get(btn_idx, {
            "pack": f"VIP Pack {int(btn_idx) + 1}",
            "price": "299",
            "desc": ""
        })

        pack_name = details.get("pack", f"Pack {int(btn_idx) + 1}")
        price = details.get("price", "0")
        desc = details.get("desc", "Instant access.")
        txn_id = generate_txn_id(pack_name)

        # Store session
        pending_verifications[chat_id] = {
            "txn_id": txn_id,
            "price": price,
            "pack": pack_name
        }

        # Dynamic UPI generation
        upi_link = make_upi_uri(
            upi_id=cfg["upi_id"],
            payee_name=cfg["payee_name"],
            amount=price,
            note=txn_id
        )
        qr_url = generate_upi_qr_url(upi_link)

        # Payment card formatted like screenshot
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

        # Send QR image with the payment description
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=qr_url,
            caption=payment_text,
            reply_markup=payment_markup,
            parse_mode="Markdown"
        )

    elif query.data.startswith("btn_send_ss_"):
        session = pending_verifications.get(chat_id)
        if not session:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Session expired. Please choose a package again.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(cfg["home_button_text"], callback_data="btn_home", api_kwargs={"style": "primary"})
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

    # Grab highest resolution photo
    photo_file = update.message.photo[-1]
    cfg = get_settings()

    # Confirmation to user
    await update.message.reply_text(
        f"✅ *Screenshot Received!*\n\n"
        f"🧾 *Txn:* `{session['txn_id']}`\n"
        f"📦 *Pack:* {session['pack']}\n\n"
        f"Your transaction is currently being verified by an admin. You will receive your access link here shortly.",
        parse_mode="Markdown"
    )

    # Forward to Admin if admin_chat_id is configured
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
            logger.error(f"Failed forwarding screenshot to admin: {e}")

    # Clear pending state
    del pending_verifications[chat_id]

# --- FastAPI Server Lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    if cfg["token"]:
        await bot_manager.restart(cfg["token"])
    yield
    await bot_manager.stop()

app = FastAPI(lifespan=lifespan)

# --- Web Admin Dashboard UI ---
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
                    <label>🎀 Pack Name (Shown on Payment):</label>
                    <input type="text" name="pack_name_{idx}" value="{details.get('pack', '')}" required />
                </div>
                <div>
                    <label>💰 Price in ₹ (Auto encoded in QR):</label>
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
            input[type="text"], textarea {{ width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #4b5563; border-radius: 6px; background: #111827; color: #fff; margin-bottom: 10px; font-size: 14px; font-family: inherit; }}
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
            <h1>⚙️ Bot Console (Instant QR & Screenshot Engine)</h1>
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
                    <h3 style="color: #38bdf8; margin-top: 0;">⚡ UPI & Receipt Settings</h3>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
                        <div>
                            <label>Your UPI ID / VPA (e.g. thesalesgod@nyes):</label>
                            <input type="text" name="upi_id" value="{cfg['upi_id']}" placeholder="name@upi" required style="border-left: 5px solid #38bdf8;" />
                        </div>
                        <div>
                            <label>Payee / Brand Name:</label>
                            <input type="text" name="payee_name" value="{cfg['payee_name']}" placeholder="Store Name" required />
                        </div>
                    </div>
                    <label style="color: #fbbf24;">Admin Telegram Chat ID (Screenshots will be forwarded here):</label>
                    <input type="text" name="admin_chat_id" value="{cfg['admin_chat_id']}" placeholder="e.g. 123456789 (leave blank to skip forwarding)" />
                </div>

                <div class="card">
                    <h3>2. Start Messages Setup</h3>
                    <label>1st Message: Bulk Images (One URL per line):</label>
                    <textarea name="welcome_images" rows="3">{welcome_images_text}</textarea>

                    <label>2nd Message: Caption:</label>
                    <textarea name="welcome_caption" rows="3">{cfg['welcome_caption']}</textarea>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
                        <div>
                            <label style="color: #38bdf8;">🔵 Home Button Label (2nd Message):</label>
                            <input type="text" name="home_button_text" value="{cfg['home_button_text']}" required />
                        </div>
                        <div>
                            <label style="color: #38bdf8;">🔵 Back Button Label (Payment Screens):</label>
                            <input type="text" name="back_button_text" value="{cfg['back_button_text']}" required />
                        </div>
                    </div>
                </div>

                <div class="card">
                    <h3>3. 17 Items Configuration (Pack Name, Price, Videos)</h3>
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

    # 2. Captions & UPI settings
    update_field("welcome_caption", form.get("welcome_caption", "").strip())
    update_field("home_button_text", form.get("home_button_text", "🏠 Home").strip())
    update_field("back_button_text", form.get("back_button_text", "🔙 Back").strip())
    update_field("upi_id", form.get("upi_id", "").strip())
    update_field("payee_name", form.get("payee_name", "").strip())
    update_field("admin_chat_id", form.get("admin_chat_id", "").strip())

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
            "desc": form.get(f"pack_desc_{idx}", "").strip(),
        }

    update_field("buttons_json", json.dumps(buttons))
    update_field("button_videos_json", json.dumps(button_videos))
    update_field("button_details_json", json.dumps(button_details))

    return RedirectResponse(url="/admin", status_code=303)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
