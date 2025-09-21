import os
import glob
import yaml
from datetime import datetime

from ibuild1.0.modules_py import config, log


REQUIRED_FIELDS = ["name", "version", "source"]


class MetaError(Exception):
    """Erro ao carregar ou validar .meta"""
    pass


def get_pkg_dir(pkg_name: str, category: str | None = None) -> str:
    repo_dir = config.get("repo_dir")
    if category:
        return os.path.join(repo_dir, category, pkg_name)

    for cat in os.listdir(repo_dir):
        candidate = os.path.join(repo_dir, cat, pkg_name)
        if os.path.isdir(candidate):
            return candidate

    raise MetaError(f"Pacote {pkg_name} não encontrado em nenhuma categoria")


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
    data["_meta_path"] = meta_path
    data["_pkg_dir"] = os.path.dirname(meta_path)
    data["_patches"] = find_patches(data["_pkg_dir"])
    return data


def validate_meta(meta: dict, pkg_name: str):
    for field in REQUIRED_FIELDS:
        if field not in meta:
            raise MetaError(f"Campo obrigatório '{field}' ausente em {pkg_name}.meta")


def find_patches(pkg_dir: str) -> list[str]:
    patch_dir = os.path.join(pkg_dir, "patches")
    if not os.path.isdir(patch_dir):
        return []
    return sorted(glob.glob(os.path.join(patch_dir, "*.patch")))


def list_categories() -> list[str]:
    repo_dir = config.get("repo_dir")
    return [d for d in os.listdir(repo_dir) if os.path.isdir(os.path.join(repo_dir, d))]


def list_packages(category: str) -> list[str]:
    repo_dir = config.get("repo_dir")
    cat_dir = os.path.join(repo_dir, category)
    if not os.path.isdir(cat_dir):
        raise MetaError(f"Categoria não encontrada: {category}")
    return [d for d in os.listdir(cat_dir) if os.path.isdir(os.path.join(cat_dir, d))]


def create_meta(pkg_name: str, category: str, version: str = "1.0.0",
                maintainer: str = "unknown") -> str:
    """
    Cria estrutura básica de pacote:
      - diretório do pacote
      - arquivo .meta com template
      - pasta patches/
    """
    repo_dir = config.get("repo_dir")
    pkg_dir = os.path.join(repo_dir, category, pkg_name)

    if os.path.exists(pkg_dir):
        raise MetaError(f"Pacote {pkg_name} já existe em {category}")

    os.makedirs(os.path.join(pkg_dir, "patches"), exist_ok=True)

    meta_path = os.path.join(pkg_dir, f"{pkg_name}.meta")
    template = {
        "name": pkg_name,
        "version": version,
        "maintainer": maintainer,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": {
            "url": "http://example.com/source.tar.gz",
            "sha256": "TODO"
        },
        "dependencies": [],
        "build": [
            "./configure --prefix=/usr",
            "make -j$(nproc)"
        ],
        "install": [
            "make install"
        ]
    }

    with open(meta_path, "w", encoding="utf-8") as f:
        yaml.dump(template, f, sort_keys=False)

    log.info("Criado novo pacote %s em categoria %s", pkg_name, category)
    return pkg_dir
