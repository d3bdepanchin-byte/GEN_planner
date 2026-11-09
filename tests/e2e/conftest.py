"""Сквозной тест использует фикстуры конвейера (фейки, часы, справочник) без копирования."""
import importlib.util
import sys
from pathlib import Path

_PIPELINE = Path(__file__).resolve().parent.parent / 'pipeline'
if str(_PIPELINE) not in sys.path:  # samples.py лежит рядом с фикстурами конвейера
    sys.path.insert(0, str(_PIPELINE))

_spec = importlib.util.spec_from_file_location('pipeline_fixtures', _PIPELINE / 'conftest.py')
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

for _name, _obj in vars(_module).items():
    if type(_obj).__name__ == 'FixtureFunctionDefinition':
        globals()[_name] = _obj
