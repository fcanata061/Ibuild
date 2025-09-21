# rollback.py
"""
Rollback, orphan cleanup and reverse-dependency (revdep) tools for Ibuild.

Features:
- snapshot_before_operation(pkgs, op_name): saves installed.meta + manifest for pkgs
- list_snapshots(), show_snapshot(snapshot_id)
- rollback_last(): undo last recorded operation (transactional batch rollback)
- rollback_pkg_to_version(pkg, target_version): rollback one package to a given version (if artifact available)
- remove_orphans(dry_run=True, auto_only=True): finds and optionally removes orphaned packages
  (packages installed as auto-dependency and not required by any explicit package)
- revdep_check(check_ldd=True): detect broken reverse-dependencies (missing deps, missing shared libs)
- revdep_fix(fix=False, concurrent_jobs=None): if fix=True will attempt to rebuild/reinstall affected pkgs
- history(n=None): show last n rollback operations (readable)
- All operations are logged to rollback.log and each snapshot saved under pkg_db/snapshots/<ts>/

Notes:
- This module uses other ibuild modules: package, meta, dependency, upgrade, sandbox, fakeroot, log, config, utils.
- Rollback operations try to simulate first in a sandbox before committing to the real system.
- Removing packages uses package.remove_package. Repair/restore of files uses package.install_package when available.
"""

from __future__ import annotations

import os
import shutil
import json
import time
import tarfile
import logging
from typing import Dict, List, Optional, Tuple, Set

from ibuild1.0.modules_py import (
    config,
    log,
    package as package_mod,
    meta as meta_mod,
    dependency as dep_mod,
    upgrade as upgrade_mod,
    sandbox as sb_mod,
    fakeroot as fr_mod,
    utils,
)

logger = log.get_logger("rollback")

# Constants / dirs
_PKG_DB = lambda: os.path.abspath(config.get("pkg_db"))
_SNAP_DIR = lambda: os.path.join(_PKG_DB(), "snapshots")
_ROLLBACK_LOG = os.path.join(_PKG_DB(), "rollback.log")


# Utilities
def _now_ts() -> str:
    return time.strftime("%Y%m%d%H%M%S")


def _ensure_dirs():
    os.makedirs(_PKG_DB(), exist_ok=True)
    os.makedirs(_SNAP_DIR(), exist_ok=True)


def _append_log(entry: dict):
    _ensure_dirs()
    try:
        with open(_ROLLBACK_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warn("Não foi possível gravar rollback.log: %s", e)


def _read_log(n: Optional[int] = None) -> List[dict]:
    _ensure_dirs()
    if not os.path.isfile(_ROLLBACK_LOG):
        return []
    out = []
    with open(_ROLLBACK_LOG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    if n:
        return out[-n:]
    return out


def _installed_meta_path(pkg_name: str) -> str:
    return os.path.join(_PKG_DB(), f"{pkg_name}.installed.meta")


def _manifest_path(pkg_name: str) -> str:
    return os.path.join(_PKG_DB(), f"{pkg_name}.manifest.txt")


def _load_installed_meta(pkg_name: str) -> Optional[dict]:
    p = _installed_meta_path(pkg_name)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _safe_copy(src: str, dst: str):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


# Snapshot handling ---------------------------------------------------------
def snapshot_before_operation(pkgs: List[str], op_name: str, extra: Optional[dict] = None) -> str:
    """
    Snapshot the current installed.meta and manifest for listed pkgs.
    Returns snapshot_id (timestamp string).
    """
    _ensure_dirs()
    ts = _now_ts()
    snapdir = os.path.join(_SNAP_DIR(), ts)
    os.makedirs(snapdir, exist_ok=True)

    saved = []
    for p in pkgs:
        im = _installed_meta_path(p)
        man = _manifest_path(p)
        if os.path.isfile(im):
            try:
                _safe_copy(im, os.path.join(snapdir, os.path.basename(im)))
                saved.append(os.path.basename(im))
            except Exception as e:
                logger.warn("Falha ao salvar installed.meta para %s: %s", p, e)
        if os.path.isfile(man):
            try:
                _safe_copy(man, os.path.join(snapdir, os.path.basename(man)))
                saved.append(os.path.basename(man))
            except Exception as e:
                logger.warn("Falha ao salvar manifest para %s: %s", p, e)

    # Save metadata about snapshot
    meta = {
        "id": ts,
        "timestamp": ts,
        "operation": op_name,
        "packages": pkgs,
        "saved_files": saved,
        "extra": extra or {},
    }
    with open(os.path.join(snapdir, "snapshot.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    # log entry
    _append_log({"ts": ts, "type": "snapshot", "op": op_name, "packages": pkgs})
    logger.info("Snapshot criado %s (op=%s) para pacotes: %s", ts, op_name, ", ".join(pkgs))
    return ts


def list_snapshots() -> List[str]:
    _ensure_dirs()
    try:
        return sorted(os.listdir(_SNAP_DIR()))
    except Exception:
        return []


def show_snapshot(snapshot_id: str) -> Optional[dict]:
    path = os.path.join(_SNAP_DIR(), snapshot_id, "snapshot.json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# Rollback helpers ---------------------------------------------------------
def _restore_installed_meta_from_snapshot(snapshot_id: str, pkg_name: str, dry_run: bool = False) -> bool:
    """
    Restore installed.meta and manifest for a single pkg from a snapshot.
    Returns True if at least one file restored.
    """
    snapdir = os.path.join(_SNAP_DIR(), snapshot_id)
    if not os.path.isdir(snapdir):
        raise FileNotFoundError(f"Snapshot {snapshot_id} não encontrado")
    restored = False
    im_src = os.path.join(snapdir, f"{pkg_name}.installed.meta")
    man_src = os.path.join(snapdir, f"{pkg_name}.manifest.txt")
    if os.path.isfile(im_src):
        if dry_run:
            logger.info("dry_run: restauraria %s -> %s", im_src, _installed_meta_path(pkg_name))
        else:
            shutil.copy2(im_src, _installed_meta_path(pkg_name))
            restored = True
    if os.path.isfile(man_src):
        if dry_run:
            logger.info("dry_run: restauraria %s -> %s", man_src, _manifest_path(pkg_name))
        else:
            shutil.copy2(man_src, _manifest_path(pkg_name))
            restored = True
    return restored


def rollback_last(commit: bool = False, simulate_in_sandbox: bool = True, keep_sandbox: bool = False) -> dict:
    """
    Rollback the last snapshot recorded in rollback.log (type=snapshot).
    If commit=True, apply to real system; otherwise only simulate/restore metadata.
    If simulate_in_sandbox=True, first perform installation of old artifacts in a sandbox to validate.
    Returns a report dict.
    """
    logs = _read_log()
    # find last snapshot log entry
    last_snap = None
    for entry in reversed(logs):
        if entry.get("type") == "snapshot":
            last_snap = entry
            break
    if not last_snap:
        raise RuntimeError("Nenhuma snapshot encontrada para rollback")

    snapshot_id = last_snap.get("ts")
    snap_info = show_snapshot(snapshot_id)
    if not snap_info:
        raise RuntimeError(f"Snapshot {snapshot_id} não encontrada no disco")

    pkgs = snap_info.get("packages", [])
    report = {"snapshot": snapshot_id, "packages": pkgs, "restored": [], "errors": []}

    # For safety, try to validate artifacts exist for the versions in snapshot installed.meta
    artifacts_needed = {}
    for p in pkgs:
        inst = _load_installed_meta(p)
        # The installed.meta might have changed; snapshot contains copies of installed.meta so open them
        snap_im = os.path.join(_SNAP_DIR(), snapshot_id, f"{p}.installed.meta")
        if os.path.isfile(snap_im):
            try:
                with open(snap_im, "r", encoding="utf-8") as f:
                    snap_data = json.load(f)
                art = snap_data.get("artifact")
                artifacts_needed[p] = art
            except Exception as e:
                logger.warn("Não foi possível ler snapshot installed.meta para %s: %s", p, e)
        else:
            logger.warn("Snapshot não contém installed.meta para %s", p)

    # create validation sandbox
    sb_name = f"rollback-{snapshot_id}"
    report["sandbox"] = sb_name
    try:
        sb_mod.create_sandbox(sb_name, binds=[config.get("repo_dir")], keep=keep_sandbox)
        sb_install_root = os.path.join(sb_mod.sandbox_root(sb_name), "install")
        os.makedirs(sb_install_root, exist_ok=True)

        # install artifacts in sandbox to validate
        for p, art in artifacts_needed.items():
            if not art or not os.path.isfile(art):
                report["errors"].append({"pkg": p, "reason": "missing_artifact", "artifact": art})
                continue
            try:
                fr_res = fr_mod.install_with_fakeroot(art, sb_install_root)
                report.setdefault("sandbox_installs", {})[p] = fr_res
            except Exception as e:
                logger.exception("Falha ao instalar %s no sandbox: %s", p, e)
                report["errors"].append({"pkg": p, "reason": str(e)})
        # if errors and not commit, just return report
        if report["errors"] and not commit:
            return report

        # apply restoration of installed meta + manifests to pkg_db
        for p in pkgs:
            try:
                restored = _restore_installed_meta_from_snapshot(snapshot_id, p, dry_run=not commit)
                report["restored"].append({"pkg": p, "restored": restored})
            except Exception as e:
                report["errors"].append({"pkg": p, "reason": str(e)})

        # If commit, also install artifacts to real system via package.install_package
        if commit:
            install_root = config.get("install_root") or "/usr/local"
            for p, art in artifacts_needed.items():
                try:
                    if not art or not os.path.isfile(art):
                        report["errors"].append({"pkg": p, "reason": "missing_artifact_on_commit"})
                        continue
                    package_mod.install_package(art, dest_dir=install_root, overwrite=True, upgrade=True)
                    report.setdefault("commit_applied", []).append(p)
                except Exception as e:
                    logger.exception("Falha ao aplicar commit para %s: %s", p, e)
                    report["errors"].append({"pkg": p, "reason": str(e)})
        # record rollback operation in log
        _append_log({"ts": _now_ts(), "type": "rollback", "snapshot": snapshot_id, "packages": pkgs, "commit": bool(commit)})
        return report

    finally:
        if keep_sandbox:
            logger.info("Mantendo sandbox %s após rollback (keep_sandbox=True)", sb_name)
        else:
            try:
                sb_mod.destroy_sandbox(sb_name)
            except Exception:
                logger.warn("Falha ao remover sandbox %s", sb_name)


def rollback_pkg_to_version(pkg_name: str, target_version: str, simulate_in_sandbox: bool = True, commit: bool = False) -> dict:
    """
    Rollback a single package to a specific version.
    Strategy:
     - look for artifact in cache: cache_dir/packages/<name>-<version>.tar.gz
     - if not found, try to locate in snapshots (search snapshot installed.meta for that version)
     - simulate install in sandbox, then if commit=True apply package.install_package
    """
    cache_pkg = os.path.join(config.get("cache_dir"), "packages", f"{pkg_name}-{target_version}.tar.gz")
    artifact = None
    if os.path.isfile(cache_pkg):
        artifact = cache_pkg
    else:
        # search snapshots
        for sid in list_snapshots():
            snap_im = os.path.join(_SNAP_DIR(), sid, f"{pkg_name}.installed.meta")
            if os.path.isfile(snap_im):
                try:
                    with open(snap_im, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    ver = data.get("version")
                    art = data.get("artifact")
                    if ver == target_version and art and os.path.isfile(art):
                        artifact = art
                        break
                except Exception:
                    continue
    if not artifact:
        raise FileNotFoundError(f"Artefato para {pkg_name} version {target_version} não encontrado no cache ou snapshots")

    report = {"pkg": pkg_name, "target_version": target_version, "artifact": artifact, "simulated": None, "commit": None, "errors": []}

    # simulate in sandbox
    sb_name = f"rollback-{pkg_name}-{_now_ts()}"
    sb_mod.create_sandbox(sb_name, binds=[config.get("repo_dir")], keep=False)
    try:
        try:
            res = fr_mod.install_with_fakeroot(artifact, os.path.join(sb_mod.sandbox_root(sb_name), "install"))
            report["simulated"] = res
        except Exception as e:
            report["errors"].append({"phase": "sandbox_install", "error": str(e)})
            if not simulate_in_sandbox:
                raise

        if commit:
            try:
                package_mod.install_package(artifact, dest_dir=config.get("install_root") or "/usr/local", overwrite=True, upgrade=True)
                report["commit"] = True
                # snapshot the action
                snapshot_before_operation([pkg_name], f"rollback_to_{target_version}", {"artifact": artifact})
                _append_log({"ts": _now_ts(), "type": "rollback_pkg", "pkg": pkg_name, "target_version": target_version})
            except Exception as e:
                report["errors"].append({"phase": "commit", "error": str(e)})
                report["commit"] = False
    finally:
        try:
            sb_mod.destroy_sandbox(sb_name)
        except Exception:
            pass
    return report


# Orphan detection & removal -----------------------------------------------
def _build_installed_graph() -> Tuple[Dict[str, dict], Dict[str, Set[str]]]:
    """
    Build graph of installed packages:
    - returns (metas_by_pkg, graph)
    graph: dependency_name -> set(dependents)
    """
    metas = {}
    graph = {}
    installed = package_mod.list_installed()
    installed_names = [p["name"] for p in installed]
    for p in installed:
        name = p["name"]
        try:
            m = meta_mod.load_meta(name)
        except Exception:
            # If meta not present in repo, still register minimal meta from installed meta
            m = {}
        metas[name] = m
    # build reverse graph
    graph = {n: set() for n in metas.keys()}
    for n, m in metas.items():
        raw_deps = m.get("dependencies", []) or []
        for raw in raw_deps:
            # expand simple string dependency to name
            if isinstance(raw, str):
                depname = raw.split("==")[0].split(">=")[0].strip()
            elif isinstance(raw, dict):
                depname = raw.get("name")
            else:
                # list of alternatives etc => naive approach: skip complex
                continue
            if depname in graph:
                graph[depname].add(n)
    return metas, graph


def orphan_dry_run() -> List[str]:
    """
    Return list of orphan packages (candidates for removal).
    Criteria:
      - installed meta field 'explicit' == True -> keep
      - otherwise, if no other installed package depends on it -> orphan
    """
    metas, rev_graph = _build_installed_graph()
    installed = package_mod.list_installed()
    installed_names = [p["name"] for p in installed]
    orphans = []
    for p in installed:
        name = p["name"]
        imeta = _load_installed_meta(name) or {}
        if imeta.get("explicit", False):
            continue
        dependents = rev_graph.get(name, set())
        if not dependents:
            orphans.append(name)
    return orphans


def remove_orphans(dry_run: bool = True, force: bool = False) -> dict:
    """
    Remove orphan packages found by orphan_dry_run.
    If dry_run=True only reports which would be removed.
    If force=True, remove even some packages that are explicit (use with caution).
    Returns report dict.
    """
    to_remove = orphan_dry_run()
    report = {"candidates": to_remove, "removed": [], "errors": []}
    if dry_run:
        logger.info("Orphan dry-run: %s", ", ".join(to_remove))
        return report
    for pkg in to_remove:
        try:
            removed = package_mod.remove_package(pkg, purge=False)
            if removed:
                report["removed"].append(pkg)
                _append_log({"ts": _now_ts(), "type": "orphan_removed", "pkg": pkg})
            else:
                report["errors"].append({"pkg": pkg, "reason": "remove_failed"})
        except Exception as e:
            report["errors"].append({"pkg": pkg, "reason": str(e)})
    return report


# Reverse dependency (revdep) checks ---------------------------------------
def revdep_check(check_ldd: bool = True) -> dict:
    """
    Check reverse-dependencies and binaries for missing libs or missing packages.
    Returns dict with:
      - missing_deps: {pkg: [missing_dep_names]}
      - broken_bins: {pkg: [ (file, missing_libs) ]}
    """
    report = {"missing_deps": {}, "broken_bins": {}}
    installed = package_mod.list_installed()
    installed_names = [p["name"] for p in installed]
    name_set = set(installed_names)
    for pkg in installed:
        name = pkg["name"]
        try:
            m = meta_mod.load_meta(name)
        except Exception:
            m = {}
        deps = m.get("dependencies", []) or []
        missing = []
        for raw in deps:
            if isinstance(raw, str):
                depname = raw.split("==")[0].split(">=")[0].strip()
            elif isinstance(raw, dict):
                depname = raw.get("name")
            else:
                continue
            if depname and depname not in name_set:
                missing.append(depname)
        if missing:
            report["missing_deps"][name] = missing

        # check binary shared libs via ldd for files in manifest
        if check_ldd:
            manifest = pkg.get("manifest") or _manifest_path(name)
            broken = []
            if manifest and os.path.isfile(manifest):
                try:
                    with open(manifest, "r", encoding="utf-8") as f:
                        for fp in f.read().splitlines():
                            if not os.path.isfile(fp):
                                continue
                            # check executable bit heuristically
                            try:
                                st = os.stat(fp)
                                if not (st.st_mode & 0o111):
                                    continue
                            except Exception:
                                continue
                            # run ldd if available
                            if shutil.which("ldd"):
                                try:
                                    rc, out, err = utils.run(["ldd", fp], check=False)
                                    missing_libs = []
                                    for line in out.splitlines():
                                        if "not found" in line:
                                            missing_libs.append(line.strip())
                                    if missing_libs:
                                        broken.append({"file": fp, "missing_libs": missing_libs})
                                except Exception:
                                    continue
                except Exception:
                    pass
            if broken:
                report["broken_bins"][name] = broken
    return report


def revdep_fix(fix: bool = False, dry_run: bool = True, jobs: Optional[int] = None) -> dict:
    """
    For packages with missing deps or broken bins, attempt to rebuild/reinstall dependents.
    If fix=True and dry_run=False, actually call upgrade_mod.upgrade_package on affected packages with commit=True.
    Returns a report with planned actions and results.
    """
    check = revdep_check(check_ldd=True)
    actions = []
    # collect affected packages (dependents of missing deps + broken bins)
    affected = set(check.get("missing_deps", {}).keys()) | set(check.get("broken_bins", {}).keys())
    report = {"affected": list(affected), "planned": [], "results": []}
    for pkg in affected:
        # plan: call upgrade on pkg
        report["planned"].append({"pkg": pkg, "action": "rebuild_and_reinstall"})
    if dry_run:
        return report

    if fix:
        for plan in report["planned"]:
            pkg = plan["pkg"]
            try:
                # call upgrade_mod.upgrade_package with commit=True
                res = upgrade_mod.upgrade_package(pkg, commit=True, resolve_deps=True, jobs=jobs, keep_sandbox=False, dry_run=False)
                report["results"].append({"pkg": pkg, "result": "ok", "detail": res})
                _append_log({"ts": _now_ts(), "type": "revdep_fix", "pkg": pkg, "result": "ok"})
            except Exception as e:
                report["results"].append({"pkg": pkg, "result": "failed", "error": str(e)})
                _append_log({"ts": _now_ts(), "type": "revdep_fix", "pkg": pkg, "result": "failed", "error": str(e)})
    return report


# History / audit ----------------------------------------------------------
def history(n: Optional[int] = None) -> List[dict]:
    """
    Return the last n entries from rollback.log (most recent last).
    """
    return _read_log(n)


# Small convenience wrapper to snapshot, run upgrade, and record rollback snapshot
def snapshot_and_upgrade(pkgs: List[str], upgrade_opts: dict) -> dict:
    """
    Helper: snapshot current state for pkgs, then run upgrade (via upgrade_mod),
    then record snapshot so we can rollback if needed.
    upgrade_opts forwarded to upgrade_mod.upgrade_package
    """
    sid = snapshot_before_operation(pkgs, "pre_upgrade", extra={"opts": upgrade_opts})
    try:
        # run upgrade for primary pkg (first in list)
        primary = pkgs[0] if pkgs else None
        if not primary:
            raise ValueError("Nenhum pacote informado para upgrade")
        res = upgrade_mod.upgrade_package(primary, **upgrade_opts)
        # after success snapshot post state (so rollback can restore previous)
        snapshot_before_operation(pkgs, "post_upgrade", extra={"result": "success", "upgrade_report": res})
        _append_log({"ts": _now_ts(), "type": "upgrade_op", "pkgs": pkgs, "result": "success"})
        return {"snapshot_before": sid, "upgrade_result": res}
    except Exception as e:
        _append_log({"ts": _now_ts(), "type": "upgrade_op", "pkgs": pkgs, "result": "failed", "error": str(e)})
        # snapshot failed state
        snapshot_before_operation(pkgs, "post_upgrade_failed", extra={"error": str(e)})
        raise


# Public API exported by module
__all__ = [
    "snapshot_before_operation",
    "list_snapshots",
    "show_snapshot",
    "rollback_last",
    "rollback_pkg_to_version",
    "orphan_dry_run",
    "remove_orphans",
    "revdep_check",
    "revdep_fix",
    "history",
    "snapshot_and_upgrade",
]
```0
