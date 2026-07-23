"""
WhatsApp Field Worker Data Entry Bot
--------------------------------------
Workers chat with this bot on WhatsApp (typing OR sending voice notes).
The bot asks: Name -> Total Target -> Total Actual, then writes the row
to Google Sheets with Achievement % auto-calculated.

Flow is a simple state machine stored per phone number.
"""

import os
import re
import json
import requests
from datetime import datetime
from flask import Flask, request, jsonify
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ---------------------------------------------------------------------------
# CONFIG  (all values come from environment variables - set these on Render)
# ---------------------------------------------------------------------------
WHATSAPP_TOKEN = os.environ["WHATSAPP_TOKEN"]                # Meta permanent/temp access token
PHONE_NUMBER_ID = os.environ["PHONE_NUMBER_ID"]               # from Meta WhatsApp app
VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]                     # any string you make up
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]               # the long ID in your sheet's URL
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]  # full JSON as a string

# Voice notes are OFF for now (free tier). Set this env var to "true" later
# once you add OpenAI credit, and add back the transcription code below.
VOICE_ENABLED = os.environ.get("VOICE_ENABLED", "false").lower() == "true"

WHATSAPP_API_URL = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
MEDIA_API_URL = "https://graph.facebook.com/v20.0"

# Google Sheets setup
_creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
_creds = Credentials.from_service_account_info(
    _creds_dict,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
gc = gspread.authorize(_creds)
sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1  # first tab; change if needed

# In-memory conversation state: { phone_number: {"step": ..., "data": {...}} }
# NOTE: this resets if the server restarts. For production, swap this dict
# for a small database (SQLite/Firestore) - flagged in the guide.
sessions = {}

STEPS = ["ask_project", "ask_name", "ask_activity", "ask_target", "ask_actual", "done"]


# ---------------------------------------------------------------------------
# WHATSAPP HELPERS
# ---------------------------------------------------------------------------
def send_text(to, body):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    requests.post(WHATSAPP_API_URL, headers=headers, json=payload, timeout=15)


def extract_number(text):
    """Pull the first number out of a worker's reply, e.g. 'target is 250 units' -> 250"""
    match = re.search(r"[\d,]+(\.\d+)?", text.replace(",", ""))
    return float(match.group()) if match else None


# ---------------------------------------------------------------------------
# CONVERSATION LOGIC
# ---------------------------------------------------------------------------
def get_session(phone):
    if phone not in sessions:
        sessions[phone] = {"step": "ask_project", "data": {}}
    return sessions[phone]


def handle_message(phone, text):
    session = get_session(phone)
    step = session["step"]

    if step == "ask_project":
        session["data"]["project"] = text.strip().title()
        session["step"] = "ask_name"
        send_text(phone, f"Got it — {session['data']['project']}. What is your name?")

    elif step == "ask_name":
        session["data"]["name"] = text.strip().title()
        session["step"] = "ask_activity"
        send_text(phone, f"Thanks {session['data']['name']}! What activity did you work on? (e.g. Pipe Installation, Wall conduiting)")

    elif step == "ask_activity":
        session["data"]["activity"] = text.strip().title()
        session["step"] = "ask_target"
        send_text(phone, "Got it. What was your Target for this activity? (just the number)")

    elif step == "ask_target":
        num = extract_number(text)
        if num is None:
            send_text(phone, "I didn't catch a number. Please send Target as a number, e.g. 250")
            return
        session["data"]["target"] = num
        session["step"] = "ask_actual"
        send_text(phone, "Got it. Now what is your Actual (completed) for this activity?")

    elif step == "ask_actual":
        num = extract_number(text)
        if num is None:
            send_text(phone, "I didn't catch a number. Please send Actual as a number, e.g. 180")
            return
        session["data"]["actual"] = num
        save_to_sheet(phone, session["data"])
        achievement = round((session["data"]["actual"] / session["data"]["target"]) * 100, 1) if session["data"]["target"] else 0
        send_text(
            phone,
            f"✅ Saved!\nProject: {session['data']['project']}\nName: {session['data']['name']}\n"
            f"Activity: {session['data']['activity']}\nTarget: {session['data']['target']}\n"
            f"Actual: {session['data']['actual']}\nAchievement: {achievement}%\n\n"
            "Type anything to log another entry."
        )
        sessions[phone] = {"step": "ask_project", "data": {}}  # reset for next entry

    else:
        session["step"] = "ask_project"
        send_text(phone, "Hi! Which project is this for?")


def save_to_sheet(phone, data):
    target = data["target"]
    actual = data["actual"]
    achievement = round((actual / target) * 100, 1) if target else 0
    # Matches sheet headers: Date | Project | Worker Name | Activity | Target | Actual | Achievement%
    sheet.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        data["project"],
        data["name"],
        data["activity"],
        target,
        actual,
        achievement
    ])


# ---------------------------------------------------------------------------
# WEBHOOK ROUTES
# ---------------------------------------------------------------------------
@app.route("/webhook", methods=["GET"])
def verify():
    """Meta calls this once when you connect the webhook, to confirm it's you."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def incoming():
    body = request.get_json()

    try:
        entry = body["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            return jsonify(status="ignored"), 200  # e.g. delivery/read receipts

        message = entry["messages"][0]
        phone = message["from"]
        msg_type = message["type"]

        if msg_type == "text":
            text = message["text"]["body"]
            handle_message(phone, text)

        elif msg_type == "audio":
            # Voice notes are disabled for now (kept free / no OpenAI cost).
            # Turn on later by setting VOICE_ENABLED=true and restoring
            # the transcription code (see IMPLEMENTATION_GUIDE.md, Part 7).
            send_text(phone, "Voice notes aren't supported yet — please type your reply for now 🙂")

        else:
            send_text(phone, "Please send a text message.")

    except (KeyError, IndexError):
        pass  # non-message webhook event (status updates etc.)

    return jsonify(status="ok"), 200


@app.route("/", methods=["GET"])
def health():
    return "Bot is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
