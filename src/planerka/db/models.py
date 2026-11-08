"""Модель данных. Статусы хранятся метками для людей (TaskStatus.value)."""
import warnings
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Integer, String, Text,
    Index, UniqueConstraint, func, text,
)
from sqlalchemy.exc import SAWarning
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Изменения списков и словарей на месте (append, ключ) видны SQLAlchemy.
TextList = MutableList.as_mutable(ARRAY(Text))
IntList = MutableList.as_mutable(ARRAY(Integer))
JsonDict = MutableDict.as_mutable(JSONB)
Timestamp = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = "employee"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(Text)
    name_variants: Mapped[list[str]] = mapped_column(
        TextList, server_default=text("'{}'"), default=list)
    email: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("employee.id"))
    access_level: Mapped[str | None] = mapped_column(Text)


class ChannelId(Base):
    __tablename__ = "channel_id"
    __table_args__ = (UniqueConstraint("channel", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"))
    channel: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)


class Source(Base):
    __tablename__ = "source"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    received_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    meeting_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text)
    closed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), default=False)
    summary: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[dict[str, Any]] = mapped_column(
        JsonDict, server_default=text("'{}'::jsonb"), default=dict)


class Token(Base):
    __tablename__ = "token"
    __table_args__ = (
        # Токены сотрудников (source_id NULL) глобально уникальны, внешних людей
        # и контактов — в пределах источника.
        UniqueConstraint("source_id", "token", postgresql_nulls_not_distinct=True),
        # Один постоянный токен на сотрудника.
        Index("uq_token_employee_permanent", "employee_id", unique=True,
              postgresql_where=text("source_id IS NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employee.id"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id"))
    surface: Mapped[str | None] = mapped_column(Text)


# Task.registry — осознанное имя поля из спеки; SQLAlchemy считает `registry` зарезервированным.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="Attribute name 'registry'", category=SAWarning)

    class Task(Base):
        __tablename__ = "task"

        id: Mapped[int] = mapped_column(primary_key=True)
        number: Mapped[int] = mapped_column(Integer, unique=True)
        title: Mapped[str] = mapped_column(Text)
        type: Mapped[str] = mapped_column(Text)
        priority: Mapped[str | None] = mapped_column(Text)
        goal_2027: Mapped[str | None] = mapped_column(Text)
        author_id: Mapped[int | None] = mapped_column(ForeignKey("employee.id"))
        owner_id: Mapped[int | None] = mapped_column(ForeignKey("employee.id"))
        coexecutor_ids: Mapped[list[int]] = mapped_column(
            IntList, server_default=text("'{}'"), default=list)
        due_date: Mapped[date | None] = mapped_column(Date)
        control_date: Mapped[date | None] = mapped_column(Date)
        done_criteria: Mapped[str | None] = mapped_column(Text)
        source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id"))
        timecode: Mapped[str | None] = mapped_column(Text)
        quote: Mapped[str | None] = mapped_column(Text)
        confidence: Mapped[str | None] = mapped_column(Text)
        confidence_reason: Mapped[str | None] = mapped_column(Text)
        questions: Mapped[list[str]] = mapped_column(
            TextList, server_default=text("'{}'"), default=list)
        flags: Mapped[list[str]] = mapped_column(
            TextList, server_default=text("'{}'"), default=list)
        source_closed: Mapped[bool] = mapped_column(
            Boolean, server_default=text("false"), default=False)
        registry: Mapped[str] = mapped_column(Text, server_default="общий", default="общий")
        registry_suggested: Mapped[str | None] = mapped_column(Text)
        status: Mapped[str] = mapped_column(Text)
        created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
        updated_at: Mapped[datetime] = mapped_column(
            Timestamp, server_default=func.now(), onupdate=func.now())


class TaskEdit(Base):
    __tablename__ = "task_edit"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_number: Mapped[int] = mapped_column(Integer)
    change: Mapped[str] = mapped_column(Text)
    quote: Mapped[str | None] = mapped_column(Text)
    timecode: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id"))
    status: Mapped[str] = mapped_column(Text)


class History(Base):
    __tablename__ = "history"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("task.id"))
    event: Mapped[str] = mapped_column(Text)
    old: Mapped[str | None] = mapped_column(Text)
    new: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    changed_by: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class Job(Base):
    __tablename__ = "job"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JsonDict, server_default=text("'{}'::jsonb"), default=dict)
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    run_after: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(Timestamp, server_default=func.now())


class ReviewFile(Base):
    __tablename__ = "review_file"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(Text)  # 'executor' | 'owner'
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employee.id"))
    path: Mapped[str] = mapped_column(Text)
    task_numbers: Mapped[list[int]] = mapped_column(
        IntList, server_default=text("'{}'"), default=list)
    sent_at: Mapped[datetime | None] = mapped_column(Timestamp)
    due_at: Mapped[datetime | None] = mapped_column(Timestamp)
    reminded_at: Mapped[datetime | None] = mapped_column(Timestamp)
    escalated_at: Mapped[datetime | None] = mapped_column(Timestamp)
    returned_at: Mapped[datetime | None] = mapped_column(Timestamp)
    message_id: Mapped[str | None] = mapped_column(Text)
