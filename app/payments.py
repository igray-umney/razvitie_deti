import os, uuid
from yookassa import Configuration, Payment

Configuration.account_id = os.getenv("YOOKASSA_SHOP_ID")
Configuration.secret_key  = os.getenv("YOOKASSA_SECRET_KEY")
PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL")

PRICES = {
    "month":     float(os.getenv("PRICE_MONTH", "390")),
    "3month":    float(os.getenv("PRICE_3MONTH", "990")),
    "6month":    float(os.getenv("PRICE_6MONTH", "1690")),
    "lifetime":  float(os.getenv("PRICE_LIFETIME", "2990")),
}

def create_payment(user_id: int, tariff: str):
    amount = PRICES[tariff]
    payment = Payment.create({
        "amount": {"value": f"{amount:.2f}", "currency": os.getenv("CURRENCY","RUB")},
        "confirmation": {
            "type": "redirect",
            "return_url": f"{PUBLIC_BASE}/paid?user_id={user_id}"
        },
        "capture": True,
        "description": f"Развитие для детей — {tariff}",
        "metadata": {"user_id": str(user_id), "tariff": tariff}
    }, idempotency_key=str(uuid.uuid4()))
    return payment

def tariff_to_delta(tariff: str):
    from datetime import timedelta
    if tariff == "month": return timedelta(days=30)
    if tariff == "3month": return timedelta(days=90)
    if tariff == "6month": return timedelta(days=180)
    if tariff == "lifetime": return None
    raise ValueError("Unknown tariff")
