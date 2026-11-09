from pathlib import Path

def test_package_imports():
    import planerka
    assert planerka.__version__ == '0.1.0'

def test_gitignore_protects_real_data_and_secrets():
    lines = Path('.gitignore').read_text().splitlines()
    for entry in ['.env', 'data/', 'models/', 'reports/', 'backups/']:
        assert entry in lines

def test_env_example_has_keys_but_no_values():
    pairs = [l.split('=', 1) for l in Path('.env.example').read_text().splitlines()
             if l and not l.startswith('#')]
    keys = {k for k, _ in pairs}
    assert {'DATABASE_URL', 'ANTHROPIC_API_KEY', 'HF_TOKEN', 'BACKUP_PASSPHRASE'} <= keys
    assert all(v == '' for _, v in pairs)


def test_gitignore_hides_env_variants_but_not_example():
    lines = Path('.gitignore').read_text().splitlines()
    assert '.env.*' in lines and '!.env.example' in lines
    assert lines.index('!.env.example') > lines.index('.env.*')  # исключение после шаблона


def _service(name: str) -> str:
    """Блок сервиса из docker-compose.yml (без YAML-парсера: его нет в образе)."""
    lines = Path('docker-compose.yml').read_text().splitlines()
    start = lines.index(f'  {name}:')
    block = []
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith('   '):
            break
        block.append(line)
    return '\n'.join(block)


def test_db_restarts_after_server_reboot():
    assert 'restart: unless-stopped' in _service('db')
    assert 'restart: unless-stopped' in _service('worker')


def test_app_uses_local_model_cache_like_worker():
    for name in ('app', 'worker'):
        block = _service(name)
        assert 'HF_HUB_OFFLINE: "1"' in block and 'HF_HOME: /app/models/hf' in block
