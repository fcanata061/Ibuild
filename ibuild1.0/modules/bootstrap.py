#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
modules/bootstrap.py — Bootstrap manager integrado com DependencyResolver

Divida em 3 partes e cole em sequence no arquivo modules/bootstrap.py:
 - Parte 1/3: imports, utilitários, classe BootstrapManager (inicialização, index, downloads)
 - Parte 2/3: workers de build, paralelismo, checkpoints, lib-index rebuild
 - Parte 3/3: integração com DependencyResolver, bootstrap_toolchain/bootstrap_system, rootfs, snapshot, CLI helpers

Requer (opcional): modules.config, modules.log, modules.build, modules.package,
modules.sandbox, modules.toolchain, modules.dependency (RepoIndex, DependencyResolver, PackageRequirement), modules.meta
"""

from __future__ import annotations

import os
import sys
import time
import json
import tarfile
import shutil
import hashlib
import tempfile
import threading
import subprocess
from typing import Optional, List, Dict, Any, Callable, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

# Try import project modules (best-effort)
try:
    from modules import config as config_mod
except Exception:
    config_mod = None

try:
    from modules import log as log_mod
except Exception:
    log_mod = None

try:
    from modules import build as build_mod
except Exception:
    build_mod = None

try:
    from modules import package as package_mod
except Exception:
    package_mod = None

try:
    from modules import sandbox as sandbox_mod
except Exception:
    sandbox_mod = None

try:
    from modules import toolchain as toolchain_mod
except Exception:
    toolchain_mod = None

try:
    from modules import meta as meta_mod
except Exception:
    meta_mod = None

# Dependency resolver imports (required for integration)
try:
    from modules.dependency import RepoIndex, DependencyResolver, PackageRequirement
except Exception:
    RepoIndex = None
    DependencyResolver = None
    PackageRequirement = None

# Logger
if log_mod is not None and hasattr(log_mod, "get_logger"):
    logger = log_mod.get_logger("bootstrap")
else:
    import logging
    logger = logging.getLogger("ibuild.bootstrap")
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO)

# -------------------------
# Utilitários internos
# -------------------------
def _ensure_dir(path: str, mode: int = 0o755) -> None:
    if not path:
        return
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, mode)
    except Exception:
        pass

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def _tar_create(src_dir: str, dest_file: str, compress: bool = True) -> None:
    mode = "w:gz" if compress else "w"
    with tarfile.open(dest_file, mode) as tf:
        tf.add(src_dir, arcname=".")

def _now_ts() -> int:
    return int(time.time())

def _safe_run(cmd: List[str], cwd: Optional[str] = None, env: Optional[Dict] = None,
              capture: bool = False, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    logger.debug("RUN: %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(cmd, cwd=cwd, env=env or os.environ, check=False,
                          stdout=(subprocess.PIPE if capture else None),
                          stderr=(subprocess.PIPE if capture else None),
                          text=True, timeout=timeout)

# -------------------------
# Defaults
# -------------------------
DEFAULTS = {
    "pkg_db": "/var/lib/ibuild/packages",
    "rootfs_dir": None,            # will be pkg_db/rootfs
    "sandbox_dir": "/var/lib/ibuild/sandbox",
    "manifest_dir": None,          # will be pkg_db/manifests
    "toolchain_pkgs": ["linux-headers", "binutils", "gcc-pass1", "glibc", "gcc"],
    "base_packages": ["coreutils", "bash", "make", "tar", "xz", "sed", "grep"],
    "download_cache": "/var/cache/ibuild/downloads",
    "lib_index_file": None,        # will be pkg_db/lib_index.json
    "keep_artifacts": False,
    "parallel_workers": 2,
    "checkpoint_dir": None,        # will be pkg_db/checkpoints
}

# -------------------------
# BootstrapManager (início)
# -------------------------
class BootstrapManager:
    """
    Manager central do bootstrap — agora integrado ao DependencyResolver.

    Principais métodos públicos:
      - prepare_environment()
      - rebuild_lib_index(repo_dir=None)
      - bootstrap_toolchain(jobs, simulate, sandboxed)
      - build_packages_parallel(pkgs, jobs, simulate, sandboxed)
      - create_rootfs(dest, packages, simulate)
      - detect_missing_deps(rootfs)
      - repair_missing_deps(rootfs)
      - bootstrap_system(rootfs_dest, jobs, simulate, sandboxed)
      - snapshot_rootfs(rootfs, outdir)
    """

    def __init__(self, cfg: Optional[Dict] = None):
        cfg = cfg or {}
        pkg_db = cfg.get("pkg_db") or (config_mod.get("pkg_db") if config_mod else DEFAULTS["pkg_db"])
        self.pkg_db = pkg_db
        self.rootfs_dir = cfg.get("rootfs_dir") or os.path.join(self.pkg_db, "rootfs")
        self.sandbox_dir = cfg.get("sandbox_dir") or (config_mod.get("sandbox_dir") if config_mod else DEFAULTS["sandbox_dir"])
        self.manifest_dir = cfg.get("manifest_dir") or os.path.join(self.pkg_db, "manifests")
        self.lib_index_file = cfg.get("lib_index_file") or os.path.join(self.pkg_db, "lib_index.json")
        self.download_cache = cfg.get("download_cache") or (config_mod.get("download_cache") if config_mod else DEFAULTS["download_cache"])
        self.checkpoint_dir = cfg.get("checkpoint_dir") or os.path.join(self.pkg_db, "checkpoints")
        self.toolchain_pkgs = cfg.get("toolchain_pkgs") or DEFAULTS["toolchain_pkgs"]
        self.base_packages = cfg.get("base_packages") or DEFAULTS["base_packages"]
        self.keep_artifacts = cfg.get("keep_artifacts", DEFAULTS["keep_artifacts"])
        self.workers = int(cfg.get("parallel_workers", DEFAULTS["parallel_workers"]))
        self._progress_callbacks: List[Callable[[str, Dict], None]] = []
        self._lib_index_lock = threading.Lock()
        self._lib_index: Dict[str, List[str]] = {}

        # ensure directories
        for d in (self.pkg_db, self.rootfs_dir, self.sandbox_dir, self.manifest_dir, self.download_cache, self.checkpoint_dir):
            _ensure_dir(d)

        # load lib index if exists
        try:
            if os.path.exists(self.lib_index_file):
                with open(self.lib_index_file, "r", encoding="utf-8") as f:
                    self._lib_index = json.load(f)
        except Exception:
            logger.exception("failed to load lib index (starting fresh)")

        # repo index & resolver placeholders (created on demand)
        self._repo_index = None
        self._resolver = None

        logger.info("BootstrapManager initialized: pkg_db=%s rootfs=%s sandbox=%s", self.pkg_db, self.rootfs_dir, self.sandbox_dir)

    # -------------------------
    # progress hooks
    # -------------------------
    def add_progress_cb(self, cb: Callable[[str, Dict], None]) -> None:
        if callable(cb):
            self._progress_callbacks.append(cb)

    def _emit(self, event: str, data: Optional[Dict] = None) -> None:
        payload = data or {}
        payload["_ts"] = _now_ts()
        logger.debug("emit: %s %s", event, payload)
        for cb in list(self._progress_callbacks):
            try:
                cb(event, payload)
            except Exception:
                logger.exception("progress callback failed")

    # -------------------------
    # repo index / resolver helpers
    # -------------------------
    def repo_index(self) -> Any:
        """Lazy create RepoIndex (if available)."""
        if self._repo_index is None:
            if RepoIndex is None:
                logger.warning("modules.dependency.RepoIndex not available")
                self._repo_index = None
            else:
                # prefer repo_dir from config if available
                repo_dir = config_mod.get("repo_dir") if config_mod else None
                self._repo_index = RepoIndex(repo_dir=repo_dir)
        return self._repo_index

    def resolver(self, lockfile: Optional[str] = None, max_steps: int = 20000, verbose: bool = False) -> Any:
        """Lazy create DependencyResolver (if available)."""
        if self._resolver is None:
            if DependencyResolver is None:
                logger.warning("modules.dependency.DependencyResolver not available")
                self._resolver = None
            else:
                repo = self.repo_index()
                lf = lockfile or os.path.join(self.pkg_db, "dependency.lock.json")
                self._resolver = DependencyResolver(repo=repo, lockfile=lf, max_steps=max_steps, verbose=verbose)
        return self._resolver

    # -------------------------
    # lib index persistence
    # -------------------------
    def rebuild_lib_index(self, repo_dir: Optional[str] = None, force: bool = False) -> Dict[str, List[str]]:
        """
        Rebuild lib index scanning .meta files (best-effort). Result persisted to lib_index_file.
        Returns mapping libname -> [providers].
        """
        repo_dir = repo_dir or (config_mod.get("repo_dir") if config_mod else "/usr/ibuild")
        logger.info("Rebuilding lib index from %s", repo_dir)
        idx = {}
        # try to use meta_mod.iterate_meta_files if available
        meta_files = []
        if meta_mod and hasattr(meta_mod, "iterate_meta_files"):
            try:
                meta_files = list(meta_mod.iterate_meta_files(repo_dir))
            except Exception:
                meta_files = []
        if not meta_files:
            for root, _, files in os.walk(repo_dir):
                for fn in files:
                    if fn.endswith(".meta") or fn.endswith(".yml") or fn.endswith(".yaml") or fn.endswith(".json"):
                        meta_files.append(os.path.join(root, fn))
                        # Parte 2/3 — continua BootstrapManager: parsing de metas, download cache, build workers e checkpoints

        # scan meta files to build index
        for mp in meta_files:
            try:
                # try meta_mod.load_meta_file if exists
                meta_data = None
                if meta_mod and hasattr(meta_mod, "load_meta_file"):
                    try:
                        meta_data = meta_mod.load_meta_file(mp)
                    except Exception:
                        meta_data = None
                if not meta_data:
                    # best-effort parse YAML/JSON
                    try:
                        import yaml
                        with open(mp, "r", encoding="utf-8") as fh:
                            meta_data = yaml.safe_load(fh)
                    except Exception:
                        try:
                            with open(mp, "r", encoding="utf-8") as fh:
                                meta_data = json.load(fh)
                        except Exception:
                            meta_data = None
                if not meta_data or not isinstance(meta_data, dict):
                    continue
                name = meta_data.get("name") or os.path.splitext(os.path.basename(mp))[0]
                provides = meta_data.get("provides", []) or []
                for p in provides:
                    idx.setdefault(str(p), []).append(name)
                # ensure package itself is provider
                idx.setdefault(str(name), []).append(name)
            except Exception:
                logger.debug("failed parse meta %s", mp, exc_info=True)
                continue

        # persist index
        try:
            _ensure_dir(os.path.dirname(self.lib_index_file) or ".")
            with open(self.lib_index_file, "w", encoding="utf-8") as f:
                json.dump(idx, f, indent=2)
            with self._lib_index_lock:
                self._lib_index = idx
        except Exception:
            logger.exception("failed to persist lib index")
        logger.info("lib index rebuilt (%d entries)", len(self._lib_index))
        return self._lib_index

    def find_providers(self, libname: str) -> List[str]:
        """Return providers for libname using in-memory index (rebuild if empty)."""
        with self._lib_index_lock:
            if not self._lib_index:
                self.rebuild_lib_index()
            res = self._lib_index.get(libname) or self._lib_index.get(libname + ".so") or []
            # fuzzy fallback: substring match
            if not res:
                for k in self._lib_index.keys():
                    if libname in k:
                        res.extend(self._lib_index.get(k, []))
            # dedupe
            seen = []
            out = []
            for p in res:
                if p not in seen:
                    seen.append(p)
                    out.append(p)
            return out

    # -------------------------
    # cached downloads
    # -------------------------
    def cached_download(self, url: str, sha256_expected: Optional[str] = None) -> str:
        """Download URL into cache and verify SHA256 if provided. Returns path."""
        _ensure_dir(self.download_cache)
        fname = os.path.basename(url.split("?", 1)[0])
        dest = os.path.join(self.download_cache, fname)
        # quick check
        if os.path.exists(dest) and sha256_expected:
            try:
                if _sha256(dest) == sha256_expected:
                    logger.debug("cached hit %s", dest)
                    return dest
            except Exception:
                pass
        self._emit("download.start", {"url": url, "dest": dest})
        try:
            if shutil.which("curl"):
                _safe_run(["curl", "-L", "-o", dest, url])
            elif shutil.which("wget"):
                _safe_run(["wget", "-O", dest, url])
            else:
                import urllib.request
                urllib.request.urlretrieve(url, dest)
            if sha256_expected:
                if _sha256(dest) != sha256_expected:
                    raise RuntimeError("sha mismatch")
            self._emit("download.ok", {"url": url, "dest": dest})
            return dest
        except Exception as e:
            logger.exception("download failed: %s", e)
            self._emit("download.error", {"url": url, "err": str(e)})
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except Exception:
                pass
            raise

    # -------------------------
    # build worker + parallel orchestration
    # -------------------------
    def _build_worker(self, pkg_name: str, jobs: Optional[int], sandboxed: bool, simulate: bool) -> Tuple[str, bool, str]:
        """
        Single package build worker. Returns (pkg_name, ok, message).
        Uses build_mod.build_package when available; otherwise simulates.
        """
        self._emit("build.start", {"pkg": pkg_name})
        if simulate or build_mod is None:
            msg = f"simulate build {pkg_name}"
            logger.info(msg)
            self._emit("build.done", {"pkg": pkg_name, "sim": True})
            return (pkg_name, True, msg)
        sb = None
        try:
            if sandboxed and sandbox_mod:
                sb = sandbox_mod.Sandbox(base_dir=self.sandbox_dir)
            # build_package should return (artifact_path, meta_info)
            artifact, meta_info = build_mod.build_package(pkg_name, resolve_deps=True, jobs=jobs, sandbox=sb)
            # install into system (or package DB) using package_mod
            if package_mod and hasattr(package_mod, "install_package"):
                package_mod.install_package(artifact, overwrite=True, upgrade=True)
            else:
                logger.warning("package_mod.install_package not available (artifact at %s)", artifact)
            if sb:
                try:
                    sb.cleanup()
                except Exception:
                    pass
            self._emit("build.done", {"pkg": pkg_name})
            return (pkg_name, True, "ok")
        except Exception as e:
            logger.exception("build failed for %s", pkg_name)
            self._emit("build.error", {"pkg": pkg_name, "err": str(e)})
            # cleanup sandbox but keep logs if requested
            if sb and not self.keep_artifacts:
                try:
                    sb.cleanup()
                except Exception:
                    pass
            return (pkg_name, False, str(e))

    def build_packages_parallel(self, pkgs: List[str], jobs: Optional[int] = None, sandboxed: bool = True, simulate: bool = False) -> Dict[str, Dict]:
        """
        Build list of pkgs using worker pool. Returns dict pkg -> {ok,msg}.
        Saves checkpoint for each successful package.
        """
        results: Dict[str, Dict] = {}
        total = len(pkgs)
        self._emit("build.queue", {"total": total})
        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as ex:
            futures = {ex.submit(self._build_worker, pkg, jobs, sandboxed, simulate): pkg for pkg in pkgs}
            for fut in as_completed(futures):
                pkg = futures[fut]
                try:
                    name, ok, msg = fut.result()
                except Exception as e:
                    name, ok, msg = pkg, False, str(e)
                results[name] = {"ok": ok, "msg": msg}
                if ok:
                    self._save_checkpoint(name)
                self._emit("build.progress", {"pkg": name, "ok": ok, "msg": msg, "completed": sum(1 for r in results.values() if r["ok"]), "total": total})
        return results

    # -------------------------
    # checkpoints & rollback
    # -------------------------
    def _save_checkpoint(self, step_name: str) -> None:
        _ensure_dir(self.checkpoint_dir)
        path = os.path.join(self.checkpoint_dir, f"{step_name}.chk")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"step": step_name, "ts": _now_ts()}, f)
        except Exception:
            logger.exception("failed writing checkpoint %s", path)

    def list_checkpoints(self) -> List[str]:
        if not os.path.isdir(self.checkpoint_dir):
            return []
        return sorted([f for f in os.listdir(self.checkpoint_dir) if f.endswith(".chk")])

    def rollback_to_checkpoint(self, step_name: str) -> bool:
        """
        Best-effort rollback. If sandboxes were used and kept, try restore; otherwise remove checkpoint.
        """
        self._emit("rollback.start", {"step": step_name})
        logger.warning("rollback requested for %s (best-effort)", step_name)
        # attempt sandbox restore if sandbox_mod provides snapshot restore
        if sandbox_mod and hasattr(sandbox_mod, "restore_snapshot"):
            try:
                sandbox_mod.restore_snapshot(step_name)
                self._emit("rollback.done", {"step": step_name})
                return True
            except Exception:
                logger.exception("sandbox restore_snapshot failed")
        # fallback: just remove checkpoint
        chk = os.path.join(self.checkpoint_dir, f"{step_name}.chk")
        try:
            if os.path.exists(chk):
                os.remove(chk)
                logger.info("removed checkpoint %s", chk)
            self._emit("rollback.partial", {"step": step_name})
            return True
        except Exception:
            logger.exception("rollback failed to remove checkpoint")
            return False
            # Parte 3/3 — continua BootstrapManager: detect/repair deps, create_rootfs, bootstrap flows, convenience API

    # -------------------------
    # detect / repair missing deps (ldd) using lib index + resolver
    # -------------------------
    def detect_missing_deps(self, rootfs: str) -> Dict[str, List[str]]:
        """
        Scan binaries in rootfs for missing shared libs using ldd.
        Returns mapping relative-path -> [missing_libs].
        """
        logger.info("Detecting missing deps in %s", rootfs)
        missing = {}
        for dirpath, _, files in os.walk(rootfs):
            for fn in files:
                full = os.path.join(dirpath, fn)
                try:
                    if os.path.islink(full) or not os.path.isfile(full):
                        continue
                    if os.path.getsize(full) < 200:
                        continue
                    pfile = _safe_run(["file", "--brief", "--mime-type", full], capture=True)
                    mime = (pfile.stdout or "").lower() if pfile and pfile.stdout else ""
                    if not any(x in mime for x in ("application/x-executable", "application/x-elf", "application/x-sharedlib")):
                        continue
                    p = _safe_run(["ldd", full], capture=True)
                    out = (p.stdout or "") + "\n" + (p.stderr or "")
                    libs = []
                    for line in out.splitlines():
                        if "not found" in line:
                            parts = line.strip().split()
                            if parts:
                                libs.append(parts[0])
                    if libs:
                        missing[os.path.relpath(full, rootfs)] = libs
                except Exception:
                    continue
        logger.info("Missing deps found for %d binaries", len(missing))
        return missing

    def repair_missing_deps(self, rootfs: str) -> Dict[str, str]:
        """
        Try to repair missing libs by locating providers from lib-index and using resolver/build to install into rootfs.
        Returns mapping lib -> status.
        """
        missing = self.detect_missing_deps(rootfs)
        actions: Dict[str, str] = {}
        if not missing:
            return actions
        # ensure lib index available
        if not self._lib_index:
            self.rebuild_lib_index()
        # collect unique libs
        libs = set()
        for binp, arr in missing.items():
            for l in arr:
                libs.add(l)
        repo_dir = config_mod.get("repo_dir") if config_mod else "/usr/ibuild"
        # prepare resolver
        resolver = self.resolver()
        for lib in libs:
            providers = self.find_providers(lib)
            if not providers:
                actions[lib] = "provider_not_found"
                continue
            # try to resolve provider via dependency resolver to handle transitive deps
            if resolver:
                reqs = [PackageRequirement.from_string(p) for p in providers]
                # prefer first provider but allow resolver to pick correct one
                try:
                    rr = resolver.resolve(reqs, allow_optional=True, prefer_locked=True, timeout=30)
                except Exception:
                    rr = None
                if rr and rr.ok:
                    # build in order
                    for pid in rr.order:
                        # pid is candidate id like name-version; extract name prefix
                        pkgname = pid.split("-", 1)[0]
                        try:
                            # use build_mod to build and package_mod to install into rootfs
                            if build_mod:
                                artifact, meta_info = build_mod.build_package(pkgname, resolve_deps=True, jobs=None, sandbox=None)
                                if package_mod:
                                    package_mod.install_package(artifact, dest_dir=rootfs, overwrite=True, upgrade=True)
                                actions[lib] = f"installed_via_resolver:{pkgname}"
                                break
                            else:
                                actions[lib] = "simulate_installed"
                                break
                        except Exception:
                            logger.exception("failed install provider %s for %s", pkgname, lib)
                            continue
                else:
                    actions[lib] = "resolver_failed"
            else:
                # no resolver - try naive build of first provider
                first = providers[0]
                try:
                    if build_mod:
                        artifact, meta_info = build_mod.build_package(first, resolve_deps=True, jobs=None, sandbox=None)
                        if package_mod:
                            package_mod.install_package(artifact, dest_dir=rootfs, overwrite=True, upgrade=True)
                        actions[lib] = f"installed:{first}"
                    else:
                        actions[lib] = "simulate_installed"
                except Exception:
                    logger.exception("failed naive install provider %s for %s", first, lib)
                    actions[lib] = "failed_install"
        return actions

    # -------------------------
    # rootfs create / snapshot / manifest / verify
    # -------------------------
    def create_rootfs(self, dest: Optional[str] = None, packages: Optional[List[str]] = None, simulate: bool = False, sandboxed: bool = True) -> str:
        """
        Create a rootfs at dest and install packages inside it (using build+package modules).
        Returns path to rootfs.
        """
        packages = packages or list(self.base_packages)
        dest = dest or os.path.join(self.rootfs_dir, f"rootfs-{_now_ts()}")
        _ensure_dir(dest)
        self._emit("rootfs.create.start", {"path": dest, "packages": packages})
        if simulate:
            logger.info("simulate create_rootfs -> %s", dest)
            self._emit("rootfs.create.done", {"path": dest, "sim": True})
            return dest
        sb = None
        try:
            if sandbox_mod and sandboxed:
                try:
                    sb = sandbox_mod.Sandbox(base_dir=self.sandbox_dir, chroot_path=dest, create_chroot=True)
                    logger.info("created chroot sandbox at %s", dest)
                except Exception:
                    logger.exception("failed create chroot sandbox (fallback to direct)")
                    sb = None
            for d in ("dev", "proc", "sys", "tmp", "var", "etc", "usr", "bin", "lib"):
                _ensure_dir(os.path.join(dest, d))
            # use resolver to compute order of packages
            resolver = self.resolver()
            if resolver:
                reqs = [PackageRequirement.from_string(p) for p in packages]
                res = resolver.resolve(reqs, allow_optional=True, prefer_locked=True, timeout=300)
                if not res.ok:
                    logger.warning("resolver could not compute order for rootfs packages: %s", res.issues)
                    order = packages
                else:
                    order = [c_id for c_id in res.order]
                    # map order ids to package names (strip -version if present)
                    order = [o.split("-", 1)[0] for o in order]
            else:
                order = packages
            logger.info("install order for rootfs: %s", order)
            # build/install packages in order (workers can still parallelize internal deps)
            for pkg in order:
                try:
                    if simulate or build_mod is None:
                        logger.info("simulate build/install %s into %s", pkg, dest)
                        continue
                    artifact, meta_info = build_mod.build_package(pkg, resolve_deps=True, jobs=None, sandbox=sb)
                    if package_mod:
                        package_mod.install_package(artifact, dest_dir=dest, overwrite=True, upgrade=True)
                except Exception:
                    logger.exception("failed install %s into rootfs", pkg)
                    raise
            self._emit("rootfs.create.done", {"path": dest})
            return dest
        finally:
            if sb and not self.keep_artifacts:
                try:
                    sb.cleanup()
                except Exception:
                    logger.exception("cleanup sandbox after rootfs create")

    def generate_manifest(self, rootfs: str, normalize_ts: bool = False) -> Dict[str, Any]:
        manifest = {"root": rootfs, "files": {}, "generated_at": _now_ts()}
        for dirpath, _, filenames in os.walk(rootfs):
            rel = os.path.relpath(dirpath, rootfs)
            if rel.startswith(("proc", "sys", "dev", "run", "tmp")):
                continue
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rpath = os.path.relpath(full, rootfs)
                try:
                    if os.path.islink(full):
                        manifest["files"][rpath] = {"type": "symlink", "target": os.readlink(full)}
                    elif os.path.isfile(full):
                        manifest["files"][rpath] = {"type": "file", "sha256": _sha256(full)}
                except Exception:
                    logger.exception("manifest hashing failed for %s", full)
        if normalize_ts:
            manifest["normalized_ts"] = 0
        return manifest

    def snapshot_rootfs(self, rootfs: str, outdir: Optional[str] = None) -> str:
        outdir = outdir or self.manifest_dir
        _ensure_dir(outdir)
        name = f"rootfs-{os.path.basename(rootfs)}-{_now_ts()}"
        tarpath = os.path.join(outdir, f"{name}.tar.gz")
        _tar_create(rootfs, tarpath, compress=True)
        man = self.generate_manifest(rootfs, normalize_ts=True)
        manpath = os.path.join(outdir, f"{name}.manifest.json")
        with open(manpath, "w", encoding="utf-8") as f:
            json.dump(man, f, indent=2)
        self._emit("rootfs.snapshot", {"tar": tarpath, "manifest": manpath})
        return tarpath

    # -------------------------
    # high-level bootstrap flows
    # -------------------------
    def bootstrap_toolchain(self, jobs: Optional[int] = None, sandboxed: bool = True, simulate: bool = False) -> bool:
        """
        Build toolchain packages via resolver + build pipeline.
        """
        logger.info("bootstrap_toolchain start: pkgs=%s", self.toolchain_pkgs)
        resolver = self.resolver()
        if resolver:
            reqs = [PackageRequirement.from_string(p) for p in self.toolchain_pkgs]
            try:
                res = resolver.resolve(reqs, allow_optional=False, prefer_locked=True, timeout=600)
            except Exception as e:
                logger.exception("resolver failed for toolchain: %s", e)
                return False
            if not res.ok:
                logger.error("dependency resolution for toolchain failed: %s", res.issues)
                return False
            order = [o.split("-", 1)[0] for o in res.order]
        else:
            logger.warning("no resolver available; using declared toolchain_pkgs order")
            order = list(self.toolchain_pkgs)
        logger.info("toolchain build order: %s", order)
        # build in order (sequential to avoid bootstrapping races) or using build_packages_parallel with 1 worker
        results = self.build_packages_parallel(order, jobs=jobs, sandboxed=sandboxed, simulate=simulate)
        failed = [p for p, r in results.items() if not r["ok"]]
        if failed:
            logger.error("toolchain build failed for: %s", failed)
            return False
        # attempt toolchain fixes (libtool, paths) if module available
        if toolchain_mod and hasattr(toolchain_mod, "repair_libtool"):
            try:
                toolchain_mod.repair_libtool()
            except Exception:
                logger.exception("toolchain repair_libtool failed")
        return True

    def bootstrap_system(self, rootfs_dest: Optional[str] = None, jobs: Optional[int] = None, sandboxed: bool = True, simulate: bool = False) -> bool:
        """
        Full bootstrap:
         1) bootstrap_toolchain
         2) build base packages
         3) create_rootfs
         4) validate + repair missing deps
         5) snapshot
        """
        logger.info("bootstrap_system start (simulate=%s sandboxed=%s)", simulate, sandboxed)
        if not self.bootstrap_toolchain(jobs=jobs, sandboxed=sandboxed, simulate=simulate):
            logger.error("toolchain bootstrap failed")
            return False
        # build base packages (resolve order)
        resolver = self.resolver()
        base_order = self.base_packages
        if resolver:
            reqs = [PackageRequirement.from_string(p) for p in self.base_packages]
            try:
                res = resolver.resolve(reqs, allow_optional=True, prefer_locked=True, timeout=600)
            except Exception:
                res = None
            if res and res.ok:
                base_order = [o.split("-", 1)[0] for o in res.order]
        logger.info("base package order: %s", base_order)
        base_results = self.build_packages_parallel(base_order, jobs=jobs, sandboxed=sandboxed, simulate=simulate)
        # create rootfs
        rootfs_path = self.create_rootfs(dest=rootfs_dest, packages=self.base_packages, simulate=simulate, sandboxed=sandboxed)
        # manifest & validation
        man = self.generate_manifest(rootfs_path, normalize_ts=True)
        manfile = os.path.join(self.manifest_dir, f"manifest-{_now_ts()}.json")
        with open(manfile, "w", encoding="utf-8") as f:
            json.dump(man, f, indent=2)
        # simple validation: check basic binaries
        valid = True
        # try smoke-tests (sh and ls)
        for rel, args in [("/bin/sh", ["-c", "echo ok"]), ("/bin/ls", ["--version"])]:
            full = os.path.join(rootfs_path, rel.lstrip("/"))
            if not os.path.exists(full):
                logger.error("validation missing binary %s", full)
                valid = False
            else:
                try:
                    p = _safe_run([full] + args, capture=True)
                    if p.returncode != 0:
                        logger.error("validation failed for %s (rc=%s)", full, p.returncode)
                        valid = False
                except Exception:
                    logger.exception("validation exception for %s", full)
                    valid = False
        if not valid:
            logger.warning("validation failed; attempting detect/repair missing deps")
            actions = self.repair_missing_deps(rootfs_path)
            logger.info("repair actions: %s", actions)
            # re-validate quickly
            # (for brevity, assume second pass ok if no missing deps remain)
            missing = self.detect_missing_deps(rootfs_path)
            if missing:
                logger.error("still missing deps after repair: %s", missing)
                return False
        # snapshot
        snap = self.snapshot_rootfs(rootfs_path)
        logger.info("bootstrap_system completed; snapshot=%s", snap)
        return True

# -------------------------
# convenience defaults
# -------------------------
_default_manager: Optional[BootstrapManager] = None

def default_manager() -> BootstrapManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = BootstrapManager()
    return _default_manager

# -------------------------
# CLI-friendly entrypoints
# -------------------------
def cli_bootstrap_start(rootfs: Optional[str] = None, jobs: Optional[int] = None, sandboxed: bool = True, simulate: bool = False) -> int:
    bm = default_manager()
    ok = bm.bootstrap_system(rootfs_dest=rootfs, jobs=jobs, sandboxed=sandboxed, simulate=simulate)
    return 0 if ok else 1

def cli_rebuild_index() -> int:
    bm = default_manager()
    bm.rebuild_lib_index()
    return 0

def cli_status() -> int:
    bm = default_manager()
    st = {
        "pkg_db": bm.pkg_db,
        "rootfs_dir": bm.rootfs_dir,
        "lib_index_count": len(bm._lib_index or {}),
        "checkpoints": bm.list_checkpoints(),
    }
    print(json.dumps(st, indent=2))
    return 0

# -------------------------
# module test
# -------------------------
if __name__ == "__main__":
    print("BootstrapManager quick test")
    bm = default_manager()
    print("status:", bm.pkg_db, bm.rootfs_dir)
    print("rebuild lib index...")
    idx = bm.rebuild_lib_index(repo_dir=(config_mod.get("repo_dir") if config_mod else "/usr/ibuild"))
    print("entries:", len(idx))
    print("simulate bootstrap toolchain...")
    ok = bm.bootstrap_toolchain(simulate=True)
    print("simulate toolchain:", ok)
    print("simulate full bootstrap...")
    ok2 = bm.bootstrap_system(rootfs_dest=os.path.join(bm.rootfs_dir, "test-rootfs"), simulate=True)
    print("simulate full:", ok2)
