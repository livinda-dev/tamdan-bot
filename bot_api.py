import os
import requests
from fastapi import FastAPI
from pydantic import BaseModel
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime
import re
from bs4 import BeautifulSoup

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SITE_URL = os.getenv("SITE_URL", "https://tamdan.online")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in environment.")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ---------- FastAPI ----------
app = FastAPI()


class MessageRequest(BaseModel):
    phone_number: str
    message: str


def convert_html_to_telegram(html: str) -> str:
    """
    Converts general HTML to Telegram-compatible HTML tags:
    Supported by Telegram: <b>, <i>, <u>, <s>, <a>, <code>, <pre>, <blockquote>
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # Remove unsupported metadata and media tags entirely
    for tag in soup(["img", "style", "script", "head", "meta", "iframe", "svg"]):
        tag.decompose()

    # Convert headings to bold with clean spacing
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        text = h.get_text(strip=True)
        if text:
            h.replace_with(f"<b>{text}</b>\n\n")
        else:
            h.decompose()

    # Convert list items to bullet points
    for li in soup.find_all("li"):
        text = li.get_text(strip=True)
        if text:
            li.replace_with(f"• {text}\n")
        else:
            li.decompose()

    # Convert links to standard Telegram <a href="...">text</a> format
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"].strip()
        if text and href:
            a.replace_with(f'<a href="{href}">{text}</a>')
        elif text:
            a.replace_with(text)
        else:
            a.decompose()

    # Convert paragraphs and breaks
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for p in soup.find_all("p"):
        text = p.get_text()
        p.replace_with(f"{text}\n\n")
    for div in soup.find_all("div"):
        text = div.get_text()
        div.replace_with(f"{text}\n")

    # Stringify preserving valid tags
    result = str(soup)

    # Strip any tags NOT supported by Telegram
    allowed_pattern = r'<(?!/?(?:b|strong|i|em|u|ins|s|strike|del|a|code|pre|blockquote)(?: [^>]*)?>)[^>]+>'
    cleaned = re.sub(allowed_pattern, '', result)

    # Clean up excessive newlines
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned


MAX_LENGTH = 4096


def split_into_chunks(text: str, max_len: int = MAX_LENGTH) -> list:
    """Splits message into Telegram-safe chunks at newline or word boundaries."""
    chunks = []
    while len(text) > max_len:
        split_pos = text.rfind("\n", 0, max_len)
        if split_pos == -1 or split_pos < max_len // 2:
            split_pos = text.rfind(" ", 0, max_len)
        if split_pos == -1:
            split_pos = max_len
        chunks.append(text[:split_pos].strip())
        text = text[split_pos:].lstrip()
    if text:
        chunks.append(text.strip())
    return [c for c in chunks if c]


@app.post("/send-message")
def send_message(req: MessageRequest):
    try:
        user = (
            supabase.table("users")
            .select("chat_id")
            .eq("phone_number", req.phone_number)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        print(f"Database error in send_message: {e}")
        return {"error": f"Database error: {str(e)}"}

    if not user or not user.data:
        return {"error": "Phone number not registered"}

    chat_id = user.data.get("chat_id")
    if not chat_id:
        return {"error": "User has not connected Telegram"}

    telegram_text = convert_html_to_telegram(req.message)
    chunks = split_into_chunks(telegram_text)

    for i, chunk in enumerate(chunks, start=1):
        # 1. Attempt sending with HTML parsing
        resp = requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )

        # 2. Fallback to plain text if HTML entity parsing fails in Telegram
        if resp.status_code != 200:
            print(f"HTML parse failed for chunk {i}: {resp.status_code} - {resp.text}. Retrying with plain text fallback...")
            plain_chunk = re.sub(r'<[^>]+>', '', chunk)
            fallback_resp = requests.post(
                f"{TELEGRAM_API_URL}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": plain_chunk,
                    "disable_web_page_preview": False,
                },
                timeout=15,
            )
            if fallback_resp.status_code != 200:
                print("---- TELEGRAM ERROR ----")
                print("Status:", fallback_resp.status_code)
                print("Response:", fallback_resp.text)
                print("-----------------------")
                return {"error": "Telegram send failed", "chunk": i, "details": fallback_resp.text}

    return {"status": "sent", "chunks": len(chunks)}


# ---------- Telegram Webhook ----------
# Using synchronous def so FastAPI handles each webhook in its worker threadpool
@app.post("/webhook")
def telegram_webhook(update: dict):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"status": "no message"}

    chat_id = message["chat"]["id"]
    raw_text = (message.get("text") or "").strip()
    cmd_lower = raw_text.lower()

    # -------- Handle /start --------
    if cmd_lower.startswith("/start"):
        parts = raw_text.split(" ", 1)

        # Check if user is already linked
        try:
            existing_user = (
                supabase.table("users")
                .select("phone_number")
                .eq("chat_id", str(chat_id))
                .maybe_single()
                .execute()
            )

            if existing_user and existing_user.data:
                # User already linked
                requests.post(
                    f"{TELEGRAM_API_URL}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": (
                            f"✅ You're already connected to TAMDAN!\n\n"
                            f"Linked phone number: {existing_user.data['phone_number']}\n\n"
                            f"You'll receive news updates here automatically.\n\n"
                            f"Daily news will be sent to you at 7:00 AM every day based on your selected topics.\n\n"
                            "Thank you for using TAMDAN."
                        ),
                    },
                    timeout=10,
                )
                return {"status": "already_linked"}
        except Exception as e:
            print(f"Error checking existing user: {e}")

        if len(parts) == 2:
            token_or_phone = parts[1].strip()

            # Try token-based linking first (automatic from website)
            if len(token_or_phone) == 64:  # hex token is 64 chars
                try:
                    # Verify token
                    token_result = (
                        supabase.table("telegram_tokens")
                        .select("*")
                        .eq("token", token_or_phone)
                        .eq("used", False)
                        .maybe_single()
                        .execute()
                    )

                    if not token_result or not token_result.data:
                        requests.post(
                            f"{TELEGRAM_API_URL}/sendMessage",
                            json={
                                "chat_id": chat_id,
                                "text": "❌ Invalid or expired link. Please try again from the website.",
                            },
                            timeout=10,
                        )
                        return {"status": "invalid_token"}

                    token_data = token_result.data

                    # Check expiration
                    expires_at = datetime.fromisoformat(token_data["expires_at"].replace("Z", "+00:00"))
                    if datetime.now(expires_at.tzinfo) > expires_at:
                        requests.post(
                            f"{TELEGRAM_API_URL}/sendMessage",
                            json={
                                "chat_id": chat_id,
                                "text": "❌ Link expired. Please generate a new one from the website.",
                            },
                            timeout=10,
                        )
                        return {"status": "token_expired"}

                    phone_number = token_data["phone_number"]

                    # Mark token as used
                    supabase.table("telegram_tokens").update({"used": True}).eq(
                        "token", token_or_phone
                    ).execute()

                    # Link chat_id to user directly in Supabase
                    try:
                        supabase.table("users").update({"chat_id": str(chat_id)}).eq("phone_number", phone_number).execute()
                        print(f"[save_chat_id] Successfully updated chat_id in Supabase for {phone_number}")
                    except Exception as e:
                        print(f"[save_chat_id] Supabase update error: {e}")

                    requests.post(
                        f"{TELEGRAM_API_URL}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": f"✅ Successfully linked to {phone_number}!\n\nYou'll now receive news updates here.",
                        },
                        timeout=10,
                    )
                    return {"status": "linked_via_token"}

                except Exception as e:
                    print(f"Token verification error: {e}")
                    requests.post(
                        f"{TELEGRAM_API_URL}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": "⚠️ Server error. Please try again.",
                        },
                        timeout=10,
                    )
                    return {"status": "error"}

            # Fallback: manual phone linking
            phone_number = token_or_phone
            try:
                check = (
                    supabase.table("users")
                    .select("*")
                    .eq("phone_number", phone_number)
                    .maybe_single()
                    .execute()
                )
            except Exception as e:
                print(f"Error looking up phone number: {e}")
                requests.post(
                    f"{TELEGRAM_API_URL}/sendMessage",
                    json={"chat_id": chat_id, "text": "⚠️ Server error, try again."},
                    timeout=10,
                )
                return {"status": "db_error"}

            if not check or not getattr(check, "data", None):
                requests.post(
                    f"{TELEGRAM_API_URL}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": (
                            f"❌ The phone number *{phone_number}* is not registered.\n\n"
                            f"Please sign in first:\n{SITE_URL}"
                        ),
                        "parse_mode": "Markdown",
                    },
                    timeout=10,
                )
                return {"status": "phone_not_found"}

            # Save chat_id directly in Supabase
            try:
                supabase.table("users").update({"chat_id": str(chat_id)}).eq("phone_number", phone_number).execute()
                print(f"[save_chat_id] Successfully updated chat_id directly in Supabase for {phone_number}")
            except Exception as e:
                print(f"[save_chat_id] Supabase update error: {e}")

            requests.post(
                f"{TELEGRAM_API_URL}/sendMessage",
                json={"chat_id": chat_id, "text": "✅ Linked successfully!"},
                timeout=10,
            )
            return {"status": "linked"}

        # No parameter provided - show welcome only if not already linked
        requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": (
                    "👋 Welcome to TAMDAN!\n\n"
                    f"To link your account:\n"
                    f"1. Log in at {SITE_URL}\n"
                    f"2. Click 'Connect Telegram' on the website\n\n"
                    "Or manually send: `/start your_phone_number`"
                ),
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        return {"status": "start_no_param"}

    # -------- Default echo --------
    requests.post(
        f"{TELEGRAM_API_URL}/sendMessage",
        json={"chat_id": chat_id, "text": f"You said: {raw_text}"},
        timeout=10,
    )

    return {"status": "ok"}


@app.get("/")
def root():
    return {"message": "Telegram bot is running"}
