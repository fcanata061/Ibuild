import os
import shutil
import hashlib
import tarfile
import requests
import subprocess
import yaml
import json

from ibuild1.0.modules_py import log, config


# -------------------------
# Sistema de arquivos
# -------------------------
def ensure_dir(path: str):
    """Cria diretório se não existir"""
    os.makedirs(path, exist_ok=True)


def clean_dir(path: str):
    """Remove diretório se existir e recria vazio"""
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path)


def copy_file(src: str, dst: str):
    """Copia arquivo preservando metadados"""
    ensure_dir(os.path.dirname(dst))
    shutil.copy2(src, dst)


def rm(path: str):
    """Remove arquivo ou diretório"""
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.isfile(path):
        os.remove(path)


# -------------------------
# Execução de comandos
# -------------------------
def run(cmd: list[str], cwd: str | None = None, env: dict | None = None, check=True):
    """Wrapper para rodar comandos com log"""
    rc, out, err = log.run_cmd(cmd, cwd=cwd, env=env)
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, cmd, out, err)
    return rc, out, err


# -------------------------
# Download e cache
# -------------------------
def download(url: str, dest: str, expected_sha256: str | None = None):
    """Baixa arquivo com cache e checagem opcional de SHA256"""
    ensure_dir(os.path.dirname(dest))

    if os.path.isfile(dest):
        log.info("Arquivo já existe em cache: %s", dest)
        if expected_sha256 and not verify_sha256(dest, expected_sha256):
            log.error("Hash incorreto para %s, rebaixando...", dest)
            os.remove(dest)
        else:
            return dest

    log.info("Baixando %s → %s", url, dest)
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

    if expected_sha256 and not verify_sha256(dest, expected_sha256):
        raise ValueError(f"SHA256 inválido para {dest}")

    return dest


def verify_sha256(path: str, expected: str) -> bool:
    """Verifica SHA256 de um arquivo"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    digest = h.hexdigest()
    return digest == expected.lower()


# -------------------------
# Extração de arquivos
# -------------------------
def extract_tarball(tar_path: str, dest_dir: str):
    """Extrai tarball (.tar.gz, .tar.xz, etc.)"""
    ensure_dir(dest_dir)
    log.info("Extraindo %s → %s", tar_path, dest_dir)
    with tarfile.open(tar_path, "r:*") as tar:
        tar.extractall(dest_dir)
    return dest_dir


# -------------------------
# Patches
# -------------------------
def apply_patch(patch_file: str, src_dir: str, strip: int = 1):
    """Aplica patch a partir de um arquivo .patch"""
    log.info("Aplicando patch %s", patch_file)
    rc, _, _ = run(
        ["patch", f"-p{strip}", "-i", os.path.abspath(patch_file)],
        cwd=src_dir,
        check=False
    )
    if rc != 0:
        raise RuntimeError(f"Falha ao aplicar patch {patch_file}")


# -------------------------
# Leitura de configs
# -------------------------
def load_yaml(path: str) -> dict:
    """Carrega YAML em dict"""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_json(path: str) -> dict:
    """Carrega JSON em dict"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# -------------------------
# Helpers diversos
# -------------------------
def get_cache_path(filename: str) -> str:
    """Retorna caminho completo dentro do cache"""
    return os.path.join(config.get("cache_dir"), filename)
