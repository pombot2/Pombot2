import asyncio
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Persistent Storage Directory for Railway Volume ---
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.join(os.getcwd(), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "bot_config.db")

# --- Database Setup & Helper Functions ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            token TEXT,
            welcome_image TEXT,
            welcome_caption TEXT,
            buttons_json TEXT,
            home_button_text TEXT,
            videos_json TEXT,
            payment_caption TEXT,
            qr_image_url TEXT
        )
    """)
    c.execute("SELECT COUNT(*) FROM settings")
    if c.fetchone()[0] == 0:
        default_buttons = [f"VIP Pack {i} - Full Access Stream" for i in range(1, 18)]
        default_videos = [
            "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4"
        ]
        c.execute("""
            INSERT INTO settings (
                id, token, welcome_image, welcome_caption, buttons_json, 
                home_button_text, videos_json, payment_caption, qr_image_url
            ) VALUES (1, '', ?, ?, ?, ?, ?, ?, ?)
        """, (
            "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=800",
            "✨ *Welcome to our Exclusive Hub!*\n\nChoose an option from the list below to begin:",
            json.dumps(default_buttons),
            "🏠 Return to Main Menu",
            json.dumps(default_videos),
            "💳 *Select your payment option to unlock immediate access.*",
            "https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=https://telegram.org"
        ))
        conn.commit()
    conn.close()

def get_settings():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
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
