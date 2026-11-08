"""Файлы промптов `prompts/<имя>.v<N>.md`."""
from dataclasses import dataclass
from pathlib import Path
from string import Template

from planerka.settings import PROJECT_ROOT

SEPARATOR = '---USER---'


class PromptVariableError(KeyError):
    """В шаблоне есть переменная, которой нет в variables. В сообщении — имя переменной, без текста."""


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    system: str
    template: str

    def render(self, variables: dict[str, str]) -> str:
        try:
            return Template(self.template).substitute(variables)
        except KeyError as e:
            raise PromptVariableError(f'нет переменной {e.args[0]!r} для промпта {self.name}.v{self.version}') from None


def load_prompt(name: str, version: int, dir: Path = PROJECT_ROOT / 'prompts') -> Prompt:
    path = dir / f'{name}.v{version}.md'
    raw = path.read_text(encoding='utf-8')
    system, sep, template = raw.partition(SEPARATOR)
    if not sep:
        raise ValueError(f'в {path.name} нет строки {SEPARATOR}')
    return Prompt(name, version, system.strip(), template.strip())
