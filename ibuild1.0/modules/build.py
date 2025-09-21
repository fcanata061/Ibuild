# build.py
"""
Orquestrador de build do Ibuild com suporte a hooks.

Pipeline:
  fetch → extract → patch → build → check → install → package

Hooks:
  - pre_fetch / post_fetch
  - pre_extract / post_extract
  - pre_patch / post_patch
  - pre_build / post_build
  - pre_check / post_check
  - pre_install / post_install
  - pre_package / post_package

Cada hook é uma lista de comandos (string ou lista) declarados em pkg_meta["hooks"].
Executados dentro do sandbox com sandbox.run_in_sandbox().
"""

from __future__ import annotations
import os
import shutil
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
)

# Exceções
class BuildError(Exception): pass
class FetchError(BuildError): pass
class PatchError(BuildError): pass
class InstallError(BuildError): pass

# Helpers ---------------------------------------------------------------
def _artifact_name(pkg_meta: dict) -> str:
    return f"{pkg_meta['name']}-{pkg_meta.get('version','0')}.tar.gz"

def _artifact_path(pkg_meta: dict) -> str:
    out_dir = os.path.join(config.get("cache_dir"), "packages")
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, _artifact_name(pkg_meta))

def _pkg_db_meta_path(pkg_meta: dict) -> str:
    pkg_db = config.get("pkg_db")
    os.makedirs(pkg_db, exist_ok=True)
    return os.path.join(pkg_db, f"{pkg_meta['name']}.installed.meta")

def _checksum_file(path: str, algo: str = "sha256") -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

# Hooks ---------------------------------------------------------------
def run_hooks(pkg_name: str, pkg_meta: dict, phase: str, cwd: Optional[str] = None):
    hooks = pkg_meta.get("hooks", {})
    cmds = hooks.get(phase, [])
    if not cmds: return
    log.info("Executando hooks %s (%d)", phase, len(cmds))
    for cmd in cmds:
        if isinstance(cmd, str):
            cmd = ["bash", "-c", cmd]
        sandbox.run_in_sandbox(pkg_name, cmd, cwd=cwd, phase=phase)

# Core steps ---------------------------------------------------------------
def fetch_source(pkg_name: str, pkg_meta: dict, dest_dir: str, force: bool = False) -> str:
    src = pkg_meta.get("source")
    if not src:
        raise FetchError("Source não definido no .meta")
    os.makedirs(dest_dir, exist_ok=True)
    run_hooks(pkg_name, pkg_meta, "pre_fetch", cwd=dest_dir)
    if isinstance(src, str):
        url = src
        dest = os.path.join(dest_dir, os.path.basename(url.split("?")[0]))
        path = utils.download(url, dest)
    elif isinstance(src, dict):
        if "git" in src:
            clone_dir = os.path.join(dest_dir, "source_git")
            if os.path.isdir(clone_dir):
                shutil.rmtree(clone_dir)
            utils.run(["git", "clone", "--depth", "1", src["git"], clone_dir], check=True)
            path = clone_dir
        else:
            url = src.get("url")
            sha = src.get("sha256")
            dest = os.path.join(dest_dir, os.path.basename(url.split("?")[0]))
            path = utils.download(url, dest, expected_sha256=sha)
    else:
        raise FetchError("Formato de source inválido")
    run_hooks(pkg_name, pkg_meta, "post_fetch", cwd=dest_dir)
    return path

def extract_source(pkg_name: str, pkg_meta: dict, archive_path: str, dest_dir: str) -> str:
    run_hooks(pkg_name, pkg_meta, "pre_extract", cwd=dest_dir)
    if os.path.isdir(archive_path):
        src_tree = archive_path
    else:
        utils.extract_tarball(archive_path, dest_dir)
        entries = [e for e in os.listdir(dest_dir) if not e.startswith(".")]
        if len(entries) == 1 and os.path.isdir(os.path.join(dest_dir, entries[0])):
            src_tree = os.path.join(dest_dir, entries[0])
        else:
            src_tree = dest_dir
    run_hooks(pkg_name, pkg_meta, "post_extract", cwd=src_tree)
    return src_tree

def apply_all_patches(pkg_name: str, pkg_meta: dict, src_tree: str):
    run_hooks(pkg_name, pkg_meta, "pre_patch", cwd=src_tree)
    for p in pkg_meta.get("_patches", []):
        if os.path.isfile(p):
            utils.apply_patch(p, src_tree, strip=1)
        else:
            log.warn("Patch não encontrado: %s", p)
    run_hooks(pkg_name, pkg_meta, "post_patch", cwd=src_tree)

def run_build(pkg_name: str, pkg_meta: dict, src_tree: str, jobs: Optional[int] = None):
    run_hooks(pkg_name, pkg_meta, "pre_build", cwd=src_tree)
    env = {}
    if jobs: env["JOBS"] = str(jobs)
    for line in pkg_meta.get("build", []):
        cmd = ["bash", "-c", line] if isinstance(line, str) else line
        sandbox.run_in_sandbox(pkg_name, cmd, cwd=src_tree, env=env, phase="build")
    run_hooks(pkg_name, pkg_meta, "post_build", cwd=src_tree)

def run_check(pkg_name: str, pkg_meta: dict, src_tree: str):
    run_hooks(pkg_name, pkg_meta, "pre_check", cwd=src_tree)
    for line in pkg_meta.get("check", []):
        cmd = ["bash", "-c", line] if isinstance(line, str) else line
        sandbox.run_in_sandbox(pkg_name, cmd, cwd=src_tree, phase="check")
    run_hooks(pkg_name, pkg_meta, "post_check", cwd=src_tree)

def run_install(pkg_name: str, pkg_meta: dict, src_tree: str):
    run_hooks(pkg_name, pkg_meta, "pre_install", cwd=src_tree)
    for line in pkg_meta.get("install", []):
        cmd = ["bash", "-c", line] if isinstance(line, str) else line
        sandbox.run_in_sandbox(pkg_name, cmd, cwd=src_tree, phase="install")
    run_hooks(pkg_name, pkg_meta, "post_install", cwd=src_tree)

def package_artifact(pkg_name: str, pkg_meta: dict, sb_root: str) -> str:
    run_hooks(pkg_name, pkg_meta, "pre_package", cwd=sb_root)
    install_dir = os.path.join(sb_root, "install")
    os.makedirs(install_dir, exist_ok=True)
    artifact_path = _artifact_path(pkg_meta)
    base_name = artifact_path[:-7]  # remove .tar.gz
    if os.path.exists(artifact_path): os.remove(artifact_path)
    log.info("Empacotando -> %s", artifact_path)
    shutil.make_archive(base_name, "gztar", root_dir=install_dir)
    # checksum + registro
    chksum = _checksum_file(artifact_path)
    installed_meta = {
        "name": pkg_meta["name"],
        "version": pkg_meta.get("version"),
        "artifact": artifact_path,
        "sha256": chksum,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "meta_source": pkg_meta.get("_meta_path"),
    }
    with open(_pkg_db_meta_path(pkg_meta), "w", encoding="utf-8") as f:
        import json; json.dump(installed_meta, f, indent=2)
    run_hooks(pkg_name, pkg_meta, "post_package", cwd=sb_root)
    return artifact_path

# Orquestrador ---------------------------------------------------------------
def build_package(pkg_name: str,
                  category: Optional[str] = None,
                  resolve_deps: bool = True,
                  jobs: Optional[int] = None,
                  keep_sandbox: bool = False,
                  stages: Optional[List[str]] = None) -> Tuple[str, dict]:
    stages = stages or ["fetch","extract","patch","build","check","install","package"]
    pkg_meta = meta.load_meta(pkg_name, category)
    sb_name = f"{pkg_meta['name']}-{pkg_meta.get('version','0')}"
    sandbox.create_sandbox(sb_name, binds=[config.get("repo_dir")], keep=keep_sandbox)
    tmp_src_cache = os.path.join(config.get("cache_dir"), "sources", pkg_meta["name"])
    os.makedirs(tmp_src_cache, exist_ok=True)

    artifact_path = None
    try:
        src_artifact = None
        src_tree = None
        if "fetch" in stages:
            src_artifact = fetch_source(sb_name, pkg_meta, tmp_src_cache)
        if "extract" in stages:
            sb_build_dir = os.path.join(sandbox.sandbox_root(sb_name), "build")
            if os.path.exists(sb_build_dir): shutil.rmtree(sb_build_dir)
            os.makedirs(sb_build_dir, exist_ok=True)
            src_tree = extract_source(sb_name, pkg_meta, src_artifact, sb_build_dir)
        if "patch" in stages: apply_all_patches(sb_name, pkg_meta, src_tree)
        if "build" in stages: run_build(sb_name, pkg_meta, src_tree, jobs=jobs)
        if "check" in stages: run_check(sb_name, pkg_meta, src_tree)
        if "install" in stages: run_install(sb_name, pkg_meta, src_tree)
        if "package" in stages:
            artifact_path = package_artifact(sb_name, pkg_meta, sandbox.sandbox_root(sb_name))
        return artifact_path, pkg_meta
    except Exception as e:
        log.exception("Erro em build de %s: %s", pkg_name, e)
        raise BuildError(f"Falha em {pkg_name}: {e}") from e
    finally:
        if keep_sandbox:
            log.info("Mantendo sandbox %s para depuração", sb_name)
        else:
            sandbox.destroy_sandbox(sb_name)
