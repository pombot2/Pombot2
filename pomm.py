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

# --- Railway Persistent Storage Path ---
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.join(os.getcwd(), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "bot_config.db")

# --- Database Initialization & Defaults ---
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
            home_button_text TEXT,
            payment_caption TEXT,
            qr_image_url TEXT
        )
    """)
    c.execute("SELECT COUNT(*) FROM settings")
    if c.fetchone()[0] == 0:
        default_buttons = [f"VIP Pack {i}" for i in range(1, 18)]
        
        # Default bulk images (up to 10 in an album)
        default_images = [
            "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=800",
            "https://images.unsplash.com/photo-1579546929518-9e396f3cc809?w=800"
        ]
        
        # Dictionary mapping each button index (0 to 16) to its own list of video URLs
        default_button_videos = {
            str(i): [
                "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4"
            ] for i in range(17)
        }

        c.execute("""
            INSERT INTO settings (
                id, token, welcome_images_json, welcome_caption, buttons_json,
                button_videos_json, home_button_text, payment_caption, qr_image_url
            ) VALUES (1, '', ?, ?, ?, ?, ?, ?, ?)
        """, (
            json.dumps(default_images),
            "✨ *Welcome to our Exclusive Hub!*\n\nSelect a package below to preview content:",
            json.dumps(default_buttons),
            json.dumps(default_button_videos),
            "🏠 Return to Main Menu",
            "💳 *Select an option below to complete your payment and unlock full access.*",
            "https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=https://telegram.org"
        ))
        conn.commit()
    conn.close()

def get_settings():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT token, welcome_images_json, welcome_caption, buttons_json,
               button_videos_json, home_button_text, payment_caption, qr_image_url 
        FROM settings WHERE id = 1
    """)
    row = c.fetchone()
    conn.close()
    return {
        "token": row[0] or "",
        "welcome_images": json.loads(row[1]),
        "welcome_caption": row[2],
        "buttons": json.loads(row[3]),
        "button_videos": json.loads(row[4]),
        "home_button_text": row[5],
        "payment_caption": row[6],
        "qr_image_url": row[7]
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
            logger.info("Bot execution cancelled.")
        except Exception as e:
            logger.error(f"Bot failed with error: {e}")
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

# --- Telegram UI Builders ---
def build_main_keyboard(cfg):
    keyboard = []
    # 17 Green full-width buttons
    for idx, text in enumerate(cfg["buttons"][:17]):
        btn = InlineKeyboardButton(
            text=text,
            callback_data=f"btn_cat_{idx}",
            api_kwargs={"style": "success"}
        )
        keyboard.append([btn])

    # 1 Blue Home Button at the bottom
    home_btn = InlineKeyboardButton(
        text=cfg["home_button_text"],
        callback_data="btn_home",
        api_kwargs={"style": "primary"}
    )
    keyboard.append([home_btn])
    return InlineKeyboardMarkup(keyboard)

# --- Helper: Send Media in Batches (Telegram Limit = 10 per album) ---
async def send_media_in_chunks(context: ContextTypes.DEFAULT_TYPE, chat_id: int, media_list: list):
    for i in range(0, len(media_list), 10):
        chunk = media_list[i:i + 10]
        if len(chunk) == 1:
            # Single item fallback
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

    # 1st Message: Bulk Images (Album) without text
    valid_images = [img.strip() for img in cfg["welcome_images"] if img.strip()]
    if valid_images:
        media_group = [InputMediaPhoto(media=url) for url in valid_images]
        try:
            await send_media_in_chunks(context, chat_id, media_group)
        except Exception as e:
            logger.error(f"Failed to send bulk welcome images: {e}")

    # 2nd Message: Editable Caption + 17 Green Buttons + 1 Blue Home Button
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
        button_videos = cfg["button_videos"].get(btn_idx, [])
        valid_videos = [v.strip() for v in button_videos if v.strip()]

        # 1. Send bulk videos for this specific button as an album
        if valid_videos:
            media_group = [InputMediaVideo(media=url, supports_streaming=True) for url in valid_videos]
            try:
                await send_media_in_chunks(context, chat_id, media_group)
            except Exception as e:
                logger.error(f"Failed to deliver bulk videos for button {btn_idx}: {e}")

        # 2. Payment message prompt
        payment_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💳 Buy Now (Pay QR)",
                    callback_data="btn_show_qr",
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
            text=cfg["payment_caption"],
            reply_markup=payment_markup,
            parse_mode="Markdown"
        )

    elif query.data == "btn_show_qr":
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
                caption="📲 *Scan this QR code to complete your payment.*\nOnce completed, send your screenshot receipt here.",
                reply_markup=qr_markup,
                parse_mode="Markdown"
            )

# --- FastAPI Lifespan ---
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

    # Build 17 Button configuration cards (Label + its specific bulk videos)
    button_sections_html = ""
    for idx in range(17):
        b_name = cfg["buttons"][idx] if idx < len(cfg["buttons"]) else f"Category {idx+1}"
        v_list = cfg["button_videos"].get(str(idx), [])
        v_text = "\n".join(v_list)
        
        button_sections_html += f"""
        <div style="background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 14px; margin-bottom: 14px;">
            <label style="color: #4ade80; font-weight: bold; font-size: 14px;">🟢 Green Button #{idx + 1} Name:</label>
            <input type="text" name="btn_label_{idx}" value="{b_name}" required style="border-left: 5px solid #22c55e;" />
            
            <label style="color: #94a3b8; font-size: 13px;">Bulk Videos for this button (One URL per line):</label>
            <textarea name="btn_videos_{idx}" rows="2" placeholder="https://domain.com/video1.mp4&#10;https://domain.com/video2.mp4">{v_text}</textarea>
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
            <h1>⚙️ Bot Control Console</h1>
            <p>Engine Status: <span class="badge {bot_manager.status}">{bot_manager.status}</span></p>

            <!-- 1. Bot Token Control -->
            <div class="card">
                <h3>1. Telegram Bot Token</h3>
                <form action="/admin/save-token" method="post">
                    <label>Bot Token (@BotFather):</label>
                    <input type="text" name="token" value="{cfg['token']}" placeholder="123456:ABC-DEF1234..." required />
                    <button type="submit">Update & Restart Engine</button>
                </form>
                <form action="/admin/stop-bot" method="post" style="margin-top: 10px;">
                    <button type="submit" class="btn-danger">Stop Bot</button>
                </form>
            </div>

            <!-- 2. Content & Bulk Media Configuration -->
            <form action="/admin/save-content" method="post">
                <div class="card">
                    <h3>2. 1st Message: Bulk Images (Album Banner)</h3>
                    <label>Image URLs (One per line, sent together as an album):</label>
                    <textarea name="welcome_images" rows="3" placeholder="https://domain.com/photo1.jpg&#10;https://domain.com/photo2.jpg">{welcome_images_text}</textarea>

                    <h3 style="margin-top: 18px;">3. 2nd Message: Caption & Home Button</h3>
                    <label>2nd Message Text Caption:</label>
                    <textarea name="welcome_caption" rows="3">{cfg['welcome_caption']}</textarea>

                    <label style="color: #38bdf8;">🔵 Home Button Label:</label>
                    <input type="text" name="home_button_text" value="{cfg['home_button_text']}" style="border-left: 5px solid #38bdf8;" required />
                </div>

                <div class="card">
                    <h3>4. 17 Green Buttons & Dedicated Bulk Videos</h3>
                    <p style="font-size: 12px; color: #9ca3af; margin-top: -6px;">Each button has its own set of videos delivered on click.</p>
                    {button_sections_html}
                </div>

                <div class="card">
                    <h3>5. Payment & QR Configuration</h3>
                    <label>Payment Prompt Caption:</label>
                    <textarea name="payment_caption" rows="2">{cfg['payment_caption']}</textarea>

                    <label>Payment QR Image URL:</label>
                    <input type="text" name="qr_image_url" value="{cfg['qr_image_url']}" required />
                </div>

                <button type="submit" class="btn-save">💾 Save All Settings</button>
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

    # 1. Bulk Images for 1st message
    raw_images = form.get("welcome_images", "").splitlines()
    images = [img.strip() for img in raw_images if img.strip()]
    update_field("welcome_images_json", json.dumps(images))

    # 2. Text captions & Home button
    update_field("welcome_caption", form.get("welcome_caption", "").strip())
    update_field("home_button_text", form.get("home_button_text", "").strip())
    update_field("payment_caption", form.get("payment_caption", "").strip())
    update_field("qr_image_url", form.get("qr_image_url", "").strip())

    # 3. 17 Buttons and their individual bulk video sets
    buttons = []
    button_videos = {}

    for idx in range(17):
        btn_name = form.get(f"btn_label_{idx}", f"Category {idx+1}").strip()
        buttons.append(btn_name)

        raw_vids = form.get(f"btn_videos_{idx}", "").splitlines()
        button_videos[str(idx)] = [v.strip() for v in raw_vids if v.strip()]

    update_field("buttons_json", json.dumps(buttons))
    update_field("button_videos_json", json.dumps(button_videos))

    return RedirectResponse(url="/admin", status_code=303)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
    c.execute("""
        SELECT token, welcome_image, welcome_caption, buttons_json, 
               home_button_text, videos_json, payment_caption, qr_image_url 
        FROM settings WHERE id = 1
    """)
    row = c.fetchone()
    conn.close()
    return {
        "token": row[0] or "",
        "welcome_image": row[1],
        "welcome_caption": row[2],
        "buttons": json.loads(row[3]),
        "home_button_text": row[4],
        "videos": json.loads(row[5]),
        "payment_caption": row[6],
        "qr_image_url": row[7]
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
            logger.info("Bot started successfully on Railway.")

            while self.status == "Running":
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info("Bot execution cancelled.")
        except Exception as e:
            logger.error(f"Bot failed with error: {e}")
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

# --- Telegram Layout Builders ---
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

# --- Telegram Handlers ---
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cfg = get_settings()

    if cfg["welcome_image"]:
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=cfg["welcome_image"])
        except Exception as e:
            logger.error(f"Failed to send banner image: {e}")

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
        for video_url in cfg["videos"]:
            if video_url.strip():
                try:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=video_url.strip(),
                        supports_streaming=True
                    )
                except Exception as e:
                    logger.error(f"Error sending video: {e}")

        payment_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💳 Buy Now (Pay QR)",
                    callback_data="btn_show_qr",
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
            text=cfg["payment_caption"],
            reply_markup=payment_markup,
            parse_mode="Markdown"
        )

    elif query.data == "btn_show_qr":
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
                caption="📲 *Scan this QR code to complete payment.*\nSend receipt screenshot here once done.",
                reply_markup=qr_markup,
                parse_mode="Markdown"
            )

# --- FastAPI Lifespan ---
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

    button_rows = ""
    for idx, name in enumerate(cfg["buttons"]):
        button_rows += f"""
        <div style="margin-bottom: 10px;">
            <label style="font-size: 13px; color: #15803d; font-weight: bold;">Green Button {idx + 1}:</label>
            <input type="text" name="btn_{idx}" value="{name}" required style="border-left: 5px solid #22c55e;" />
        </div>
        """

    videos_text = "\n".join(cfg["videos"])

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Telegram Bot Web Admin</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 24px; }}
            .container {{ max-width: 800px; margin: auto; background: #1e293b; border-radius: 12px; padding: 28px; box-shadow: 0 10px 25px rgba(0,0,0,0.4); }}
            h1 {{ margin-top: 0; font-size: 22px; color: #38bdf8; border-bottom: 1px solid #334155; padding-bottom: 12px; }}
            .card {{ background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 20px; margin-bottom: 20px; }}
            .badge {{ display: inline-block; padding: 4px 10px; border-radius: 20px; font-weight: bold; font-size: 12px; }}
            .badge.Running {{ background: #166534; color: #4ade80; }}
            .badge.Stopped {{ background: #991b1b; color: #fca5a5; }}
            label {{ display: block; font-size: 14px; font-weight: 600; margin-bottom: 6px; color: #94a3b8; }}
            input[type="text"], textarea {{ width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #475569; border-radius: 6px; background: #1e293b; color: #fff; margin-bottom: 14px; font-size: 14px; }}
            button {{ background: #2563eb; color: #fff; font-weight: bold; border: none; padding: 12px 20px; border-radius: 6px; cursor: pointer; }}
            button:hover {{ background: #1d4ed8; }}
            .btn-danger {{ background: #dc2626; }}
            .btn-danger:hover {{ background: #b91c1c; }}
            .btn-save {{ background: #16a34a; width: 100%; font-size: 16px; padding: 14px; }}
            .btn-save:hover {{ background: #15803d; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>⚙️ Telegram Bot Console (Railway)</h1>
            <p>Bot Status: <span class="badge {bot_manager.status}">{bot_manager.status}</span></p>

            <div class="card">
                <h3>1. Telegram Bot Token</h3>
                <form action="/admin/save-token" method="post">
                    <label>Token from @BotFather:</label>
                    <input type="text" name="token" value="{cfg['token']}" placeholder="123456:ABC-DEF1234..." required />
                    <button type="submit">Update & Restart Bot</button>
                </form>
                <form action="/admin/stop-bot" method="post" style="margin-top: 10px;">
                    <button type="submit" class="btn-danger">Stop Bot</button>
                </form>
            </div>

            <form action="/admin/save-content" method="post">
                <div class="card">
                    <h3>2. Start Messages Setup</h3>
                    <label>1st Message: Image URL:</label>
                    <input type="text" name="welcome_image" value="{cfg['welcome_image']}" required />

                    <label>2nd Message: Caption:</label>
                    <textarea name="welcome_caption" rows="3">{cfg['welcome_caption']}</textarea>
                </div>

                <div class="card">
                    <h3>3. 17 Green Buttons & 1 Blue Home Button</h3>
                    {button_rows}

                    <label style="color: #38bdf8; font-weight: bold; margin-top: 14px;">Blue Button Text (Home):</label>
                    <input type="text" name="home_button_text" value="{cfg['home_button_text']}" style="border-left: 5px solid #38bdf8;" required />
                </div>

                <div class="card">
                    <h3>4. Video Delivery & Payment Flow</h3>
                    <label>Video URLs (One per line):</label>
                    <textarea name="videos" rows="3">{videos_text}</textarea>

                    <label>Payment Message Caption:</label>
                    <textarea name="payment_caption" rows="2">{cfg['payment_caption']}</textarea>

                    <label>QR Code Image URL:</label>
                    <input type="text" name="qr_image_url" value="{cfg['qr_image_url']}" required />
                </div>

                <button type="submit" class="btn-save">💾 Save All Settings</button>
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

    update_field("welcome_image", form.get("welcome_image", "").strip())
    update_field("welcome_caption", form.get("welcome_caption", "").strip())
    update_field("home_button_text", form.get("home_button_text", "").strip())
    update_field("payment_caption", form.get("payment_caption", "").strip())
    update_field("qr_image_url", form.get("qr_image_url", "").strip())

    buttons = [form.get(f"btn_{i}", f"Category {i+1}").strip() for i in range(17)]
    update_field("buttons_json", json.dumps(buttons))

    raw_videos = form.get("videos", "").splitlines()
    videos = [v.strip() for v in raw_videos if v.strip()]
    update_field("videos_json", json.dumps(videos))

    return RedirectResponse(url="/admin", status_code=303)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
