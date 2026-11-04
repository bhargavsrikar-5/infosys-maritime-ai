"""One place that sends e-mail (OTP codes, reports, alerts). Gmail SMTP with an app password: set EMAIL_ID + EMAIL_PASSWORD (Colab secrets or env)."""
import os, ssl, smtplib
from email.message import EmailMessage
def creds(): return os.environ.get("EMAIL_ADDRESS") or os.environ.get("EMAIL_ID") or "", (os.environ.get("EMAIL_PASSWORD") or os.environ.get("EMAIL_APP_PASSWORD") or "").replace(" ", "")
def configured(): a, b = creds(); return bool(a and b)
def send(to, subject, body, attachments=(), html=None):
    """attachments: [(filename, bytes, mimetype)]. Returns (ok, message)."""
    em, pw = creds()
    if not (em and pw): return False, "E-mail is not configured (set EMAIL_ADDRESS and EMAIL_PASSWORD)."
    try:
        m = EmailMessage(); m["Subject"] = subject; m["From"] = em; m["To"] = to; m.set_content(body)
        if html: m.add_alternative(html, subtype="html")
        for fn, data, mt in attachments: m.add_attachment(data, maintype=mt.split("/")[0], subtype=mt.split("/")[1], filename=fn)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=20) as s: s.login(em, pw); s.send_message(m)
        return True, f"Sent to {to}"
    except Exception as e: return False, f"Could not send: {e}"
