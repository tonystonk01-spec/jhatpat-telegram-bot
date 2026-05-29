import os
import re
from pathlib import Path
from dotenv import load_dotenv

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

SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

# chat_id wise data
sessions = {}

STATE_IDLE = "idle"
STATE_WAITING_LOGIN_ID = "waiting_login_id"
STATE_WAITING_PASSWORD = "waiting_password"
STATE_WAITING_CAPTCHA = "waiting_captcha"


def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔐 New Login")],
            [KeyboardButton("📸 Screenshot"), KeyboardButton("📊 Status")],
            [KeyboardButton("🛑 Close Session"), KeyboardButton("❔ Help")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Choose an option..."
    )


def cancel_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("❌ Cancel")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def is_allowed(update: Update) -> bool:
    return bool(
        update.effective_user
        and update.effective_user.id in ALLOWED_USER_IDS
    )


def get_chat_id(update: Update) -> int:
    return update.effective_chat.id


def ensure_session(chat_id: int):
    if chat_id not in sessions:
        sessions[chat_id] = {
            "state": STATE_IDLE,
            "login_id": None,
            "password": None,
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


async def send_text(update: Update, text: str, keyboard=None):
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=keyboard or main_keyboard(),
    )


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
        "login_id": None,
        "password": None,
        "playwright": None,
        "browser": None,
        "context": None,
        "page": None,
        "logged_in": False,
    }


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
        "🔐 <b>New Login</b> - customer login start\n"
        "📸 <b>Screenshot</b> - current page image\n"
        "📊 <b>Status</b> - bot ka current state\n"
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
        "Simple flow:\n\n"
        "1️⃣ Press <b>🔐 New Login</b>\n"
        "2️⃣ Send Login ID / Mobile Number\n"
        "3️⃣ Send Password\n"
        "4️⃣ Bot captcha screenshot bhejega\n"
        "5️⃣ Captcha answer normal message me bhej do\n"
        "6️⃣ Bot login karke screenshot bhej dega\n\n"
        "No need to type /login or /captcha.",
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
    login_id = session.get("login_id")

    if state == STATE_WAITING_LOGIN_ID:
        state_text = "Waiting for Login ID"
    elif state == STATE_WAITING_PASSWORD:
        state_text = "Waiting for Password"
    elif state == STATE_WAITING_CAPTCHA:
        state_text = "Waiting for Captcha"
    elif logged_in:
        state_text = "Logged in / Page active"
    else:
        state_text = "Idle"

    await send_text(
        update,
        "📊 <b>Bot Status</b>\n\n"
        f"👤 Login ID: <code>{mask_text(login_id)}</code>\n"
        f"🟢 State: <b>{state_text}</b>\n\n"
        "Use buttons below 👇",
        main_keyboard(),
    )


async def start_new_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    ensure_session(chat_id)

    await close_browser_session(chat_id)
    ensure_session(chat_id)

    sessions[chat_id]["state"] = STATE_WAITING_LOGIN_ID

    await send_text(
        update,
        "🔐 <b>New Login Started</b>\n\n"
        "Please send <b>Login ID / Mobile Number</b>.",
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
        playwright = await async_playwright().start()

        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
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

        session["playwright"] = playwright
        session["browser"] = browser
        session["context"] = browser_context
        session["page"] = page
        session["logged_in"] = False

        await progress_msg.edit_text(
            "🌐 Opening Jhatpat portal...",
            parse_mode=ParseMode.HTML,
        )

        await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2500)

        await progress_msg.edit_text(
            "🔐 Switching to Password Login...",
            parse_mode=ParseMode.HTML,
        )

        clicked_password_tab = False

        password_tab_selectors = [
            "text=Password Login",
            "xpath=//*[contains(normalize-space(), 'Password Login')]",
        ]

        for selector in password_tab_selectors:
            try:
                await page.locator(selector).first.click(timeout=10000)
                clicked_password_tab = True
                break
            except Exception:
                pass

        if not clicked_password_tab:
            error_path = SCREENSHOT_DIR / f"password_tab_error_{chat_id}.png"
            await page.screenshot(path=str(error_path), full_page=True)

            await update.message.reply_photo(
                photo=open(error_path, "rb"),
                caption="❌ Password Login tab click nahi hua. Current page screenshot.",
                reply_markup=main_keyboard(),
            )

            raise RuntimeError("Password Login tab not found/clickable.")

        await page.wait_for_timeout(1500)

        await progress_msg.edit_text(
            "✍️ Filling login details...",
            parse_mode=ParseMode.HTML,
        )

        login_filled = False

        login_selectors = [
            "input[placeholder*='Login ID']",
            "input[placeholder*='Mobile']",
            "input[type='text']",
        ]

        for selector in login_selectors:
            try:
                await page.locator(selector).first.fill(login_id, timeout=7000)
                login_filled = True
                break
            except Exception:
                pass

        if not login_filled:
            raise RuntimeError("Login ID / Mobile Number field not found.")

        password_filled = False

        password_selectors = [
            "input[placeholder*='Password']",
            "input[type='password']",
        ]

        for selector in password_selectors:
            try:
                await page.locator(selector).first.fill(password, timeout=7000)
                password_filled = True
                break
            except Exception:
                pass

        if not password_filled:
            raise RuntimeError("Password field not found.")

        await page.wait_for_timeout(1000)

        captcha_path = SCREENSHOT_DIR / f"captcha_{chat_id}.png"
        await page.screenshot(path=str(captcha_path), full_page=True)

        session["state"] = STATE_WAITING_CAPTCHA

        await progress_msg.edit_text(
            "🧩 Captcha ready.",
            parse_mode=ParseMode.HTML,
        )

        await update.message.reply_photo(
            photo=open(captcha_path, "rb"),
            caption=(
                "🧩 <b>Please enter solved captcha...</b>\n\n"
                "Example: <code>8</code>\n\n"
                "Sirf number bhejna hai."
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

        await progress_msg.edit_text(
            f"❌ <b>Error</b>\n\n<code>{str(e)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def submit_captcha_and_login(update: Update, context: ContextTypes.DEFAULT_TYPE, captcha_answer: str):
    chat_id = get_chat_id(update)
    session = sessions[chat_id]
    page = session.get("page")

    if not page:
        await send_text(
            update,
            "❌ Browser page not found. Please start again.",
            main_keyboard(),
        )
        session["state"] = STATE_IDLE
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
                await page.locator(selector).first.fill(captcha_answer, timeout=5000)
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
                        await inputs.nth(i).fill(captcha_answer)
                        captcha_filled = True
                        break
                except Exception:
                    pass

        if not captcha_filled:
            # Layout fallback: Login ID, Password, Captcha
            await page.locator("input").nth(2).fill(captcha_answer)
            captcha_filled = True

        await progress_msg.edit_text(
            "🚪 Clicking Login button...",
            parse_mode=ParseMode.HTML,
        )

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
                await page.locator(selector).first.click(timeout=10000)
                clicked_login = True
                break
            except Exception:
                pass

        if not clicked_login:
            raise RuntimeError("Login button not found/clickable.")

        await progress_msg.edit_text(
            "⏳ Waiting for page after login...",
            parse_mode=ParseMode.HTML,
        )

        try:
            await page.wait_for_load_state("networkidle", timeout=25000)
        except PlaywrightTimeoutError:
            pass

        await page.wait_for_timeout(4000)

        after_login_path = SCREENSHOT_DIR / f"after_login_{chat_id}.png"
        await page.screenshot(path=str(after_login_path), full_page=True)

        session["state"] = STATE_IDLE
        session["logged_in"] = True

        # password memory clear after login
        session["password"] = None

        await progress_msg.edit_text(
            "✅ Login process completed.",
            parse_mode=ParseMode.HTML,
        )

        await update.message.reply_photo(
            photo=open(after_login_path, "rb"),
            caption=(
                "✅ <b>Login ke baad first page screenshot.</b>\n\n"
                "📸 Screenshot button se current page dekh sakte ho.\n"
                "🛑 Close Session se browser close kar sakte ho."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )

    except Exception as e:
        session["state"] = STATE_WAITING_CAPTCHA

        error_path = SCREENSHOT_DIR / f"login_error_{chat_id}.png"

        try:
            await page.screenshot(path=str(error_path), full_page=True)
            await progress_msg.edit_text(
                "❌ Login error. Screenshot bhej raha hoon.",
                parse_mode=ParseMode.HTML,
            )

            await update.message.reply_photo(
                photo=open(error_path, "rb"),
                caption=(
                    f"❌ Login error:\n{str(e)}\n\n"
                    "Captcha galat ho sakta hai. Correct captcha dobara bhejo."
                ),
                reply_markup=cancel_keyboard(),
            )

        except Exception:
            await progress_msg.edit_text(
                f"❌ <b>Login error</b>\n\n<code>{str(e)}</code>",
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
            "Press 🔐 New Login first.",
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
            f"❌ Screenshot error:\n<code>{str(e)}</code>",
            main_keyboard(),
        )


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

    if text == "📸 Screenshot":
        await screenshot_action(update, context)
        return

    if text == "📊 Status":
        await status_command(update, context)
        return

    if text == "🛑 Close Session":
        await close_browser_session(chat_id)
        await send_text(
            update,
            "🛑 <b>Session closed.</b>\n\n"
            "Back to main menu.",
            main_keyboard(),
        )
        return

    if text == "❔ Help":
        await help_command(update, context)
        return

    if text == "❌ Cancel":
        await cancel_flow(update, context)
        return

    # State handling
    if state == STATE_WAITING_LOGIN_ID:
        login_id = text

        if len(login_id) < 5:
            await send_text(
                update,
                "⚠️ Login ID / Mobile Number too short lag raha hai.\n\n"
                "Please send correct Login ID / Mobile Number.",
                cancel_keyboard(),
            )
            return

        session["login_id"] = login_id
        session["state"] = STATE_WAITING_PASSWORD

        await send_text(
            update,
            "✅ <b>Login ID received.</b>\n\n"
            "Now please send <b>Password</b>.",
            cancel_keyboard(),
        )
        return

    if state == STATE_WAITING_PASSWORD:
        password = text

        if len(password) < 3:
            await send_text(
                update,
                "⚠️ Password too short lag raha hai.\n\n"
                "Please send correct password.",
                cancel_keyboard(),
            )
            return

        session["password"] = password

        await send_text(
            update,
            "✅ <b>Password received.</b>\n\n"
            "Opening portal now...",
            cancel_keyboard(),
        )

        await open_site_and_send_captcha(update, context)
        return

    if state == STATE_WAITING_CAPTCHA:
        captcha_answer = text

        if not re.fullmatch(r"[0-9]{1,4}", captcha_answer):
            await send_text(
                update,
                "⚠️ Captcha invalid lag raha hai.\n\n"
                "Sirf number bhejo.\n"
                "Example: <code>8</code>",
                cancel_keyboard(),
            )
            return

        await submit_captcha_and_login(update, context, captcha_answer)
        return

    # Idle unknown text
    await send_text(
        update,
        "👋 Button choose karo 👇\n\n"
        "Login start karne ke liye <b>🔐 New Login</b> press karo.",
        main_keyboard(),
    )


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Access denied.")
        return

    chat_id = get_chat_id(update)
    await close_browser_session(chat_id)

    await send_text(
        update,
        "🛑 <b>Session closed.</b>",
        main_keyboard(),
    )


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

    # Commands still available, but papa can ignore them.
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("screenshot", screenshot_command))
    app.add_handler(CommandHandler("close", close_command))

    # Main button/text router
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    app.add_error_handler(error_handler)

    print("Jhatpat button Telegram bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()