import logging
from datetime import datetime, timezone

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    PreCheckoutQuery, SuccessfulPayment,
)

from config import PLANS, TRIAL_DAYS, TRIAL_TRAFFIC_GB
from database import UserDB, PaymentDB, db
from subscription import SubscriptionService

logger = logging.getLogger(__name__)
router = Router()


# ───────────────────────── /start ─────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user

    await UserDB.get_or_create_user(
        user.id, user.username or "", user.first_name or "", None
    )

    await message.answer(
        f"👋 Привет, <b>{user.first_name}</b>!\n\n"
        f"🚀 <b>PushkaVPN</b> — быстрый и надёжный VPN\n\n"
        f"✅ Обход блокировок (МТС, Билайн, МегаФон)\n"
        f"⚡ Высокая скорость без ограничений\n"
        f"🔒 Безопасное шифрование\n"
        f"📺 YouTube без рекламы\n\n"
        f"💎 <b>Тариф:</b> 250 ₽/месяц · 50 ГБ · 2 устройства\n"
        f"🎁 <b>Пробный период:</b> 1 день · 5 ГБ — бесплатно\n\n"
        f"👇 Откройте личный кабинет через кнопку меню внизу",
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════════════
#  ЮKassa — Telegram Payments (нужны для оплаты в мини-апп)
# ═══════════════════════════════════════════════════════════════

@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment: SuccessfulPayment = message.successful_payment
    payload = payment.invoice_payload
    amount = payment.total_amount / 100
    user_id = message.from_user.id

    parts = payload.split(":")

    if parts[0] == "topup":
        await PaymentDB.update_payment_status(payload, "paid")
        new_balance = await UserDB.add_balance(user_id, amount)
        await message.answer(
            f"✅ <b>Баланс пополнен!</b>\n\n"
            f"💰 Зачислено: <b>{amount:.0f} ₽</b>\n"
            f"📊 Новый баланс: <b>{new_balance:.2f} ₽</b>\n\n"
            f"Откройте кабинет для управления подпиской.",
            parse_mode="HTML",
        )

    elif parts[0] == "plan":
        plan_key = parts[1]
        plan = PLANS.get(plan_key, {})

        await PaymentDB.update_payment_status(payload, "paid")
        await UserDB.add_balance(user_id, amount)
        ok, vless_link = await SubscriptionService.activate_plan(user_id, plan_key)

        if ok:
            await message.answer(
                f"🎉 <b>Подписка активирована!</b>\n\n"
                f"Тариф: {plan.get('name', plan_key)}\n\n"
                f"<b>VLESS ключ:</b>\n<code>{vless_link}</code>\n\n"
                f"📱 Откройте кабинет, чтобы скопировать ключ и подключиться.",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                f"⚠️ Оплата прошла, но подписка не активировалась.\n"
                f"Обратитесь в поддержку: payload <code>{payload}</code>",
                parse_mode="HTML",
            )
    else:
        logger.warning(f"Unknown payment payload: {payload}")


# ───────────────────────── /link CODE ─────────────────────────

@router.message(Command("link"))
async def cmd_link(message: Message):
    """Link Telegram account to a site (email) account using a code from the website."""
    user = message.from_user
    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer(
            "Используйте команду так:\n<code>/link КОД</code>\n\n"
            "Код можно получить на сайте в разделе «Привязать Telegram».",
            parse_mode="HTML",
        )
        return

    code = args[1].strip().upper()

    if db is None:
        await message.answer("❌ Ошибка сервера. Попробуйте позже.")
        return

    doc_ref = db.collection("tg_link_codes").document(code)
    doc = doc_ref.get()

    if not doc.exists:
        await message.answer("❌ Код не найден. Проверьте правильность или получите новый код на сайте.")
        return

    data = doc.to_dict()
    if data.get("used"):
        await message.answer("❌ Этот код уже был использован. Получите новый на сайте.")
        return

    expires = data.get("expires_at")
    if expires and expires.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        await message.answer("❌ Код просрочен. Получите новый на сайте.")
        return

    site_user_id = data.get("site_user_id")
    tg_user_id = user.id

    # Mark code as used
    doc_ref.update({"used": True, "linked_tg_id": tg_user_id})

    # Merge: update site user to know TG info, and TG user to know site user email
    site_user = await UserDB.get_user(site_user_id)
    tg_user_doc = await UserDB.get_user(tg_user_id)

    email = site_user.get("email", "") if site_user else ""

    if tg_user_doc:
        # TG user already exists — add email
        if email:
            await UserDB.update_user(tg_user_id, {"email": email})
            if db:
                db.collection("email_users").document(email.lower()).set(
                    {"email": email.lower(), "user_id": tg_user_id}
                )
    else:
        await UserDB.get_or_create_user(tg_user_id, user.username or "", user.first_name or "")
        if email:
            await UserDB.update_user(tg_user_id, {"email": email})

    # Update site sessions to point to TG user_id if different
    if site_user_id != tg_user_id and email:
        if db:
            db.collection("email_users").document(email.lower()).set(
                {"email": email.lower(), "user_id": tg_user_id}
            )
        logger.info(f"/link: merged site_user {site_user_id} -> tg_user {tg_user_id}, email={email}")

    await message.answer(
        f"✅ <b>Аккаунты привязаны!</b>\n\n"
        f"Ваш Telegram привязан к сайту PushkaVPN.\n"
        f"Теперь войдите на сайт через почту <b>{email}</b> — данные синхронизированы.",
        parse_mode="HTML",
    )
