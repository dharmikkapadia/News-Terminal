"""alerts.py — send watchlist hits to Slack, Telegram, and/or email.

Every channel is optional: it fires only if its environment variables are set,
so you can enable one, two, or all three. Called by the poller on new matches.

Configure via environment variables (or .streamlit/secrets.toml mirrored to env):
  SLACK_WEBHOOK_URL
  TELEGRAM_BOT_TOKEN  +  TELEGRAM_CHAT_ID
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, ALERT_EMAIL_TO, ALERT_EMAIL_FROM
"""

import os
import smtplib
from email.mime.text import MIMEText

import requests


def _format(items):
    lines = []
    for it in items[:25]:
        flag = "🔔" if it.get("watch_hits") else "•"
        lines.append(f"{flag} [{it['source_name']}] {it['title']}\n   {it['link']}")
    body = "MarketWire — new items worth a look:\n\n" + "\n\n".join(lines)
    if len(items) > 25:
        body += f"\n\n…and {len(items) - 25} more."
    return body


def send_slack(text):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return False
    requests.post(url, json={"text": text}, timeout=15).raise_for_status()
    return True


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
        timeout=15,
    ).raise_for_status()
    return True


def send_email(text):
    host = os.environ.get("SMTP_HOST")
    to = os.environ.get("ALERT_EMAIL_TO")
    if not (host and to):
        return False
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    pw = os.environ.get("SMTP_PASS", "")
    sender = os.environ.get("ALERT_EMAIL_FROM", user or "marketwire@localhost")

    msg = MIMEText(text)
    msg["Subject"] = "MarketWire alert"
    msg["From"] = sender
    msg["To"] = to

    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls()
        if user:
            s.login(user, pw)
        s.sendmail(sender, [a.strip() for a in to.split(",")], msg.as_string())
    return True


def dispatch_alerts(items):
    """Send to every configured channel. Returns the list of channels that fired."""
    text = _format(items)
    sent = []
    for name, fn in (("slack", send_slack), ("telegram", send_telegram), ("email", send_email)):
        try:
            if fn(text):
                sent.append(name)
        except Exception as ex:
            print(f"[alerts] {name} failed: {ex}")
    return sent
