import os
import secrets
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select, delete, desc, func

from database import Session, Product, Category, WebAccount, WebCartItem, WebFavorite, WebOrder, WebOrderItem, WebWallet, WebLedgerEntry, WebTopUpRequest, WebSupportTicket, WebDeveloperRequest, WebAdminAudit, WebDevOrder, init_db
from web.backend.auth import (
    token_from_request, create_session_token, COOKIE_NAME, check_login_rate, clear_session_cookie, hash_password,
    normalize_email, read_session_token, reset_login_rate,
    set_session_cookie, verify_password,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Yomkost Store API", version="3.0.0", lifespan=lifespan)
origins = [x.strip() for x in os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5500,http://127.0.0.1:5500,http://localhost:3000,http://127.0.0.1:3000",
).split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    allow_origin_regex=os.getenv("CORS_ORIGIN_REGEX") or None,
)


class RegisterBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=128)
    display_name: str = Field(min_length=2, max_length=80)


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class ProfileBody(BaseModel):
    display_name: str = Field(min_length=2, max_length=80)


class CheckoutBody(BaseModel):
    customer_name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    phone: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=1000)
    payment_method: str = Field(default="manual_transfer", pattern="^(manual_transfer|contact|wallet)$")


class TopUpBody(BaseModel):
    amount: Decimal = Field(ge=Decimal("50.00"), le=Decimal("100000.00"), max_digits=14, decimal_places=2)
    customer_note: str = Field(default="", max_length=500)

class AdminDecisionBody(BaseModel):
    note: str = Field(default="", max_length=500)


class ProductAdminBody(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    description: str = Field(default="", max_length=5000)
    price: Decimal = Field(gt=Decimal("0"), le=Decimal("1000000"), max_digits=12, decimal_places=2)
    category_id: int | None = None
    delivery_text: str = Field(default="", max_length=5000)
    image_url: str = Field(default="", max_length=1000)
    active: bool = True

class RoleBody(BaseModel):
    role: str = Field(pattern="^(customer|developer|support|moderator|admin)$")

class OrderStatusBody(BaseModel):
    status: str = Field(pattern="^(awaiting_payment|awaiting_confirmation|payment_reported|processing|completed|cancelled|refunded|rejected)$")
    note: str = Field(default="", max_length=500)

class SupportBody(BaseModel):
    subject: str = Field(min_length=3, max_length=160)
    message: str = Field(min_length=5, max_length=5000)

class SupportReplyBody(BaseModel):
    status: str = Field(default="answered", pattern="^(open|in_progress|answered|closed)$")
    reply: str = Field(default="", max_length=5000)

class DeveloperRequestBody(BaseModel):
    skills: str = Field(min_length=2, max_length=500)
    experience: str = Field(default="", max_length=4000)
    portfolio_url: str = Field(default="", max_length=500)



def order_payload(order: WebOrder, items: list[WebOrderItem] | None = None) -> dict:
    data = {
        "id": order.id,
        "order_number": order.order_number,
        "customer_name": order.customer_name,
        "email": order.email,
        "phone": order.phone,
        "note": order.note,
        "payment_method": order.payment_method,
        "status": order.status,
        "total": str(order.total),
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "payment_reported_at": order.payment_reported_at.isoformat() if order.payment_reported_at else None,
    }
    if items is not None:
        data["items"] = [{
            "product_id": item.product_id,
            "title": item.product_title,
            "unit_price": str(item.unit_price),
            "quantity": item.quantity,
            "line_total": str(item.line_total),
        } for item in items]
    return data


def account_data(account: WebAccount) -> dict:
    return {
        "id": account.id,
        "email": account.email,
        "display_name": account.display_name,
        "role": account.role,
        "created_at": account.created_at.isoformat() if account.created_at else None,
    }


async def current_account(request: Request) -> WebAccount:
    account_id = read_session_token(token_from_request(request))
    if not account_id:
        raise HTTPException(status_code=401, detail="Увійди у свій акаунт")
    async with Session() as session:
        account = await session.get(WebAccount, account_id)
        if not account or not account.is_active:
            raise HTTPException(status_code=401, detail="Сесію завершено. Увійди ще раз")
        owner_emails = {x.strip().lower() for x in (os.getenv("WEB_OWNER_EMAILS", "") + "," + os.getenv("WEB_OWNER_EMAIL", "")).split(",") if x.strip()}
        admin_emails = {x.strip().lower() for x in os.getenv("WEB_ADMIN_EMAILS", "").split(",") if x.strip()}
        if account.email in owner_emails and account.role != "owner":
            account.role = "owner"
            await session.commit()
            await session.refresh(account)
        elif account.email in admin_emails and account.role not in {"admin", "owner"}:
            account.role = "admin"
            await session.commit()
            await session.refresh(account)
        return account


async def get_or_create_wallet(session, account_id: int | None = None, system: bool = False, lock: bool = False):
    key = "system:clearing" if system else f"web:{account_id}"
    stmt = select(WebWallet).where(WebWallet.wallet_key == key)
    if lock:
        stmt = stmt.with_for_update()
    wallet = await session.scalar(stmt)
    if wallet:
        return wallet
    wallet = WebWallet(wallet_key=key, account_id=None if system else account_id,
                       balance=Decimal("0.00"), currency="UAH")
    session.add(wallet)
    await session.flush()
    return wallet


def money_value(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def finance_admin(account: WebAccount):
    if account.role not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Ця дія доступна лише адміністратору")


def wallet_data(wallet: WebWallet) -> dict:
    return {"balance": str(money_value(wallet.balance)), "currency": wallet.currency}

async def post_balanced_transaction(session, user_wallet: WebWallet, system_wallet: WebWallet,
                                    amount: Decimal, entry_type: str, reference: str, description: str):
    amount = money_value(amount)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Сума має бути більшою за нуль")
    tx_id = "TX-" + secrets.token_hex(12).upper()
    user_wallet.balance = money_value(user_wallet.balance + amount)
    system_wallet.balance = money_value(system_wallet.balance - amount)
    session.add_all([
        WebLedgerEntry(transaction_id=tx_id, wallet_id=user_wallet.id, entry_type=entry_type,
                       amount=amount, reference=reference, description=description),
        WebLedgerEntry(transaction_id=tx_id, wallet_id=system_wallet.id, entry_type=entry_type,
                       amount=-amount, reference=reference, description=description),
    ])
    return tx_id



@app.get("/")
async def root():
    return {"name": "Yomkost Store API", "status": "online", "version": "3.0.0"}


@app.get("/api/health")
async def health():
    try:
        async with Session() as session:
            await session.execute(select(1))
        return {"status": "ok", "database": "connected"}
    except Exception:
        return {"status": "degraded", "database": "unavailable"}


@app.post("/api/auth/register", status_code=201)
async def register(body: RegisterBody, request: Request, response: Response):
    check_login_rate(request)
    email = normalize_email(body.email)
    name = body.display_name.strip()
    if len(name) < 2:
        raise HTTPException(status_code=422, detail="Ім’я має містити щонайменше 2 символи")
    async with Session() as session:
        existing = await session.scalar(select(WebAccount).where(WebAccount.email == email))
        if existing:
            raise HTTPException(status_code=409, detail="Акаунт із такою поштою вже існує")
        account = WebAccount(email=email, password_hash=hash_password(body.password), display_name=name, role="customer")
        session.add(account)
        await session.commit()
        await session.refresh(account)
        set_session_cookie(response, account.id, request)
        return {"account": account_data(account), "token": create_session_token(account.id)}


@app.post("/api/auth/login")
async def login(body: LoginBody, request: Request, response: Response):
    check_login_rate(request)
    try:
        email = normalize_email(body.email)
    except HTTPException:
        email = body.email.strip().lower()[:254]
    async with Session() as session:
        account = await session.scalar(select(WebAccount).where(WebAccount.email == email))
        if not account or not account.is_active or not verify_password(body.password, account.password_hash):
            raise HTTPException(status_code=401, detail="Неправильна пошта або пароль")
        reset_login_rate(request)
        set_session_cookie(response, account.id, request)
        return {"account": account_data(account), "token": create_session_token(account.id)}


@app.get("/api/auth/me")
async def me(request: Request):
    account = await current_account(request)
    return {"account": account_data(account)}


@app.patch("/api/auth/profile")
async def update_profile(body: ProfileBody, request: Request):
    account_id = read_session_token(token_from_request(request))
    if not account_id:
        raise HTTPException(status_code=401, detail="Увійди у свій акаунт")
    name = body.display_name.strip()
    if len(name) < 2:
        raise HTTPException(status_code=422, detail="Ім’я має містити щонайменше 2 символи")
    async with Session() as session:
        account = await session.get(WebAccount, account_id)
        if not account or not account.is_active:
            raise HTTPException(status_code=401, detail="Сесію завершено. Увійди ще раз")
        account.display_name = name
        await session.commit()
        await session.refresh(account)
        return {"account": account_data(account)}


@app.post("/api/auth/logout")
async def logout(response: Response):
    clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/categories")
async def categories():
    async with Session() as session:
        rows = (await session.scalars(
            select(Category).where(Category.active.is_(True)).order_by(Category.name)
        )).all()
        return [{"id": x.id, "name": x.name} for x in rows]


def img(x):
    u = (x.photo_file_id or "").strip()
    return u if u.startswith(("http://", "https://")) else None


@app.get("/api/products")
async def products(category_id: int | None = None, q: str | None = None, sort: str = "new"):
    async with Session() as session:
        stmt = select(Product).where(Product.active.is_(True))
        if category_id is not None:
            stmt = stmt.where(Product.category_id == category_id)
        if q:
            stmt = stmt.where(Product.title.ilike(f"%{q[:100]}%"))
        if sort == "cheap":
            stmt = stmt.order_by(Product.price.asc())
        elif sort == "expensive":
            stmt = stmt.order_by(Product.price.desc())
        elif sort == "popular":
            stmt = stmt.order_by(Product.sales.desc())
        else:
            stmt = stmt.order_by(Product.created_at.desc())
        rows = (await session.scalars(stmt.limit(60))).all()
        return [{
            "id": x.id, "category_id": x.category_id, "title": x.title,
            "description": x.description, "price": x.price,
            "sales": x.sales, "image_url": img(x),
        } for x in rows]


async def product_payload(session, product_id: int):
    product = await session.get(Product, product_id)
    if not product or not product.active:
        return None
    return {"id": product.id, "category_id": product.category_id, "title": product.title,
            "description": product.description, "price": product.price,
            "sales": product.sales, "image_url": img(product)}

@app.get("/api/products/{product_id}")
async def product_detail(product_id: int):
    async with Session() as session:
        item = await product_payload(session, product_id)
        if not item:
            raise HTTPException(status_code=404, detail="Товар не знайдено або він недоступний")
        return item

@app.get("/api/cart")
async def get_cart(request: Request):
    account = await current_account(request)
    async with Session() as session:
        rows = (await session.scalars(select(WebCartItem).where(WebCartItem.account_id == account.id).order_by(WebCartItem.created_at.desc()))).all()
        result = []
        for row in rows:
            item = await product_payload(session, row.product_id)
            if item:
                item["quantity"] = row.quantity
                result.append(item)
        return {"items": result, "total": round(sum(float(x["price"] or 0) * x["quantity"] for x in result), 2)}

class CartBody(BaseModel):
    product_id: int
    quantity: int = Field(default=1, ge=1, le=99)

@app.post("/api/cart", status_code=201)
async def add_cart(body: CartBody, request: Request):
    account = await current_account(request)
    async with Session() as session:
        product = await session.get(Product, body.product_id)
        if not product or not product.active:
            raise HTTPException(status_code=404, detail="Товар недоступний")
        row = await session.scalar(select(WebCartItem).where(WebCartItem.account_id == account.id, WebCartItem.product_id == body.product_id))
        if row:
            row.quantity = min(99, row.quantity + body.quantity)
        else:
            session.add(WebCartItem(account_id=account.id, product_id=body.product_id, quantity=body.quantity))
        await session.commit()
        return {"ok": True}

@app.patch("/api/cart/{product_id}")
async def update_cart(product_id: int, body: CartBody, request: Request):
    account = await current_account(request)
    async with Session() as session:
        row = await session.scalar(select(WebCartItem).where(WebCartItem.account_id == account.id, WebCartItem.product_id == product_id))
        if not row:
            raise HTTPException(status_code=404, detail="Цього товару немає в кошику")
        row.quantity = body.quantity
        await session.commit()
        return {"ok": True}

@app.delete("/api/cart/{product_id}")
async def remove_cart(product_id: int, request: Request):
    account = await current_account(request)
    async with Session() as session:
        await session.execute(delete(WebCartItem).where(WebCartItem.account_id == account.id, WebCartItem.product_id == product_id))
        await session.commit()
        return {"ok": True}

@app.get("/api/favorites")
async def get_favorites(request: Request):
    account = await current_account(request)
    async with Session() as session:
        rows = (await session.scalars(select(WebFavorite).where(WebFavorite.account_id == account.id).order_by(WebFavorite.created_at.desc()))).all()
        items = []
        for row in rows:
            item = await product_payload(session, row.product_id)
            if item: items.append(item)
        return {"items": items}

@app.post("/api/favorites/{product_id}")
async def toggle_favorite(product_id: int, request: Request):
    account = await current_account(request)
    async with Session() as session:
        product = await session.get(Product, product_id)
        if not product or not product.active: raise HTTPException(status_code=404, detail="Товар недоступний")
        row = await session.scalar(select(WebFavorite).where(WebFavorite.account_id == account.id, WebFavorite.product_id == product_id))
        if row:
            await session.delete(row); saved = False
        else:
            session.add(WebFavorite(account_id=account.id, product_id=product_id)); saved = True
        await session.commit()
        return {"saved": saved}


# ------------------------- WEB FINANCE (PART 5) -------------------------

@app.get("/api/wallet")
async def get_wallet(request: Request):
    account = await current_account(request)
    async with Session() as session:
        wallet = await get_or_create_wallet(session, account.id)
        await session.commit()
        rows = (await session.scalars(select(WebLedgerEntry).where(WebLedgerEntry.wallet_id == wallet.id).order_by(desc(WebLedgerEntry.created_at)).limit(30))).all()
        return {"wallet": wallet_data(wallet), "entries": [{
            "transaction_id": x.transaction_id, "type": x.entry_type, "amount": str(x.amount),
            "reference": x.reference, "description": x.description,
            "created_at": x.created_at.isoformat() if x.created_at else None,
        } for x in rows]}

@app.post("/api/wallet/topups", status_code=201)
async def create_topup(body: TopUpBody, request: Request):
    account = await current_account(request)
    amount = money_value(body.amount)
    if amount < Decimal("50.00"):
        raise HTTPException(status_code=422, detail="Мінімальне поповнення 50 грн")
    async with Session() as session:
        await get_or_create_wallet(session, account.id)
        topup = WebTopUpRequest(
            request_number="TP-" + datetime.now(timezone.utc).strftime("%y%m%d") + "-" + secrets.token_hex(3).upper(),
            account_id=account.id, amount=amount, payment_method="manual_transfer",
            status="awaiting_payment", customer_note=body.customer_note.strip())
        session.add(topup)
        await session.commit()
        await session.refresh(topup)
        return {"topup": {"id": topup.id, "request_number": topup.request_number,
                "amount": str(topup.amount), "status": topup.status},
                "payment_instructions": os.getenv("PAYMENT_INSTRUCTIONS", "Менеджер надасть реквізити після перевірки заявки.")}

@app.get("/api/wallet/topups")
async def list_topups(request: Request):
    account = await current_account(request)
    async with Session() as session:
        rows = (await session.scalars(select(WebTopUpRequest).where(WebTopUpRequest.account_id == account.id).order_by(desc(WebTopUpRequest.created_at)).limit(50))).all()
        return {"items": [{"id": x.id, "request_number": x.request_number, "amount": str(x.amount),
            "status": x.status, "created_at": x.created_at.isoformat() if x.created_at else None,
            "admin_note": x.admin_note} for x in rows]}

@app.post("/api/wallet/topups/{topup_id}/report-payment")
async def report_topup_payment(topup_id: int, request: Request):
    account = await current_account(request)
    async with Session() as session:
        topup = await session.scalar(select(WebTopUpRequest).where(WebTopUpRequest.id == topup_id, WebTopUpRequest.account_id == account.id))
        if not topup:
            raise HTTPException(status_code=404, detail="Заявку не знайдено")
        if topup.status != "awaiting_payment":
            raise HTTPException(status_code=409, detail="Для цієї заявки не можна повідомити про оплату")
        topup.status = "payment_reported"
        topup.payment_reported_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()
        return {"ok": True, "message": "Повідомлення збережено. Баланс буде поповнено після перевірки"}

@app.get("/api/admin/finance/topups")
async def admin_topups(request: Request):
    account = await current_account(request); finance_admin(account)
    async with Session() as session:
        rows = (await session.scalars(select(WebTopUpRequest).where(WebTopUpRequest.status == "payment_reported").order_by(WebTopUpRequest.created_at).limit(100))).all()
        return {"items": [{"id": x.id, "request_number": x.request_number, "account_id": x.account_id,
            "amount": str(x.amount), "email": (await session.get(WebAccount, x.account_id)).email,
            "status": x.status, "created_at": x.created_at.isoformat() if x.created_at else None} for x in rows]}

@app.post("/api/admin/finance/topups/{topup_id}/approve")
async def approve_topup(topup_id: int, body: AdminDecisionBody, request: Request):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        topup = await session.scalar(select(WebTopUpRequest).where(WebTopUpRequest.id == topup_id).with_for_update())
        if not topup or topup.status != "payment_reported":
            raise HTTPException(status_code=409, detail="Заявка відсутня або вже оброблена")
        user_wallet = await get_or_create_wallet(session, topup.account_id, lock=True)
        system_wallet = await get_or_create_wallet(session, system=True, lock=True)
        tx_id = await post_balanced_transaction(session, user_wallet, system_wallet, topup.amount,
            "TOPUP", topup.request_number, body.note.strip() or "Підтверджене поповнення балансу")
        topup.status = "approved"; topup.admin_note = body.note.strip()
        topup.processed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()
        return {"ok": True, "transaction_id": tx_id, "balance": str(user_wallet.balance)}

@app.post("/api/admin/finance/topups/{topup_id}/reject")
async def reject_topup(topup_id: int, body: AdminDecisionBody, request: Request):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        topup = await session.scalar(select(WebTopUpRequest).where(WebTopUpRequest.id == topup_id).with_for_update())
        if not topup or topup.status != "payment_reported":
            raise HTTPException(status_code=409, detail="Заявка відсутня або вже оброблена")
        topup.status = "rejected"; topup.admin_note = body.note.strip()
        topup.processed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()
        return {"ok": True}

@app.get("/api/admin/finance/summary")
async def admin_finance_summary(request: Request):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        users = (await session.scalars(select(WebWallet).where(WebWallet.account_id.is_not(None)))).all()
        system = await session.scalar(select(WebWallet).where(WebWallet.wallet_key == "system:clearing"))
        pending = await session.scalar(select(func.count(WebTopUpRequest.id)).where(WebTopUpRequest.status == "payment_reported"))
        return {"wallet_count": len(users), "customer_balances_total": str(sum((money_value(w.balance) for w in users), Decimal("0.00"))),
                "clearing_balance": str(money_value(system.balance) if system else Decimal("0.00")),
                "pending_topups": int(pending or 0)}

# ------------------------- WEB ORDERS (PART 4) -------------------------

@app.post("/api/orders", status_code=201)
async def create_order(body: CheckoutBody, request: Request):
    account = await current_account(request)
    customer_name = body.customer_name.strip()
    email = normalize_email(body.email)
    if len(customer_name) < 2:
        raise HTTPException(status_code=422, detail="Вкажи ім’я покупця")
    async with Session() as session:
        cart_rows = (await session.scalars(
            select(WebCartItem).where(WebCartItem.account_id == account.id)
        )).all()
        if not cart_rows:
            raise HTTPException(status_code=400, detail="Кошик порожній")

        order_lines = []
        total = Decimal("0.00")
        for row in cart_rows:
            product = await session.get(Product, row.product_id)
            if not product or not product.active:
                raise HTTPException(status_code=409, detail="Один із товарів більше недоступний. Онови кошик")
            price = Decimal(str(product.price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            line_total = (price * row.quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            total += line_total
            order_lines.append((product.id, product.title, price, row.quantity, line_total))

        order = WebOrder(
            order_number="YS-" + datetime.now(timezone.utc).strftime("%y%m%d") + "-" + secrets.token_hex(3).upper(),
            account_id=account.id,
            customer_name=customer_name,
            email=email,
            phone=body.phone.strip(),
            note=body.note.strip(),
            payment_method=body.payment_method,
            status="processing" if body.payment_method == "wallet" else ("awaiting_payment" if body.payment_method == "manual_transfer" else "awaiting_confirmation"),
            total=total,
        )
        if body.payment_method == "wallet":
            user_wallet = await get_or_create_wallet(session, account.id, lock=True)
            if money_value(user_wallet.balance) < money_value(total):
                raise HTTPException(status_code=409, detail="Недостатньо коштів на балансі. Поповни гаманець або обери інший спосіб оплати")
            system_wallet = await get_or_create_wallet(session, system=True, lock=True)
            session.add(order)
            await session.flush()
            user_wallet.balance = money_value(user_wallet.balance - total)
            system_wallet.balance = money_value(system_wallet.balance + total)
            tx_id = "TX-" + secrets.token_hex(12).upper()
            session.add_all([
                WebLedgerEntry(transaction_id=tx_id, wallet_id=user_wallet.id, entry_type="PAYMENT", amount=-money_value(total), reference=order.order_number, description="Оплата замовлення з балансу"),
                WebLedgerEntry(transaction_id=tx_id, wallet_id=system_wallet.id, entry_type="PAYMENT", amount=money_value(total), reference=order.order_number, description="Оплата замовлення з балансу"),
            ])
        else:
            session.add(order)
            await session.flush()
        for product_id, title, price, quantity, line_total in order_lines:
            session.add(WebOrderItem(
                order_id=order.id, product_id=product_id, product_title=title,
                unit_price=price, quantity=quantity, line_total=line_total,
            ))
        for row in cart_rows:
            await session.delete(row)
        await session.commit()
        await session.refresh(order)
        return {
            "order": order_payload(order),
            "payment_instructions": os.getenv(
                "PAYMENT_INSTRUCTIONS",
                "Після перевірки замовлення менеджер надасть реквізити для оплати. Не переказуй кошти на невідомі реквізити."
            ),
        }


@app.get("/api/orders")
async def list_orders(request: Request):
    account = await current_account(request)
    async with Session() as session:
        rows = (await session.scalars(
            select(WebOrder).where(WebOrder.account_id == account.id).order_by(desc(WebOrder.created_at)).limit(100)
        )).all()
        return {"items": [order_payload(row) for row in rows]}


@app.get("/api/orders/{order_id}")
async def get_order(order_id: int, request: Request):
    account = await current_account(request)
    async with Session() as session:
        order = await session.scalar(select(WebOrder).where(
            WebOrder.id == order_id, WebOrder.account_id == account.id
        ))
        if not order:
            raise HTTPException(status_code=404, detail="Замовлення не знайдено")
        items = (await session.scalars(select(WebOrderItem).where(WebOrderItem.order_id == order.id))).all()
        data = order_payload(order, items)
        if order.status == "completed":
            for row, item in zip(data["items"], items):
                product = await session.get(Product, item.product_id) if item.product_id else None
                row["delivery_text"] = product.delivery_text if product else ""
        return {"order": data}


@app.post("/api/orders/{order_id}/report-payment")
async def report_order_payment(order_id: int, request: Request):
    account = await current_account(request)
    async with Session() as session:
        order = await session.scalar(select(WebOrder).where(
            WebOrder.id == order_id, WebOrder.account_id == account.id
        ))
        if not order:
            raise HTTPException(status_code=404, detail="Замовлення не знайдено")
        if order.status != "awaiting_payment":
            raise HTTPException(status_code=409, detail="Для цього замовлення зараз не можна повідомити про оплату")
        order.status = "payment_reported"
        order.payment_reported_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()
        return {"order": order_payload(order), "message": "Повідомлення про оплату збережено. Очікуй перевірки менеджером"}


@app.post("/api/orders/{order_id}/cancel")
async def cancel_order(order_id: int, request: Request):
    account = await current_account(request)
    async with Session() as session:
        order = await session.scalar(select(WebOrder).where(
            WebOrder.id == order_id, WebOrder.account_id == account.id
        ))
        if not order:
            raise HTTPException(status_code=404, detail="Замовлення не знайдено")
        if order.status not in {"awaiting_payment", "awaiting_confirmation"}:
            raise HTTPException(status_code=409, detail="Це замовлення вже не можна скасувати самостійно")
        order.status = "cancelled"
        await session.commit()
        return {"order": order_payload(order)}


@app.post("/api/admin/finance/orders/{order_id}/refund")
async def refund_wallet_order(order_id: int, body: AdminDecisionBody, request: Request):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        order = await session.scalar(select(WebOrder).where(WebOrder.id == order_id).with_for_update())
        if not order:
            raise HTTPException(status_code=404, detail="Замовлення не знайдено")
        if order.payment_method != "wallet" or order.status in {"cancelled", "refunded"}:
            raise HTTPException(status_code=409, detail="Повернення доступне лише для оплаченого з балансу замовлення")
        user_wallet = await get_or_create_wallet(session, order.account_id, lock=True)
        system_wallet = await get_or_create_wallet(session, system=True, lock=True)
        tx_id = await post_balanced_transaction(session, user_wallet, system_wallet, money_value(order.total),
            "REFUND", order.order_number, body.note.strip() or "Повернення за замовлення")
        order.status = "refunded"
        await session.commit()
        return {"ok": True, "transaction_id": tx_id, "balance": str(user_wallet.balance)}


# ----------------------- ADMIN / DEVELOPER / SUPPORT (PART 6) -----------------------
async def audit_admin(session, actor: WebAccount, action: str, target: str = "", details: str = ""):
    session.add(WebAdminAudit(actor_id=actor.id, action=action, target=target, details=details[:1000]))

@app.get("/api/admin/overview")
async def admin_overview(request: Request):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        accounts = await session.scalar(select(func.count(WebAccount.id))) or 0
        orders = await session.scalar(select(func.count(WebOrder.id))) or 0
        products_count = await session.scalar(select(func.count(Product.id))) or 0
        pending = await session.scalar(select(func.count(WebOrder.id)).where(WebOrder.status.in_(["awaiting_payment", "awaiting_confirmation"]))) or 0
        revenue = await session.scalar(select(func.coalesce(func.sum(WebOrder.total), 0)).where(WebOrder.status.in_(["processing", "completed"]))) or 0
        topups = await session.scalar(select(func.count(WebTopUpRequest.id)).where(WebTopUpRequest.status == "payment_reported")) or 0
        dev_requests = await session.scalar(select(func.count(WebDeveloperRequest.id)).where(WebDeveloperRequest.status == "pending")) or 0
        return {"accounts": accounts, "orders": orders, "products": products_count, "pending_orders": pending, "gross_orders_total": str(revenue), "topups_to_review": topups, "developer_requests": dev_requests}

@app.get("/api/admin/users")
async def admin_users(request: Request, q: str = ""):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        stmt = select(WebAccount).order_by(WebAccount.created_at.desc())
        if q.strip(): stmt = stmt.where((WebAccount.email.ilike(f"%{q[:100]}%")) | (WebAccount.display_name.ilike(f"%{q[:100]}%")))
        rows = (await session.scalars(stmt.limit(100))).all()
        return {"items": [account_data(x) | {"is_active": x.is_active} for x in rows]}

@app.patch("/api/admin/users/{account_id}/role")
async def admin_set_role(account_id: int, body: RoleBody, request: Request):
    actor = await current_account(request); finance_admin(actor)
    if actor.role != "owner": raise HTTPException(403, "Змінювати ролі може лише власник")
    async with Session() as session:
        target = await session.get(WebAccount, account_id)
        if not target: raise HTTPException(404, "Акаунт не знайдено")
        if target.role == "owner": raise HTTPException(403, "Роль власника не змінюється через цю панель")
        target.role = body.role
        await audit_admin(session, actor, "user.role_changed", str(account_id), body.role)
        await session.commit()
        return {"ok": True, "account": account_data(target)}

@app.post("/api/admin/users/{account_id}/active")
async def admin_toggle_user(account_id: int, request: Request):
    actor = await current_account(request); finance_admin(actor)
    if actor.role != "owner": raise HTTPException(403, "Блокувати акаунти може лише власник")
    async with Session() as session:
        target = await session.get(WebAccount, account_id)
        if not target or target.role == "owner": raise HTTPException(404, "Акаунт не знайдено або його не можна блокувати")
        target.is_active = not target.is_active
        await audit_admin(session, actor, "user.active_toggled", str(account_id), str(target.is_active))
        await session.commit()
        return {"ok": True, "is_active": target.is_active}

@app.get("/api/admin/orders")
async def admin_orders(request: Request, status: str = ""):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        stmt = select(WebOrder).order_by(WebOrder.created_at.desc())
        if status: stmt = stmt.where(WebOrder.status == status)
        rows = (await session.scalars(stmt.limit(150))).all()
        return {"items": [order_payload(x) for x in rows]}

@app.patch("/api/admin/orders/{order_id}/status")
async def admin_update_order(order_id: int, body: OrderStatusBody, request: Request):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        order = await session.get(WebOrder, order_id)
        if not order: raise HTTPException(404, "Замовлення не знайдено")
        if order.payment_method == "wallet" and body.status == "refunded":
            raise HTTPException(409, "Для wallet-замовлення використай окрему дію повернення коштів")
        old = order.status; order.status = body.status
        await audit_admin(session, actor, "order.status_changed", order.order_number, f"{old} -> {body.status}; {body.note}")
        await session.commit()
        return {"ok": True, "order": order_payload(order)}

@app.get("/api/admin/products")
async def admin_products(request: Request):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        rows = (await session.scalars(select(Product).order_by(Product.created_at.desc()).limit(200))).all()
        return {"items": [{"id":x.id,"title":x.title,"description":x.description,"price":str(x.price),"category_id":x.category_id,"delivery_text":x.delivery_text,"image_url":x.photo_file_id,"active":x.active,"sales":x.sales} for x in rows]}

@app.post("/api/admin/products", status_code=201)
async def admin_create_product(body: ProductAdminBody, request: Request):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        item = Product(title=body.title.strip(), description=body.description, price=float(body.price), category_id=body.category_id, delivery_text=body.delivery_text, photo_file_id=body.image_url, active=body.active)
        session.add(item); await session.flush(); await audit_admin(session, actor, "product.created", str(item.id), item.title); await session.commit()
        return {"ok": True, "id": item.id}

@app.patch("/api/admin/products/{product_id}")
async def admin_edit_product(product_id: int, body: ProductAdminBody, request: Request):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        item = await session.get(Product, product_id)
        if not item: raise HTTPException(404, "Товар не знайдено")
        item.title=body.title.strip(); item.description=body.description; item.price=float(body.price); item.category_id=body.category_id; item.delivery_text=body.delivery_text; item.photo_file_id=body.image_url; item.active=body.active
        await audit_admin(session, actor, "product.updated", str(item.id), item.title); await session.commit()
        return {"ok": True}

@app.get("/api/admin/developer-requests")
async def admin_developer_requests(request: Request):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        rows = (await session.scalars(select(WebDeveloperRequest).order_by(WebDeveloperRequest.created_at.desc()).limit(100))).all()
        result=[]
        for x in rows:
            acc=await session.get(WebAccount,x.account_id)
            result.append({"id":x.id,"account_id":x.account_id,"email":acc.email if acc else "","name":acc.display_name if acc else "","skills":x.skills,"experience":x.experience,"portfolio_url":x.portfolio_url,"status":x.status,"admin_note":x.admin_note})
        return {"items":result}

@app.post("/api/admin/developer-requests/{request_id}/decision")
async def admin_developer_decision(request_id: int, body: AdminDecisionBody, request: Request):
    actor=await current_account(request); finance_admin(actor)
    async with Session() as session:
        item=await session.get(WebDeveloperRequest,request_id)
        if not item: raise HTTPException(404,"Заявку не знайдено")
        acc=await session.get(WebAccount,item.account_id)
        item.status="approved"; item.admin_note=body.note; item.reviewed_at=datetime.utcnow()
        if acc: acc.role="developer"
        await audit_admin(session,actor,"developer.approved",str(request_id),body.note); await session.commit()
        return {"ok":True}

@app.post("/api/developer/apply", status_code=201)
async def developer_apply(body: DeveloperRequestBody, request: Request):
    account=await current_account(request)
    async with Session() as session:
        existing=await session.scalar(select(WebDeveloperRequest).where(WebDeveloperRequest.account_id==account.id,WebDeveloperRequest.status=="pending"))
        if existing: raise HTTPException(409,"У тебе вже є заявка на розгляд")
        item=WebDeveloperRequest(account_id=account.id,skills=body.skills.strip(),experience=body.experience,portfolio_url=body.portfolio_url)
        session.add(item); await session.commit(); return {"ok":True,"id":item.id,"status":item.status}

@app.get("/api/developer/summary")
async def developer_summary(request: Request):
    account=await current_account(request)
    if account.role!="developer": raise HTTPException(403,"Панель доступна лише розробникам")
    async with Session() as session:
        orders=await session.scalar(select(func.count(WebOrder.id)).where(WebOrder.email==account.email)) or 0
        return {"role":account.role,"orders_associated":orders,"message":"Розділ заявок та виплат для розробників буде розширено після налаштування призначення вебзамовлень."}

@app.post("/api/support/tickets", status_code=201)
async def create_support_ticket(body: SupportBody, request: Request):
    account=await current_account(request)
    async with Session() as session:
        item=WebSupportTicket(account_id=account.id,subject=body.subject.strip(),message=body.message.strip())
        session.add(item); await session.commit(); return {"ok":True,"id":item.id,"status":item.status}

@app.get("/api/support/tickets")
async def my_support_tickets(request: Request):
    account=await current_account(request)
    async with Session() as session:
        rows=(await session.scalars(select(WebSupportTicket).where(WebSupportTicket.account_id==account.id).order_by(WebSupportTicket.created_at.desc()).limit(50))).all()
        return {"items":[{"id":x.id,"subject":x.subject,"message":x.message,"status":x.status,"admin_reply":x.admin_reply,"created_at":x.created_at.isoformat()} for x in rows]}

@app.get("/api/admin/support/tickets")
async def admin_support_tickets(request: Request):
    actor=await current_account(request); finance_admin(actor)
    async with Session() as session:
        rows=(await session.scalars(select(WebSupportTicket).order_by(WebSupportTicket.created_at.desc()).limit(150))).all(); result=[]
        for x in rows:
            acc=await session.get(WebAccount,x.account_id); result.append({"id":x.id,"email":acc.email if acc else "","subject":x.subject,"message":x.message,"status":x.status,"admin_reply":x.admin_reply})
        return {"items":result}

@app.patch("/api/admin/support/tickets/{ticket_id}")
async def admin_reply_ticket(ticket_id: int, body: SupportReplyBody, request: Request):
    actor=await current_account(request); finance_admin(actor)
    async with Session() as session:
        item=await session.get(WebSupportTicket,ticket_id)
        if not item: raise HTTPException(404,"Звернення не знайдено")
        item.status=body.status; item.admin_reply=body.reply; await audit_admin(session,actor,"support.replied",str(ticket_id),body.status); await session.commit(); return {"ok":True}

@app.get("/api/owner/terminal")
async def owner_terminal_info(request: Request):
    actor=await current_account(request)
    if actor.role!="owner": raise HTTPException(403,"Owner Terminal доступний лише власникам")
    return {"commands":["#stats","#finance","#user ID","#balance ID","#order ORDER","#status ORDER STATUS","#reconcile","#logs"]}

@app.post("/api/owner/terminal")
async def owner_terminal_command(body: AdminDecisionBody, request: Request):
    actor=await current_account(request)
    if actor.role!="owner": raise HTTPException(403,"Owner Terminal доступний лише власникам")
    command=body.note.strip(); parts=command.split()
    if not parts or not parts[0].startswith("#"): raise HTTPException(422,"Команда має починатися з #")
    async with Session() as session:
        if parts[0]=="#stats":
            data={"accounts":await session.scalar(select(func.count(WebAccount.id))) or 0,"orders":await session.scalar(select(func.count(WebOrder.id))) or 0,"products":await session.scalar(select(func.count(Product.id))) or 0}
            return {"command":command,"result":data}
        if parts[0]=="#finance":
            data={"orders_total":str(await session.scalar(select(func.coalesce(func.sum(WebOrder.total),0))) or 0),"wallets":await session.scalar(select(func.count(WebWallet.id))) or 0,"ledger_entries":await session.scalar(select(func.count(WebLedgerEntry.id))) or 0}
            return {"command":command,"result":data}
        if parts[0]=="#user" and len(parts)==2 and parts[1].isdigit():
            acc=await session.get(WebAccount,int(parts[1])); return {"command":command,"result":account_data(acc) if acc else "Користувача не знайдено."}
        if parts[0]=="#order" and len(parts)==2:
            order=await session.scalar(select(WebOrder).where(WebOrder.order_number==parts[1])); return {"command":command,"result":order_payload(order) if order else "Замовлення не знайдено."}
        if parts[0]=="#balance" and len(parts)==2 and parts[1].isdigit():
            wallet=await session.scalar(select(WebWallet).where(WebWallet.account_id==int(parts[1])))
            return {"command":command,"result":wallet_data(wallet) if wallet else "Гаманець ще не створено."}
        if parts[0]=="#reconcile":
            rows=(await session.execute(select(WebLedgerEntry.transaction_id,func.sum(WebLedgerEntry.amount)).group_by(WebLedgerEntry.transaction_id))).all()
            bad=[tx for tx,total in rows if money_value(total or 0)!=Decimal("0.00")]
            return {"command":command,"result":{"transactions_checked":len(rows),"unbalanced_transactions":bad,"ok":not bad}}
        if parts[0]=="#status" and len(parts)==3 and parts[2] in {"awaiting_payment","awaiting_confirmation","processing","completed","cancelled","rejected"}:
            order=await session.scalar(select(WebOrder).where(WebOrder.order_number==parts[1]))
            if not order: raise HTTPException(404,"Замовлення не знайдено")
            old=order.status; order.status=parts[2]; await audit_admin(session,actor,"owner.terminal.status",order.order_number,f"{old}->{order.status}"); await session.commit(); return {"command":command,"result":order_payload(order)}
        if parts[0]=="#logs":
            rows=(await session.scalars(select(WebAdminAudit).order_by(WebAdminAudit.created_at.desc()).limit(30))).all(); return {"command":command,"result":[{"action":x.action,"target":x.target,"details":x.details,"created_at":x.created_at.isoformat()} for x in rows]}
        raise HTTPException(422,"Невідома команда або неправильні аргументи. Перевір список команд")


class CategoryBody(BaseModel):
    name: str = Field(min_length=2, max_length=100)


@app.post("/api/admin/categories", status_code=201)
async def admin_create_category(body: CategoryBody, request: Request):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        name = body.name.strip()
        row = await session.scalar(select(Category).where(Category.name == name))
        if not row:
            row = Category(name=name)
            session.add(row)
            await session.flush()
            await audit_admin(session, actor, "category.created", str(row.id), name)
            await session.commit()
        return {"ok": True, "id": row.id}


# ----------------------- ЗАЯВКИ НА РОЗРОБКУ (як у боті) -----------------------
DEV_OPEN = ("new", "discuss", "work")
DEV_FINAL = ("done", "cancelled")
MAX_OPEN_DEV = 3
DEV_STAFF = {"admin", "owner", "developer"}


class DevOrderBody(BaseModel):
    description: str = Field(min_length=5, max_length=3000)
    links: str = Field(default="", max_length=1500)
    wishes: str = Field(default="", max_length=2000)
    deadline: str = Field(default="", max_length=200)
    budget: str = Field(default="", max_length=200)


class DevStatusBody(BaseModel):
    status: str = Field(pattern="^(discuss|work)$")


class DevQuoteBody(BaseModel):
    amount: Decimal = Field(gt=Decimal("0"), lt=Decimal("10000000"), max_digits=12, decimal_places=2)


class DevPayBody(BaseModel):
    verdict: str = Field(pattern="^(ok|no)$")


class DevDeliverBody(BaseModel):
    text: str = Field(min_length=2, max_length=5000)


def dev_staff(account: WebAccount):
    if account.role not in DEV_STAFF:
        raise HTTPException(status_code=403, detail="Доступ лише для команди розробки")


def dev_payload(o: WebDevOrder, email: str = "") -> dict:
    return {"id": o.id, "status": o.status, "pay_state": o.pay_state, "description": o.description,
            "links": o.links, "wishes": o.wishes, "deadline": o.deadline, "budget": o.budget,
            "assignee_id": o.assignee_id, "assignee_name": o.assignee_name,
            "price": str(o.price) if o.price is not None else None, "email": email,
            "result_text": o.result_text if o.status == "done" else "",
            "created_at": o.created_at.isoformat() if o.created_at else None}


async def dev_get(session, order_id: int, account_id: int | None = None) -> WebDevOrder:
    stmt = select(WebDevOrder).where(WebDevOrder.id == order_id).with_for_update()
    if account_id is not None:
        stmt = stmt.where(WebDevOrder.account_id == account_id)
    order = await session.scalar(stmt)
    if not order:
        raise HTTPException(status_code=404, detail="Заявку не знайдено")
    return order


def dev_not_final(o: WebDevOrder):
    if o.status in DEV_FINAL:
        raise HTTPException(status_code=409, detail="Заявку завершено, змінювати її вже не можна")


@app.post("/api/dev/orders", status_code=201)
async def dev_create(body: DevOrderBody, request: Request):
    account = await current_account(request)
    async with Session() as session:
        open_count = await session.scalar(select(func.count(WebDevOrder.id)).where(
            WebDevOrder.account_id == account.id, WebDevOrder.status.in_(DEV_OPEN))) or 0
        if open_count >= MAX_OPEN_DEV:
            raise HTTPException(status_code=409, detail=f"Вже є {MAX_OPEN_DEV} активні заявки. Дочекайся їх виконання або напиши в підтримку")
        order = WebDevOrder(account_id=account.id, description=body.description.strip(), links=body.links.strip(),
                            wishes=body.wishes.strip(), deadline=body.deadline.strip(), budget=body.budget.strip())
        session.add(order)
        await session.commit()
        await session.refresh(order)
        return {"order": dev_payload(order)}


@app.get("/api/dev/orders")
async def dev_mine(request: Request):
    account = await current_account(request)
    async with Session() as session:
        rows = (await session.scalars(select(WebDevOrder).where(WebDevOrder.account_id == account.id)
                .order_by(desc(WebDevOrder.id)).limit(50))).all()
        return {"items": [dev_payload(x) for x in rows]}


@app.get("/api/dev/orders/{order_id}")
async def dev_one(order_id: int, request: Request):
    account = await current_account(request)
    async with Session() as session:
        order = await dev_get(session, order_id, account.id)
        return {"order": dev_payload(order), "payment_instructions": os.getenv("PAYMENT_INSTRUCTIONS", "Відскануй QR у застосунку банку, введи суму і вкажи номер заявки в коментарі")}


@app.post("/api/dev/orders/{order_id}/report-payment")
async def dev_report_payment(order_id: int, request: Request):
    account = await current_account(request)
    async with Session() as session:
        order = await dev_get(session, order_id, account.id)
        dev_not_final(order)
        if order.pay_state != "sent":
            raise HTTPException(status_code=409, detail="Оплата зараз не очікується")
        order.pay_state = "reported"
        await session.commit()
        return {"ok": True, "message": "Передано розробнику, він перевірить надходження"}


@app.get("/api/staff/dev/orders")
async def dev_staff_list(request: Request, status: str = ""):
    actor = await current_account(request); dev_staff(actor)
    async with Session() as session:
        stmt = select(WebDevOrder).order_by(desc(WebDevOrder.id))
        if status:
            stmt = stmt.where(WebDevOrder.status == status)
        rows = (await session.scalars(stmt.limit(100))).all()
        items = []
        for x in rows:
            acc = await session.get(WebAccount, x.account_id)
            items.append(dev_payload(x, acc.email if acc else "") | {"client": acc.display_name if acc else ""})
        return {"items": items}


@app.post("/api/staff/dev/orders/{order_id}/take")
async def dev_take(order_id: int, request: Request):
    actor = await current_account(request); dev_staff(actor)
    async with Session() as session:
        o = await dev_get(session, order_id); dev_not_final(o)
        if o.assignee_id and o.assignee_id != actor.id and actor.role == "developer":
            raise HTTPException(status_code=409, detail=f"Її вже веде {o.assignee_name}")
        o.assignee_id, o.assignee_name = actor.id, actor.display_name[:100]
        await audit_admin(session, actor, "dev.take", str(order_id))
        await session.commit()
        return {"ok": True}


@app.post("/api/staff/dev/orders/{order_id}/status")
async def dev_set_status(order_id: int, body: DevStatusBody, request: Request):
    actor = await current_account(request); dev_staff(actor)
    async with Session() as session:
        o = await dev_get(session, order_id); dev_not_final(o)
        o.status = body.status
        if not o.assignee_id:
            o.assignee_id, o.assignee_name = actor.id, actor.display_name[:100]
        await audit_admin(session, actor, "dev.status", str(order_id), body.status)
        await session.commit()
        return {"ok": True}


@app.post("/api/staff/dev/orders/{order_id}/quote")
async def dev_quote(order_id: int, body: DevQuoteBody, request: Request):
    actor = await current_account(request); dev_staff(actor)
    async with Session() as session:
        o = await dev_get(session, order_id); dev_not_final(o)
        if o.pay_state == "paid":
            raise HTTPException(status_code=409, detail="Ця заявка вже оплачена")
        o.price, o.pay_state = money_value(body.amount), "sent"
        if not o.assignee_id:
            o.assignee_id, o.assignee_name = actor.id, actor.display_name[:100]
        await audit_admin(session, actor, "dev.quote", str(order_id), str(o.price))
        await session.commit()
        return {"ok": True}


@app.post("/api/staff/dev/orders/{order_id}/payment")
async def dev_payment(order_id: int, body: DevPayBody, request: Request):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        o = await dev_get(session, order_id); dev_not_final(o)
        if o.pay_state != "reported":
            raise HTTPException(status_code=409, detail="Це вже оброблено")
        o.pay_state = "paid" if body.verdict == "ok" else "sent"
        await audit_admin(session, actor, "dev.pay_" + body.verdict, str(order_id))
        await session.commit()
        return {"ok": True}


@app.post("/api/staff/dev/orders/{order_id}/deliver")
async def dev_deliver(order_id: int, body: DevDeliverBody, request: Request):
    actor = await current_account(request); dev_staff(actor)
    async with Session() as session:
        o = await dev_get(session, order_id); dev_not_final(o)
        if o.pay_state != "paid":
            raise HTTPException(status_code=409, detail="Видати результат можна лише після оплати")
        o.result_text, o.status = body.text.strip(), "done"
        await audit_admin(session, actor, "dev.delivered", str(order_id))
        await session.commit()
        return {"ok": True}


@app.post("/api/staff/dev/orders/{order_id}/cancel")
async def dev_cancel(order_id: int, request: Request):
    actor = await current_account(request); finance_admin(actor)
    async with Session() as session:
        o = await dev_get(session, order_id); dev_not_final(o)
        o.status = "cancelled"
        await audit_admin(session, actor, "dev.cancelled", str(order_id))
        await session.commit()
        return {"ok": True}
