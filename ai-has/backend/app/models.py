from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now():
    return datetime.utcnow()


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20), default="employee")  # employee | agent | admin
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)  # pbkdf2_sha256$...
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | resolved | escalated
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)  # from the first redacted question
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    role: Mapped[str] = mapped_column(String(10))  # user | assistant | tool
    content: Mapped[str] = mapped_column(Text)  # PII-redacted before saving
    intent: Mapped[str | None] = mapped_column(String(40), nullable=True)
    citations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)  # answered | escalated | action_proposed | refused | clarify | blocked
    feedback: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # -1, 0 or 1
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)  # content is a placeholder
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    summary: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(10), default="medium")  # low | medium | high
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | in_progress | resolved
    source: Mapped[str] = mapped_column(String(20), default="ai")  # ai | escalation
    external_ref: Mapped[str | None] = mapped_column(String(60), nullable=True)  # e.g. Jira key
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # for MTTR


class KBDocument(Base):
    __tablename__ = "kb_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    source_path: Mapped[str] = mapped_column(String(300))
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(80))  # "user:<id>" or "ai", never an email
    action: Mapped[str] = mapped_column(String(60))  # e.g. reset_password, create_ticket
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PendingAction(Base):
    __tablename__ = "pending_actions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # secrets.token_urlsafe
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(40))  # create_ticket | request_password_reset
    arguments: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # validated, PII-redacted
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | confirmed | cancelled | done | failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)
