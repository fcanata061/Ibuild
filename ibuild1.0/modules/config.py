#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py — Módulo de configuração do ibuild (evoluído)

- Suporta $IBUILD_CONFIG > ~/.config/ibuild/config.yml > /etc/ibuild/config.yml > defaults
- Mantém compatibilidade com YAML
- Permite leitura, escrita, reset e listagem completa da config
"""

import os
import yaml

# Caminhos padrão
USER_CONFIG = os.path.expanduser("~/.config/ibuild/config.yml")
SYSTEM_CONFIG = "/etc/ibuild/config.yml"

# Valores padrão
DEFAULTS = {
    "repo_dir": "/usr/ibuild",               # onde ficam os .meta
    "cache_dir": "/var/cache/ibuild",        # sources e pacotes binários
    "pkg_db": "/var/lib/ibuild/packages",    # pacotes instalados
    "log_dir": "/var/log/ibuild",            # logs de build/install
    "sandbox_dir": "/var/lib/ibuild/sandbox",
    "repo_url": "https://github.com/fcanata061/Ibuild",
    "notify": True,
}

_config = DEFAULTS.copy()

def _load_from(path: str) -> dict:
    """Carrega configuração de um arquivo YAML se existir."""
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                if not isinstance(data, dict):
                    return {}
                return data
    except Exception:
        pass
    return {}

def load_config() -> dict:
    """Carrega config seguindo a hierarquia: env > user > system > defaults"""
    global _config

    # 1. Variável de ambiente
    env_path = os.getenv("IBUILD_CONFIG")
    if env_path and os.path.exists(env_path):
        _config = {**DEFAULTS, **_load_from(env_path)}
        return _config

    # 2. Configuração do usuário
    if os.path.exists(USER_CONFIG):
        _config = {**DEFAULTS, **_load_from(USER_CONFIG)}
        return _config

    # 3. Configuração global
    if os.path.exists(SYSTEM_CONFIG):
        _config = {**DEFAULTS, **_load_from(SYSTEM_CONFIG)}
        return _config

    # 4. Defaults
    _config = DEFAULTS.copy()
    return _config

def _save(cfg: dict, system: bool = False) -> None:
    """Salva configuração em YAML (usuário ou sistema)."""
    path = SYSTEM_CONFIG if system else USER_CONFIG
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True)

def get(key: str, default=None):
    """Obtém valor de uma chave da configuração (com fallback)."""
    if not _config:
        load_config()
    return _config.get(key, DEFAULTS.get(key, default))

def set(key: str, value, system: bool = False):
    """Define valor para uma chave e salva em config.yml."""
    cfg = load_config()
    cfg[key] = value
    _save(cfg, system=system)
    _config.update(cfg)

def all() -> dict:
    """Retorna configuração completa (merge de defaults + arquivo carregado)."""
    return load_config()

def reset(system: bool = False):
    """Restaura configuração para os valores padrão."""
    _save(DEFAULTS.copy(), system=system)
    load_config()

def ensure_dirs():
    """Garante que diretórios essenciais existem."""
    cfg = load_config()
    for key in ["cache_dir", "pkg_db", "log_dir", "sandbox_dir"]:
        os.makedirs(cfg[key], exist_ok=True)

# Carrega config logo no import
load_config()

# Execução direta para debug
if __name__ == "__main__":
    print("Config atual:")
    import json
    print(json.dumps(all(), indent=2, ensure_ascii=False))
