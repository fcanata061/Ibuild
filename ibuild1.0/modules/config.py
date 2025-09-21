cat > ibuild1.0/modules_py/config.py <<'PY'
import os
import yaml

# Diretórios padrão
DEFAULTS = {
    "repo_dir": "/usr/ibuild",             # onde ficam os .meta
    "cache_dir": "/var/cache/ibuild",      # sources e pacotes binários
    "pkg_db": "/var/lib/ibuild/packages",  # pacotes instalados
    "log_dir": "/var/log/ibuild",          # logs de build/install
    "config_file": "/etc/ibuild/config.yml"
}

_config = DEFAULTS.copy()


def load_config():
    """Carrega config do usuário ou sistema"""
    global _config
    paths = [
        os.path.expanduser("~/.config/ibuild/config.yml"),
        DEFAULTS["config_file"],
    ]
    for path in paths:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
                _config.update(user_cfg)
            break
    return _config


def get(key: str) -> str:
    """Obtém valor da config carregada"""
    if not _config:
        load_config()
    return _config.get(key)


def ensure_dirs():
    """Garante que diretórios essenciais existem"""
    for key in ["cache_dir", "pkg_db", "log_dir"]:
        os.makedirs(_config[key], exist_ok=True)


# Carrega config logo no import
load_config()
PY
