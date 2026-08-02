import io
import re
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import execute
from keyboards.menus import category_inline_menu
from states import AMOUNT, CATEGORY, RECEIPT_CONFIRM, RECEIPT_GROUP_SELECT
from utils import auto_delete, DELETE_AFTER_ACK

logger = logging.getLogger(__name__)

try:
    import pytesseract
    from PIL import Image, ImageOps
    OCR_AVAILABLE = True
except ImportError:
    # pytesseract/Pillow (or the system `tesseract-ocr` binary they wrap)
    # aren't installed. The feature degrades gracefully to "couldn't read
    # this automatically, enter the amount yourself" rather than crashing.
    OCR_AVAILABLE = False


# Pass 1: amount immediately preceded by a currency marker (₹, Rs, Rs.,
# INR). Tried first since UPI apps show the paid amount with one of these
# markers, prominently, near the top -- when OCR reads the glyph correctly.
_CURRENCY_AMOUNT_RE = re.compile(
    r"(?:₹|Rs\.?|INR)\s*(\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

# Pass 2: a keyword like "paid"/"amount"/"total" sitting right next to a
# number, for screenshots where the ₹ glyph got dropped or misread by OCR
# (very common -- tesseract often reads ₹ as "z", "3", "T", or nothing).
_AMOUNT_KEYWORD_RE = re.compile(
    r"(?:paid|amount|total|payment of|amt|debited|credited)\D{0,12}?(\d[\d,]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

# Pass 3 (last resort): any amount-shaped number on a line that doesn't look
# like a reference/UTR/date/phone line.
_ANY_AMOUNT_RE = re.compile(r"\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?")
_NON_AMOUNT_LINE_RE = re.compile(
    r"(utr|ref(?:erence)?|txn|transaction|order\s*(id|no)|invoice|a/c|account|contact|phone|mobile|"
    r"\bid\b|\bno\.?\b|date|time|\d{1,2}:\d{2}\s*(am|pm)?)",
    re.IGNORECASE,
)

# Raw runs of 7+ consecutive digits are phone numbers, UTRs, order/txn IDs
# -- never amounts. Stripped out before scanning a line, since otherwise
# they fragment into false-positive 3-digit "amount" candidates (e.g.
# "9876543210" would otherwise yield 987, 654, 321...).
_LONG_DIGIT_RUN_RE = re.compile(r"\d{7,}")

_MAX_PLAUSIBLE_RUPEES = 10_000_000  # anything above this is almost certainly a reference number, not an amount


def _to_float(raw: str):
    try:
        return float(raw.replace(",", ""))
    except (ValueError, AttributeError, TypeError):
        return None


def extract_amount_from_text(text: str):
    """
    Best-effort extraction of the transaction amount from OCR'd receipt
    text, tried in decreasing order of confidence. Returns a float, or None
    if nothing usable was found.
    """
    if not text:
        return None

    for raw in _CURRENCY_AMOUNT_RE.findall(text):
        amt = _to_float(raw)
        if amt and 0 < amt < _MAX_PLAUSIBLE_RUPEES:
            return round(amt, 2)

    for raw in _AMOUNT_KEYWORD_RE.findall(_LONG_DIGIT_RUN_RE.sub(" ", text)):
        amt = _to_float(raw)
        if amt and 0 < amt < _MAX_PLAUSIBLE_RUPEES:
            return round(amt, 2)

    candidates = []
    for line in text.splitlines():
        if _NON_AMOUNT_LINE_RE.search(line):
            continue
        line = _LONG_DIGIT_RUN_RE.sub(" ", line)
        for raw in _ANY_AMOUNT_RE.findall(line):
            amt = _to_float(raw)
            if amt and 0 < amt < _MAX_PLAUSIBLE_RUPEES:
                candidates.append(amt)

    if candidates:
        return round(max(candidates), 2)

    return None


def _largest_font_amount(image):
    """
    Payment apps always render the transaction amount as the single biggest
    piece of text on the confirmation screen. This scans OCR bounding-box
    data (not just plain text) and returns the numeric token with the
    tallest bounding box -- a strong, layout-based signal that plain regex
    on flattened text can't use. Used as a secondary check alongside
    extract_amount_from_text() rather than a replacement for it.
    """
    try:
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    except Exception:
        logger.exception("image_to_data failed during largest-font amount detection")
        return None

    best_amount, best_height = None, 0
    for i in range(len(data.get("text", []))):
        token = data["text"][i].strip()
        if not token:
            continue
        cleaned = re.sub(r"[^\d.,]", "", token).strip(",.")
        if not re.fullmatch(r"\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?", cleaned or ""):
            continue
        amt = _to_float(cleaned)
        if not amt or not (0 < amt < _MAX_PLAUSIBLE_RUPEES):
            continue
        try:
            height = int(data["height"][i])
        except (KeyError, ValueError):
            height = 0
        if height > best_height:
            best_height, best_amount = height, amt

    return best_amount


async def _ocr_photo(photo_file):
    """Downloads a Telegram photo into memory, preprocesses it, and OCRs it.

    Returns (raw_text, processed_image) -- the image is returned too so the
    caller can run the font-size-aware fallback pass on the same data
    without re-downloading/re-processing.
    """
    buf = io.BytesIO()
    await photo_file.download_to_memory(out=buf)
    buf.seek(0)
    image = Image.open(buf).convert("L")  # grayscale
    image = ImageOps.autocontrast(image)  # cheap contrast boost helps tesseract a lot on screenshots

    # Phone screenshots are often small/compressed; tesseract's accuracy
    # drops off sharply under ~1200px on the long edge. Upscaling costs a
    # bit of latency but meaningfully improves hit rate on real screenshots.
    longest_edge = max(image.size)
    if longest_edge < 1200:
        scale = 1200 / longest_edge
        image = image.resize(
            (int(image.width * scale), int(image.height * scale)),
            Image.LANCZOS,
        )

    text = pytesseract.image_to_string(image)
    return text, image


def _confirm_keyboard(has_amount: bool):
    rows = []
    if has_amount:
        rows.append([InlineKeyboardButton("✅ Yes, add as expense", callback_data="receipt:confirm")])
        rows.append([InlineKeyboardButton("✏️ Wrong amount", callback_data="receipt:fix_amount")])
    else:
        rows.append([InlineKeyboardButton("✏️ Enter amount manually", callback_data="receipt:fix_amount")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="receipt:cancel")])
    return InlineKeyboardMarkup(rows)


def _amount_line(amount):
    if amount:
        return f"💰 **Amount detected:** ₹{amount:.2f}\n\n"
    return "⚠️ I couldn't confidently read an amount from this image.\n\n"


async def receipt_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Entry point: fires on any photo sent to the bot, whether forwarded into
    a registered group chat or shared straight to the bot's private chat.
    Runs OCR, tries to detect the paid amount, then resolves which group
    the expense belongs to before asking for confirmation.
    """
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id

    context.user_data.clear()
    context.user_data["initiator_id"] = user.id

    status_msg = await context.bot.send_message(chat_id=chat_id, text="🔍 Reading receipt...")

    detected_amount = None
    if OCR_AVAILABLE:
        try:
            photo = update.message.photo[-1]  # highest resolution size
            photo_file = await photo.get_file()
            text, ocr_image = await _ocr_photo(photo_file)
            detected_amount = extract_amount_from_text(text)
            if not detected_amount:
                detected_amount = _largest_font_amount(ocr_image)
            logger.info("Receipt OCR text: %r -> amount=%s", text[:300], detected_amount)
        except Exception as e:
            if "tesseract" in str(e).lower():
                logger.error(
                    "Tesseract OCR engine not found on this server. Install it with "
                    "`sudo apt install tesseract-ocr` (the pip package alone isn't enough). Error: %s", e
                )
            else:
                logger.exception("Receipt OCR failed")
    else:
        logger.warning("pytesseract/Pillow not installed -- receipt OCR is disabled, falling back to manual entry")

    context.user_data["amount"] = detected_amount
    context.user_data["card_msg_id"] = status_msg.message_id

    # -------------------------------------------------------------
    # Private chat: we don't know which group this is for yet.
    # -------------------------------------------------------------
    if chat.type == "private":
        groups = execute(
            """
            SELECT g.group_id, g.group_name
            FROM members m JOIN groups g ON m.group_id = g.group_id
            WHERE m.telegram_user_id = ? AND m.is_active = 1
            ORDER BY g.group_name
            """,
            (user.id,),
            fetch=True,
        )

        if not groups:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text="❌ You're not registered in any group yet. Send /start inside a group chat first.",
            )
            context.user_data.clear()
            return ConversationHandler.END

        # Only one group -> skip the picker entirely, per the "one-tap" idea.
        if len(groups) == 1:
            group_id, group_name = groups[0]
            context.user_data["group_id"] = group_id
            context.user_data["group_name"] = group_name
            context.user_data["group_locked"] = True
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=f"📷 **Receipt detected!**\n\n{_amount_line(detected_amount)}Add this to **{group_name}**?",
                parse_mode="Markdown",
                reply_markup=_confirm_keyboard(has_amount=bool(detected_amount)),
            )
            return RECEIPT_CONFIRM

        keyboard_rows = [
            [InlineKeyboardButton(name, callback_data=f"receipt_group:{gid}")]
            for gid, name in groups
        ]
        keyboard_rows.append([InlineKeyboardButton("❌ Cancel", callback_data="receipt:cancel")])

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=f"📷 **Receipt detected!**\n\n{_amount_line(detected_amount)}Which group is this for?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
        )
        return RECEIPT_GROUP_SELECT

    # -------------------------------------------------------------
    # Group chat: the group is already implied by where the photo landed.
    # -------------------------------------------------------------
    group = execute("SELECT group_id FROM groups WHERE telegram_chat_id=?", (chat_id,), fetch=True)
    if not group:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text="❌ This group isn't set up yet. Send /start here first.",
        )
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["group_id"] = group[0][0]

    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=status_msg.message_id,
        text=f"📷 **Receipt detected!**\n\n{_amount_line(detected_amount)}Add this as an expense?",
        parse_mode="Markdown",
        reply_markup=_confirm_keyboard(has_amount=bool(detected_amount)),
    )
    return RECEIPT_CONFIRM


async def receipt_group_selected_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the group picker shown when a receipt is shared in a private chat."""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id != context.user_data.get("initiator_id"):
        await query.answer("❌ Only the person who shared this receipt can respond.", show_alert=True)
        return RECEIPT_GROUP_SELECT

    await query.answer()

    if query.data == "receipt:cancel":
        return await _receipt_cancel(update, context)

    group_id = int(query.data.split(":")[1])
    group_row = execute("SELECT group_name FROM groups WHERE group_id=?", (group_id,), fetch=True)
    group_name = group_row[0][0] if group_row else "this group"

    context.user_data["group_id"] = group_id
    context.user_data["group_name"] = group_name
    context.user_data["group_locked"] = True

    detected_amount = context.user_data.get("amount")
    await query.edit_message_text(
        text=f"📷 **Receipt detected!**\n\n{_amount_line(detected_amount)}Add this to **{group_name}**?",
        parse_mode="Markdown",
        reply_markup=_confirm_keyboard(has_amount=bool(detected_amount)),
    )
    return RECEIPT_CONFIRM


async def receipt_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Yes / Wrong amount / Cancel on the receipt-detected confirmation card."""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id != context.user_data.get("initiator_id"):
        await query.answer("❌ Only the person who shared this receipt can respond.", show_alert=True)
        return RECEIPT_CONFIRM

    await query.answer()
    data = query.data

    if data == "receipt:cancel":
        return await _receipt_cancel(update, context)

    if data == "receipt:fix_amount":
        await query.edit_message_text(
            text="💰 **Enter the correct expense amount:**",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_expense")]]),
        )
        return AMOUNT

    if data == "receipt:confirm":
        group_id = context.user_data.get("group_id")
        member = execute(
            "SELECT member_id FROM members WHERE group_id=? AND telegram_user_id=?",
            (group_id, user_id),
            fetch=True,
        )
        if not member:
            await query.edit_message_text("❌ You're not registered in that group. Send /start there first.")
            context.user_data.clear()
            return ConversationHandler.END

        context.user_data["payer_member_id"] = member[0][0]

        amount = context.user_data.get("amount")
        await query.edit_message_text(
            text=f"💰 **Amount:** ₹{amount:.2f}\n\n📂 **Select Category:**",
            parse_mode="Markdown",
            reply_markup=category_inline_menu,
        )
        return CATEGORY

    return RECEIPT_CONFIRM


async def _receipt_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id

    await query.edit_message_text("❌ Receipt cancelled.")
    auto_delete(context, chat_id, query.message.message_id, delay=DELETE_AFTER_ACK)

    context.user_data.clear()
    return ConversationHandler.END