"""
Webhook server for:
- v-kassa payment callbacks
- Mini App API backend
Run with: uvicorn webhook:app --host 0.0.0.0 --port 8080
"""

import logging
import json
import secrets
import time
import random
import smtplib
import asyncio
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone, timedelta
import hmac
import hashlib
from urllib.parse import unquote

from aiogram import Bot
from aiogram.types import LabeledPrice

from config import VKASSA_SECRET_KEY, BOT_TOKEN, PLANS, YUKASSA_PROVIDER_TOKEN, GMAIL_USER, GMAIL_APP_PASSWORD
from database import UserDB, PaymentDB, db
from subscription import SubscriptionService
from payment import vkassa

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_telegram_init_data(init_data: str) -> dict | None:
    """
    Verify Telegram Mini App init data.
    Spec: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    try:
        parsed = {}
        for pair in init_data.split("&"):
            if "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            parsed[unquote(key)] = unquote(value)

        received_hash = parsed.pop("hash", "")
        if not received_hash:
            logger.warning("No hash in init_data")
            return None

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )

        # secret_key = HMAC-SHA256(key="WebAppData", msg=bot_token)
        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode("utf-8"),
            hashlib.sha256
        ).digest()

        expected_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(received_hash, expected_hash):
            logger.warning(f"Hash mismatch. Expected={expected_hash}, Got={received_hash}")
            return None

        user_data = json.loads(parsed.get("user", "{}"))
        return user_data

    except Exception as e:
        logger.error(f"Init data verification error: {e}", exc_info=True)
        return None


def parse_init_data_header(header_value: str | None) -> str | None:
    """Extract raw initData string from header value."""
    if not header_value:
        return None
    # Some frontends send "tma <initdata>"
    if header_value.startswith("tma "):
        return header_value[4:]
    return header_value


# =================== v-kassa webhook ===================

@app.post("/webhook/vkassa")
async def vkassa_webhook(request: Request):
    data = await request.json()
    logger.info(f"v-kassa webhook: {data}")

    if not vkassa.verify_webhook(data):
        raise HTTPException(status_code=400, detail="Invalid signature")

    payment_id = data.get("order_id")
    status = data.get("status")
    amount = float(data.get("amount", 0)) / 100

    if not payment_id:
        return JSONResponse({"ok": True})

    payment = await PaymentDB.get_payment(payment_id)
    if not payment:
        logger.warning(f"Payment {payment_id} not found in DB")
        return JSONResponse({"ok": True})

    if status == "paid" and payment.get("status") != "paid":
        await PaymentDB.update_payment_status(payment_id, "paid")
        user_id = payment.get("user_id")
        plan = payment.get("plan")

        if plan == "topup":
            new_balance = await UserDB.add_balance(user_id, amount)
            logger.info(f"Balance topped up: user {user_id}, +{amount}, balance={new_balance}")
        else:
            await UserDB.add_balance(user_id, amount)
            ok, vless_link = await SubscriptionService.activate_plan(user_id, plan)
            if ok:
                logger.info(f"Subscription activated: user {user_id}, plan {plan}")
            else:
                logger.error(f"Failed to activate subscription for {user_id}: {vless_link}")

    return JSONResponse({"ok": True})


# =================== Mini App API ===================

@app.get("/api/user")
async def get_user(x_init_data: str = Header(None)):
    """Main endpoint — Mini App calls this on load to get fresh user data"""
    raw = parse_init_data_header(x_init_data)
    if not raw:
        logger.warning("/api/user called without x-init-data header")
        raise HTTPException(status_code=401, detail="No init data provided")

    tg_user = verify_telegram_init_data(raw)
    if not tg_user:
        raise HTTPException(status_code=401, detail="Invalid Telegram init data")

    user_id = tg_user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="No user id in init data")

    # Auto-create user on first visit from Mini App
    user = await UserDB.get_or_create_user(
        user_id,
        tg_user.get("username", ""),
        tg_user.get("first_name", ""),
    )

    sub_info = await SubscriptionService.get_subscription_info(user_id)

    # Fetch VLESS link if subscription is active
    vless_link = ""
    xui_uuid = user.get("xui_uuid")
    logger.info(f"/api/user user_id={user_id} xui_uuid={xui_uuid} sub_active={sub_info.get('active')}")
    if sub_info.get("active") and xui_uuid:
        from xui_client import xui_client
        try:
            vless_link = await xui_client.get_client_vless_link(xui_uuid) or ""
            logger.info(f"vless_link for {user_id}: {vless_link[:60]}...")
        except Exception as e:
            logger.error(f"VLESS link fetch error for user {user_id}: {e}")

    return {
        "user_id": user_id,
        "first_name": tg_user.get("first_name", ""),
        "username": tg_user.get("username", ""),
        "balance": user.get("balance", 0.0),
        "subscription": sub_info,
        "trial_used": user.get("trial_used", False),
        "referrals_count": len(user.get("referrals", [])),
        "referral_earnings": user.get("referral_earnings", 0.0),
        "vless_link": vless_link,
        "email": user.get("email", ""),
    }


@app.post("/api/trial/activate")
async def activate_trial_api(x_init_data: str = Header(None)):
    raw = parse_init_data_header(x_init_data)
    tg_user = verify_telegram_init_data(raw) if raw else None
    if not tg_user:
        raise HTTPException(status_code=401, detail="Invalid init data")

    user_id = tg_user.get("id")
    ok, result = await SubscriptionService.activate_trial(user_id)
    return {"success": ok, "vless_link": result if ok else None, "error": result if not ok else None}


@app.post("/api/subscription/activate")
async def activate_subscription_api(request: Request, x_init_data: str = Header(None)):
    raw = parse_init_data_header(x_init_data)
    tg_user = verify_telegram_init_data(raw) if raw else None
    if not tg_user:
        raise HTTPException(status_code=401, detail="Invalid init data")

    body = await request.json()
    plan_key = body.get("plan")
    user_id = tg_user.get("id")

    ok, result = await SubscriptionService.activate_plan(user_id, plan_key)
    return {"success": ok, "vless_link": result if ok else None, "error": result if not ok else None}


@app.post("/api/payment/create")
async def create_payment_api(request: Request, x_init_data: str = Header(None)):
    raw = parse_init_data_header(x_init_data)
    tg_user = verify_telegram_init_data(raw) if raw else None
    if not tg_user:
        raise HTTPException(status_code=401, detail="Invalid init data")

    body = await request.json()
    amount = float(body.get("amount", 0))
    plan = body.get("plan", "topup")
    user_id = tg_user.get("id")

    if amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid amount")

    order_id = f"{plan}_{user_id}_{int(datetime.now().timestamp())}"
    payment_data = await vkassa.create_payment(
        amount=amount, order_id=order_id,
        description=f"VPN {plan}", user_id=user_id,
    )
    if not payment_data:
        raise HTTPException(status_code=500, detail="Payment creation failed")

    await PaymentDB.create_payment(order_id, user_id, amount, plan)
    return {"payment_url": payment_data["payment_url"], "order_id": order_id}


@app.get("/api/plans")
async def get_plans():
    return PLANS


@app.post("/api/payment/invoice")
async def create_invoice_api(request: Request, x_init_data: str = Header(None)):
    """Create Telegram invoice link for Mini App tg.openInvoice()"""
    raw = parse_init_data_header(x_init_data)
    tg_user = verify_telegram_init_data(raw) if raw else None
    if not tg_user:
        raise HTTPException(status_code=401, detail="Invalid init data")

    body = await request.json()
    plan_key = body.get("plan", "topup")
    amount = float(body.get("amount", 0))
    user_id = tg_user.get("id")

    if plan_key == "topup":
        if amount < 10:
            raise HTTPException(status_code=400, detail="Минимальная сумма — 10₽")
        title = "Пополнение баланса"
        description = f"Зачислим {amount:.0f} ₽ на ваш счёт"
        payload = f"topup:{user_id}:{int(datetime.now().timestamp())}"
    else:
        plan = PLANS.get(plan_key)
        if not plan:
            raise HTTPException(status_code=400, detail="Неверный тариф")
        amount = float(plan["price"])
        title = plan["name"]
        traffic = f"{'∞' if plan['traffic_gb'] == 0 else str(plan['traffic_gb']) + ' ГБ'}"
        description = f"VPN {plan['days']} дней · {traffic} · {plan['devices']} устр."
        payload = f"plan:{plan_key}:{user_id}:{int(datetime.now().timestamp())}"

    await PaymentDB.create_payment(payload, user_id, amount, plan_key)

    provider_data = json.dumps({
        "receipt": {
            "items": [
                {
                    "description": title,
                    "quantity": 1,
                    "amount": {
                        "value": f"{amount:.2f}",
                        "currency": "RUB",
                    },
                    "vat_code": 1,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }
            ],
            "tax_system_code": 1,
        }
    })

    bot = Bot(token=BOT_TOKEN)
    try:
        link = await bot.create_invoice_link(
            title=title,
            description=description,
            payload=payload,
            provider_token=YUKASSA_PROVIDER_TOKEN,
            currency="RUB",
            prices=[LabeledPrice(label=title, amount=int(amount * 100))],
            provider_data=provider_data,
            need_email=True,
            send_email_to_provider=True,
        )
    finally:
        await bot.session.close()

    return {"invoice_link": link}


@app.get("/api/vless")
async def get_vless_link(x_init_data: str = Header(None)):
    raw = parse_init_data_header(x_init_data)
    tg_user = verify_telegram_init_data(raw) if raw else None
    if not tg_user:
        raise HTTPException(status_code=401, detail="Invalid init data")

    user_id = tg_user.get("id")
    user = await UserDB.get_user(user_id)
    if not user or not user.get("xui_uuid"):
        raise HTTPException(status_code=404, detail="No active subscription")

    from xui_client import xui_client
    vless_link = await xui_client.get_client_vless_link(user["xui_uuid"])
    return {"vless_link": vless_link}


# ══════════════════════════════════════════════════════════
#  STANDALONE WEBSITE — session auth + site API
# ══════════════════════════════════════════════════════════

# In-memory session cache: token -> user_id
_sessions: dict = {}


async def _get_session_user(token: str):
    if token in _sessions:
        return _sessions[token]
    if db is None:
        return None
    doc = db.collection("sessions").document(token).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    expires = data.get("expires_at")
    if expires and expires < datetime.now(timezone.utc):
        return None
    uid = data.get("user_id")
    if uid:
        _sessions[token] = uid
    return uid


async def _require_site_user(authorization: str = Header(None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="No session token")
    token = authorization[7:]
    uid = await _get_session_user(token)
    if not uid:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return uid


# ── Serve static site files ──

@app.get("/")
async def serve_site():
    return FileResponse("site.html")

@app.get("/logo.png")
async def serve_logo():
    return FileResponse("logo.png")

@app.get("/Connect.json")
async def serve_connect():
    return FileResponse("Connect.json")


# ── Telegram Login Widget auth ──

@app.post("/api/auth/telegram")
async def auth_telegram(request: Request):
    """Verify Telegram Login Widget data and issue a session token."""
    raw = await request.json()
    data = dict(raw)
    check_hash = data.pop("hash", "")
    if not check_hash:
        raise HTTPException(status_code=401, detail="No hash")

    auth_date = int(data.get("auth_date", 0))
    if time.time() - auth_date > 86400:
        raise HTTPException(status_code=401, detail="Auth data expired")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, check_hash):
        raise HTTPException(status_code=401, detail="Invalid signature")

    user_id = int(data["id"])
    await UserDB.get_or_create_user(
        user_id,
        data.get("username", ""),
        data.get("first_name", ""),
    )

    token = secrets.token_hex(32)
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    if db:
        db.collection("sessions").document(token).set({
            "user_id": user_id,
            "created_at": datetime.now(timezone.utc),
            "expires_at": expires,
        })
    _sessions[token] = user_id
    logger.info(f"Site auth: user_id={user_id}")
    return {"token": token}



@app.post("/api/site/trial/activate")
async def site_activate_trial(user_id: int = Depends(_require_site_user)):
    ok, result = await SubscriptionService.activate_trial(user_id)
    return {"success": ok, "vless_link": result if ok else None, "error": result if not ok else None}


@app.post("/api/site/payment/create")
async def site_create_payment(request: Request, user_id: int = Depends(_require_site_user)):
    """Create v-kassa payment for standalone site (returns redirect URL)."""
    body = await request.json()
    plan_key = body.get("plan", "topup")
    amount = float(body.get("amount", 0))

    if amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid amount")

    plan = PLANS.get(plan_key)
    if not plan:
        raise HTTPException(status_code=400, detail="Invalid plan")

    amount = float(plan["price"])
    order_id = f"{plan_key}_{user_id}_{int(datetime.now().timestamp())}"
    description = f"PushkaVPN {plan['name']} {plan['days']} дней"

    payment_data = await vkassa.create_payment(
        amount=amount,
        order_id=order_id,
        description=description,
        user_id=user_id,
    )
    if not payment_data:
        raise HTTPException(status_code=500, detail="Payment creation failed")

    await PaymentDB.create_payment(order_id, user_id, amount, plan_key)
    return {"payment_url": payment_data["payment_url"], "order_id": order_id}


# ══════════════════════════════════════════════════════════
#  EMAIL AUTH — OTP send / verify
# ══════════════════════════════════════════════════════════

async def _send_otp_email(to_email: str, code: str):
    def _send():
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Код входа в PushkaVPN: {code}"
        msg["From"] = f"PushkaVPN <{GMAIL_USER}>"
        msg["To"] = to_email
        html = f"""
        <div style="font-family:Inter,sans-serif;background:#000;color:#f5f5f7;padding:40px;max-width:480px;margin:0 auto;border-radius:16px">
          <img src="https://194-58-95-176.nip.io/logo.png" width="40" style="border-radius:8px;margin-bottom:16px">
          <h2 style="margin:0 0 8px;font-size:22px">Код подтверждения</h2>
          <p style="color:#86868b;margin:0 0 24px">Введите этот код на сайте PushkaVPN</p>
          <div style="background:#111;border:1px solid #222;border-radius:12px;padding:24px;text-align:center;font-size:36px;font-weight:800;letter-spacing:0.2em">{code}</div>
          <p style="color:#48484a;font-size:12px;margin-top:20px">Код действителен 10 минут. Если вы не запрашивали код — просто игнорируйте это письмо.</p>
        </div>"""
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.send_message(msg)
    await asyncio.get_event_loop().run_in_executor(None, _send)


def _get_email_user_id(email: str):
    """Lookup user_id for email from email_users collection."""
    if db is None:
        return None
    doc = db.collection("email_users").document(email.lower()).get()
    return doc.to_dict().get("user_id") if doc.exists else None


def _set_email_user_id(email: str, user_id: int):
    if db:
        db.collection("email_users").document(email.lower()).set({"email": email.lower(), "user_id": user_id})


@app.post("/api/auth/email/send")
async def auth_email_send(request: Request):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Некорректный email")

    code = str(random.randint(100000, 999999))
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    if db:
        db.collection("email_codes").add({
            "email": email,
            "code": code,
            "expires_at": expires,
            "used": False,
        })
    try:
        await _send_otp_email(email, code)
    except Exception as e:
        logger.error(f"Email send error: {e}")
        raise HTTPException(status_code=500, detail="Не удалось отправить письмо")
    logger.info(f"OTP sent to {email}")
    return {"ok": True}


@app.post("/api/auth/email/verify")
async def auth_email_verify(request: Request):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    code = (body.get("code") or "").strip()

    if not email or not code:
        raise HTTPException(status_code=400, detail="email и code обязательны")

    if db is None:
        raise HTTPException(status_code=500, detail="DB unavailable")

    now = datetime.now(timezone.utc)
    docs = db.collection("email_codes")\
        .where("email", "==", email)\
        .where("code", "==", code)\
        .where("used", "==", False)\
        .stream()

    valid_doc = None
    for d in docs:
        data = d.to_dict()
        exp = data.get("expires_at")
        if exp and exp.replace(tzinfo=timezone.utc) > now:
            valid_doc = d
            break

    if not valid_doc:
        raise HTTPException(status_code=401, detail="Неверный или просроченный код")

    valid_doc.reference.update({"used": True})

    # Get or create user
    user_id = _get_email_user_id(email)
    if not user_id:
        user_id = random.randint(10**12, 10**13)
        await UserDB.get_or_create_user(user_id, "", email.split("@")[0])
        await UserDB.update_user(user_id, {"email": email})
        _set_email_user_id(email, user_id)
    else:
        user = await UserDB.get_user(user_id)
        if not user:
            await UserDB.get_or_create_user(user_id, "", email.split("@")[0])
            await UserDB.update_user(user_id, {"email": email})

    token = secrets.token_hex(32)
    expires_session = datetime.now(timezone.utc) + timedelta(days=30)
    db.collection("sessions").document(token).set({
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc),
        "expires_at": expires_session,
    })
    _sessions[token] = user_id
    logger.info(f"Email auth: {email} -> user_id={user_id}")
    return {"token": token}


# ── Return email in site user response ──
# (patch site_get_user to include email field)

@app.get("/api/site/user")
async def site_get_user_v2(user_id: int = Depends(_require_site_user)):
    user = await UserDB.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    sub_info = await SubscriptionService.get_subscription_info(user_id)
    vless_link = ""
    xui_uuid = user.get("xui_uuid")
    if sub_info.get("active") and xui_uuid:
        from xui_client import xui_client
        try:
            vless_link = await xui_client.get_client_vless_link(xui_uuid) or ""
        except Exception as e:
            logger.error(f"Site VLESS link error user {user_id}: {e}")
    return {
        "user_id": user_id,
        "first_name": user.get("first_name", ""),
        "username": user.get("username", ""),
        "email": user.get("email", ""),
        "tg_username": user.get("username", ""),
        "balance": user.get("balance", 0.0),
        "subscription": sub_info,
        "trial_used": user.get("trial_used", False),
        "vless_link": vless_link,
    }


# ── Generate TG link code (site user wants to link Telegram) ──

@app.post("/api/site/link/tg/generate")
async def site_generate_tg_link(user_id: int = Depends(_require_site_user)):
    code = secrets.token_hex(4).upper()  # e.g. "A1B2C3D4"
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)
    if db:
        db.collection("tg_link_codes").document(code).set({
            "site_user_id": user_id,
            "expires_at": expires,
            "used": False,
        })
    return {"code": code, "bot": "pushka_vpnbot"}


# ── Mini App: TG user links email ──

@app.post("/api/auth/link/email/send")
async def link_email_send(request: Request, x_init_data: str = Header(None)):
    raw = parse_init_data_header(x_init_data)
    tg_user = verify_telegram_init_data(raw) if raw else None
    if not tg_user:
        raise HTTPException(status_code=401, detail="Invalid init data")

    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Некорректный email")

    code = str(random.randint(100000, 999999))
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    if db:
        db.collection("email_codes").add({
            "email": email,
            "code": code,
            "expires_at": expires,
            "used": False,
            "link_tg_id": tg_user.get("id"),
        })
    try:
        await _send_otp_email(email, code)
    except Exception as e:
        logger.error(f"Link email send error: {e}")
        raise HTTPException(status_code=500, detail="Не удалось отправить письмо")
    return {"ok": True}


@app.post("/api/auth/link/email/verify")
async def link_email_verify(request: Request, x_init_data: str = Header(None)):
    raw = parse_init_data_header(x_init_data)
    tg_user = verify_telegram_init_data(raw) if raw else None
    if not tg_user:
        raise HTTPException(status_code=401, detail="Invalid init data")

    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    code = (body.get("code") or "").strip()
    tg_user_id = tg_user.get("id")

    if db is None:
        raise HTTPException(status_code=500, detail="DB unavailable")

    now = datetime.now(timezone.utc)
    docs = db.collection("email_codes")\
        .where("email", "==", email)\
        .where("code", "==", code)\
        .where("used", "==", False)\
        .stream()

    valid_doc = None
    for d in docs:
        data = d.to_dict()
        exp = data.get("expires_at")
        if exp and exp.replace(tzinfo=timezone.utc) > now:
            valid_doc = d
            break

    if not valid_doc:
        raise HTTPException(status_code=401, detail="Неверный или просроченный код")

    valid_doc.reference.update({"used": True})

    # Link email to TG user
    await UserDB.update_user(tg_user_id, {"email": email})
    _set_email_user_id(email, tg_user_id)
    logger.info(f"Email linked: {email} -> tg_user_id={tg_user_id}")
    return {"ok": True}
