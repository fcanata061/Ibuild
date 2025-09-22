#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
modules/toolchain.py — Gerenciamento avançado de toolchain nativa
(gcc, binutils, glibc, kernel headers)

Funcionalidades:
- repair_libtool()
- validate_toolchain()
- test_compile() com execução real
- coerência entre gcc, ld, glibc
- select_version(), list_versions()
- rollback automático
- profiles persistentes em JSON
- ensure_toolchain_ready() (usado pelo bootstrap)
"""

import os
import shutil
import subprocess
import glob
import json
from typing import List, Dict, Optional

import logging
logger = logging.getLogger("ibuild.toolchain")

PROFILE_FILE = "/var/lib/ibuild/toolchain.profile.json"

# -------------------------
# Helpers internos
# -------------------------
def _run(cmd: List[str], capture: bool = True, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    logger.debug("RUN: %s", " ".join(cmd))
    return subprocess.run(cmd, text=True, capture_output=capture, check=False, cwd=cwd)

def _which(binname: str) -> Optional[str]:
    return shutil.which(binname)

def _save_profile(profile: Dict) -> None:
    os.makedirs(os.path.dirname(PROFILE_FILE), exist_ok=True)
    with open(PROFILE_FILE, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)

def _load_profile() -> Dict:
    if os.path.exists(PROFILE_FILE):
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

# -------------------------
# Libtool fix
# -------------------------
def repair_libtool() -> bool:
    lt = _which("libtool")
    if not lt:
        logger.warning("libtool não encontrado")
        return False
    logger.info("Verificando libtool em %s", lt)
    autogen = shutil.which("autoreconf")
    if autogen:
        try:
            _run([autogen, "--force", "--install"])
            logger.info("libtool regenerado via autoreconf")
            return True
        except Exception:
            logger.exception("Falha ao regenerar libtool")
            return False
    return True

# -------------------------
# Toolchain validation
# -------------------------
def validate_toolchain() -> Dict[str, bool]:
    """
    Valida gcc, binutils (ld/as), glibc e kernel headers.
    """
    results: Dict[str, bool] = {}

    # gcc
    gcc = _which("gcc")
    results["gcc"] = bool(gcc and _run([gcc, "--version"]).returncode == 0)

    # binutils
    for tool in ["ld", "as"]:
        path = _which(tool)
        results[tool] = bool(path and _run([path, "--version"]).returncode == 0)

    # glibc
    libc = "/lib/libc.so.6"
    results["glibc"] = os.path.exists(libc) and _run([libc]).returncode == 0

    # kernel headers
    results["kernel_headers"] = os.path.isdir("/usr/include/linux")

    logger.info("Validação toolchain: %s", results)
    return results

# -------------------------
# Teste real de compilação e execução
# -------------------------
def test_compile() -> bool:
    """
    Compila e executa um hello world em C para validar gcc+glibc+ld.
    """
    src = "/tmp/ibuild_test.c"
    binf = "/tmp/ibuild_test"
    with open(src, "w", encoding="utf-8") as f:
        f.write('#include <stdio.h>\nint main(){printf("hello ibuild\\n");return 0;}\n')

    gcc = _which("gcc")
    if not gcc:
        logger.error("gcc não encontrado")
        return False

    # compilar
    if _run([gcc, src, "-o", binf]).returncode != 0:
        logger.error("Falha ao compilar programa de teste")
        return False

    # rodar
    p = _run([binf])
    ok = (p.returncode == 0 and "hello ibuild" in (p.stdout or ""))

    # limpeza
    for f in [src, binf]:
        if os.path.exists(f):
            os.remove(f)

    logger.info("Teste real de compilação: %s", "OK" if ok else "falhou")
    return ok

# -------------------------
# Seleção de versões
# -------------------------
def list_versions(component: str) -> List[str]:
    versions: List[str] = []
    if component == "gcc":
        for path in glob.glob("/usr/bin/gcc-*"):
            ver = os.path.basename(path).split("gcc-")[-1]
            versions.append(ver)
    elif component == "kernel":
        for path in glob.glob("/usr/src/linux-*"):
            ver = os.path.basename(path).split("linux-")[-1]
            versions.append(ver)
    return sorted(versions)

def select_version(component: str, version: str) -> bool:
    """
    Define versão de gcc ou kernel headers como padrão e salva no profile.
    """
    profile = _load_profile()

    if component == "gcc":
        gcc_bin = f"/usr/bin/gcc-{version}"
        if not os.path.exists(gcc_bin):
            logger.error("gcc versão %s não encontrado", version)
            return False
        try:
            if os.path.exists("/usr/bin/gcc"):
                os.remove("/usr/bin/gcc")
            os.symlink(gcc_bin, "/usr/bin/gcc")
            profile["gcc"] = version
            _save_profile(profile)
            logger.info("gcc %s definido como padrão", version)
            return True
        except Exception:
            logger.exception("Falha ao trocar gcc")
            return False

    elif component == "kernel":
        kh_dir = f"/usr/src/linux-{version}"
        if not os.path.isdir(kh_dir):
            logger.error("kernel headers %s não encontrados", version)
            return False
        try:
            if os.path.exists("/usr/src/linux"):
                os.remove("/usr/src/linux")
            os.symlink(kh_dir, "/usr/src/linux")
            profile["kernel"] = version
            _save_profile(profile)
            logger.info("kernel headers %s definidos como padrão", version)
            return True
        except Exception:
            logger.exception("Falha ao trocar kernel headers")
            return False

    return False

# -------------------------
# Rollback
# -------------------------
def rollback(component: str) -> bool:
    profile = _load_profile()
    if component not in profile:
        logger.error("Nenhuma versão anterior salva para %s", component)
        return False
    old_ver = profile[component]
    logger.info("Revertendo %s para versão %s", component, old_ver)
    return select_version(component, old_ver)

# -------------------------
# Integração com bootstrap
# -------------------------
def ensure_toolchain_ready() -> bool:
    """
    Garante que a toolchain está íntegra antes de builds.
    """
    status = validate_toolchain()
    if not all(status.values()):
        logger.warning("Toolchain incompleta: %s", status)
        return False
    if not test_compile():
        logger.warning("Compilação/execução de teste falhou")
        return False
    return True
