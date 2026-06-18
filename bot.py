import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else None
CRYPTO_PAY_API_BASE = "https://pay.crypt.bot/api"


def require_env() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not CRYPTO_PAY_API_TOKEN:
        missing.append("CRYPTO_PAY_API_TOKEN")
    if missing:
        print("Missing environment variables: " + ", ".join(missing))
        sys.exit(1)


def http_json(url: str, *, headers: dict | None = None, params: dict | None = None) -> dict:
    if params:
        query = urllib.parse.urlencode(params)
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{query}"

    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc}") from exc


def crypto_pay(method: str, **params) -> dict:
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN}
    data = http_json(f"{CRYPTO_PAY_API_BASE}/{method}", headers=headers, params=params or None)
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "Crypto Pay API returned an error"))
    return data


def telegram(method: str, **params) -> dict:
    data = http_json(f"{TELEGRAM_API_BASE}/{method}", params=params or None)
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API returned an error"))
    return data


def escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def extract_hash(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return value

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme and parsed.netloc:
        query = urllib.parse.parse_qs(parsed.query)
        for key in ("start", "startapp"):
            if query.get(key):
                return query[key][0]
        if parsed.fragment:
            return parsed.fragment
        return value.rstrip("/").split("/")[-1]

    if "=" in value:
        tail = value.split("=")[-1].strip()
        if tail:
            return tail

    return value


def fetch_checks(*, status: str | None = None, count: int = 20) -> list[dict]:
    params = {"count": max(1, min(count, 100))}
    if status:
        params["status"] = status
    return crypto_pay("getChecks", **params)["result"]["items"]


def find_check(search_value: str) -> dict | None:
    normalized = extract_hash(search_value)
    if not normalized:
        return None

    if normalized.isdigit():
        items = crypto_pay("getChecks", check_ids=normalized)["result"]["items"]
        return items[0] if items else None

    for status in ("active", "activated", None):
        checks = fetch_checks(status=status, count=100)
        for check in checks:
            if str(check.get("hash", "")).strip() == normalized:
                return check
            if str(check.get("bot_check_url", "")).strip() == search_value.strip():
                return check
    return None


def format_check_line(check: dict) -> str:
    amount = check.get("amount", "?")
    asset = check.get("asset", "?")
    check_id = check.get("check_id", "?")
    status = check.get("status", "?")
    hash_value = check.get("hash", "?")
    return (
        f"ID: <code>{check_id}</code> | {escape_html(str(amount))} {escape_html(str(asset))} | "
        f"{escape_html(str(status))}\n"
        f"hash: <code>{escape_html(str(hash_value))}</code>"
    )


def help_text() -> str:
    return (
        "Commands:\n"
        "/checks - show last 10 checks\n"
        "/active - show active checks\n"
        "/activated - show activated checks\n"
        "/find <check_id | hash | url> - find a specific check\n\n"
        "Tip: if you only have a Crypto Bot check link, send it to /find and the bot will try to match it by hash."
    )


def handle_message(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return help_text()

    if text.startswith("/start") or text.startswith("/help"):
        return help_text()

    if text.startswith("/checks"):
        checks = fetch_checks(count=10)
        if not checks:
            return "No checks found."
        return "\n\n".join(format_check_line(check) for check in checks)

    if text.startswith("/active"):
        checks = fetch_checks(status="active", count=10)
        if not checks:
            return "No active checks found."
        return "\n\n".join(format_check_line(check) for check in checks)

    if text.startswith("/activated"):
        checks = fetch_checks(status="activated", count=10)
        if not checks:
            return "No activated checks found."
        return "\n\n".join(format_check_line(check) for check in checks)

    if text.startswith("/find"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return "Usage: /find <check_id | hash | url>"
        check = find_check(parts[1])
        if not check:
            return "Check not found. Try /checks to see the latest checks."
        return format_check_line(check)

    return help_text()


def send_message(chat_id: int, text: str) -> None:
    telegram(
        "sendMessage",
        chat_id=str(chat_id),
        text=text,
        parse_mode="HTML",
        disable_web_page_preview="true",
    )


def poll() -> None:
    offset = 0
    print("Bot is running. Press Ctrl+C to stop.")
    while True:
        try:
            response = telegram("getUpdates", timeout="30", offset=str(offset))
            for update in response.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue
                chat_id = message["chat"]["id"]
                text = message.get("text", "")
                try:
                    reply = handle_message(text)
                except Exception as exc:
                    reply = f"Error: <code>{escape_html(str(exc))}</code>"
                send_message(chat_id, reply)
        except KeyboardInterrupt:
            print("Bot stopped.")
            return
        except Exception as exc:
            print(f"Polling error: {exc}")
            time.sleep(3)


if __name__ == "__main__":
    require_env()
    poll()
