#!/usr/bin/env python3
# -*- coding: utf-8

import os
import glob
import yaml
from datetime import datetime
from modules import config, log

REQUIRED_FIELDS = ["name", "version", "source"]
OPTIONAL_FIELDS = [
    "maintainer", "description", "category",
    "dependencies", "optional_dependencies", "provides",
    "conflicts", "replaces",
    "build", "check", "install",
    "hooks"
]

class MetaError(Exception):
    """Erro ao carregar ou validar .meta"""
    pass

def get_pkg_dir(pkg_name: str, category: str | None = None) -> str:
    repo_dir = config.get("repo_dir")
    if category:
        candidate = os.path.join(repo_dir, category, pkg_name)
        if os.path.isdir(candidate):
            return candidate
        else:
            raise MetaError(f"Categoria {category} ou pacote {pkg_name} não existe")
    # tentar achar em qualquer categoria
    for cat in os.listdir(repo_dir):
        candidate = os.path.join(repo_dir, cat, pkg_name)
        if os.path.isdir(candidate):
            return candidate
    raise MetaError(f"Pacote {pkg_name} não encontrado em nenhuma categoria em {repo_dir}")

def get_meta_path(pkg_name: str, category: str | None = None) -> str:
    pkg_dir = get_pkg_dir(pkg_name, category)
    meta_path = os.path.join(pkg_dir, f"{pkg_name}.meta")
    if not os.path.isfile(meta_path):
        raise MetaError(f".meta não encontrado: {meta_path}")
    return meta_path

def load_meta(pkg_name: str, category: str | None = None) -> dict:
    meta_path = get_meta_path(pkg_name, category)
    log.info("Carregando .meta: %s", meta_path)
    with open(meta_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    validate_meta(data, pkg_name)
    # meta-infos auxiliares
    data["_meta_path"] = meta_path
    data["_pkg_dir"] = os.path.dirname(meta_path)
    data["_patches"] = find_patches(data["_pkg_dir"])
    # definir categoria se não estiver presente
    if "category" not in data:
        # tentativa de inferir categoria
        repo_dir = config.get("repo_dir")
        for cat in os.listdir(repo_dir):
            if os.path.isdir(os.path.join(repo_dir, cat, pkg_name)):
                data["category"] = cat
                break
    return data

def validate_meta(meta: dict, pkg_name: str):
    for field in REQUIRED_FIELDS:
        if field not in meta or meta[field] is None:
            raise MetaError(f"Campo obrigatório '{field}' ausente ou vazio em {pkg_name}.meta")
    # validações extras:
    # versão
    version = meta.get("version", "")
    if not isinstance(version, str) or version.strip() == "":
        raise MetaError(f"Versão inválida em {pkg_name}.meta: '{version}'")
    # source: pode ser dict ou list
    source = meta.get("source")
    if isinstance(source, dict):
        if "url" not in source:
            raise MetaError(f"Field source.url obrigatório em {pkg_name}.meta")
    elif isinstance(source, list):
        for s in source:
            if not isinstance(s, dict) or "url" not in s:
                raise MetaError(f"Cada item de source em lista deve ter url em {pkg_name}.meta")
    else:
        raise MetaError(f"Field source deve ser dict ou lista em {pkg_name}.meta")

def find_patches(pkg_dir: str) -> list[str]:
    patch_dir = os.path.join(pkg_dir, "patches")
    if not os.path.isdir(patch_dir):
        return []
    return sorted(glob.glob(os.path.join(patch_dir, "*.patch")))

def list_categories() -> list[str]:
    repo_dir = config.get("repo_dir")
    return sorted([d for d in os.listdir(repo_dir)
                   if os.path.isdir(os.path.join(repo_dir, d))])

def list_packages(category: str) -> list[str]:
    repo_dir = config.get("repo_dir")
    cat_dir = os.path.join(repo_dir, category)
    if not os.path.isdir(cat_dir):
        raise MetaError(f"Categoria não encontrada: {category}")
    return sorted([d for d in os.listdir(cat_dir)
                   if os.path.isdir(os.path.join(cat_dir, d))])

def create_meta(pkg_name: str, category: str,
                version: str = "1.0.0", maintainer: str = "unknown",
                description: str = "", license: str = "",
                dependencies: list = None,
                optional_dependencies: list = None) -> str:
    """
    Cria a estrutura básica de pacote:
      - diretório do pacote
      - arquivo .meta com template completo
      - pasta patches/
    Retorna o path do diretório do pacote criado.
    """

    repo_dir = config.get("repo_dir")
    if repo_dir is None:
        raise MetaError("repo_dir não configurado no config")

    pkg_dir = os.path.join(repo_dir, category, pkg_name)
    if os.path.exists(pkg_dir):
        raise MetaError(f"Pacote {pkg_name} já existe em {category}")

    os.makedirs(pkg_dir, exist_ok=True)
    os.makedirs(os.path.join(pkg_dir, "patches"), exist_ok=True)

    meta_path = os.path.join(pkg_dir, f"{pkg_name}.meta")

    template = {
        "name": pkg_name,
        "version": version,
        "maintainer": maintainer,
        "description": description,
        "license": license,
        "category": category,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": {
            "url": "http://example.com/source.tar.gz",
            "sha256": "TODO SHA256",
        },
        "dependencies": dependencies or [],
        "optional_dependencies": optional_dependencies or [],
        "provides": [],
        "conflicts": [],
        "replaces": [],
        "build": [
            "./configure --prefix=/usr",
            "make -j$(nproc)"
        ],
        "check": [],
        "install": [
            "make install"
        ],
        "hooks": {
            "pre_fetch": [],
            "post_fetch": [],
            "pre_build": [],
            "post_build": [],
            "pre_install": [],
            "post_install": []
        }
    }

    with open(meta_path, "w", encoding="utf-8") as f:
        yaml.dump(template, f, sort_keys=False)

    log.info("Criado novo pacote %s em categoria %s", pkg_name, category)
    return pkg_dir

# __all__ para facilitar importações
__all__ = [
    "get_pkg_dir", "get_meta_path", "load_meta", "validate_meta", "find_patches",
    "list_categories", "list_packages", "create_meta", "MetaError"
]
