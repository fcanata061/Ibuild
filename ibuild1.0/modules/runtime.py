#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runtime.py — Gerenciamento robusto de runtimes (Python, Ruby, Java, Node, etc.)

- Suporta múltiplas linguagens
- Instalação via .meta
- Troca de versões (global e usuário)
- Diagnóstico e reparo automático
- Correção de conflitos de symlinks
"""

import os
import shutil
import subprocess
from modules import config, log

# Linguagens suportadas (podem ser extendidas pelo config.yml)
SUPPORTED_LANGUAGES = config.get("runtimes", [
    "python", "ruby", "java", "node", "go", "php", "perl"
])

def _runtime_base_dir(language: str) -> str:
    """Retorna o diretório base de instalação de runtimes."""
    base = os.path.join(config.get("pkg_db"), "runtimes", language)
    os.makedirs(base, exist_ok=True)
    return base

def _bin_candidates(language: str) -> list:
    """Retorna lista de binários prováveis para teste de cada linguagem."""
    return {
        "python": ["python3", "python"],
        "ruby": ["ruby"],
        "java": ["java"],
        "node": ["node"],
        "go": ["go"],
        "php": ["php"],
        "perl": ["perl"],
    }.get(language, [language])

def list_runtimes(language: str, detailed: bool = False) -> list:
    """Lista versões disponíveis de uma linguagem (com status opcional)."""
    base = _runtime_base_dir(language)
    versions = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]

    if not detailed:
        return versions

    results = []
    for v in versions:
        ok = validate_runtime(language, v)
        is_default = (os.path.realpath(os.path.join(base, "current")) ==
                      os.path.realpath(os.path.join(base, v)))
        results.append({
            "version": v,
            "status": "OK" if ok else "BROKEN",
            "default": is_default,
        })
    return results

def install_runtime(language: str, version: str) -> bool:
    """Instala uma versão da linguagem via .meta."""
    log.info(f"Instalando {language} {version}...")
    meta_dir = os.path.join(config.get("repo_dir"), f"{language}-{version}")
    meta_path = os.path.join(meta_dir, f"{language}-{version}.meta")

    if not os.path.exists(meta_path):
        log.error(f".meta para {language}-{version} não encontrado em {meta_path}")
        return False

    # TODO: integração real com build/install
    log.info(f"{language} {version} instalado com sucesso (simulação).")
    return True

def set_default(language: str, version: str, user: bool = False) -> bool:
    """Define uma versão como padrão (symlink para current)."""
    base = _runtime_base_dir(language)
    if version not in list_runtimes(language):
        log.error(f"Versão {version} de {language} não está instalada.")
        return False

    target_dir = os.path.join(base, version)
    current = os.path.join(base, "current")

    # Corrige symlink antigo quebrado
    if os.path.islink(current) or os.path.exists(current):
        try:
            os.remove(current)
        except Exception:
            shutil.rmtree(current, ignore_errors=True)

    os.symlink(target_dir, current)

    if user:
        user_bin = os.path.expanduser("~/.local/bin")
        os.makedirs(user_bin, exist_ok=True)
        for b in os.listdir(os.path.join(target_dir, "bin")):
            src = os.path.join(target_dir, "bin", b)
            dst = os.path.join(user_bin, b)
            if os.path.exists(dst):
                os.remove(dst)
            os.symlink(src, dst)

    log.info(f"{language} {version} definido como padrão ({'usuário' if user else 'global'}).")
    return True

def remove_runtime(language: str, version: str) -> bool:
    """Remove uma versão instalada da linguagem."""
    base = _runtime_base_dir(language)
    target = os.path.join(base, version)
    if not os.path.exists(target):
        log.error(f"{language} {version} não encontrado.")
        return False
    shutil.rmtree(target)
    log.info(f"{language} {version} removido.")
    return True

def validate_runtime(language: str, version: str) -> bool:
    """Executa testes da runtime (binário responde com versão)."""
    base = _runtime_base_dir(language)
    bin_dir = os.path.join(base, version, "bin")

    if not os.path.exists(bin_dir):
        log.error(f"Binário não encontrado para {language} {version}.")
        return False

    candidates = _bin_candidates(language)
    for cand in candidates:
        bin_path = os.path.join(bin_dir, cand)
        if os.path.exists(bin_path):
            try:
                result = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    log.info(f"{language} {version} OK: {result.stdout.strip() or result.stderr.strip()}")
                    return True
            except Exception as e:
                log.error(f"Erro ao validar {language} {version}: {e}")
    return False

def repair_runtime(language: str) -> None:
    """Repara runtimes quebradas (symlinks, reinstalação)."""
    log.warn(f"Tentando reparar {language}...")
    for version in list_runtimes(language):
        if not validate_runtime(language, version):
            log.warn(f"{language} {version} quebrado. Tentando reinstalar...")
            ok = install_runtime(language, version)
            if not ok:
                log.error(f"Falha ao reparar {language} {version}. Considere remover.")
    log.info(f"Reparo de {language} concluído.")

def detect_runtime(language: str) -> str:
    """Detecta a versão atualmente ativa (symlink current)."""
    base = _runtime_base_dir(language)
    current = os.path.join(base, "current")
    if os.path.islink(current):
        return os.path.basename(os.readlink(current))
    return None

def diagnose_runtime(language: str) -> dict:
    """Executa diagnóstico completo de uma linguagem."""
    results = {
        "language": language,
        "default": detect_runtime(language),
        "versions": [],
    }
    for v in list_runtimes(language):
        results["versions"].append({
            "version": v,
            "ok": validate_runtime(language, v),
        })
    return results

# API pública
__all__ = [
    "SUPPORTED_LANGUAGES",
    "list_runtimes",
    "install_runtime",
    "set_default",
    "remove_runtime",
    "validate_runtime",
    "repair_runtime",
    "detect_runtime",
    "diagnose_runtime",
]
