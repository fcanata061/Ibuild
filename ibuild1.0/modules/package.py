# package.py
"""
Gerenciamento de pacotes instalados no Ibuild.

Funcionalidades:
- install_package: instala um .tar.gz no sistema (sandbox ou host)
- remove_package: remove pacotes instalados
- list_installed: lista pacotes instalados
- query_package: retorna info de um pacote instalado
- verify_package: checa integridade (sha256) de artefato
- who_requires / what_provides: relação de dependências reversa
"""

from __future__ import annotations
import os
import shutil
import tarfile
import json
import hashlib
from typing import Dict, List, Optional

from ibuild1.0.modules_py import config, log, meta

logger = log.get_logger("package")

# Helpers ---------------------------------------------------------------
def _pkg_db_dir() -> str:
    d = config.get("pkg_db")
    os.makedirs(d, exist_ok=True)
    return d

def _pkg_db_meta_path(name: str) -> str:
    return os.path.join(_pkg_db_dir(), f"{name}.installed.meta")

def _load_pkg_meta(name: str) -> Optional[dict]:
    path = _pkg_db_meta_path(name)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _checksum_file(path: str, algo: str = "sha256") -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

# Core API ---------------------------------------------------------------
def install_package(artifact_path: str, dest_dir: Optional[str] = None, overwrite: bool = False) -> dict:
    """
    Instala um pacote a partir de um .tar.gz.
    - extrai em dest_dir (default: /usr/local ou config['install_root'])
    - grava metadado em pkg_db
    Retorna dict com info instalada.
    """
    if not os.path.isfile(artifact_path):
        raise FileNotFoundError(f"Artefato não encontrado: {artifact_path}")

    # ler metadado embutido se existir
    base_name = os.path.basename(artifact_path)
    name, _, version = base_name.partition("-")
    version = version.replace(".tar.gz", "")

    # calcular checksum
    sha = _checksum_file(artifact_path)

    # destino
    dest_dir = dest_dir or config.get("install_root") or "/usr/local"
    os.makedirs(dest_dir, exist_ok=True)

    # verificar pkg_db
    pkg_meta_path = _pkg_db_meta_path(name)
    if os.path.exists(pkg_meta_path) and not overwrite:
        raise FileExistsError(f"Pacote {name} já instalado. Use overwrite=True para reinstalar.")

    # extrair
    logger.info("Instalando %s em %s", name, dest_dir)
    with tarfile.open(artifact_path, "r:gz") as tar:
        tar.extractall(path=dest_dir)

    installed_meta = {
        "name": name,
        "version": version,
        "artifact": artifact_path,
        "sha256": sha,
        "install_root": dest_dir,
    }
    with open(pkg_meta_path, "w", encoding="utf-8") as f:
        json.dump(installed_meta, f, indent=2)

    logger.info("Instalado: %s %s", name, version)
    return installed_meta

def remove_package(name: str, purge: bool = False) -> bool:
    """
    Remove pacote do sistema:
    - apaga registro em pkg_db
    - se purge=True, também remove arquivos (cuidado!)
    """
    meta = _load_pkg_meta(name)
    if not meta:
        logger.warn("Pacote %s não encontrado em pkg_db", name)
        return False

    if purge:
        root = meta.get("install_root") or "/usr/local"
        logger.info("Removendo arquivos de %s de %s", name, root)
        # Não temos lista de arquivos rastreados (futuro: gerar manifest.txt)
        logger.warn("Sem manifest — purge remove apenas diretório base, cuidado!")
        shutil.rmtree(root, ignore_errors=True)

    os.remove(_pkg_db_meta_path(name))
    logger.info("Removido %s do banco de pacotes", name)
    return True

def list_installed() -> List[dict]:
    """
    Lista todos pacotes instalados no pkg_db.
    """
    pkgs = []
    for fn in os.listdir(_pkg_db_dir()):
        if fn.endswith(".installed.meta"):
            with open(os.path.join(_pkg_db_dir(), fn), "r", encoding="utf-8") as f:
                pkgs.append(json.load(f))
    return sorted(pkgs, key=lambda x: x["name"])

def query_package(name: str) -> Optional[dict]:
    """
    Retorna metadados de um pacote instalado.
    """
    return _load_pkg_meta(name)

def verify_package(name: str) -> bool:
    """
    Verifica integridade do artefato associado a um pacote.
    """
    meta = _load_pkg_meta(name)
    if not meta:
        raise FileNotFoundError(f"{name} não encontrado em pkg_db")
    art = meta.get("artifact")
    if not os.path.isfile(art):
        logger.error("Artefato %s não encontrado", art)
        return False
    sha = _checksum_file(art)
    ok = sha == meta.get("sha256")
    if not ok:
        logger.error("Checksum incorreto para %s (esperado %s, obtido %s)",
                     name, meta.get("sha256"), sha)
    return ok

def who_requires(name: str) -> List[str]:
    """
    Retorna lista de pacotes instalados que dependem de `name`.
    """
    dependents = []
    for pkg in list_installed():
        deps = meta.load_meta(pkg["name"]).get("dependencies", [])
        if any(name in str(d) for d in deps):
            dependents.append(pkg["name"])
    return dependents

def what_provides(virtual: str) -> List[str]:
    """
    Retorna pacotes instalados que fornecem uma capability (provides).
    """
    providers = []
    for pkg in list_installed():
        provs = meta.load_meta(pkg["name"]).get("provides", [])
        if virtual in provs:
            providers.append(pkg["name"])
    return providers
