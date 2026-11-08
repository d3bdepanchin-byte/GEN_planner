"""Схема ответа LLM на разбор записи. Люди — токенами ([P7], [P3|P8], [X1]), даты — ISO."""
from datetime import date
from typing import Literal

from pydantic import BaseModel


class CandidateOut(BaseModel):
    title: str
    type: Literal['проект', 'поручение', 'регламент', 'разбирательство'] | None
    priority: Literal['A', 'B', 'C'] | None
    goal_2027: str | None
    owner: str | None  # токен
    coexecutors: list[str]  # токены
    author: str | None  # токен
    due_date: date | None
    control_date: date | None
    done_criteria: str | None
    timecode: str | None
    quote: str
    confidence: Literal['высокая', 'средняя', 'низкая']
    confidence_reason: str
    questions: list[str]
    from_final_block: bool
    closed_registry_suggested: bool


class EditOut(BaseModel):
    task_number: str  # «И-021»
    change: str
    quote: str
    timecode: str | None


class ExtractionResult(BaseModel):
    final_block_found: bool
    summary: str
    candidates: list[CandidateOut]
    edits: list[EditOut]
