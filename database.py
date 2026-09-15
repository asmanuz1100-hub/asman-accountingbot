import json
import os
import re
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, func, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///asman.db").strip()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _norm_tin(value: Optional[str]) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _norm_account(value: Optional[str]) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()


def _norm_name(value: Optional[str]) -> str:
    return " ".join(str(value or "").split()).strip().casefold()


def _partner_key(tin: Optional[str], account_number: Optional[str], name: Optional[str]) -> tuple[str, str]:
    tin_n = _norm_tin(tin)
    if tin_n:
        return ("tin", tin_n)
    account_n = _norm_account(account_number)
    if account_n:
        return ("account", account_n)
    return ("name", _norm_name(name))


def _merge_partner_type(current: str, new: str) -> str:
    current = (current or "auto").strip().lower()
    new = (new or "auto").strip().lower()
    if new == "auto":
        return current
    if current in ("auto", "customer"):
        return new
    if current == new or current == "both":
        return current
    if {current, new} <= {"incoming", "outgoing"}:
        return "both"
    return new


class Base(DeclarativeBase):
    pass


class Partner(Base):
    __tablename__ = "partners"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Name is display data. Identity is TIN first, bank account second.
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    tin: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    account_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    partner_type: Mapped[str] = mapped_column(String(32), default="customer")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Contract(Base):
    __tablename__ = "contracts"
    __table_args__ = (UniqueConstraint("partner_id", "number", name="uq_partner_contract"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"))
    number: Mapped[str] = mapped_column(String(128))
    contract_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    currency: Mapped[str] = mapped_column(String(16), default="UZS")
    total_amount: Mapped[float] = mapped_column(Float, default=0)
    used_amount: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Material(Base):
    __tablename__ = "materials"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    unit: Mapped[str] = mapped_column(String(32), default="kg")
    qty: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_file_id: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    document_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    partner_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    document_number: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    document_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    total_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


engine = create_engine(_db_url(), pool_pre_ping=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
    # Existing Render/Postgres databases need a lightweight migration because
    # create_all() does not add new columns to an existing table.
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("partners")}
        if "account_number" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE partners ADD COLUMN account_number VARCHAR(64)"))
    except Exception:
        # Fresh databases already have the column; keep startup resilient.
        pass


def upsert_partner(name: str, tin: Optional[str] = None, partner_type: str = "customer",
                   account_number: Optional[str] = None) -> Partner:
    clean = " ".join((name or "").split()).strip()
    if not clean:
        raise ValueError("Ҳамкор номи бўш бўлиши мумкин эмас")

    tin_n = _norm_tin(tin) or None
    account_n = _norm_account(account_number) or None

    with Session(engine) as session:
        partner = None

        # TIN is the strongest legal-entity identifier. This prevents the same
        # company appearing twice just because the name is written differently.
        if tin_n:
            partner = session.scalar(select(Partner).where(Partner.tin == tin_n).order_by(Partner.id))

        # If TIN is unavailable, bank account becomes the identity key.
        if partner is None and account_n:
            partner = session.scalar(
                select(Partner).where(Partner.account_number == account_n).order_by(Partner.id)
            )

        # Name is only a last-resort fallback when no strong identifier exists.
        if partner is None and not tin_n and not account_n:
            partner = session.scalar(select(Partner).where(func.lower(Partner.name) == clean.lower()))

        # Old rows may have been created by name before TIN/account support.
        if partner is None and (tin_n or account_n):
            legacy = session.scalar(select(Partner).where(func.lower(Partner.name) == clean.lower()))
            if legacy and not _norm_tin(legacy.tin) and not _norm_account(legacy.account_number):
                partner = legacy

        if partner:
            if tin_n and not _norm_tin(partner.tin):
                partner.tin = tin_n
            if account_n and not _norm_account(partner.account_number):
                partner.account_number = account_n
            partner.partner_type = _merge_partner_type(partner.partner_type, partner_type)
            session.commit()
            session.refresh(partner)
            return partner

        # Existing DBs still have a unique name constraint. In the rare case
        # two different legal entities have exactly the same display name,
        # keep the user-facing name readable while making the stored row unique.
        stored_name = clean
        same_name = session.scalar(select(Partner).where(func.lower(Partner.name) == clean.lower()))
        if same_name:
            suffix = tin_n or account_n or str(int(datetime.utcnow().timestamp()))
            stored_name = f"{clean} [{suffix[-8:]}]"[:255]

        partner = Partner(
            name=stored_name,
            tin=tin_n,
            account_number=account_n,
            partner_type=partner_type or "auto",
        )
        session.add(partner)
        session.commit()
        session.refresh(partner)
        return partner


def list_partners(limit: int = 50):
    with Session(engine) as session:
        rows = list(session.scalars(select(Partner).order_by(Partner.id)).all())

    # Hide legacy duplicates by TIN/account in all menus and reports.
    unique = {}
    for p in rows:
        key = _partner_key(p.tin, p.account_number, p.name)
        old = unique.get(key)
        if old is None:
            unique[key] = p
        elif not _norm_account(old.account_number) and _norm_account(p.account_number):
            unique[key] = p

    result = sorted(unique.values(), key=lambda p: p.name.casefold())
    return result[:limit]


def add_contract(partner_name: str, number: str, total_amount: float,
                 contract_date: Optional[str] = None, currency: str = "UZS",
                 tin: Optional[str] = None, account_number: Optional[str] = None) -> Contract:
    partner = upsert_partner(partner_name, tin=tin, account_number=account_number)
    with Session(engine) as session:
        existing = session.scalar(
            select(Contract).where(
                Contract.partner_id == partner.id,
                Contract.number == number,
            )
        )
        if existing:
            existing.total_amount = float(total_amount or 0)
            if contract_date:
                existing.contract_date = contract_date
            if currency:
                existing.currency = currency
            session.commit()
            session.refresh(existing)
            return existing

        contract = Contract(
            partner_id=partner.id,
            number=number,
            total_amount=float(total_amount or 0),
            contract_date=contract_date,
            currency=currency or "UZS",
        )
        session.add(contract)
        session.commit()
        session.refresh(contract)
        return contract


def list_contracts(limit: int = 50):
    with Session(engine) as session:
        return session.execute(
            select(Contract, Partner)
            .join(Partner, Contract.partner_id == Partner.id)
            .order_by(Contract.id.desc())
            .limit(limit)
        ).all()


def add_material(name: str, qty: float, unit: str = "kg") -> None:
    clean = (name or "").strip()
    if not clean:
        raise ValueError("Хом ашё номи бўш")

    with Session(engine) as session:
        material = next((m for m in session.scalars(select(Material)).all() if m.name.casefold() == clean.casefold()), None)
        if material:
            units = {"kg": ("mass", 1), "кг": ("mass", 1), "ton": ("mass", 1000), "t": ("mass", 1000), "тонна": ("mass", 1000), "т": ("mass", 1000), "g": ("mass", .001), "г": ("mass", .001)}
            old, new = units.get(material.unit.lower()), units.get((unit or material.unit).lower())
            if unit and unit.lower() != material.unit.lower():
                if not old or not new or old[0] != new[0]:
                    raise ValueError("Ўлчов бирликлари мос эмас")
                qty = float(qty) * new[1] / old[1]
            material.qty += float(qty)
        else:
            session.add(Material(name=clean, qty=float(qty), unit=unit or "kg"))
        session.commit()


def list_materials(limit: int = 100):
    with Session(engine) as session:
        return list(session.scalars(select(Material).order_by(Material.name).limit(limit)).all())


def save_document(data: dict, telegram_file_id: Optional[str], filename: Optional[str],
                  mime_type: Optional[str]) -> int:
    partner = data.get("partner") or {}
    document_number = data.get("invoice_number") or data.get("contract_number") or data.get("document_number")
    document_date = data.get("invoice_date") or data.get("contract_date") or data.get("document_date")

    total = data.get("total")
    try:
        total = float(total) if total is not None else None
    except (TypeError, ValueError):
        total = None

    with Session(engine) as session:
        document = Document(
            telegram_file_id=telegram_file_id,
            filename=filename,
            mime_type=mime_type,
            document_type=data.get("document_type"),
            partner_name=partner.get("name") if isinstance(partner, dict) else None,
            document_number=document_number,
            document_date=document_date,
            currency=data.get("currency"),
            total_amount=total,
            raw_json=json.dumps(data, ensure_ascii=False, default=str),
        )
        session.add(document)
        session.commit()
        session.refresh(document)
        return document.id


def report() -> dict:
    with Session(engine) as session:
        contracts = session.scalar(select(func.count()).select_from(Contract)) or 0
        materials = session.scalar(select(func.count()).select_from(Material)) or 0
        documents = session.scalar(select(func.count()).select_from(Document)) or 0
        contract_balance = session.scalar(
            select(func.coalesce(func.sum(Contract.total_amount - Contract.used_amount), 0))
        ) or 0

    return {
        "partners": len(list_partners(limit=100000)),
        "contracts": int(contracts),
        "materials": int(materials),
        "documents": int(documents),
        "contract_balance": float(contract_balance),
    }
