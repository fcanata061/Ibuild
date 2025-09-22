#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py — Módulo de configuração do ibuild (evoluído)

- Suporta $IBUILD_CONFIG > ~/.config/ibuild/config.yml > /etc/ibuild/config.yml > defaults
- Mantém compatibilidade com YAML
- Permite leitura, escrita, reset e listagem completa da config
- Inclui campos extras para compilação, sandbox, rollback e toolchain
"""

import os
import yaml

# Caminhos padrão
USER_CONFIG = os.path.expanduser("~/.config/ibuild/config.yml")
SYSTEM_CONFIG = "/etc/ibuild/config.yml"

# Valores padrão (completo)
DEFAULTS = {
    # Diretórios principais
    "repo_dir": "/usr/ibuild",
    "cache_dir": "/var/cache/ibuild",
    "pkg_db": "/var/lib/ibuild/packages",
    "log_dir": "/var/log/ibuild",
    "sandbox_dir": "/var/lib/ibuild/sandbox",
    "snapshots_dir": "/var/lib/ibuild/snapshots",

    # Repositório
    "repo_url": "https://github.com/fcanata061/Ibuild",

    # Fontes adicionais
    "sources_dir": "/var/cache/ibuild/sources",
    "packages_dir": "/var/cache/ibuild/packages",
    "patches_dir": "/usr/ibuild/patches",
    "hooks_dir": "/usr/ibuild/hooks",

    # Notificações e update
    "notify": True,
    "auto_update": False,
    "check_interval": 3600,  # segundos (1h)

    # Compilação
    "makeflags": "-j4",
    "cflags": "-O2 -pipe",
    "cxxflags": "-O2 -pipe",
    "ldflags": "-Wl,-O1",

    # Sandbox
    "sandbox_engine": "bwrap",  # bwrap | chroot | docker
    "sandbox_tmpfs": True,

    # Toolchain
    "default_gcc": "13.2.0",
    "default_kernel": "6.9.3",

    # Rollback
    "keep_snapshots": 5,
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
    for key in ["cache_dir", "pkg_db", "log_dir", "sandbox_dir", "snapshots_dir"]:
        os.makedirs(cfg[key], exist_ok=True)

# Carrega config logo no import
load_config()

# Execução direta para debug
if __name__ == "__main__":
    import json
    print("Config atual:")
    print(json.dumps(all(), indent=2, ensure_ascii=False))
