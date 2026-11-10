"""Watches the Ulm appointment system (Staatsangehoerigkeitsbehoerde, "Anliegen rund um
die Einbuergerung") and sends an email as soon as free appointments appear.

It walks the same steps a person does in the browser - select the concern, select the
location - and reads the result page. It never books anything; you book by hand from
the link in the email.

Usage:
    python watcher.py           # watch forever
    python watcher.py --once    # check once, print the result, exit
    python watcher.py --test     # send a test alert
    python watcher.py --max-minutes 295   # used by the GitHub Actions workflow
"""

import argparse
import configparser
import http.cookiejar
import os
import random
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

BASE = "https://ssc.wilkencloud.de/ulm/"
START_URL = BASE + "select2?md=4"
# Step 2 -> 3: concern 600 = "Anliegen rund um die Einbuergerung", mdt 19 = Staatsangehoerigkeitsbehoerde.
LOCATION_URL = BASE + "location?mdt=19&select_cnc=1&cnc-600=1"
# Step 3 -> 4: location 17 = Buergerdienste, Olgastr. 66.
LOCATION_FORM = {
    "loc": "17",
    "gps_lat": "48.400349",
    "gps_long": "9.985684",
    "select_location": "Weiter",
}
NO_SLOTS_MARKERS = ("Kein freier Termin verfügbar", "Keine Zeiten verfügbar")
SLOTS_PAGE_MARKER = "Schritt 4"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) termin-watcher (personal use)"

CONFIG_PATH = Path(__file__).with_name("config.ini")


def log(message):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def check_availability():
    """Returns ("available" | "none" | "unknown", page_html)."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    opener.addheaders = [("User-Agent", USER_AGENT), ("Accept-Language", "de-DE,de;q=0.9")]

    opener.open(START_URL, timeout=30).read()
    opener.open(LOCATION_URL, timeout=30).read()
    body = urllib.parse.urlencode(LOCATION_FORM).encode()
    with opener.open(LOCATION_URL, data=body, timeout=30) as response:
        html = response.read().decode("utf-8", errors="replace")

    if any(marker in html for marker in NO_SLOTS_MARKERS):
        return "none", html
    if SLOTS_PAGE_MARKER in html:
        return "available", html
    # The site changed or the session failed: don't claim slots we can't see.
    return "unknown", html


def load_config():
    config = configparser.ConfigParser()
    # In the cloud (GitHub Actions) the settings come from environment variables / secrets.
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        config["telegram"] = {
            "bot_token": os.environ["TELEGRAM_BOT_TOKEN"],
            "chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
        }
        config["watch"] = {}
        for key in ("interval_seconds", "reminder_minutes"):
            if os.environ.get(f"WATCH_{key.upper()}"):
                config["watch"][key] = os.environ[f"WATCH_{key.upper()}"]
        return config
    if os.environ.get("SMTP_HOST"):
        config["smtp"] = {
            key: os.environ[f"SMTP_{key.upper()}"]
            for key in ("host", "port", "user", "password", "to")
            if os.environ.get(f"SMTP_{key.upper()}")
        }
        config["watch"] = {}
        for key in ("interval_seconds", "reminder_minutes"):
            if os.environ.get(f"WATCH_{key.upper()}"):
                config["watch"][key] = os.environ[f"WATCH_{key.upper()}"]
        return config
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing {CONFIG_PATH}. Copy config.example.ini to config.ini and fill it in.")
    config.read(CONFIG_PATH, encoding="utf-8")
    return config


def notify(config, subject, text):
    """Sends the alert by Telegram when configured, otherwise by email."""
    if config.has_section("telegram"):
        send_telegram(config, f"{subject}\n\n{text}")
    else:
        send_email(config, subject, text)


def send_telegram(config, text):
    telegram = config["telegram"]
    url = f"https://api.telegram.org/bot{telegram['bot_token']}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": telegram["chat_id"], "text": text}).encode()
    with urllib.request.urlopen(url, data=body, timeout=30) as response:
        response.read()
    log("Telegram message sent.")


def send_email(config, subject, text):
    smtp = config["smtp"]
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp.get("from", smtp["user"])
    message["To"] = smtp["to"]
    message.set_content(text)

    host, port = smtp["host"], smtp.getint("port", 587)
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    with server:
        server.login(smtp["user"], smtp["password"])
        server.send_message(message)
    log(f"Email sent: {subject}")


def beep():
    try:
        import winsound

        for _ in range(3):
            winsound.Beep(1200, 400)
    except Exception:
        pass


def watch(config, max_minutes=None):
    deadline = time.time() + max_minutes * 60 if max_minutes else None
    watch_cfg = config["watch"] if config.has_section("watch") else {}
    # 60 s is the floor: faster gains nothing (booking by hand takes longer) and risks a block.
    interval = max(60, int(watch_cfg.get("interval_seconds", 300)))
    reminder = int(watch_cfg.get("reminder_minutes", 30)) * 60
    # Warn after ~15 minutes of failures, whatever the interval.
    error_limit = max(3, 900 // interval)

    last_state = None
    last_alert = 0.0
    errors = 0
    error_mail_sent = False

    log(f"Watching every ~{interval}s. Stop with Ctrl+C.")
    while True:
        try:
            state, _ = check_availability()
            errors = 0
            error_mail_sent = False
        except Exception as exc:
            state = "error"
            errors += 1
            log(f"Check failed ({errors} in a row): {exc}")

        if state == "available":
            now = time.time()
            if last_state != "available" or now - last_alert >= reminder:
                log("FREE APPOINTMENTS FOUND!")
                beep()
                notify(
                    config,
                    "Einbürgerung Ulm: freie Termine verfügbar!",
                    "The watcher found free appointments for 'Anliegen rund um die Einbürgerung'.\n\n"
                    f"Book now: {START_URL}\n\n"
                    "Steps: tick 'Anliegen rund um die Einbürgerung' -> Weiter -> "
                    "Bürgerdienste -> Weiter -> pick a time -> enter your details.\n",
                )
                last_alert = now
        elif state == "none":
            log("No free appointments.")
        elif state == "unknown":
            log("Unexpected page - the site may have changed.")

        if state in ("error", "unknown"):
            if state == "unknown":
                errors += 1
            if errors >= error_limit and not error_mail_sent:
                notify(
                    config,
                    "Einbürgerung Ulm watcher: checks are failing",
                    f"The last {errors} checks failed or returned an unexpected page.\n"
                    "The site may have changed - the watcher keeps trying but may miss slots.\n"
                    f"Check manually: {START_URL}\n",
                )
                error_mail_sent = True

        if state != "error":
            last_state = state
        # Jitter so the requests don't hit the server on an exact rhythm.
        pause = interval + random.randint(0, max(10, interval // 5))
        if deadline and time.time() + pause > deadline:
            log("Time limit reached.")
            return
        time.sleep(pause)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--test", "--test-email", dest="test", action="store_true",
                        help="send a test alert (Telegram or email) and exit")
    parser.add_argument("--max-minutes", type=int, help="stop watching after this many minutes")
    args = parser.parse_args()

    if args.once:
        state, _ = check_availability()
        log(f"Result: {state}")
        return
    config = load_config()
    if args.test:
        try:
            state, _ = check_availability()
        except Exception as exc:
            state = f"error ({exc})"
        log(f"Site check: {state}")
        notify(config, "Einbürgerung Ulm watcher: test", f"Alerts work. Site check result: {state}\n")
        return
    try:
        watch(config, args.max_minutes)
    except KeyboardInterrupt:
        log("Stopped.")


if __name__ == "__main__":
    main()
