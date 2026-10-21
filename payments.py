"""
Reads Venmo "___ paid you $10.00" emails from a Gmail inbox so the app can
unlock players who paid their entry fee. Only standard-library modules.

Setup (done once, by the commissioner):
  * A Gmail inbox that receives Venmo's payment emails (directly, or via a
    Gmail filter that forwards them).
  * MAIL_ADDRESS and MAIL_APP_PASSWORD set on the server. The app password
    comes from myaccount.google.com/apppasswords and can be revoked anytime.

The app only ever reads this inbox (read-only, nothing is deleted or marked read).
"""
import email
import hashlib
import html
import imaplib
import re
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

# "Jake Smith paid you $10.00"  (also works with "Fwd: ..." in front)
SUBJECT_RE = re.compile(r"(?:^|:\s*)(?P<name>[^:]+?)\s+paid you\s+\$(?P<amt>[\d,]+(?:\.\d{1,2})?)", re.I)
# "#K7Q2" — the player's code from the note the app pre-fills
NOTE_CODE_RE = re.compile(r"Pick'?ems\s*#\s*([A-Z0-9]{4})\b", re.I)
CODE_RE = re.compile(r"#\s?([A-Z0-9]{4})\b", re.I)


def _decode(value):
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _body_text(msg):
    """Plain text of the email (falls back to the HTML part with tags removed)."""
    plain, rich = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ctype == "text/plain":
            plain.append(text)
        elif ctype == "text/html":
            text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
            rich.append(html.unescape(re.sub(r"<[^>]+>", " ", text)))
    # Search both versions: Venmo's plain-text part can leave out the payment note.
    text = " ".join(plain + rich)
    text = text.replace(" ", " ").replace("​", "").replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip()


def verified_venmo(msg):
    """True if Google's own check says Venmo really sent this email.

    Only the top Authentication-Results header is trusted: Gmail adds it when
    the email arrives, and anything below it could have been typed by a sender.
    """
    results = msg.get_all("Authentication-Results") or []
    if not results:
        return False
    top = re.sub(r"\s+", " ", results[0]).lower()
    if not top.startswith("mx.google.com"):
        return False
    return bool(re.search(r"dkim=pass[^;]*header\.(?:i|d)=@?(?:[\w-]+\.)*venmo\.com(?=[\s;]|$)", top))


def parse_message(raw):
    """Turn one raw email into a payment dict, or None if it isn't a payment."""
    msg = email.message_from_bytes(raw) if isinstance(raw, bytes) else email.message_from_string(raw)
    subject = _decode(msg.get("Subject"))
    m = SUBJECT_RE.search(subject)
    if not m:
        return None
    body = _body_text(msg)
    code = NOTE_CODE_RE.search(body) or CODE_RE.search(body) or CODE_RE.search(subject)
    try:
        received = parsedate_to_datetime(msg.get("Date")).isoformat()
    except Exception:
        received = ""
    msg_id = (msg.get("Message-ID") or "").strip() or hashlib.sha1(
        raw if isinstance(raw, bytes) else raw.encode()).hexdigest()
    note = ""
    nm = re.search(r"(Vignery Pick'?ems[^.\n]{0,40})", body, re.I)
    if nm:
        note = nm.group(1).strip()
    return {
        "msg_id": msg_id,
        "payer": m.group("name").strip(),
        "amount": float(m.group("amt").replace(",", "")),
        "code": code.group(1).upper() if code else None,
        "note": note,
        "verified": verified_venmo(msg),
        "from": parseaddr(msg.get("From", ""))[1].lower(),
        "received": received,
    }


def fetch_payments(address, password, since, seen, host="imap.gmail.com", port=993):
    """Log in, find "paid you" emails since a date, and parse the ones we haven't seen.

    since: datetime.date. seen: set of message ids already handled.
    Returns a list of payment dicts. Raises on login/network problems.
    """
    conn = imaplib.IMAP4_SSL(host, port, timeout=20)
    try:
        conn.login(address, password)
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, "SINCE", since.strftime("%d-%b-%Y"), "SUBJECT", '"paid you"')
        if typ != "OK":
            return []
        found = []
        for num in data[0].split():
            typ, head = conn.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            mid = ""
            if typ == "OK" and head and isinstance(head[0], tuple):
                mid = email.message_from_bytes(head[0][1]).get("Message-ID", "").strip()
            if mid and mid in seen:
                continue
            typ, full = conn.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not full or not isinstance(full[0], tuple):
                continue
            p = parse_message(full[0][1])
            if p and p["msg_id"] not in seen:
                found.append(p)
        return found
    finally:
        try:
            conn.logout()
        except Exception:
            pass
