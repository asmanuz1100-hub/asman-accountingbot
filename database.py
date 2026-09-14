import json
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///asman.db").strip()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Base(DeclarativeBase):
    pass


class Partner(Base):
    __tablename__ = "partners"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    tin: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
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


def upsert_partner(name: str, tin: Optional[str] = None, partner_type: str = "customer") -> Partner:
    clean = (name or "").strip()
    if not clean:
        raise ValueError("Ҳамкор номи бўш бўлиши мумкин эмас")

    with Session(engine) as session:
        partner = session.scalar(select(Partner).where(func.lower(Partner.name) == clean.lower()))
        if partner:
            if tin and not partner.tin:
                partner.tin = tin
            session.commit()
            session.refresh(partner)
            return partner

        partner = Partner(name=clean, tin=tin, partner_type=partner_type)
        session.add(partner)
        session.commit()
        session.refresh(partner)
        return partner


def list_partners(limit: int = 50):
    with Session(engine) as session:
        return list(session.scalars(select(Partner).order_by(Partner.name).limit(limit)).all())


def add_contract(partner_name: str, number: str, total_amount: float,
                 contract_date: Optional[str] = None, currency: str = "UZS",
                 tin: Optional[str] = None) -> Contract:
    partner = upsert_partner(partner_name, tin=tin)
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
        material = session.scalar(select(Material).where(func.lower(Material.name) == clean.lower()))
        if material:
            material.qty += float(qty)
            if unit:
                material.unit = unit
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
        partners = session.scalar(select(func.count()).select_from(Partner)) or 0
        contracts = session.scalar(select(func.count()).select_from(Contract)) or 0
        materials = session.scalar(select(func.count()).select_from(Material)) or 0
        documents = session.scalar(select(func.count()).select_from(Document)) or 0
        contract_balance = session.scalar(
            select(func.coalesce(func.sum(Contract.total_amount - Contract.used_amount), 0))
        ) or 0

        return {
            "partners": int(partners),
            "contracts": int(contracts),
            "materials": int(materials),
            "documents": int(documents),
            "contract_balance": float(contract_balance),
        }
