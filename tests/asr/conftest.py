import os
import socket
from pathlib import Path

import pytest

from planerka.settings import Env


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(database_url='postgresql://x', models_dir=tmp_path / 'models', hf_token='hf_test')


@pytest.fixture
def sample_wav() -> Path:
    path = os.environ.get('PLANERKA_SAMPLE_WAV')
    if not path:
        pytest.skip('PLANERKA_SAMPLE_WAV не задан (реальная запись только на сервере)')
    return Path(path)


@pytest.fixture
def no_network(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise OSError('network disabled in test')

    monkeypatch.setattr(socket.socket, 'connect', refuse)
