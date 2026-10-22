import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from threading import RLock

import jwt
import streamlit as st

# Configuration is supplied by the notebook through Colab Secrets / environment variables.
JWT_SECRET = os.environ.get("JWT_SECRET", "")
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
USER_DB_PATH = Path(os.environ.get("USER_DB_PATH", "/content/infosys_milestone1_users.json"))
JWT_ALGORITHM = "HS256"
JWT_TTL_MINUTES = 60
PASSWORD_HASH_ITERATIONS = 260_000
OTP_TTL_SECONDS = 300
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 60

# Per-process lock protects simultaneous Streamlit sessions within this server process.
DATA_LOCK = RLock()
SECURITY_QUESTIONS = [
    "What was the name of your first school?",
    "What is the name of your childhood best friend?",
    "What was the name of your first pet?",
    "In which city were you born?",
]
EMAIL_RE = re.compile(
    r"^(?=(?:[^@]*[A-Za-z]){2})[A-Za-z0-9._%+-]+@(?=(?:[^.]*[A-Za-z]){2})[A-Za-z0-9-]+(?:\.[A-Za-z]{2,})+$"
)

st.set_page_config(page_title="Infosys Milestone 1 · Authentication", page_icon="🔐", layout="centered")

if len(JWT_SECRET) < 32:
    st.error("Configuration missing or incomplete: add a JWT_SECRET of at least 32 characters in Colab Secrets and enable notebook access.")
    st.stop()
if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
    st.error("Email configuration is missing. Add EMAIL_ADDRESS and EMAIL_PASSWORD in Colab Secrets and enable notebook access.")
    st.stop()
if not ADMIN_USERNAME or not ADMIN_PASSWORD:
    st.error("Admin configuration is missing. Add ADMIN_USERNAME and ADMIN_PASSWORD in Colab Secrets and enable notebook access.")
    st.stop()


def read_users_unlocked():
    if not USER_DB_PATH.exists():
        return {}
    try:
        with USER_DB_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_users_unlocked(users):
    USER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = USER_DB_PATH.with_suffix(USER_DB_PATH.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(users, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, USER_DB_PATH)


def get_users():
    with DATA_LOCK:
        return read_users_unlocked()


def normalize_username(username):
    return username.strip().casefold()


def normalize_email(email):
    return email.strip().casefold()


def hash_value(value):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_value(value, stored_hash):
    try:
        algorithm, iteration_text, salt_hex, expected_hex = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iteration_text)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
        actual = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def password_errors(password):
    errors = []
    if len(password) < 8:
        errors.append("Use at least 8 characters.")
    if not any(char.isupper() for char in password):
        errors.append("Include at least one uppercase letter.")
    if not any(char.islower() for char in password):
        errors.append("Include at least one lowercase letter.")
    if not any(char.isdigit() for char in password):
        errors.append("Include at least one number.")
    if not any(not char.isalnum() and not char.isspace() for char in password):
        errors.append("Include at least one special symbol (not a space).")
    return errors


def find_user_record(identifier, users=None):
    users = users if users is not None else get_users()
    candidate = identifier.strip()
    if not candidate:
        return None, None
    key = normalize_username(candidate)
    if key in users:
        return key, users[key]
    candidate_email = normalize_email(candidate)
    for stored_key, record in users.items():
        if normalize_email(record.get("email", "")) == candidate_email:
            return stored_key, record
    return None, None


def issue_jwt(username):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + timedelta(minutes=JWT_TTL_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def authenticated_username():
    token = st.session_state.get("jwt_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        username = payload.get("sub")
        users = get_users()
        if not username or normalize_username(username) not in users:
            clear_user_session()
            return None
        return users[normalize_username(username)]["username"]
    except jwt.PyJWTError:
        clear_user_session()
        return None


def clear_user_session():
    st.session_state.pop("jwt_token", None)
    st.session_state.pop("jwt_username", None)


def set_page(name):
    st.session_state["page"] = name


def send_otp_email(recipient, otp):
    message = EmailMessage()
    message["Subject"] = "Your password reset verification code"
    message["From"] = EMAIL_ADDRESS
    message["To"] = recipient
    message.set_content(
        f"Your password reset code is: {otp}\n\n"
        f"It expires in 5 minutes and can be used only once. If you did not request this, ignore this email."
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
        smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        smtp.send_message(message)


def clear_otp_state():
    for key in (
        "otp_email", "otp_hash", "otp_expires_at", "otp_attempts",
        "otp_last_sent_at", "otp_verified_email", "otp_verified_until",
    ):
        st.session_state.pop(key, None)


def reset_password_for_key(user_key, new_password):
    with DATA_LOCK:
        users = read_users_unlocked()
        if user_key not in users:
            return False
        users[user_key]["password_hash"] = hash_value(new_password)
        write_users_unlocked(users)
    return True


# Initialize navigation state before displaying the sidebar.
st.session_state.setdefault("page", "Login")
username_logged_in = authenticated_username()
if username_logged_in:
    st.session_state["jwt_username"] = username_logged_in
    allowed_pages = ["Dashboard", "Logout"]
    if st.session_state.get("page") not in allowed_pages:
        st.session_state["page"] = "Dashboard"
else:
    # Do not allow navigation directly to a protected dashboard without a valid JWT.
    if st.session_state.get("page") == "Dashboard":
        st.session_state["page"] = "Login"

with st.sidebar:
    st.title("🔐 Milestone 1")
    st.caption("User Authentication Module")
    if username_logged_in:
        st.write(f"Signed in as **{username_logged_in}**")
        if st.button("Dashboard", use_container_width=True, key="nav_dashboard"):
            set_page("Dashboard")
            st.rerun()
        if st.button("Logout", use_container_width=True, key="nav_logout"):
            clear_user_session()
            st.session_state.pop("admin_authenticated", None)
            st.session_state["page"] = "Login"
            st.rerun()
    else:
        for label in ("Login", "Sign Up", "Forgot Password", "Admin Login"):
            if st.button(label, use_container_width=True, key="nav_" + label.lower().replace(" ", "_")):
                set_page(label)
                st.rerun()
    st.divider()
    st.caption("Demo project. Use test accounts only; do not enter sensitive personal information.")

# Show one-time flash messages after navigation.
flash = st.session_state.pop("flash_message", None)
if flash:
    st.success(flash)

page = st.session_state.get("page", "Login")

if page == "Login":
    st.title("Login")
    st.write("Sign in using your username or registered email address.")
    with st.form("login_form"):
        identifier = st.text_input("Username or Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login", use_container_width=True)
    if submitted:
        if not identifier.strip() or not password:
            st.warning("Please complete all required fields.")
        else:
            users = get_users()
            user_key, record = find_user_record(identifier, users)
            # Keep the public error generic regardless of which credential failed.
            if record and verify_value(password, record.get("password_hash", "")):
                st.session_state["jwt_token"] = issue_jwt(record["username"])
                st.session_state["jwt_username"] = record["username"]
                st.session_state["page"] = "Dashboard"
                st.rerun()
            else:
                st.error("Invalid username/email or password.")

elif page == "Sign Up":
    st.title("Create an account")
    st.write("All fields are required. Passwords are stored as salted hashes.")
    with st.form("signup_form"):
        username = st.text_input("Username")
        email = st.text_input("Email address")
        password = st.text_input("Password", type="password")
        confirm_password = st.text_input("Confirm password", type="password")
        security_question = st.selectbox("Security question", SECURITY_QUESTIONS)
        security_answer = st.text_input("Security answer", type="password")
        submitted = st.form_submit_button("Create account", use_container_width=True)
    if submitted:
        if not all([username.strip(), email.strip(), password, confirm_password, security_question, security_answer.strip()]):
            st.warning("Please complete all required fields.")
        elif len(username.strip()) < 2:
            st.error("Username must contain at least 2 characters.")
        elif not EMAIL_RE.fullmatch(email.strip()):
            st.error("Enter an email with at least 2 letters before @, at least 2 letters in the domain, and at least 2 letters after the final dot (for example, ab@cd.ef).")
        elif password != confirm_password:
            st.error("Password and Confirm Password must match exactly.")
        elif password_errors(password):
            st.error("Password does not meet the required rules:\n\n- " + "\n- ".join(password_errors(password)))
        else:
            user_key = normalize_username(username)
            clean_email = email.strip()
            with DATA_LOCK:
                users = read_users_unlocked()
                username_exists = user_key in users
                email_exists = any(normalize_email(item.get("email", "")) == normalize_email(clean_email) for item in users.values())
                if username_exists:
                    error_message = "That username is already registered. Choose another username."
                elif email_exists:
                    error_message = "That email is already registered. Try logging in or use Forgot Password."
                else:
                    users[user_key] = {
                        "username": username.strip(),
                        "email": clean_email,
                        "password_hash": hash_value(password),
                        "security_question": security_question,
                        "security_answer_hash": hash_value(security_answer.strip().casefold()),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    write_users_unlocked(users)
                    error_message = None
            if error_message:
                st.error(error_message)
            else:
                st.session_state["flash_message"] = "Account created successfully. Please log in."
                st.session_state["page"] = "Login"
                st.rerun()

elif page == "Forgot Password":
    st.title("Forgot password")
    st.write("Choose either the security-question route or email OTP route.")
    security_tab, otp_tab = st.tabs(["Security question", "Email OTP"])

    with security_tab:
        st.subheader("Reset with your security question")
        with st.form("lookup_security_form"):
            lookup_username = st.text_input("Username", key="security_lookup_username")
            lookup_submitted = st.form_submit_button("Find security question", use_container_width=True)
        if lookup_submitted:
            if not lookup_username.strip():
                st.warning("Enter your username.")
            else:
                found_key, found_record = find_user_record(lookup_username)
                if found_record:
                    st.session_state["security_recovery_key"] = found_key
                    st.session_state["security_recovery_question"] = found_record["security_question"]
                    st.rerun()
                else:
                    st.error("We could not verify that account. Check the username and try again.")

        recovery_key = st.session_state.get("security_recovery_key")
        if recovery_key:
            users_now = get_users()
            record = users_now.get(recovery_key)
            if record:
                st.info("Security question: " + record["security_question"])
                with st.form("security_reset_form"):
                    answer = st.text_input("Security answer", type="password")
                    new_password = st.text_input("New password", type="password")
                    confirm_new_password = st.text_input("Confirm new password", type="password")
                    reset_submitted = st.form_submit_button("Reset password", use_container_width=True)
                if reset_submitted:
                    if not answer.strip() or not new_password or not confirm_new_password:
                        st.warning("Please complete all required fields.")
                    elif new_password != confirm_new_password:
                        st.error("New password and confirmation must match exactly.")
                    elif password_errors(new_password):
                        st.error("Password does not meet the required rules:\n\n- " + "\n- ".join(password_errors(new_password)))
                    elif not verify_value(answer.strip().casefold(), record.get("security_answer_hash", "")):
                        st.error("The answer could not be verified. Try again.")
                    elif reset_password_for_key(recovery_key, new_password):
                        st.session_state.pop("security_recovery_key", None)
                        st.session_state.pop("security_recovery_question", None)
                        clear_otp_state()
                        st.session_state["flash_message"] = "Password reset successfully. Please log in with your new password."
                        st.session_state["page"] = "Login"
                        st.rerun()
                    else:
                        st.error("Could not reset the password. Please try again.")

    with otp_tab:
        st.subheader("Reset with an email OTP")
        st.caption("The code expires after 5 minutes and is limited to 5 verification attempts. Resending is limited to one request per minute in this session.")
        with st.form("send_otp_form"):
            otp_email_input = st.text_input("Registered email address", key="otp_email_input")
            send_submitted = st.form_submit_button("Send OTP", use_container_width=True)
        if send_submitted:
            candidate = otp_email_input.strip()
            now = time.time()
            if not candidate:
                st.warning("Enter your registered email address.")
            elif not EMAIL_RE.fullmatch(candidate):
                st.error("Enter a valid email address.")
            elif now - st.session_state.get("otp_last_sent_at", 0) < OTP_RESEND_SECONDS:
                st.warning("Please wait before requesting another OTP.")
            else:
                users = get_users()
                email_key, email_record = find_user_record(candidate, users)
                if not email_record or normalize_email(email_record.get("email", "")) != normalize_email(candidate):
                    # Generic response avoids confirming whether an email is registered.
                    st.info("If an account exists for this email, an OTP will be sent.")
                    st.session_state["otp_last_sent_at"] = now
                else:
                    otp = f"{secrets.randbelow(1_000_000):06d}"
                    try:
                        send_otp_email(email_record["email"], otp)
                        st.session_state["otp_email"] = normalize_email(email_record["email"])
                        st.session_state["otp_hash"] = hashlib.sha256(otp.encode("ascii")).hexdigest()
                        st.session_state["otp_expires_at"] = now + OTP_TTL_SECONDS
                        st.session_state["otp_attempts"] = 0
                        st.session_state.pop("otp_verified_email", None)
                        st.session_state.pop("otp_verified_until", None)
                        st.session_state["otp_last_sent_at"] = now
                        st.success("If an account exists for this email, an OTP has been sent. Check your inbox and spam folder.")
                    except (smtplib.SMTPException, OSError) as exc:
                        # Do not display SMTP credentials or sensitive exception details.
                        st.error("We could not send the email. Check the Gmail App Password and try again.")

        otp_email = st.session_state.get("otp_email")
        otp_hash = st.session_state.get("otp_hash")
        otp_expires_at = st.session_state.get("otp_expires_at", 0)
        if otp_email and otp_hash and time.time() < otp_expires_at:
            with st.form("verify_otp_form"):
                otp_entered = st.text_input("6-digit OTP", max_chars=6)
                verify_submitted = st.form_submit_button("Verify OTP", use_container_width=True)
            if verify_submitted:
                if st.session_state.get("otp_attempts", 0) >= OTP_MAX_ATTEMPTS:
                    clear_otp_state()
                    st.error("Too many attempts. Request a new OTP.")
                elif not otp_entered or not otp_entered.isdigit() or len(otp_entered) != 6:
                    st.warning("Enter the 6-digit code from your email.")
                elif hmac.compare_digest(hashlib.sha256(otp_entered.encode("ascii")).hexdigest(), otp_hash):
                    st.session_state["otp_verified_email"] = otp_email
                    st.session_state["otp_verified_until"] = time.time() + OTP_TTL_SECONDS
                    st.session_state.pop("otp_hash", None)  # one-time: code cannot be verified again
                    st.session_state["otp_expires_at"] = 0
                    st.success("Email verified. Set your new password below.")
                    st.rerun()
                else:
                    st.session_state["otp_attempts"] = st.session_state.get("otp_attempts", 0) + 1
                    if st.session_state["otp_attempts"] >= OTP_MAX_ATTEMPTS:
                        clear_otp_state()
                        st.error("Too many incorrect attempts. Request a new OTP.")
                    else:
                        st.error(f"Incorrect OTP. Attempts remaining: {OTP_MAX_ATTEMPTS - st.session_state['otp_attempts']}.")
        elif otp_email and otp_hash and time.time() >= otp_expires_at:
            st.session_state.pop("otp_hash", None)
            st.error("Your OTP has expired. Request a new one.")

        verified_email = st.session_state.get("otp_verified_email")
        verified_until = st.session_state.get("otp_verified_until", 0)
        if verified_email and time.time() < verified_until:
            with st.form("otp_password_reset_form"):
                new_password_otp = st.text_input("New password", type="password")
                confirm_password_otp = st.text_input("Confirm new password", type="password")
                reset_otp_submitted = st.form_submit_button("Set new password", use_container_width=True)
            if reset_otp_submitted:
                if not new_password_otp or not confirm_password_otp:
                    st.warning("Please complete all required fields.")
                elif new_password_otp != confirm_password_otp:
                    st.error("New password and confirmation must match exactly.")
                elif password_errors(new_password_otp):
                    st.error("Password does not meet the required rules:\n\n- " + "\n- ".join(password_errors(new_password_otp)))
                else:
                    users = get_users()
                    email_key = None
                    for key, record in users.items():
                        if normalize_email(record.get("email", "")) == verified_email:
                            email_key = key
                            break
                    if email_key and time.time() < st.session_state.get("otp_verified_until", 0) and reset_password_for_key(email_key, new_password_otp):
                        clear_otp_state()
                        st.session_state["flash_message"] = "Password reset successfully. Please log in with your new password."
                        st.session_state["page"] = "Login"
                        st.rerun()
                    else:
                        clear_otp_state()
                        st.error("Verification expired or account unavailable. Start the recovery process again.")
        elif verified_email:
            clear_otp_state()
            st.warning("Email verification expired. Request a new OTP.")

elif page == "Dashboard":
    if not username_logged_in:
        st.session_state["page"] = "Login"
        st.warning("Please log in to access the dashboard.")
        st.stop()
    users = get_users()
    key = normalize_username(username_logged_in)
    record = users.get(key)
    st.title("User Dashboard")
    st.success(f"Welcome, {username_logged_in}!")
    if record:
        st.write(f"**Registered email:** {record.get('email', '')}")
    st.info("You are viewing the protected dashboard. Your JWT is checked before this page is shown.")
    if st.button("Logout", type="primary"):
        clear_user_session()
        st.session_state["page"] = "Login"
        st.rerun()

elif page == "Admin Login":
    st.title("Admin Login")
    st.write("This is a separate admin account; regular signups cannot access this page.")
    with st.form("admin_login_form"):
        admin_username = st.text_input("Admin username")
        admin_password = st.text_input("Admin password", type="password")
        admin_submitted = st.form_submit_button("Admin sign in", use_container_width=True)
    if admin_submitted:
        user_match = hmac.compare_digest(admin_username, ADMIN_USERNAME)
        password_match = hmac.compare_digest(admin_password, ADMIN_PASSWORD)
        if user_match and password_match:
            st.session_state["admin_authenticated"] = True
            st.session_state["page"] = "Admin Dashboard"
            st.rerun()
        else:
            st.error("Invalid admin username or password.")

elif page == "Admin Dashboard":
    if not st.session_state.get("admin_authenticated", False):
        st.session_state["page"] = "Admin Login"
        st.warning("Admin login is required.")
        st.stop()
    st.title("Admin Dashboard")
    st.write("Registered users (passwords and password hashes are never displayed).")
    users = get_users()
    records = [{"Username": item.get("username", ""), "Email": item.get("email", "")} for item in users.values()]
    if records:
        st.dataframe(records, use_container_width=True, hide_index=True)
    else:
        st.info("No registered users yet.")
    if st.button("Admin logout", type="primary"):
        st.session_state.pop("admin_authenticated", None)
        st.session_state["page"] = "Admin Login"
        st.rerun()

else:
    st.session_state["page"] = "Login"
    st.rerun()
