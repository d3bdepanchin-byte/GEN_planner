"""Зависимости конвейера и их сборка из окружения и настроек (одна фабрика)."""
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from planerka.asr.base import Transcriber
from planerka.asr.factory import get_transcriber
from planerka.core.directory import load_directory
from planerka.core.workdays import Calendar
from planerka.intake.scanner import IntakeScanner
from planerka.llm.anthropic_client import AnthropicClient
from planerka.llm.base import LLMClient
from planerka.llm.prompts import Prompt, load_prompt
from planerka.mail.base import Mailer, ServiceInbox
from planerka.mail.imap import ImapServiceInbox
from planerka.mail.smtp import SmtpMailer
from planerka.masking.masker import Masker
from planerka.masking.names import NameFinder, NatashaNameFinder, SurnameEdits
from planerka.masking.tokens import TokenStore
from planerka.settings import PROJECT_ROOT, Env, get_setting

EXTRACT_PROMPT = ('extract_tasks', 1)  # prompts/extract_tasks.v1.md
CALENDAR_DIR = PROJECT_ROOT / 'config' / 'calendar'


def _extract_prompt() -> Prompt:
    return load_prompt(*EXTRACT_PROMPT)


@dataclass
class Deps:
    env: Env
    cal: Calendar
    transcriber: Transcriber
    masker: Masker
    names: NameFinder
    tokens: TokenStore
    llm: LLMClient
    mailer: Mailer
    inbox: ServiceInbox
    prompt: Prompt = field(default_factory=_extract_prompt)
    scanner: IntakeScanner | None = None  # создаётся при первом scan_inbox, живёт с воркером

    def intake(self) -> IntakeScanner:
        """Наблюдения за приёмником живут в памяти между проходами (окно стабильности)."""
        if self.scanner is None:
            self.scanner = IntakeScanner(self.env.inbox_dir, self.env.data_dir)
        return self.scanner


def build_masker(session: Session, tokens: TokenStore, names: NameFinder) -> Masker:
    """Маскировщик со справочником и допусками из настроек на текущий момент."""
    return Masker(load_directory(session), tokens, names, SurnameEdits.from_settings(session))


def build_deps(session: Session, env: Env, names: NameFinder | None = None) -> Deps:
    """Настоящие адаптеры. Модель распознавания и LLM — из настроек; смена — перезапуск воркера.

    NatashaNameFinder загружается долго: воркер создаёт его один раз.
    """
    names = names or NatashaNameFinder()
    tokens = TokenStore(session)
    llm = AnthropicClient(env.anthropic_api_key, get_setting(session, 'llm.model_extract'),
                          int(get_setting(session, 'llm.max_tokens')),
                          int(get_setting(session, 'llm.max_attempts')),
                          float(get_setting(session, 'llm.timeout_sec')))
    return Deps(env=env, cal=Calendar.load(CALENDAR_DIR),
                transcriber=get_transcriber(get_setting(session, 'asr.engine'),
                                            get_setting(session, 'asr.model'), env),
                masker=build_masker(session, tokens, names), names=names, tokens=tokens,
                llm=llm, mailer=SmtpMailer(env), inbox=ImapServiceInbox(env))
