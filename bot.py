import os
import re
import json
import uuid
from pathlib import Path
from dotenv import load_dotenv

from cryptography.fernet import Fernet

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

SITE_URL = "https://jhatpatportal.uppcl.org/"

BASE_DIR = Path(__file__).parent
SCREENSHOT_DIR = BASE_DIR / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

DATA_FILE = BASE_DIR / "saved_logins.json"
KEY_FILE = BASE_DIR / "secret.key"

sessions = {}

STATE_IDLE = "idle"
STATE_WAITING_CREDENTIALS = "waiting_credentials"
STATE_WAITING_SAVE_CHOICE = "waiting_save_choice"
STATE_WAITING_SAVED_CHOICE = "waiting_saved_choice"
STATE_WAITING_DELETE_CHOICE = "waiting_delete_choice"
STATE_WAITING_CAPTCHA = "waiting_captcha"


# ---------------- KEYBOARDS ----------------

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔐 New Login"), KeyboardButton("📂 Saved Logins")],
            [KeyboardButton("📸 Screenshot"), KeyboardButton("📊 Status")],
            [KeyboardButton("🗑 Delete Saved"), KeyboardButton("🛑 Close Session")],
            [KeyboardButton("❔ Help")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Choose an option..."
    )


def cancel_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌ Cancel")]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def save_choice_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🚀 Login Once")],
            [KeyboardButton("💾 Save & Login")],
            [KeyboardButton("❌ Cancel")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


# ---------------- BASIC HELPERS ----------------

def is_allowed(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in ALLOWED_USER_IDS)


def get_chat_id(update: Update) -> int:
    return update.effective_chat.id


def ensure_session(chat_id: int):
    if chat_id not in sessions:
        sessions[chat_id] = {
            "state": STATE_IDLE,
            "name": None,
            "login_id": None,
            "password": None,
            "pending_name": None,
            "pending_login_id": None,
            "pending_password": None,
            "playwright": None,
            "browser": None,
            "context": None,
            "page": None,
            "logged_in": False,
        }


def mask_text(text: str) -> str:
    if not text:
        return "N/A"
    if len(text) <= 4:
        return "*" * len(text)
    return text[:2] + "*" * (len(text) - 4) + text[-2:]


def html_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def send_text(update: Update, text: str, keyboard=None):
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=keyboard or main_keyboard(),
    )


def parse_name_id_password(text: str):
    """
    Format:
    Ramesh ji 7302405466 password123

    Last 2 parts are login_id and password.
    Everything before that is name.
    """
    parts = text.strip().split()

    if len(parts) < 3:
        return None, None, None

    password = parts[-1].strip()
    login_id = parts[-2].strip()
    name = " ".join(parts[:-2]).strip()

    return name, login_id, password


# ---------------- ENCRYPTED SAVED LOGINS ----------------

def get_cipher() -> Fernet:
    if not KEY_FILE.exists():
        KEY_FILE.write_bytes(Fernet.generate_key())
        try:
            os.chmod(KEY_FILE, 0o600)
        except Exception:
            pass

    key = KEY_FILE.read_bytes()
    return Fernet(key)


def load_saved_logins() -> list:
    if not DATA_FILE.exists():
        return []

    try:
        return json.loads(DATA_FILE.read_text())
    except Exception:
        return []


def save_saved_logins(items: list):
    DATA_FILE.write_text(json.dumps(items, indent=2))


def encrypt_password(password: str) -> str:
    return get_cipher().encrypt(password.encode()).decode()


def decrypt_password(token: str) -> str:
    return get_cipher().decrypt(token.encode()).decode()


def add_saved_login(name: str, login_id: str, password: str):
    items = load_saved_logins()

    # Same login ID already saved hai toh update
    for item in items:
        if item.get("login_id") == login_id:
            item["name"] = name
            item["login_id"] = login_id
            item["password"] = encrypt_password(password)
            save_saved_logins(items)
            return

    items.append(
        {
            "id": str(uuid.uuid4()),
            "name": name,
            "login_id": login_id,
            "password": encrypt_password(password),
        }
    )
    save_saved_logins(items)


def delete_saved_login_by_index(index: int) -> bool:
    items = load_saved_logins()
    if index < 0 or index >= len(items):
        return False

    items.pop(index)
    save_saved_logins(items)
    return True


def saved_logins_text(delete_mode=False) -> str:
    items = load_saved_logins()

    if not items:
        return "📂 <b>No saved logins found.</b>"

    if delete_mode:
        lines = ["🗑 <b>Delete Saved Login</b>\n"]
    else:
        lines = ["📂 <b>Saved Logins</b>\n"]

    for i, item in enumerate(items, start=1):
        name = item.get("name", "Unknown")
        login_id = item.get("login_id", "")
        lines.append(
            f"{i}. <b>{html_escape(name)}</b>  —  <code>{mask_text(login_id)}</code>"
        )

    if delete_mode:
        lines.append("\nSend number to delete.")
    else:
        lines.append("\nSend number to login.")

    lines.append("Example: <code>1</code>")

    return "\n".join(lines)


# ---------------- BROWSER SESSION ----------------

async def close_browser_session(chat_id: int):
    if chat_id not in sessions:
        return

    session = sessions[chat_id]

    try:
        if session.get("context"):
            await session["context"].close()
    except Exception:
        pass

    try:
        if session.get("browser"):
            await session["browser"].close()
    except Exception:
        pass

    try:
        if session.get("playwright"):
            await session["playwright"].stop()
    except Exception:
        pass

    sessions[chat_id] = {
        "state": STATE_IDLE,
        "name": None,
        "login_id": None,
        "password": None,
        "pending_name": None,
        "pending_login_id": None,
        "pending_password": None,
        "playwright": None,
        "browser": None,
        "context": None,
        "page": None,
        "logged_in": False,
    }


async def safe_goto(page, url: str):
    try:
        await page.goto(url, wait_until="commit", timeout=120000)
    except PlaywrightTimeoutError:
        pass

    await page.wait_for_timeout(5000)


async def is_dashboard_visible(page) -> bool:
    checks = [
        "text=Dashboard",
        "text=List of Previously Applied Applications",
        "text=Apply for New Connection",
        "text=View Details",
    ]

    for selector in checks:
        try:
            if await page.locator(selector).first.is_visible(timeout=3000):
                return True
        except Exception:
            pass

    return False


async def launch_browser_and_page():
    playwright = await async_playwright().start()

    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-default-apps",
            "--no-first-run",
        ],
    )

    browser_context = await browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    )

    page = await browser_context.new_page()
    page.set_default_timeout(30000)
    page.set_default_navigation_timeout(120000)

    return playwright, browser, browser_context, page


async def take_captcha_crop(page, chat_id: int) -> Path:
    """
    Full page ki jagah sirf captcha area crop karta hai.
    Agar crop fail ho toh full screenshot fallback.
    """
    captcha_path = SCREENSHOT_DIR / f"captcha_{chat_id}.png"

    captcha_box = None

    captcha_selectors = [
        "input[placeholder*='captcha']",
        "input[placeholder*='Captcha']",
        "input[placeholder*='CAPTCHA']",
    ]

    for selector in captcha_selectors:
        try:
            captcha_input = page.locator(selector).first
            box = await captcha_input.bounding_box(timeout=5000)

            if box:
                captcha_box = {
                    "x": max(box["x"] - 50, 0),
                    "y": max(box["y"] - 135, 0),
                    "width": max(box["width"] + 160, 340),
                    "height": 210,
                }
                break
        except Exception:
            pass

    if captcha_box:
        try:
            await page.screenshot(path=str(captcha_path), clip=captcha_box)
            return captcha_path
        except Exception:
            pass

    await page.screenshot(path=str(captcha_path), full_page=True)
    return captcha_path


# ---------------- COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Access denied.")
        return

    chat_id = get_chat_id(update)
    ensure_session(chat_id)

    await send_text(
        update,
        "⚡ <b>Jhatpat Bot Ready</b>\n\n"
        "Button choose karo 👇\n\n"
        "🔐 <b>New Login</b> - new customer login\n"
        "📂 <b>Saved Logins</b> - saved customer se login\n"
        "📸 <b>Screenshot</b> - current page image\n"
        "🗑 <b>Delete Saved</b> - saved login remove\n"
        "🛑 <b>Close Session</b> - browser close",
        main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Access denied.")
        return

    await send_text(
        update,
        "❔ <b>Help</b>\n\n"
        "New login flow:\n"
        "1️⃣ Press <b>🔐 New Login</b>\n"
        "2️⃣ Send <b>Name LoginID Password</b> in one message\n\n"
        "Example:\n"
        "<code>Ramesh ji 7302405466 mypassword</code>\n\n"
        "3️⃣ Choose <b>🚀 Login Once</b> or <b>💾 Save & Login</b>\n"
        "4️⃣ Bot sirf captcha image bhejega\n"
        "5️⃣ Captcha answer bhej do\n\n"
        "Saved login flow:\n"
        "1️⃣ Press <b>📂 Saved Logins</b>\n"
        "2️⃣ Name ke saamne number select karo\n"
        "3️⃣ Captcha solve karo",
        main_keyboard(),
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Access denied.")
        return

    chat_id = get_chat_id(update)
    ensure_session(chat_id)
    session = sessions[chat_id]

    state = session.get("state", STATE_IDLE)
    logged_in = session.get("logged_in", False)
    name = session.get("name")
    login_id = session.get("login_id")

    if state == STATE_WAITING_CREDENTIALS:
        state_text = "Waiting for Name + ID + Password"
    elif state == STATE_WAITING_SAVE_CHOICE:
        state_text = "Waiting for Save/Login choice"
    elif state == STATE_WAITING_SAVED_CHOICE:
        state_text = "Waiting for saved login selection"
    elif state == STATE_WAITING_DELETE_CHOICE:
        state_text = "Waiting for delete selection"
    elif state == STATE_WAITING_CAPTCHA:
        state_text = "Waiting for Captcha"
    elif logged_in:
        state_text = "Logged in / Page active"
    else:
        state_text = "Idle"

    saved_count = len(load_saved_logins())

    await send_text(
        update,
        "📊 <b>Bot Status</b>\n\n"
        f"👤 Name: <b>{html_escape(name or 'N/A')}</b>\n"
        f"🔑 Login ID: <code>{mask_text(login_id)}</code>\n"
        f"💾 Saved Logins: <b>{saved_count}</b>\n"
        f"🟢 State: <b>{state_text}</b>",
        main_keyboard(),
    )


# ---------------- FLOW ACTIONS ----------------

async def start_new_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    ensure_session(chat_id)

    await close_browser_session(chat_id)
    ensure_session(chat_id)

    sessions[chat_id]["state"] = STATE_WAITING_CREDENTIALS

    await send_text(
        update,
        "🔐 <b>New Login Started</b>\n\n"
        "Please send <b>Name LoginID Password</b> in one message.\n\n"
        "Example:\n"
        "<code>Ramesh ji 7302405466 mypassword</code>\n\n"
        "Note: Last 2 words should be Login ID and Password.",
        cancel_keyboard(),
    )


async def show_saved_logins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    ensure_session(chat_id)

    items = load_saved_logins()

    if not items:
        await send_text(
            update,
            "📂 <b>No saved logins found.</b>\n\n"
            "Press <b>🔐 New Login</b> and choose <b>💾 Save & Login</b> first.",
            main_keyboard(),
        )
        return

    await close_browser_session(chat_id)
    ensure_session(chat_id)

    sessions[chat_id]["state"] = STATE_WAITING_SAVED_CHOICE

    await send_text(
        update,
        saved_logins_text(delete_mode=False),
        cancel_keyboard(),
    )


async def show_delete_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    ensure_session(chat_id)

    items = load_saved_logins()

    if not items:
        await send_text(
            update,
            "🗑 <b>No saved logins to delete.</b>",
            main_keyboard(),
        )
        return

    sessions[chat_id]["state"] = STATE_WAITING_DELETE_CHOICE

    await send_text(
        update,
        saved_logins_text(delete_mode=True),
        cancel_keyboard(),
    )


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    ensure_session(chat_id)

    await close_browser_session(chat_id)

    await send_text(
        update,
        "❌ <b>Cancelled.</b>\n\n"
        "Back to main menu.",
        main_keyboard(),
    )


async def open_site_and_send_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    session = sessions[chat_id]

    login_id = session.get("login_id")
    password = session.get("password")

    progress_msg = await update.message.reply_text(
        "⚡ Opening Jhatpat portal...\nPlease wait...",
        parse_mode=ParseMode.HTML,
    )

    playwright = None

    try:
        playwright, browser, browser_context, page = await launch_browser_and_page()

        session["playwright"] = playwright
        session["browser"] = browser
        session["context"] = browser_context
        session["page"] = page
        session["logged_in"] = False

        await progress_msg.edit_text("🌐 Opening portal...", parse_mode=ParseMode.HTML)

        await safe_goto(page, SITE_URL)

        await progress_msg.edit_text("🔐 Switching to Password Login...", parse_mode=ParseMode.HTML)

        clicked_password_tab = False

        password_tab_selectors = [
            "text=Password Login",
            "xpath=//*[contains(normalize-space(), 'Password Login')]",
        ]

        for selector in password_tab_selectors:
            try:
                await page.locator(selector).first.click(timeout=30000)
                clicked_password_tab = True
                break
            except Exception:
                pass

        if not clicked_password_tab:
            error_path = SCREENSHOT_DIR / f"password_tab_error_{chat_id}.png"
            await page.screenshot(path=str(error_path), full_page=True)

            await update.message.reply_photo(
                photo=open(error_path, "rb"),
                caption="❌ Password Login tab click nahi hua.",
                reply_markup=main_keyboard(),
            )
            raise RuntimeError("Password Login tab not found/clickable.")

        await page.wait_for_timeout(1500)

        await progress_msg.edit_text("✍️ Filling details...", parse_mode=ParseMode.HTML)

        login_filled = False

        login_selectors = [
            "input[placeholder*='Login ID']",
            "input[placeholder*='Mobile']",
            "input[type='text']",
        ]

        for selector in login_selectors:
            try:
                await page.locator(selector).first.fill(login_id, timeout=30000)
                login_filled = True
                break
            except Exception:
                pass

        if not login_filled:
            raise RuntimeError("Login ID field not found.")

        password_filled = False

        password_selectors = [
            "input[placeholder*='Password']",
            "input[type='password']",
        ]

        for selector in password_selectors:
            try:
                await page.locator(selector).first.fill(password, timeout=30000)
                password_filled = True
                break
            except Exception:
                pass

        if not password_filled:
            raise RuntimeError("Password field not found.")

        await page.wait_for_timeout(1200)

        captcha_path = await take_captcha_crop(page, chat_id)

        session["state"] = STATE_WAITING_CAPTCHA

        await progress_msg.edit_text("🧩 Captcha ready.", parse_mode=ParseMode.HTML)

        await update.message.reply_photo(
            photo=open(captcha_path, "rb"),
            caption=(
                "🧩 <b>Captcha solve karo</b>\n\n"
                "Sirf answer bhejna hai.\n"
                "Example: <code>16</code>"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_keyboard(),
        )

    except Exception as e:
        if playwright:
            try:
                await playwright.stop()
            except Exception:
                pass

        session["state"] = STATE_IDLE

        try:
            error_path = SCREENSHOT_DIR / f"open_error_{chat_id}.png"
            if session.get("page"):
                await session["page"].screenshot(path=str(error_path), full_page=True)
                await update.message.reply_photo(
                    photo=open(error_path, "rb"),
                    caption=f"❌ Error:\n{str(e)}",
                    reply_markup=main_keyboard(),
                )
            else:
                await progress_msg.edit_text(
                    f"❌ <b>Error</b>\n\n<code>{html_escape(str(e))}</code>",
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            await progress_msg.edit_text(
                f"❌ <b>Error</b>\n\n<code>{html_escape(str(e))}</code>",
                parse_mode=ParseMode.HTML,
            )


async def submit_captcha_and_login(update: Update, context: ContextTypes.DEFAULT_TYPE, captcha_answer: str):
    chat_id = get_chat_id(update)
    session = sessions[chat_id]
    page = session.get("page")

    if not page:
        session["state"] = STATE_IDLE
        await send_text(update, "❌ Browser page not found. Start again.", main_keyboard())
        return

    progress_msg = await update.message.reply_text(
        "🧩 Entering captcha...",
        parse_mode=ParseMode.HTML,
    )

    try:
        captcha_filled = False

        captcha_selectors = [
            "input[placeholder*='captcha']",
            "input[placeholder*='Captcha']",
            "input[placeholder*='CAPTCHA']",
        ]

        for selector in captcha_selectors:
            try:
                await page.locator(selector).first.fill(captcha_answer, timeout=20000)
                captcha_filled = True
                break
            except Exception:
                pass

        if not captcha_filled:
            inputs = page.locator("input")
            count = await inputs.count()

            for i in range(count):
                try:
                    placeholder = await inputs.nth(i).get_attribute("placeholder")
                    if placeholder and "captcha" in placeholder.lower():
                        await inputs.nth(i).fill(captcha_answer, timeout=20000)
                        captcha_filled = True
                        break
                except Exception:
                    pass

        if not captcha_filled:
            await page.locator("input").nth(2).fill(captcha_answer, timeout=20000)

        await progress_msg.edit_text("🚪 Clicking Login...", parse_mode=ParseMode.HTML)

        await page.wait_for_timeout(500)

        clicked_login = False

        login_button_selectors = [
            "button:has-text('Login')",
            "input[type='submit'][value*='Login']",
            "text=Login",
            "xpath=//*[normalize-space()='Login']",
        ]

        for selector in login_button_selectors:
            try:
                await page.locator(selector).first.click(timeout=30000)
                clicked_login = True
                break
            except Exception:
                pass

        if not clicked_login:
            raise RuntimeError("Login button not found/clickable.")

        await progress_msg.edit_text("⏳ Waiting after login...", parse_mode=ParseMode.HTML)

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=45000)
        except PlaywrightTimeoutError:
            pass

        await page.wait_for_timeout(7000)

        after_login_path = SCREENSHOT_DIR / f"after_login_{chat_id}.png"
        await page.screenshot(path=str(after_login_path), full_page=True)

        dashboard_ok = await is_dashboard_visible(page)

        session["state"] = STATE_IDLE
        session["logged_in"] = True
        session["password"] = None
        session["pending_password"] = None

        name = session.get("name") or "Customer"

        if dashboard_ok:
            await progress_msg.edit_text("✅ Login completed.", parse_mode=ParseMode.HTML)
            caption = (
                f"✅ <b>Login successful.</b>\n\n"
                f"👤 <b>{html_escape(name)}</b>\n\n"
                "📸 Screenshot button se current page dekh sakte ho.\n"
                "🛑 Close Session se browser close kar sakte ho."
            )
        else:
            await progress_msg.edit_text("⚠️ Screenshot ready.", parse_mode=ParseMode.HTML)
            caption = (
                f"⚠️ <b>Login ke baad page screenshot.</b>\n\n"
                f"👤 <b>{html_escape(name)}</b>\n\n"
                "Agar dashboard dikh raha hai toh login successful hai.\n"
                "Agar error dikh raha hai toh captcha/password issue ho sakta hai."
            )

        await update.message.reply_photo(
            photo=open(after_login_path, "rb"),
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )

    except Exception as e:
        try:
            after_login_path = SCREENSHOT_DIR / f"after_login_errorcheck_{chat_id}.png"
            await page.screenshot(path=str(after_login_path), full_page=True)
            dashboard_ok = await is_dashboard_visible(page)

            if dashboard_ok:
                session["state"] = STATE_IDLE
                session["logged_in"] = True
                session["password"] = None
                session["pending_password"] = None

                await progress_msg.edit_text("✅ Login completed.", parse_mode=ParseMode.HTML)

                await update.message.reply_photo(
                    photo=open(after_login_path, "rb"),
                    caption="✅ <b>Login successful.</b>\n\nDashboard open ho gaya.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_keyboard(),
                )
                return

            session["state"] = STATE_WAITING_CAPTCHA

            await progress_msg.edit_text("❌ Login error. Screenshot bhej raha hoon.", parse_mode=ParseMode.HTML)

            await update.message.reply_photo(
                photo=open(after_login_path, "rb"),
                caption=(
                    f"❌ Login error:\n{str(e)}\n\n"
                    "Captcha galat ho sakta hai. Correct captcha dobara bhejo."
                ),
                reply_markup=cancel_keyboard(),
            )

        except Exception:
            session["state"] = STATE_WAITING_CAPTCHA
            await progress_msg.edit_text(
                f"❌ <b>Login error</b>\n\n<code>{html_escape(str(e))}</code>",
                parse_mode=ParseMode.HTML,
            )


async def screenshot_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    ensure_session(chat_id)

    session = sessions[chat_id]
    page = session.get("page")

    if not page:
        await send_text(
            update,
            "⚠️ No active browser session.\n\n"
            "Press 🔐 New Login or 📂 Saved Logins first.",
            main_keyboard(),
        )
        return

    try:
        path = SCREENSHOT_DIR / f"manual_screenshot_{chat_id}.png"
        await page.screenshot(path=str(path), full_page=True)

        await update.message.reply_photo(
            photo=open(path, "rb"),
            caption="📸 Current page screenshot.",
            reply_markup=main_keyboard(),
        )

    except Exception as e:
        await send_text(
            update,
            f"❌ Screenshot error:\n<code>{html_escape(str(e))}</code>",
            main_keyboard(),
        )


# ---------------- MESSAGE ROUTER ----------------

async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    if not is_allowed(update):
        await update.message.reply_text("⛔ Access denied.")
        return

    chat_id = get_chat_id(update)
    ensure_session(chat_id)

    text = update.message.text.strip()
    session = sessions[chat_id]
    state = session.get("state", STATE_IDLE)

    # Buttons
    if text == "🔐 New Login":
        await start_new_login(update, context)
        return

    if text == "📂 Saved Logins":
        await show_saved_logins(update, context)
        return

    if text == "📸 Screenshot":
        await screenshot_action(update, context)
        return

    if text == "📊 Status":
        await status_command(update, context)
        return

    if text == "🗑 Delete Saved":
        await show_delete_saved(update, context)
        return

    if text == "🛑 Close Session":
        await close_browser_session(chat_id)
        await send_text(update, "🛑 <b>Session closed.</b>", main_keyboard())
        return

    if text == "❔ Help":
        await help_command(update, context)
        return

    if text == "❌ Cancel":
        await cancel_flow(update, context)
        return

    # Waiting for Name + ID + Password
    if state == STATE_WAITING_CREDENTIALS:
        name, login_id, password = parse_name_id_password(text)

        if not name or not login_id or not password:
            await send_text(
                update,
                "⚠️ Format galat hai.\n\n"
                "Name, Login ID aur Password ek hi message me bhejo.\n\n"
                "Example:\n"
                "<code>Ramesh ji 7302405466 mypassword</code>\n\n"
                "Last 2 words should be Login ID and Password.",
                cancel_keyboard(),
            )
            return

        if len(name) < 2:
            await send_text(
                update,
                "⚠️ Name too short lag raha hai.\n\n"
                "Example:\n"
                "<code>Ramesh ji 7302405466 mypassword</code>",
                cancel_keyboard(),
            )
            return

        if len(login_id) < 5:
            await send_text(
                update,
                "⚠️ Login ID / Mobile Number too short lag raha hai.\n\n"
                "Example:\n"
                "<code>Ramesh ji 7302405466 mypassword</code>",
                cancel_keyboard(),
            )
            return

        if len(password) < 3:
            await send_text(
                update,
                "⚠️ Password too short lag raha hai.\n\n"
                "Example:\n"
                "<code>Ramesh ji 7302405466 mypassword</code>",
                cancel_keyboard(),
            )
            return

        session["pending_name"] = name
        session["pending_login_id"] = login_id
        session["pending_password"] = password
        session["state"] = STATE_WAITING_SAVE_CHOICE

        await send_text(
            update,
            "✅ <b>Login details received.</b>\n\n"
            f"👤 Name: <b>{html_escape(name)}</b>\n"
            f"🔑 Login ID: <code>{mask_text(login_id)}</code>\n\n"
            "Choose option:",
            save_choice_keyboard(),
        )
        return

    # Save/Login choice
    if state == STATE_WAITING_SAVE_CHOICE:
        name = session.get("pending_name")
        login_id = session.get("pending_login_id")
        password = session.get("pending_password")

        if not name or not login_id or not password:
            session["state"] = STATE_IDLE
            await send_text(update, "❌ Details missing. Start again.", main_keyboard())
            return

        if text == "💾 Save & Login":
            add_saved_login(name, login_id, password)

            session["name"] = name
            session["login_id"] = login_id
            session["password"] = password

            await send_text(
                update,
                "💾 <b>Saved.</b>\n\n"
                f"👤 <b>{html_escape(name)}</b>\n\n"
                "Opening portal now...",
                cancel_keyboard(),
            )

            await open_site_and_send_captcha(update, context)
            return

        if text == "🚀 Login Once":
            session["name"] = name
            session["login_id"] = login_id
            session["password"] = password

            await send_text(
                update,
                "🚀 <b>Opening portal now...</b>",
                cancel_keyboard(),
            )

            await open_site_and_send_captcha(update, context)
            return

        await send_text(
            update,
            "Please choose one option:\n\n"
            "🚀 <b>Login Once</b>\n"
            "💾 <b>Save & Login</b>",
            save_choice_keyboard(),
        )
        return

    # Saved login choice
    if state == STATE_WAITING_SAVED_CHOICE:
        if not re.fullmatch(r"[0-9]{1,3}", text):
            await send_text(update, "⚠️ Sirf number bhejo. Example: <code>1</code>", cancel_keyboard())
            return

        items = load_saved_logins()
        index = int(text) - 1

        if index < 0 or index >= len(items):
            await send_text(update, "⚠️ Invalid number. Try again.", cancel_keyboard())
            return

        item = items[index]

        try:
            name = item.get("name", "Customer")
            login_id = item["login_id"]
            password = decrypt_password(item["password"])
        except Exception:
            await send_text(update, "❌ Saved password decrypt nahi hua. Delete karke dobara save karo.", main_keyboard())
            session["state"] = STATE_IDLE
            return

        session["name"] = name
        session["login_id"] = login_id
        session["password"] = password

        await send_text(
            update,
            f"✅ Selected: <b>{html_escape(name)}</b>\n"
            f"🔑 ID: <code>{mask_text(login_id)}</code>\n\n"
            "Opening portal now...",
            cancel_keyboard(),
        )

        await open_site_and_send_captcha(update, context)
        return

    # Delete saved choice
    if state == STATE_WAITING_DELETE_CHOICE:
        if not re.fullmatch(r"[0-9]{1,3}", text):
            await send_text(update, "⚠️ Sirf number bhejo. Example: <code>1</code>", cancel_keyboard())
            return

        index = int(text) - 1
        ok = delete_saved_login_by_index(index)

        session["state"] = STATE_IDLE

        if ok:
            await send_text(update, "🗑 <b>Saved login deleted.</b>", main_keyboard())
        else:
            await send_text(update, "⚠️ Invalid number.", main_keyboard())

        return

    # Captcha
    if state == STATE_WAITING_CAPTCHA:
        captcha_answer = text

        if not re.fullmatch(r"[0-9]{1,4}", captcha_answer):
            await send_text(
                update,
                "⚠️ Captcha invalid lag raha hai.\n\n"
                "Sirf number bhejo.\n"
                "Example: <code>16</code>",
                cancel_keyboard(),
            )
            return

        await submit_captcha_and_login(update, context, captcha_answer)
        return

    await send_text(
        update,
        "👋 Button choose karo 👇\n\n"
        "Login start karne ke liye <b>🔐 New Login</b> press karo.",
        main_keyboard(),
    )


# ---------------- COMMAND WRAPPERS ----------------

async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Access denied.")
        return

    chat_id = get_chat_id(update)
    await close_browser_session(chat_id)
    await send_text(update, "🛑 <b>Session closed.</b>", main_keyboard())


async def screenshot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Access denied.")
        return

    await screenshot_action(update, context)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("Bot error:", context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing in .env")

    if not ALLOWED_USER_IDS:
        raise RuntimeError("ALLOWED_USER_IDS missing in .env")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("screenshot", screenshot_command))
    app.add_handler(CommandHandler("close", close_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    app.add_error_handler(error_handler)

    print("Jhatpat button Telegram bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
