import asyncio
import json
import logging
import os
import sqlite3
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
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.join(os.getcwd(), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "bot_config.db")

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
            qr_image_url TEXT
        )
    """)
    
    # Ensure column exists if migrating from older version
    try:
        c.execute("ALTER TABLE settings ADD COLUMN button_details_json TEXT")
    except sqlite3.OperationalError:
        pass

    c.execute("SELECT COUNT(*) FROM settings")
    if c.fetchone()[0] == 0:
        default_buttons = [f"VIP Pack {i}" for i in range(1, 18)]
        default_images = [
            "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=800"
        ]
        default_button_videos = {
            str(i): [
                "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4"
            ] for i in range(17)
        }
        
        # Default details (Pack Name, Price, Description) for all 17 items
        default_details = {
            str(i): {
                "pack": f"VIP Pack {i + 1}",
                "price": "299",
                "desc": "Complete mega collection with full HD videos and lifetime updates."
            } for i in range(17)
        }

        c.execute("""
            INSERT INTO settings (
                id, token, welcome_images_json, welcome_caption, buttons_json,
                button_videos_json, button_details_json, home_button_text, qr_image_url
            ) VALUES (1, '', ?, ?, ?, ?, ?, ?, ?)
        """, (
            json.dumps(default_images),
            "✨ *Welcome to our Exclusive Hub!*\n\nSelect a package below to preview content:",
            json.dumps(default_buttons),
            json.dumps(default_button_videos),
            json.dumps(default_details),
            "🔙 Back",
            "https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=https://telegram.org"
        ))
        conn.commit()
    conn.close()

def get_settings():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT token, welcome_images_json, welcome_caption, buttons_json,
               button_videos_json, button_details_json, home_button_text, qr_image_url 
        FROM settings WHERE id = 1
    """)
    row = c.fetchone()
    conn.close()

    button_details = {}
    if row[5]:
        try:
            button_details = json.loads(row[5])
        except Exception:
            pass

    return {
        "token": row[0] or "",
        "welcome_images": json.loads(row[1]) if row[1] else [],
        "welcome_caption": row[2] or "",
        "buttons": json.loads(row[3]) if row[3] else [],
        "button_videos": json.loads(row[4]) if row[4] else {},
        "button_details": button_details,
        "home_button_text": row[6] or "🔙 Back",
        "qr_image_url": row[7] or ""
    }

def update_field(field_name: str, value: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"UPDATE settings SET {field_name} = ? WHERE id = 1", (value,))
    conn.commit()
    conn.close()

init_db()

# --- Dynamic Bot Controller ---
class BotManager:
    def __init__(self):
        self.app: Application | None = None
        self.task: asyncio.Task | None = None
        self.status = "Stopped"

    async def _run_bot(self, token: str):
        try:
            self.app = ApplicationBuilder().token(token).build()
            self.app.add_handler(CommandHandler("start", handle_start))
            self.app.add_handler(CallbackQueryHandler(handle_callback))

            await self.app.initialize()
            await self.app.updater.start_polling()
            await self.app.start()
            self.status = "Running"
            logger.info("Bot started successfully.")

            while self.status == "Running":
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info("Bot task stopping.")
        except Exception as e:
            logger.error(f"Bot error: {e}")
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
    # 17 Green Full-Width Buttons
    for idx, text in enumerate(cfg["buttons"][:17]):
        btn = InlineKeyboardButton(
            text=text,
            callback_data=f"btn_cat_{idx}",
            api_kwargs={"style": "success"}
        )
        keyboard.append([btn])

    # 1 Blue Back / Home Button
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
    cfg = get_settings()

    # Message 1: Bulk Images Album (no caption)
    valid_images = [img.strip() for img in cfg["welcome_images"] if img.strip()]
    if valid_images:
        media_group = [InputMediaPhoto(media=url) for url in valid_images]
        try:
            await send_media_in_chunks(context, chat_id, media_group)
        except Exception as e:
            logger.error(f"Failed to send images: {e}")

    # Message 2: Welcome caption + 17 Green buttons + 1 Blue button
    await context.bot.send_message(
        chat_id=chat_id,
        text=cfg["welcome_caption"],
        reply_markup=build_main_keyboard(cfg),
        parse_mode="Markdown"
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    cfg = get_settings()

    if query.data == "btn_home":
        await handle_start(update, context)

    elif query.data.startswith("btn_cat_"):
        btn_idx = query.data.replace("btn_cat_", "")

        # 1. Send this button's bulk videos
        button_videos = cfg["button_videos"].get(btn_idx, [])
        valid_videos = [v.strip() for v in button_videos if v.strip()]
        if valid_videos:
            media_group = [InputMediaVideo(media=url, supports_streaming=True) for url in valid_videos]
            try:
                await send_media_in_chunks(context, chat_id, media_group)
            except Exception as e:
                logger.error(f"Error sending videos: {e}")

        # 2. Build payment message matching the screenshot format
        details = cfg["button_details"].get(btn_idx, {
            "pack": f"Package {int(btn_idx) + 1}",
            "price": "N/A",
            "desc": "Instant delivery upon verification."
        })

        payment_text = (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎀 *Pack*\n"
            f"{details.get('pack', '')}\n\n"
            f"💰 *Price*\n"
            f"₹{details.get('price', '')}\n\n"
            f"📄 *Description*\n"
            f"{details.get('desc', '')}\n"
            f"━━━━━━━━━━━━━━━━━━"
        )

        # 3. Two buttons: Buy Now (Green) and Back (Blue)
        payment_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💳 Buy Now",
                    callback_data=f"btn_show_qr_{btn_idx}",
                    api_kwargs={"style": "success"}
                )
            ],
            [
                InlineKeyboardButton(
                    cfg["home_button_text"],
                    callback_data="btn_home",
                    api_kwargs={"style": "primary"}
                )
            ]
        ])

        await context.bot.send_message(
            chat_id=chat_id,
            text=payment_text,
            reply_markup=payment_markup,
            parse_mode="Markdown"
        )

    elif query.data.startswith("btn_show_qr_"):
        qr_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    cfg["home_button_text"],
                    callback_data="btn_home",
                    api_kwargs={"style": "primary"}
                )
            ]
        ])

        if cfg["qr_image_url"]:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=cfg["qr_image_url"],
                caption="📲 *Scan this QR to complete payment.*\nSend a payment screenshot here to receive instant delivery.",
                reply_markup=qr_markup,
                parse_mode="Markdown"
            )

# --- FastAPI Server Lifecycle ---
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

    # Build 17 Button cards with Pack Name, Price, Description, and Videos
    button_sections_html = ""
    for idx in range(17):
        b_name = cfg["buttons"][idx] if idx < len(cfg["buttons"]) else f"Category {idx+1}"
        v_list = cfg["button_videos"].get(str(idx), [])
        v_text = "\n".join(v_list)
        
        details = cfg["button_details"].get(str(idx), {
            "pack": f"VIP Pack {idx + 1}",
            "price": "299",
            "desc": "High quality stream collection."
        })

        button_sections_html += f"""
        <div style="background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 16px; margin-bottom: 20px;">
            <h4 style="color: #4ade80; margin: 0 0 12px 0;">🟢 Button #{idx + 1} Configuration</h4>
            
            <label>Menu Button Text:</label>
            <input type="text" name="btn_label_{idx}" value="{b_name}" required style="border-left: 5px solid #22c55e;" />

            <div style="display: grid; grid-template-columns: 2fr 1fr; gap: 12px;">
                <div>
                    <label>🎀 Pack Title:</label>
                    <input type="text" name="pack_name_{idx}" value="{details.get('pack', '')}" required />
                </div>
                <div>
                    <label>💰 Price (in ₹):</label>
                    <input type="text" name="pack_price_{idx}" value="{details.get('price', '')}" required />
                </div>
            </div>

            <label>📄 Description (Editable text for this specific button):</label>
            <textarea name="pack_desc_{idx}" rows="2">{details.get('desc', '')}</textarea>

            <label style="color: #94a3b8; font-size: 13px;">Bulk Video URLs (One per line):</label>
            <textarea name="btn_videos_{idx}" rows="2" placeholder="https://domain.com/sample.mp4">{v_text}</textarea>
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
            <h1>⚙️ Bot Console</h1>
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
                <div class="card">
                    <h3>2. Start Message Setup</h3>
                    <label>1st Message: Bulk Images (One per line):</label>
                    <textarea name="welcome_images" rows="3">{welcome_images_text}</textarea>

                    <label>2nd Message: Caption:</label>
                    <textarea name="welcome_caption" rows="3">{cfg['welcome_caption']}</textarea>

                    <label style="color: #38bdf8;">🔵 Blue Button Text (Back / Home):</label>
                    <input type="text" name="home_button_text" value="{cfg['home_button_text']}" style="border-left: 5px solid #38bdf8;" required />
                </div>

                <div class="card">
                    <h3>3. 17 Items (Editable Pack, Price, Description & Videos)</h3>
                    {button_sections_html}
                </div>

                <div class="card">
                    <h3>4. Payment QR Code</h3>
                    <label>QR Code Image URL (Delivered on "Buy Now"):</label>
                    <input type="text" name="qr_image_url" value="{cfg['qr_image_url']}" required />
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

    # 1. Bulk Welcome Images
    raw_images = form.get("welcome_images", "").splitlines()
    images = [img.strip() for img in raw_images if img.strip()]
    update_field("welcome_images_json", json.dumps(images))

    # 2. Welcome caption, Back text, and QR URL
    update_field("welcome_caption", form.get("welcome_caption", "").strip())
    update_field("home_button_text", form.get("home_button_text", "").strip())
    update_field("qr_image_url", form.get("qr_image_url", "").strip())

    # 3. 17 Individual buttons, packs, prices, descriptions, and videos
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
