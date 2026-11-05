from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, String, Text, Float, Numeric, UniqueConstraint, select, func)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from config import DATABASE_URL, IS_SQLITE, SQLITE_PATH, ENGINE_ARGS, SEED_DEMO

class Base(DeclarativeBase):
    pass

def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)

class WebAccount(Base):
    """Обліковий запис сайту. Відокремлений від Telegram User, щоб вебреєстрація не вимагала Telegram ID."""
    __tablename__ = "web_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    display_name: Mapped[str] = mapped_column(String(80), default="")
    role: Mapped[str] = mapped_column(String(24), default="customer", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)



class WebCartItem(Base):
    """Кошик вебакаунта; окремий від Telegram-кошика."""
    __tablename__ = "web_cart_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("web_accounts.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("account_id", "product_id"),)

class WebOrder(Base):
    """Замовлення, створене через вебсайт; не змішується з Telegram Purchase."""
    __tablename__ = "web_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("web_accounts.id", ondelete="RESTRICT"), index=True)
    customer_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(254))
    phone: Mapped[str] = mapped_column(String(40), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    payment_method: Mapped[str] = mapped_column(String(32), default="manual_transfer")
    status: Mapped[str] = mapped_column(String(32), default="awaiting_payment", index=True)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    payment_reported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class WebOrderItem(Base):
    """Незмінний знімок товару та ціни на момент оформлення вебзамовлення."""
    __tablename__ = "web_order_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("web_orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    product_title: Mapped[str] = mapped_column(String(160))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class WebWallet(Base):
    """Гаманець вебакаунта або системний розрахунковий гаманець."""
    __tablename__ = "web_wallets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("web_accounts.id", ondelete="CASCADE"), nullable=True, unique=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(3), default="UAH")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class WebLedgerEntry(Base):
    """Незмінний запис бухгалтерського журналу; сума зі знаком."""
    __tablename__ = "web_ledger_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[str] = mapped_column(String(40), index=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("web_wallets.id", ondelete="RESTRICT"), index=True)
    entry_type: Mapped[str] = mapped_column(String(32), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    reference: Mapped[str] = mapped_column(String(80), default="")
    description: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    __table_args__ = (UniqueConstraint("transaction_id", "wallet_id"),)


class WebTopUpRequest(Base):
    """Заявка на ручне поповнення вебгаманця."""
    __tablename__ = "web_topup_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("web_accounts.id", ondelete="RESTRICT"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    payment_method: Mapped[str] = mapped_column(String(24), default="manual_transfer")
    status: Mapped[str] = mapped_column(String(24), default="awaiting_payment", index=True)
    customer_note: Mapped[str] = mapped_column(String(500), default="")
    admin_note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    payment_reported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WebFavorite(Base):
    """Обране вебакаунта; не змішується з Telegram Favorite."""
    __tablename__ = "web_favorites"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("web_accounts.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("account_id", "product_id"),)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[float] = mapped_column(Float)
    delivery_text: Mapped[str] = mapped_column(Text, default="")
    photo_file_id: Mapped[str] = mapped_column(String(300), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sales: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)  # собівартість (для бухгалтерії); 0 = не вказана
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True, **ENGINE_ARGS)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

DEMO = [
 ("Шаблони", [
   ("Стартовий шаблон сайту", "Адаптивний шаблон лендингу на HTML і CSS. Готовий до публікації на будь-якому хостингу.", 299, "Посилання на архів надішле менеджер після підтвердження оплати"),
   ("Пакет іконок для інтерфейсів", "Понад 300 векторних іконок у форматах SVG і PNG для сайтів і застосунків.", 149, "Посилання на архів надішле менеджер після підтвердження оплати")]),
 ("Roblox", [
   ("Система інвентарю для Roblox", "Готовий модуль інвентарю на Luau з збереженням даних і зрозумілою документацією.", 499, "Файл і інструкцію надішле менеджер після підтвердження оплати")]),
 ("Python", [
   ("Скрипт автоматизації звітів", "Python скрипт, який збирає дані з таблиць і формує звіт. Із прикладом налаштування.", 399, "Файл і інструкцію надішле менеджер після підтвердження оплати")]),
]

async def seed_demo():
    async with Session() as s:
        if await s.scalar(select(func.count(Product.id))):
            return
        for cat_name, items in DEMO:
            cat = await s.scalar(select(Category).where(Category.name == cat_name))
            if not cat:
                cat = Category(name=cat_name); s.add(cat); await s.flush()
            for title, desc, price, delivery in items:
                s.add(Product(category_id=cat.id, title=title, description=desc, price=price, delivery_text=delivery))
        await s.commit()

async def init_db():
    if SQLITE_PATH:
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    if SEED_DEMO:
        await seed_demo()

class WebSupportTicket(Base):
    __tablename__ = "web_support_tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("web_accounts.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(160))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    admin_reply: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class WebDeveloperRequest(Base):
    __tablename__ = "web_developer_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("web_accounts.id", ondelete="CASCADE"), index=True)
    skills: Mapped[str] = mapped_column(String(500), default="")
    experience: Mapped[str] = mapped_column(Text, default="")
    portfolio_url: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    admin_note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WebAdminAudit(Base):
    __tablename__ = "web_admin_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    target: Mapped[str] = mapped_column(String(100), default="")
    details: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class WebDevOrder(Base):
    """Заявка на індивідуальну розробку з сайту."""
    __tablename__ = "web_dev_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("web_accounts.id", ondelete="CASCADE"), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    links: Mapped[str] = mapped_column(Text, default="")
    wishes: Mapped[str] = mapped_column(Text, default="")
    deadline: Mapped[str] = mapped_column(String(200), default="")
    budget: Mapped[str] = mapped_column(String(200), default="")
    assignee_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assignee_name: Mapped[str] = mapped_column(String(100), default="")
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    pay_state: Mapped[str] = mapped_column(String(16), default="")
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    result_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
