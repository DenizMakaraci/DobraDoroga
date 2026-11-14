"""Access control. Members have email + password accounts; the admin invites them (invite link) and can issue
password-reset links. ROADWATCH_ACCESS_KEY is the administrator's bootstrap key so the admin can never be locked
out. When no key is configured the app is single-user and local: everyone is 'Local user' (admin).

Roles, lowest to highest: viewer (read, discuss) < investigator (+ edit the case, pull data, run investigations)
< counsel (same powers, labelled as a lawyer) < admin (+ manage members)."""
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict

from fastapi import HTTPException, Request

from . import store

COOKIE = "roadwatch_session"
SESSION_DAYS = 14
ROLES = ("viewer", "investigator", "counsel", "admin")
ADMIN = {"id": "admin", "name": "Administrator", "role": "admin"}
LOCAL = {"id": "local", "name": "Local user", "role": "admin"}
_attempts = defaultdict(list)  # ip -> timestamps of failed logins


def access_key():
    return os.getenv("ROADWATCH_ACCESS_KEY", "").strip()


def enabled():
    return bool(access_key())


def _secret():
    # Derive the signing secret from the access key so rotating the key invalidates every session.
    return hashlib.sha256(("roadwatch-session:" + access_key()).encode()).digest()


INVITE_HOURS = 72
MIN_PASSWORD = 10
_signups = defaultdict(list)  # ip -> timestamps of account creations


def signup_open():
    return os.getenv("ROADWATCH_SIGNUP", "invite").strip().lower() == "open"


def signup_role():
    role = os.getenv("ROADWATCH_SIGNUP_ROLE", "viewer").strip().lower()
    return role if role in ROLES and role != "admin" else "viewer"


def hash_key(key):
    return hashlib.sha256(("roadwatch-member:" + key.strip()).encode()).hexdigest()


def new_member_key():
    return "rw-" + secrets.token_urlsafe(18)


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        _, salt, digest = stored.split("$")
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
    except (AttributeError, ValueError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


def check_password_strength(password):
    if len(password) < MIN_PASSWORD:
        raise HTTPException(422, f"Use at least {MIN_PASSWORD} characters.")
    if password.lower() in {"password12", "roadwatch1", "1234567890", "qwertyuiop"} or len(set(password)) < 4:
        raise HTTPException(422, "That password is too easy to guess.")


def new_invite():
    """(code to give the person, hash to store, expiry)."""
    code = secrets.token_urlsafe(24)
    return code, hash_key("invite:" + code), str(int(time.time()) + INVITE_HOURS * 3600)


def public(user):
    return {"id": user["id"], "name": user["name"], "role": user["role"], "email": user.get("email")}


def verified_or_pending(member):
    return bool(member.get("verified", 1))


def issue_token(user):
    expires = str(int(time.time()) + SESSION_DAYS * 86400)
    salt = "" if user["id"] in ("admin", "local") else (store.member(user["id"]) or {}).get("token_salt") or ""
    payload = f"{expires}.{user['id']}"
    signature = hmac.new(_secret(), (payload + "." + salt).encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def token_user(token):
    """The signed-in user for a session token, or None. Deactivated members lose access immediately; a password
    change or reset rotates the member's token salt and invalidates every earlier session."""
    if not token or token.count(".") != 2:
        return None
    expires, user_id, signature = token.split(".")
    found = None if user_id == "admin" else store.member(user_id)
    salt = "" if user_id == "admin" else (found or {}).get("token_salt") or ""
    expected = hmac.new(_secret(), f"{expires}.{user_id}.{salt}".encode(), hashlib.sha256).hexdigest()
    if not (hmac.compare_digest(signature, expected) and expires.isdigit() and int(expires) > time.time()):
        return None
    if user_id == "admin":
        return dict(ADMIN)
    return public(found) if found and found["active"] and found.get("verified", 1) else None


def token_valid(token):
    return token_user(token) is not None


def current_user(request: Request):
    if not enabled():
        return dict(LOCAL)
    return token_user(request.cookies.get(COOKIE))


def rank(role):
    return ROLES.index(role) if role in ROLES else -1


def require(request: Request, role):
    """Raise 403 unless the signed-in user has at least `role`."""
    user = getattr(request.state, "user", None) or current_user(request)
    if not user:
        raise HTTPException(401, "Sign in required.")
    if rank(user["role"]) < rank(role):
        raise HTTPException(403, f"This action needs the {role} role; you are signed in as {user['role']}.")
    return user


def _throttle(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    _attempts[ip] = [t for t in _attempts[ip] if now - t < 900]
    if len(_attempts[ip]) >= 8:
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    return ip


def _signed_in(member):
    store.update_member(member["id"], last_login=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return public(member)


def check_login(request: Request, submitted: str):
    """Return the user for a submitted key (admin bootstrap key or a legacy member key)."""
    ip = _throttle(request)
    submitted = submitted.strip()
    if secrets.compare_digest(submitted.encode(), access_key().encode()):
        _attempts.pop(ip, None)
        return dict(ADMIN)
    found = store.member_by_key_hash(hash_key(submitted))
    if found:
        _attempts.pop(ip, None)
        return _signed_in(found)
    _attempts[ip].append(time.time())
    raise HTTPException(401, "Incorrect access key.")


def check_password_login(request: Request, email: str, password: str):
    ip = _throttle(request)
    found = store.member_by_email(email.strip())
    if found and found["active"] and found.get("password_hash") and verify_password(password, found["password_hash"]):
        _attempts.pop(ip, None)
        if not found.get("verified", 1):
            raise HTTPException(403, "Please confirm your email address first - open the link we sent you.")
        return _signed_in(found)
    _attempts[ip].append(time.time())
    if found and found["active"] and not found.get("password_hash"):
        raise HTTPException(401, "This account has no password yet. Open your invite link to set one, or ask the admin for a new link.")
    raise HTTPException(401, "Incorrect email or password.")


def accept_invite(request: Request, code: str, password: str):
    """Set the password for an invited (or reset) account and sign in."""
    ip = _throttle(request)
    found = store.member_by_invite_hash(hash_key("invite:" + code.strip()))
    if not found or not found.get("invite_expires") or int(found["invite_expires"]) < time.time():
        _attempts[ip].append(time.time())
        raise HTTPException(410, "This invite link is invalid or has expired. Ask the admin for a new one.")
    check_password_strength(password)
    _attempts.pop(ip, None)
    store.update_member(found["id"], password_hash=hash_password(password), invite_hash=None, invite_expires=None,
                        token_salt=secrets.token_hex(8))
    return _signed_in(store.member(found["id"]))


def register(request: Request, name: str, email: str, password: str, needs_verification: bool):
    """Open self-registration: a new account with the default sign-up role. Returns (member, verification code or None)."""
    if not signup_open():
        raise HTTPException(403, "This workspace is invite-only. Ask an administrator for an invite.")
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    _signups[ip] = [t for t in _signups[ip] if now - t < 3600]
    if len(_signups[ip]) >= 5:
        raise HTTPException(429, "Too many accounts created from this address. Try again later.")
    if store.member_by_email(email.strip()):
        raise HTTPException(409, "An account with that email already exists. Sign in, or use 'Forgot password'.")
    check_password_strength(password)
    code, digest, expires = (new_invite() if needs_verification else (None, None, None))
    member = store.add_member(name.strip(), signup_role(), hash_key(new_member_key()), email=email.strip().lower(),
                              invite_hash=digest, invite_expires=expires, password_hash=hash_password(password),
                              verified=0 if needs_verification else 1)
    _signups[ip].append(now)
    return member, code


def verify_email(code: str):
    found = store.member_by_invite_hash(hash_key("invite:" + code.strip()))
    if not found or not found.get("invite_expires") or int(found["invite_expires"]) < time.time():
        raise HTTPException(410, "This confirmation link is invalid or has expired. Sign up again or ask the admin.")
    store.update_member(found["id"], verified=1, invite_hash=None, invite_expires=None)
    return _signed_in(store.member(found["id"]))


def change_password(user_id, current, new):
    found = store.member(user_id)
    if not found:
        raise HTTPException(404, "Account not found.")
    if found.get("password_hash") and not verify_password(current, found["password_hash"]):
        raise HTTPException(401, "Current password is incorrect.")
    check_password_strength(new)
    store.update_member(user_id, password_hash=hash_password(new), token_salt=secrets.token_hex(8))


def smtp_configured():
    return bool(os.getenv("ROADWATCH_SMTP_HOST") and os.getenv("ROADWATCH_MAIL_FROM"))


def send_mail(to, subject, body):
    """Plain-text email through ROADWATCH_SMTP_HOST/PORT/USER/PASSWORD (STARTTLS). Raises on failure."""
    import smtplib
    from email.message import EmailMessage
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = os.getenv("ROADWATCH_MAIL_FROM"), to, subject
    message.set_content(body)
    with smtplib.SMTP(os.getenv("ROADWATCH_SMTP_HOST"), int(os.getenv("ROADWATCH_SMTP_PORT", "587")), timeout=20) as smtp:
        smtp.starttls()
        if os.getenv("ROADWATCH_SMTP_USER"):
            smtp.login(os.getenv("ROADWATCH_SMTP_USER"), os.getenv("ROADWATCH_SMTP_PASSWORD", ""))
        smtp.send_message(message)


def secure_cookies(request: Request):
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
