from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Index, Integer, String,
                        UniqueConstraint, inspect, select, text)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import DATABASE_URL


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite saat dilimini kaybeder; her zaman UTC-aware döndür."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class ManagedBot(Base):
    __tablename__ = "bots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    token: Mapped[str] = mapped_column(String(120))
    username: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # {"msg": storage_msg_id, "buttons": [[{text,url,style}]]}
    welcome: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # {"rows": [["A","B"],["C"]], "responses": {"A": {"msg": id, "buttons": [...]}}}
    menu: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    auto_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    promo_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)


class Subscriber(Base):
    __tablename__ = "subscribers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lang: Mapped[str | None] = mapped_column(String(10), nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="organik")
    status: Mapped[str] = mapped_column(String(10), default="active")  # active / blocked / unsub
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promo_last: Mapped[int | None] = mapped_column(Integer, nullable=True)  # son gösterilen promo id
    __table_args__ = (
        UniqueConstraint("bot_id", "user_id", name="uq_bot_user"),
        Index("ix_sub_bot_status", "bot_id", "status"),
        Index("ix_sub_bot_joined", "bot_id", "joined_at"),
    )


class Broadcast(Base):
    __tablename__ = "broadcasts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    msg: Mapped[int] = mapped_column(Integer)  # depo kanalındaki (ilk) mesaj ID
    msg_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)  # albümse tüm ID'ler
    btn_text: Mapped[str | None] = mapped_column(String(512), nullable=True)  # albüm altı buton mesajı yazısı
    buttons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    unsub_btn: Mapped[bool] = mapped_column(Boolean, default=True)
    bot_ids: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(12), default="queued")  # scheduled/queued/running/done/cancelled/deleted
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {bot_id: {total,sent,blocked,failed}}


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broadcast_id: Mapped[int] = mapped_column(Integer, index=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(Integer)


class LibItem(Base):
    """Promo kütüphanesi: bir kere kaydedilir, botların zincir/promo listelerine bağlanır."""
    __tablename__ = "library"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(64), default="")
    msgs: Mapped[list] = mapped_column(JSON)
    buttons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    btn_text: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Item(Base):
    """Otomatik mesaj (kind='auto') veya promo (kind='promo')."""
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(10))
    position: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(64), default="")
    msgs: Mapped[list] = mapped_column(JSON)
    buttons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    btn_text: Mapped[str | None] = mapped_column(String(512), nullable=True)
    delay_sec: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    count: Mapped[int] = mapped_column(Integer, default=0)  # gönderim / görüntülenme
    lib_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # kütüphane bağlantısı
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AutoQueue(Base):
    """Gecikmeli otomatik mesaj kuyruğu."""
    __tablename__ = "auto_queue"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    item_id: Mapped[int] = mapped_column(Integer, index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(String(512), nullable=True)


class Link(Base):
    """Yayındaki her URL butonu (tıklama takibi için)."""
    __tablename__ = "links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broadcast_id: Mapped[int] = mapped_column(Integer, index=True)
    text: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(String(2048))


class Click(Base):
    __tablename__ = "clicks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    link_id: Mapped[int] = mapped_column(Integer, index=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


def _add_missing_columns(conn):
    """Basit otomatik migration: modelde olup tabloda olmayan kolonları ekler."""
    insp = inspect(conn)
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in existing:
                ctype = col.type.compile(dialect=conn.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ctype}'))


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


async def get_bot_row(bot_id: int) -> ManagedBot | None:
    async with Session() as s:
        return await s.get(ManagedBot, bot_id)


async def all_bots() -> list[ManagedBot]:
    async with Session() as s:
        return list((await s.scalars(select(ManagedBot).order_by(ManagedBot.created_at))).all())
