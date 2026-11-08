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
