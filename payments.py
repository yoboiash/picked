"""
Reads Venmo payment-received emails ("___ paid you $10.00", or the newer
"___ paid $10.00 to your Venmo account") from a Gmail inbox so the app can
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

# Venmo's payment-received subjects (also work with "Fwd: ..." in front):
#   older: "Jake Smith paid you $10.00"
#   newer: "Jake Smith paid $10.00 to your Venmo account. Leave it in Venmo or ..."
# Payments YOU sent ("You paid Jake Smith $10.00") match neither.
SUBJECT_RES = [
    re.compile(r"(?:^|:\s*)(?P<name>[^:]+?)\s+paid you\s+\$(?P<amt>[\d,]+(?:\.\d{1,2})?)", re.I),
    re.compile(r"(?:^|:\s*)(?P<name>[^:]+?)\s+paid\s+\$(?P<amt>[\d,]+(?:\.\d{1,2})?)\s+to your venmo", re.I),
]
# Emails that are clearly NOT money coming in, even if they mention a code and an amount.
NOT_INCOMING_RE = re.compile(r"^\s*(?:(?:fwd?|fw):\s*)*(?:you\b|.*\b(?:request|requests|requested|charged|reminder|declined|cancel))", re.I)
AMOUNT_RE = re.compile(r"\$\s?([\d,]+\.\d{2})")
# "#K7Q2" — the player's code from the note the app pre-fills
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
    return re.sub(r"\s+", " ", " ".join(plain or rich)).strip()


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
    """Turn one raw email into a payment dict, or None if it isn't a payment.

    "sure" is False for an email in wording the app doesn't know yet that still
    carries a player code and an amount: the commissioner confirms those by hand.
    """
    msg = email.message_from_bytes(raw) if isinstance(raw, bytes) else email.message_from_string(raw)
    subject = _decode(msg.get("Subject"))
    body = _body_text(msg)
    code = CODE_RE.search(body) or CODE_RE.search(subject)
    m = next((m for r in SUBJECT_RES if (m := r.search(subject))), None)
    if m and m.group("name").strip().lower() == "you":
        return None
    sure = True
    if not m:
        # Unknown wording: only keep it if it has a player code and an amount,
        # and doesn't look like a payment you made, a request or a receipt.
        amt = AMOUNT_RE.search(subject) or AMOUNT_RE.search(body)
        if not (code and amt) or NOT_INCOMING_RE.search(subject):
            return None
        name = parseaddr(msg.get("From", ""))[0] or "Unknown sender"
        payer, amount, sure = (subject.strip()[:60] or name), float(amt.group(1).replace(",", "")), False
    else:
        payer, amount = m.group("name").strip(), float(m.group("amt").replace(",", ""))
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
        "payer": payer,
        "amount": amount,
        "sure": sure,
        "code": code.group(1).upper() if code else None,
        "note": note,
        "verified": verified_venmo(msg),
        "from": parseaddr(msg.get("From", ""))[1].lower(),
        "received": received,
    }


def fetch_payments(address, password, since, seen, host="imap.gmail.com", port=993):
    """Log in, read every Venmo email since a date that we haven't seen, and parse it.

    since: datetime.date. seen: set of message ids already handled.
    Returns a list of dicts: payments, plus {"msg_id", "ignore": True} for emails
    that aren't payments, so the app can remember them and not read them again.
    Raises on login/network problems.
    """
    conn = imaplib.IMAP4_SSL(host, port, timeout=20)
    try:
        conn.login(address, password)
        conn.select("INBOX", readonly=True)
        # Everything from Venmo, plus anything forwarded by hand ("Fwd: ... paid ...").
        typ, data = conn.search(None, "SINCE", since.strftime("%d-%b-%Y"),
                                "OR", "FROM", '"venmo.com"', "SUBJECT", '"paid"')
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
            elif not p and mid and mid not in seen:
                found.append({"msg_id": mid, "ignore": True})
        return found
    finally:
        try:
            conn.logout()
        except Exception:
            pass
