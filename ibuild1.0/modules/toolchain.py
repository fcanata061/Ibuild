#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
modules/toolchain.py

Gerenciador avançado de toolchain para Ibuild.

Recursos:
- Registro de versões instaladas de GCC e Kernel.
- Perfis de toolchain (default/experimental/etc).
- Rebuild orquestrado da toolchain (kernel-headers, binutils, gcc, glibc, libtool).
- Suporte a cross-toolchains por triplet.
- Ativação/seleção atômica de versões (symlinks seguros).
- Snapshots antes de operações críticas e rollback.
- Verificador/validator completo (C/C++/Fortran, binutils, glibc, kernel headers, libtool).
- Fallback automático: se ativação falhar, tenta restaurar último estado válido.
- Integração pensada para ser chamada pelo CLI (ibuild toolchain ...)

Observações:
- Este módulo faz operações de baixo nível (symlinks em /usr/bin, /boot etc).
  Use com cuidado e prefira rodar em sandbox quando possível.
- Depende de outros módulos do seu projeto: modules.log, modules.build,
  modules.package, modules.meta, modules.sandbox (se existirem).
"""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import tempfile
import time
from typing import Optional, Dict, Any, List, Tuple

# Tentar importar módulos do projeto (se disponíveis)
try:
    from modules import log, build, package, meta, sandbox, dependency
except Exception:
    # fallback para não quebrar import se alguns módulos não existirem
    log = None
    build = None
    package = None
    meta = None
    sandbox = None
    dependency = None

# logger
if log is not None:
    logger = log.get_logger("toolchain")
else:
    import logging
    logger = logging.getLogger("toolchain")
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO)

# Config / paths
STATE_FILE = "/var/lib/ibuild/toolchain.json"
SNAPSHOT_DIR = "/var/lib/ibuild/toolchain-snapshots"
HISTORY_LOG = "/var/log/ibuild/toolchain-history.log"
VERIFY_LOG = "/var/log/ibuild/toolchain-verify.log"

# Packages considered part of toolchain orchestration (names correspond to .meta)
DEFAULT_TOOLCHAIN_ORDER = [
    "linux-headers",  # kernel headers
    "binutils",
    "gcc",            # stage bootstrap / initial
    "glibc",
    "gcc",            # final rebuild with full deps
    "libtool",
]

# Utilities ------------------------------------------------------------------


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _atomic_symlink(target: str, link: str) -> None:
    """
    Cria um symlink de forma atômica: cria temporário e renomeia.
    """
    _ensure_dir(os.path.dirname(link))
    tmp = f"{link}.tmp-{int(time.time()*1000)}"
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except Exception:
            pass
    try:
        os.symlink(target, tmp)
        os.replace(tmp, link)
        logger.debug("Symlink atômico: %s -> %s", link, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def _run(cmd: List[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    logger.debug("Executando: %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)


# State management -----------------------------------------------------------


def _load_state() -> Dict[str, Any]:
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Falha ao ler state file %s: %s", STATE_FILE, e)
            # fallback to minimal structure
    # default structure
    return {
        "active_profile": "default",
        "profiles": {
            "default": {
                "gcc_active": None,
                "kernel_active": None,
                "binutils": None,
                "glibc": None,
            }
        },
        "cross": {},  # triplet -> profile-like dict
        "history": [],  # events
    }


def _save_state(state: Dict[str, Any]) -> None:
    _ensure_dir(os.path.dirname(STATE_FILE))
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _append_history(msg: str) -> None:
    _ensure_dir(os.path.dirname(HISTORY_LOG))
    with open(HISTORY_LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.asctime()} - {msg}\n")
    logger.info(msg)


# Snapshot / rollback --------------------------------------------------------


def snapshot_state(name: Optional[str] = None) -> str:
    """
    Salva um snapshot do state e alguns symlinks importantes para rollback.
    Retorna o path do snapshot.
    """
    _ensure_dir(SNAPSHOT_DIR)
    ts = int(time.time())
    name = name or f"snapshot-{ts}"
    dest = os.path.join(SNAPSHOT_DIR, f"{name}.json")
    state = _load_state()
    # capture symlink targets for main items (gcc symlinks and /boot/vmlinuz)
    extras = {}
    for b in ("gcc", "g++", "cpp"):
        path = f"/usr/bin/{b}"
        if os.path.islink(path):
            extras[f"symlink_{b}"] = os.readlink(path)
    if os.path.islink("/boot/vmlinuz"):
        extras["boot_vmlinuz"] = os.readlink("/boot/vmlinuz")
    snapshot = {"timestamp": ts, "state": state, "extras": extras}
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    _append_history(f"Snapshot criado: {dest}")
    logger.debug("Snapshot salvo em %s", dest)
    return dest


def list_snapshots() -> List[str]:
    _ensure_dir(SNAPSHOT_DIR)
    return sorted([os.path.join(SNAPSHOT_DIR, f) for f in os.listdir(SNAPSHOT_DIR) if f.endswith(".json")])


def rollback_snapshot(snapshot_path: str) -> bool:
    """
    Restaura um snapshot salvo (estado + symlinks básicos).
    Retorna True se bem-sucedido.
    """
    if not os.path.isfile(snapshot_path):
        raise FileNotFoundError(snapshot_path)
    with open(snapshot_path, "r", encoding="utf-8") as f:
        snap = json.load(f)
    state = snap.get("state", {})
    extras = snap.get("extras", {})

    # restore state
    _save_state(state)
    # restore symlinks extras
    for key, target in extras.items():
        if key.startswith("symlink_"):
            binname = key.replace("symlink_", "")
            dst = f"/usr/bin/{binname}"
            try:
                _atomic_symlink(target, dst)
            except Exception as e:
                logger.warning("Falha ao restaurar symlink %s -> %s: %s", dst, target, e)
        if key == "boot_vmlinuz":
            try:
                _atomic_symlink(target, "/boot/vmlinuz")
            except Exception as e:
                logger.warning("Falha ao restaurar kernel symlink: %s", e)
    _append_history(f"Rollback de snapshot: {snapshot_path}")
    logger.info("Rollback aplicado a partir de %s", snapshot_path)
    return True


# Version registry -----------------------------------------------------------


def register_versions(pkg_name: str, version: str) -> None:
    """
    Registra que um pacote toolchain (gcc/kernel/binutils/glibc) foi instalado
    e atualiza o profile ativo com a versão.
    """
    state = _load_state()
    profile = state.get("active_profile", "default")
    profiles = state.setdefault("profiles", {})
    p = profiles.setdefault(profile, {})
    if pkg_name == "gcc":
        p["gcc_active"] = version
        # keep a list of known gcc versions at top-level for convenience
        known = state.setdefault("gcc_versions", [])
        if version not in known:
            known.append(version)
    elif pkg_name.startswith("linux") or pkg_name in ("kernel", "linux-headers"):
        p["kernel_active"] = version
        known = state.setdefault("kernel_versions", [])
        if version not in known:
            known.append(version)
    elif pkg_name == "binutils":
        p["binutils"] = version
    elif pkg_name == "glibc":
        p["glibc"] = version
    # save
    _save_state(state)
    _append_history(f"Register: {pkg_name} -> {version}")


def list_versions() -> Dict[str, Any]:
    return _load_state()


# Activation & switching ----------------------------------------------------


def _switch_gcc(version: str, bin_base_dir: Optional[str] = None) -> None:
    """
    Atualiza symlinks /usr/bin/gcc, g++, cpp para a versão fornecida.
    Assume que a instalação do GCC coloca binários em /usr/lib/gcc/<version>/bin/
    ou permite passar bin_base_dir explicitamente.
    """
    if bin_base_dir is None:
        bin_base_dir = f"/usr/lib/gcc/{version}/bin"
    if not os.path.isdir(bin_base_dir):
        raise RuntimeError(f"Diretório de binários GCC não encontrado: {bin_base_dir}")

    targets = {"gcc": os.path.join(bin_base_dir, "gcc"),
               "g++": os.path.join(bin_base_dir, "g++"),
               "cpp": os.path.join(bin_base_dir, "cpp")}

    for name, src in targets.items():
        if not os.path.exists(src):
            # Try with versioned names (gcc-13, g++-13) as fallback
            fallback = os.path.join("/usr/bin", f"{name}-{version}")
            if os.path.exists(fallback):
                src = fallback
            else:
                logger.warning("Binário esperado não existe: %s (procurado %s)", name, src)
                # do not fail immediately, continue and allow validation later
        dst = f"/usr/bin/{name}"
        try:
            _atomic_symlink(src, dst)
        except Exception as e:
            logger.error("Falha ao criar symlink para %s -> %s: %s", dst, src, e)
            raise


def _switch_kernel(version: str) -> None:
    """
    Atualiza /boot/vmlinuz para apontar para a imagem do kernel selecionado.
    Assume kernel images em /boot/vmlinuz-<version>.
    """
    img = f"/boot/vmlinuz-{version}"
    if not os.path.isfile(img):
        raise RuntimeError(f"Kernel image não encontrada: {img}")
    _atomic_symlink(img, "/boot/vmlinuz")
    logger.info("Kernel ativo agora aponta para %s", img)


def set_active(pkg_type: str, version: str, profile: Optional[str] = None) -> None:
    """
    Ativa uma versão específica de 'gcc' ou 'kernel'.
    Se falhar, restaura snapshot anterior.
    """
    profile = profile or _load_state().get("active_profile", "default")
    snap = snapshot_state(name=f"pre-set-{pkg_type}-{int(time.time())}")
    try:
        if pkg_type == "gcc":
            _switch_gcc(version)
            # validate quickly
            if not validate_toolchain_quick():
                raise RuntimeError("Validação rápida do GCC falhou após switch")
            # update state registry
            state = _load_state()
            state["profiles"].setdefault(profile, {})["gcc_active"] = version
            _save_state(state)
            _append_history(f"GCC ativo alterado para {version} no perfil {profile}")
        elif pkg_type == "kernel":
            _switch_kernel(version)
            state = _load_state()
            state["profiles"].setdefault(profile, {})["kernel_active"] = version
            _save_state(state)
            _append_history(f"Kernel ativo alterado para {version} no perfil {profile}")
        else:
            raise ValueError("pkg_type deve ser 'gcc' ou 'kernel'")
    except Exception as e:
        logger.error("Erro ao ativar %s %s: %s -- rollback snapshot", pkg_type, version, e)
        try:
            rollback_snapshot(snap)
            logger.info("Rollback completado após falha na ativação")
        except Exception as ex:
            logger.critical("Rollback falhou: %s", ex)
        raise


# Rebuild orchestration -----------------------------------------------------


def detect_updates(toolchain_pkgs: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """
    Detecta diferenças entre meta (upstream) e versão instalada.
    Retorna lista de dicts: {name, current, new}
    """
    pkgs = toolchain_pkgs or DEFAULT_TOOLCHAIN_ORDER
    updates = []
    for name in pkgs:
        try:
            m = meta.load_meta(name)
        except Exception:
            continue
        try:
            inst = package.query_package(name)
        except Exception:
            inst = None
        cur = inst.get("version") if inst else None
        new = m.get("version")
        if new and cur != new:
            updates.append({"name": name, "current": cur, "new": new})
    return updates


def rebuild_toolchain(updates: Optional[List[Dict[str, str]]] = None,
                      jobs: Optional[int] = None,
                      sandboxed: bool = True,
                      profile: Optional[str] = None,
                      target: Optional[str] = None) -> bool:
    """
    Reconstrói a toolchain completa no seguinte fluxo seguro:
      - cria snapshot
      - para cada pacote na ordem apropriada: build + install
      - registra versões
      - valida toolchain no final
    Se algo falhar, tenta rollback automático para o snapshot salvo.
    """
    profile = profile or _load_state().get("active_profile", "default")
    if updates is None:
        updates = detect_updates()

    if not updates:
        logger.info("Nenhuma atualização detectada na toolchain (nenhum pacote mudou)")
        return True

    snap = snapshot_state(name=f"pre-rebuild-{int(time.time())}")
    logger.info("Reconstruindo toolchain, snapshot: %s", snap)

    # decide ordem: prefer default, mas se dependency module existir podemos topo-sort
    order = DEFAULT_TOOLCHAIN_ORDER.copy()
    # If dependency graph available we could compute a better order (not mandatory)
    if dependency is not None:
        try:
            # attempt to topologically order the set of updates
            names = [u["name"] for u in updates]
            topo = dependency.resolve_topo(names)
            if topo:
                order = topo
        except Exception:
            pass

    sb = sandbox.Sandbox() if (sandboxed and sandbox is not None) else None
    try:
        for pkg in order:
            # only rebuild if in updates or user wants full sequence
            if updates and pkg not in [u["name"] for u in updates]:
                logger.debug("Pulando %s (sem update)", pkg)
                continue
            logger.info("Build/install %s ...", pkg)
            # build package (pass target for cross builds)
            try:
                artifact, meta_info = build.build_package(pkg, resolve_deps=True, jobs=jobs, target=target, sandbox=sb)
                package.install_package(artifact, overwrite=True, upgrade=True, target=target)
                register_versions(pkg, meta_info.get("version"))
            except Exception as e:
                logger.error("Falha ao build/install %s: %s", pkg, e)
                raise
    except Exception as e:
        logger.error("Erro durante rebuild: %s -- executando rollback", e)
        try:
            rollback_snapshot(snap)
            logger.info("Rollback efetuado com sucesso")
        except Exception as ex:
            logger.critical("Rollback falhou: %s", ex)
        return False
    finally:
        if sb:
            try:
                sb.cleanup()
            except Exception:
                pass

    # pós-processos
    try:
        repair_libtool()
        ok = verify_toolchain()
        if not ok:
            logger.warning("Validação post-rebuild falhou (verificar logs)")
            _append_history("Rebuild finalizado com problemas - verifique verify logs")
        else:
            _append_history("Rebuild toolchain finalizado com sucesso")
    except Exception as e:
        logger.warning("Erro em pós-processamento: %s", e)
    return True


# Libtool repairs & utilities ----------------------------------------------


def repair_libtool() -> None:
    """
    Tenta corrigir issues com libtool (exec: libtoolize, aclocal, autoreconf).
    Não é garantido resolver todos os casos complexos.
    """
    tools = ["libtoolize", "aclocal", "autoreconf"]
    ran = []
    for t in tools:
        try:
            subprocess.run([t, "--version"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # not throwing means tool exists; attempt run
            try:
                subprocess.run([t, "--force"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                ran.append(t)
            except Exception:
                # try without args
                try:
                    subprocess.run([t], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    ran.append(t)
                except Exception:
                    pass
        except Exception:
            continue
    logger.info("Tentativa repair libtool realizada (ferramentas executadas: %s)", ", ".join(ran))


# Verification suite --------------------------------------------------------


def _compile_and_run(code: str, lang: str = "c", compiler: str = "gcc", extra: Optional[List[str]] = None) -> Tuple[bool, str]:
    """
    Compila código fonte (string) e executa binário, retornando (success, output-or-error).
    """
    tmpdir = tempfile.mkdtemp(prefix="ibuild-toolchain-")
    src_map = {"c": "test.c", "cpp": "test.cpp", "f": "test.f90", "glibc": "test_glibc.c", "kh": "test_kh.c"}
    srcname = src_map.get(lang, "test.c")
    srcpath = os.path.join(tmpdir, srcname)
    binpath = os.path.join(tmpdir, "a.out")
    try:
        with open(srcpath, "w", encoding="utf-8") as f:
            f.write(code)
        cmd = [compiler, srcpath, "-o", binpath]
        if extra:
            cmd.extend(extra)
        proc = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            return False, proc.stderr.strip() or proc.stdout.strip()
        # execute
        runp = subprocess.run([binpath], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if runp.returncode != 0:
            return False, runp.stderr.strip() or runp.stdout.strip()
        return True, runp.stdout.strip()
    except Exception as e:
        return False, str(e)
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass


def _check_tool_version(tool: str) -> Tuple[bool, str]:
    try:
        p = subprocess.run([tool, "--version"], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out = (p.stdout or p.stderr or "").splitlines()[0] if (p.stdout or p.stderr) else ""
        if p.returncode == 0 or out:
            return True, out.strip()
        return False, out.strip()
    except Exception as e:
        return False, str(e)


def validate_toolchain_quick() -> bool:
    """
    Checagem rápida (compila um tiny hello.c com gcc ativo).
    """
    code = '#include <stdio.h>\nint main(){puts("ok");return 0;}'
    ok, out = _compile_and_run(code, lang="c", compiler="gcc")
    if not ok:
        logger.error("Quick validate falhou: %s", out)
    return ok


def verify_toolchain() -> bool:
    """
    Bateria de testes completa para validar toolchain atual.
    - testa gcc (C), g++ (C++), gfortran (Fortran se disponível)
    - testa binutils (ld, as, ar)
    - testa glibc (threads + printf + malloc via teste)
    - testa kernel headers (inclui linux/version.h)
    - testa libtoolize
    Produz log em VERIFY_LOG.
    """
    results = {}
    logger.info("Iniciando verificação completa da toolchain...")
    # gcc C
    code_c = '#include <stdio.h>\nint main(){printf("ok-c\\n");return 0;}'
    ok, out = _compile_and_run(code_c, "c", compiler="gcc")
    results["gcc-c"] = (ok, out)

    # g++ C++
    code_cpp = '#include <iostream>\nint main(){std::cout << "ok-cpp\\n";return 0;}'
    ok, out = _compile_and_run(code_cpp, "cpp", compiler="g++")
    results["gcc-cpp"] = (ok, out)

    # gfortran
    code_f = "      program hello\n      print *, 'ok-fortran'\n      end\n"
    ok, out = _compile_and_run(code_f, "f", compiler="gfortran")
    results["gfortran"] = (ok, out)

    # binutils
    for tool in ("ld", "as", "ar"):
        ok, out = _check_tool_version(tool)
        results[f"binutils-{tool}"] = (ok, out)

    # glibc test (pthread + printf)
    code_glibc = '#include <pthread.h>\n#include <stdio.h>\n#include <stdlib.h>\nvoid *t(void* a){return a;}\nint main(){pthread_t th; if(pthread_create(&th,NULL,t,NULL)) return 1; pthread_join(th,NULL); printf("glibc-ok\\n"); return 0;}'
    ok, out = _compile_and_run(code_glibc, "glibc", compiler="gcc", extra=["-pthread"])
    results["glibc"] = (ok, out)

    # kernel headers (linux/version.h)
    code_kh = '#include <linux/version.h>\n#include <stdio.h>\nint main(){printf("kernelver=%d\\n", LINUX_VERSION_CODE); return 0;}'
    ok, out = _compile_and_run(code_kh, "kh", compiler="gcc")
    results["kernel-headers"] = (ok, out)

    # libtoolize
    ok, out = _check_tool_version("libtoolize")
    results["libtoolize"] = (ok, out)

    # write report
    _ensure_dir(os.path.dirname(VERIFY_LOG))
    with open(VERIFY_LOG, "w", encoding="utf-8") as f:
        for k, (ok, info) in results.items():
            f.write(f"{k}: {'OK' if ok else 'FAIL'} - {info}\n")

    # log and return
    failed = [k for k, (ok, _) in results.items() if not ok]
    if failed:
        logger.error("Toolchain verification failed: %s", ", ".join(failed))
        _append_history(f"verify: FAIL - {failed}")
        return False
    logger.info("Toolchain verification OK")
    _append_history("verify: OK")
    return True


# Profiles / cross / helpers -------------------------------------------------


def create_profile(name: str, base_profile: Optional[str] = None, gcc: Optional[str] = None,
                   kernel: Optional[str] = None, binutils_v: Optional[str] = None,
                   glibc_v: Optional[str] = None) -> None:
    state = _load_state()
    profiles = state.setdefault("profiles", {})
    if name in profiles:
        raise RuntimeError(f"Profile {name} já existe")
    base = profiles.get(base_profile, {}) if base_profile else {}
    profiles[name] = {
        "gcc_active": gcc or base.get("gcc_active"),
        "kernel_active": kernel or base.get("kernel_active"),
        "binutils": binutils_v or base.get("binutils"),
        "glibc": glibc_v or base.get("glibc"),
    }
    _save_state(state)
    _append_history(f"Profile criado: {name}")


def list_profiles() -> Dict[str, Any]:
    return _load_state().get("profiles", {})


def use_profile(name: str) -> None:
    state = _load_state()
    profiles = state.setdefault("profiles", {})
    if name not in profiles:
        raise RuntimeError(f"Profile {name} não existe")
    # snapshot before changing active profile
    snap = snapshot_state(name=f"pre-use-profile-{name}")
    state["active_profile"] = name
    _save_state(state)
    p = profiles[name]
    # attempt to activate listed components atomically; if fails, rollback snapshot
    try:
        if p.get("gcc_active"):
            _switch_gcc(p["gcc_active"])
        if p.get("kernel_active"):
            _switch_kernel(p["kernel_active"])
    except Exception as e:
        logger.error("Falha ao usar profile %s: %s - rollback", name, e)
        try:
            rollback_snapshot(snap)
        except Exception as ex:
            logger.critical("Rollback do profile falhou: %s", ex)
        raise
    _append_history(f"Profile {name} ativado")


def register_cross(triplet: str, gcc_version: Optional[str] = None, binutils_version: Optional[str] = None) -> None:
    state = _load_state()
    cross = state.setdefault("cross", {})
    c = cross.setdefault(triplet, {})
    if gcc_version:
        c["gcc_active"] = gcc_version
    if binutils_version:
        c["binutils"] = binutils_version
    _save_state(state)
    _append_history(f"Cross registered: {triplet} -> gcc {gcc_version}, binutils {binutils_version}")


# CLI-friendly helpers ------------------------------------------------------


def get_toolchain_status() -> Dict[str, Any]:
    """
    Retorna um dicionário resumido com informações para UI / status bars.
    """
    state = _load_state()
    active_profile = state.get("active_profile", "default")
    profile = state.get("profiles", {}).get(active_profile, {})
    return {
        "active_profile": active_profile,
        "profile": profile,
        "gcc_versions": state.get("gcc_versions", []),
        "kernel_versions": state.get("kernel_versions", []),
        "cross": state.get("cross", {}),
    }


# Public API -----------------------------------------------------------------

__all__ = [
    "snapshot_state", "list_snapshots", "rollback_snapshot",
    "register_versions", "list_versions",
    "set_active", "rebuild_toolchain", "detect_updates",
    "repair_libtool", "verify_toolchain", "validate_toolchain_quick",
    "create_profile", "list_profiles", "use_profile",
    "register_cross", "get_toolchain_status",
    # constants for external use:
    "STATE_FILE", "SNAPSHOT_DIR", "HISTORY_LOG",
                           ]
