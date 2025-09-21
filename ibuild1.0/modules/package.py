# package.py
"""
Gerenciamento de pacotes instalados no Ibuild (evoluído).

Principais recursos:
- Instalação com manifesto de arquivos
- Remoção precisa baseada em manifesto
- Upgrade / Reinstall
- Verificação de integridade de artefatos e arquivos
- Busca e consultas no pkg_db
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

def _pkg_manifest_path(name: str) -> str:
    return os.path.join(_pkg_db_dir(), f"{name}.manifest.txt")

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

def _extract_with_manifest(artifact_path: str, dest_dir: str) -> List[str]:
    """
    Extrai o tar.gz e retorna lista de arquivos extraídos (manifest).
    """
    extracted_files = []
    with tarfile.open(artifact_path, "r:gz") as tar:
        for member in tar.getmembers():
            tar.extract(member, path=dest_dir)
            full_path = os.path.join(dest_dir, member.name)
            if member.isfile():
                extracted_files.append(full_path)
    return extracted_files

# Core API ---------------------------------------------------------------
def install_package(
    artifact_path: str,
    dest_dir: Optional[str] = None,
    overwrite: bool = False,
    upgrade: bool = False,
) -> dict:
    """
    Instala um pacote a partir de um .tar.gz.
    - gera manifesto de arquivos
    - registra no pkg_db
    - suporta overwrite e upgrade
    """
    if not os.path.isfile(artifact_path):
        raise FileNotFoundError(f"Artefato não encontrado: {artifact_path}")

    base_name = os.path.basename(artifact_path)
    if not base_name.endswith(".tar.gz"):
        raise ValueError(f"Arquivo inválido: {artifact_path}")
    name = base_name.split("-")[0]
    version = base_name.replace(f"{name}-", "").replace(".tar.gz", "")

    sha = _checksum_file(artifact_path)
    dest_dir = dest_dir or config.get("install_root") or "/usr/local"
    os.makedirs(dest_dir, exist_ok=True)

    pkg_meta_path = _pkg_db_meta_path(name)
    if os.path.exists(pkg_meta_path):
        if not overwrite and not upgrade:
            raise FileExistsError(f"Pacote {name} já instalado. Use overwrite=True ou upgrade=True.")

        if upgrade:
            logger.info("Atualizando pacote %s", name)
            remove_package(name, purge=True)

    # rollback seguro em caso de erro
    extracted_files = []
    try:
        logger.info("Instalando %s em %s", name, dest_dir)
        extracted_files = _extract_with_manifest(artifact_path, dest_dir)

        # gravar manifesto
        with open(_pkg_manifest_path(name), "w", encoding="utf-8") as f:
            f.write("\n".join(extracted_files))

        installed_meta = {
            "name": name,
            "version": version,
            "artifact": artifact_path,
            "sha256": sha,
            "install_root": dest_dir,
            "manifest": _pkg_manifest_path(name),
        }
        with open(pkg_meta_path, "w", encoding="utf-8") as f:
            json.dump(installed_meta, f, indent=2)

        logger.info("Instalado: %s %s", name, version)
        return installed_meta

    except Exception as e:
        logger.error("Erro durante instalação de %s: %s", name, e)
        # rollback: remover arquivos extraídos
        for f in extracted_files:
            try: os.remove(f)
            except FileNotFoundError: pass
        raise

def remove_package(name: str, purge: bool = False) -> bool:
    """
    Remove pacote do sistema:
    - apaga arquivos listados no manifesto
    - remove registro do pkg_db
    """
    meta = _load_pkg_meta(name)
    if not meta:
        logger.warn("Pacote %s não encontrado em pkg_db", name)
        return False

    manifest_path = _pkg_manifest_path(name)
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            files = f.read().splitlines()
        for fpath in files:
            try:
                if os.path.isfile(fpath) or os.path.islink(fpath):
                    os.remove(fpath)
                elif purge and os.path.isdir(fpath):
                    shutil.rmtree(fpath, ignore_errors=True)
            except Exception as e:
                logger.warn("Falha ao remover %s: %s", fpath, e)
        os.remove(manifest_path)

    os.remove(_pkg_db_meta_path(name))
    logger.info("Removido %s do banco de pacotes", name)
    return True

def list_installed() -> List[dict]:
    pkgs = []
    for fn in os.listdir(_pkg_db_dir()):
        if fn.endswith(".installed.meta"):
            with open(os.path.join(_pkg_db_dir(), fn), "r", encoding="utf-8") as f:
                pkgs.append(json.load(f))
    return sorted(pkgs, key=lambda x: x["name"])

def search_installed(pattern: str) -> List[dict]:
    """
    Busca pacotes instalados pelo nome (substring).
    """
    return [p for p in list_installed() if pattern in p["name"]]

def query_package(name: str) -> Optional[dict]:
    return _load_pkg_meta(name)

def verify_package(name: str, deep: bool = False) -> bool:
    """
    Verifica integridade do pacote:
    - sha256 do artefato
    - se deep=True, verifica todos arquivos no manifesto
    """
    meta = _load_pkg_meta(name)
    if not meta:
        raise FileNotFoundError(f"{name} não encontrado em pkg_db")

    art = meta.get("artifact")
    if not os.path.isfile(art):
        logger.error("Artefato %s não encontrado", art)
        return False
    if _checksum_file(art) != meta.get("sha256"):
        logger.error("Checksum incorreto para %s", name)
        return False

    if deep:
        manifest_path = meta.get("manifest")
        if manifest_path and os.path.isfile(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                for fpath in f.read().splitlines():
                    if not os.path.exists(fpath):
                        logger.error("Arquivo perdido: %s", fpath)
                        return False
    return True

def who_requires(name: str) -> List[str]:
    """
    Quem depende de `name` (baseado nos .meta originais).
    """
    dependents = []
    for pkg in list_installed():
        try:
            deps = meta.load_meta(pkg["name"]).get("dependencies", [])
            if any(name in str(d) for d in deps):
                dependents.append(pkg["name"])
        except Exception:
            continue
    return dependents

def what_provides(virtual: str) -> List[str]:
    providers = []
    for pkg in list_installed():
        try:
            provs = meta.load_meta(pkg["name"]).get("provides", [])
            if virtual in provs:
                providers.append(pkg["name"])
        except Exception:
            continue
    return providers
