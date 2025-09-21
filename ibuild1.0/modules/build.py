
# build.py
"""
Orquestrador de build do Ibuild.

Funcionalidades principais:
- resolve dependências (opcional)
- cria sandbox isolado por pacote
- fetch de fontes (com cache)
- extrai fontes em sandbox/build
- aplica patches declarados em .meta
- executa fases de build: pre-build hooks, build commands, check, install
- empacota o resultado (tar.gz) e grava metadados em pkg_db
- logging detalhado, rollback e opção de manter sandbox para depuração
- parâmetros: jobs, timeout (por comando), keep_sandbox, stages a executar
"""

from __future__ import annotations

import os
import shutil
import tarfile
import time
import hashlib
from typing import List, Optional, Tuple

from ibuild1.0.modules_py import (
    config,
    log,
    utils,
    meta,
    sandbox,
    dependency,
    sync,
)

# Exceções locais
class BuildError(Exception):
    pass

class FetchError(BuildError):
    pass

class PatchError(BuildError):
    pass

class InstallError(BuildError):
    pass

# Helpers --------------------------------------------------------------------
def _artifact_name(pkg_meta: dict) -> str:
    """Nome do artefato final: <name>-<version>.tar.gz"""
    return f"{pkg_meta['name']}-{pkg_meta.get('version','0')}.tar.gz"

def _artifact_path(pkg_meta: dict) -> str:
    out_dir = os.path.join(config.get("cache_dir"), "packages")
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, _artifact_name(pkg_meta))

def _pkg_db_meta_path(pkg_meta: dict) -> str:
    pkg_db = config.get("pkg_db")
    os.makedirs(pkg_db, exist_ok=True)
    fname = f"{pkg_meta['name']}.installed.meta"
    return os.path.join(pkg_db, fname)

def _checksum_file(path: str, algo: str = "sha256") -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

# Core steps -----------------------------------------------------------------
def fetch_source(pkg_meta: dict, dest_dir: str, force: bool = False) -> str:
    """
    Baixa a fonte definida em pkg_meta['source'] para dest_dir.
    source can be:
      - string URL
      - dict {url:..., sha256:...}
      - git repo dict {git: url, checkout: tag/commit}
    Retorna caminho para o arquivo baixado (ou dir para git clone).
    """
    src = pkg_meta.get("source")
    if not src:
        raise FetchError("Source não definido no .meta")

    ensure_dir = lambda p: os.makedirs(p, exist_ok=True)
    ensure_dir(dest_dir)

    # source can be dict or string
    if isinstance(src, str):
        url = src
        sha = None
        # infer filename
        filename = os.path.basename(url.split("?")[0])
        dest = os.path.join(dest_dir, filename)
        return utils.download(url, dest, expected_sha256=sha)

    if isinstance(src, dict):
        # git source
        if "git" in src:
            git_url = src["git"]
            checkout = src.get("checkout", "HEAD")
            clone_dir = os.path.join(dest_dir, "source_git")
            if os.path.isdir(clone_dir):
                shutil.rmtree(clone_dir)
            log.info("Clonando git %s @ %s", git_url, checkout)
            utils.run(["git", "clone", "--depth", "1", "--branch", checkout, git_url, clone_dir], check=True)
            return clone_dir

        # http/ftp style
        url = src.get("url")
        sha = src.get("sha256")
        if not url:
            raise FetchError("Source dict inválido: sem 'url' ou 'git'")

        filename = os.path.basename(url.split("?")[0])
        dest = os.path.join(dest_dir, filename)
        return utils.download(url, dest, expected_sha256=sha)

    raise FetchError("Formato de source não suportado")

def extract_source(archive_path: str, dest_dir: str) -> str:
    """
    Extrai tarballs para dest_dir. Se archive_path for dir (git clone), apenas retorna o dir.
    Retorna caminho da árvore de fonte (possivelmente um subdir se o tar extrai uma pasta).
    """
    if os.path.isdir(archive_path):
        return archive_path

    # suportar tar.* via utils.extract_tarball
    utils.extract_tarball(archive_path, dest_dir)

    # detectar diretório raíz (caso o tar crie um folder)
    entries = [e for e in os.listdir(dest_dir) if not e.startswith(".")]
    if len(entries) == 1 and os.path.isdir(os.path.join(dest_dir, entries[0])):
        return os.path.join(dest_dir, entries[0])
    return dest_dir

def apply_all_patches(pkg_meta: dict, src_tree: str):
    patches = pkg_meta.get("_patches", [])
    for p in patches:
        if not os.path.isfile(p):
            log.warn("Patch declarado não encontrado: %s", p)
            continue
        try:
            utils.apply_patch(p, src_tree, strip=1)
        except Exception as e:
            log.exception("Falha ao aplicar patch %s: %s", p, e)
            raise PatchError(f"Falha ao aplicar patch {p}: {e}")

def run_build_commands_in_sandbox(pkg_name: str, pkg_meta: dict, src_tree: str,
                                  jobs: Optional[int] = None, timeout: Optional[int] = None):
    """
    Executa sequencia de comandos de build conforme pkg_meta['build'] (lista de strings).
    Cada comando é executado no sandbox/build (src_tree) via sandbox.run_in_sandbox.
    """
    build_cmds = pkg_meta.get("build", []) or []
    if not build_cmds:
        log.warn("Nenhum comando de build declarado em %s", pkg_meta['name'])
        return

    # preparar ambiente: passar JOBS como environment
    env = {}
    if jobs:
        env["MAKEFLAGS"] = f"-j{jobs}"
        env["JOBS"] = str(jobs)

    # executar cada linha
    for line in build_cmds:
        # permitir que build entries sejam strings (shell) ou listas (args)
        if isinstance(line, str):
            cmd = ["bash", "-c", line]
        else:
            cmd = list(line)

        # usar phase=build para log
        sandbox.run_in_sandbox(pkg_name, cmd, cwd=src_tree, env=env, phase="build")

def run_check_commands(pkg_name: str, pkg_meta: dict, src_tree: str):
    checks = pkg_meta.get("check", []) or []
    for line in checks:
        if isinstance(line, str):
            cmd = ["bash", "-c", line]
        else:
            cmd = list(line)
        sandbox.run_in_sandbox(pkg_name, cmd, cwd=src_tree, phase="check")

def run_install_commands(pkg_name: str, pkg_meta: dict, src_tree: str):
    installs = pkg_meta.get("install", []) or []
    if not installs:
        log.warn("Nenhum comando de install declarado para %s", pkg_meta['name'])
    for line in installs:
        if isinstance(line, str):
            # garantir que DESTDIR seja respeitado; muitos Makefiles aceitam DESTDIR env var
            cmd = ["bash", "-c", line]
        else:
            cmd = list(line)
        sandbox.run_in_sandbox(pkg_name, cmd, cwd=src_tree, phase="install")

# Orquestra ------------------------------------------------------------------
def build_package(pkg_name: str,
                  category: Optional[str] = None,
                  resolve_deps: bool = True,
                  include_optional: bool = False,
                  jobs: Optional[int] = None,
                  keep_sandbox: bool = False,
                  stages: Optional[List[str]] = None,
                  force_fetch: bool = False) -> Tuple[str, dict]:
    """
    Pipeline principal para construir um pacote.
    Retorna (artifact_path, pkg_meta) no sucesso.

    stages: lista de fases a executar na ordem possível. Valores possíveis:
      - fetch, extract, patch, build, check, install, package
    """
    stages = stages or ["fetch", "extract", "patch", "build", "check", "install", "package"]

    # carregar meta
    pkg_meta = meta.load_meta(pkg_name, category)
    log.info("Iniciando build para %s (%s)", pkg_meta["name"], pkg_meta.get("version"))

    # opcional: resolver dependências (para garantir que deps estejam construídos antes)
    if resolve_deps:
        try:
            order, metas = dependency.resolve([pkg_name], include_optional=include_optional, prefer_provided=True)
            # order includes pkg and dependencies; garantir que todos deps já estejam construidos or note
            log.debug("Ordem de dependências: %s", ", ".join(order))
        except Exception as e:
            log.warn("Falha ao resolver dependências automaticamente: %s", e)

    # preparar sandbox
    sb_name = f"{pkg_meta['name']}-{pkg_meta.get('version','0')}"
    sandbox.create_sandbox(sb_name, binds=[os.path.join(config.get("repo_dir"))], keep=keep_sandbox)

    # área temporária para origem dentro do cache
    tmp_src_cache = os.path.join(config.get("cache_dir"), "sources", pkg_meta["name"])
    os.makedirs(tmp_src_cache, exist_ok=True)

    artifact_path = _artifact_path(pkg_meta)
    src_artifact = None
    src_tree = None

    try:
        # FETCH
        if "fetch" in stages:
            log.info("Fase: fetch")
            src_artifact = fetch_source(pkg_meta, tmp_src_cache, force=force_fetch)
            log.info("Fonte disponível em %s", src_artifact)

        # EXTRACT
        if "extract" in stages:
            log.info("Fase: extract")
            # extrair dentro do sandbox build dir
            sandbox_build_dir = os.path.join(sandbox.sandbox_root(sb_name), "build")
            # garantir diretório de trabalho
            if os.path.exists(sandbox_build_dir):
                shutil.rmtree(sandbox_build_dir)
            os.makedirs(sandbox_build_dir, exist_ok=True)

            src_tree = extract_source(src_artifact, sandbox_build_dir)
            log.info("Fonte extraída em %s", src_tree)

        # PATCH
        if "patch" in stages:
            log.info("Fase: patch")
            # patches declarados em pkg_meta["_patches"] (meta.py preenche)
            # aplicar no src_tree
            apply_all_patches(pkg_meta, src_tree)

        # BUILD
        if "build" in stages:
            log.info("Fase: build")
            run_build_commands_in_sandbox(sb_name, pkg_meta, src_tree, jobs=jobs)

        # CHECK
        if "check" in stages:
            log.info("Fase: check")
            run_check_commands(sb_name, pkg_meta, src_tree)

        # INSTALL
        if "install" in stages:
            log.info("Fase: install")
            # garantir DESTDIR em sandbox/install via sandbox._sandbox_env, handled by run_in_sandbox
            run_install_commands(sb_name, pkg_meta, src_tree)

        # PACKAGE
        if "package" in stages:
            log.info("Fase: package")
            install_dir = os.path.join(sandbox.sandbox_root(sb_name), "install")
            if not os.path.isdir(install_dir):
                log.warn("Diretório de install não existe (%s), criando vazio para empacotar", install_dir)
                os.makedirs(install_dir, exist_ok=True)

            # criar tar.gz do conteúdo do install_dir
            base_name = os.path.join(os.path.dirname(artifact_path), f"{pkg_meta['name']}-{pkg_meta.get('version','0')}")
            # remove se existir
            if os.path.exists(artifact_path):
                os.remove(artifact_path)
            log.info("Empacotando install -> %s", artifact_path)
            shutil.make_archive(base_name, 'gztar', root_dir=install_dir)
            # shutil.make_archive cria base_name + .tar.gz
            packaged = f"{base_name}.tar.gz"
            # mover para artifact_path se necessário
            if packaged != artifact_path:
                shutil.move(packaged, artifact_path)

            # gravar checksum
            chksum = _checksum_file(artifact_path)
            log.info("Artefato criado: %s (sha256=%s)", artifact_path, chksum)

            # gravar .installed.meta com info
            installed_meta = {
                "name": pkg_meta["name"],
                "version": pkg_meta.get("version"),
                "artifact": artifact_path,
                "sha256": chksum,
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "meta_source": pkg_meta.get("_meta_path"),
            }
            with open(_pkg_db_meta_path(pkg_meta), "w", encoding="utf-8") as f:
                import json
                json.dump(installed_meta, f, indent=2)

        log.info("Build finalizado com sucesso: %s", pkg_meta["name"])
        return artifact_path, pkg_meta

    except Exception as e:
        log.exception("Erro durante build de %s: %s", pkg_meta["name"], e)
        # não sobrescrever artefato se falhou; opcionalmente manter sandbox para depuração
        if not keep_sandbox:
            try:
                sandbox.destroy_sandbox(sb_name)
            except Exception:
                log.warn("Falha ao remover sandbox após erro")
        raise BuildError(f"Build falhou para {pkg_meta['name']}: {e}") from e

    finally:
        # se pediu para não manter sandbox e não houve exceção, remover
        if keep_sandbox:
            log.info("keep_sandbox=True → mantendo sandbox %s para inspeção", sb_name)
        else:
            try:
                sandbox.destroy_sandbox(sb_name)
            except Exception:
                log.warn("Erro ao remover sandbox no finally")
