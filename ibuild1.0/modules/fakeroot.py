# fakeroot.py
"""
Módulo 'fakeroot' para Ibuild — executar operações que precisariam de root sem
modificar o host quando possível, ou registrar intenções de alteração.

Funcionalidades principais:
- detecta e usa 'fakeroot' / 'proot' / 'fakechroot' quando disponíveis
- run(cmd, ...) executa comando dentro do ambiente fakeroot/proot
- context manager FakerootContext para agrupar execuções
- funções para extrair artefatos e instalar em DESTDIR simulando permissões
- registra ownership manifest (ownership.json) quando não é possível aplicar chown
"""

from __future__ import annotations
import os
import json
import shutil
import stat
import subprocess
from typing import List, Optional, Dict, Tuple
from pathlib import Path

from ibuild1.0.modules_py import log, utils, config

logger = log.get_logger("fakeroot")

# ----------------------------------------------------------------------
# Detectar ferramentas disponíveis (ordem de preferência)
# ----------------------------------------------------------------------
def _which(prog: str) -> Optional[str]:
    return shutil.which(prog)


_HAS_FAKEROOT = bool(_which("fakeroot"))
_HAS_PROOT = bool(_which("proot"))
_HAS_FAKECHROOT = bool(_which("fakechroot"))

def is_available() -> bool:
    """Retorna True se há uma ferramenta de fakeroot/proot disponível."""
    return _HAS_FAKEROOT or _HAS_PROOT or _HAS_FAKECHROOT

# ----------------------------------------------------------------------
# Helpers internos
# ----------------------------------------------------------------------
def _build_prefix() -> List[str]:
    """
    Retorna o prefixo de comando para executar algo em modo 'fakeroot'.
    - prefere fakeroot (mais simples)
    - se não existir, tenta proot -0
    - se não existir nada, retorna [] (fazer fallback)
    """
    if _HAS_FAKEROOT:
        return ["fakeroot"]
    if _HAS_PROOT:
        # proot -0 emula root (note: proot flags podem variar por sistemas)
        # usamos '-0' para mapear uid 0 e '--' para terminar opções
        return ["proot", "-0", "--"]
    if _HAS_FAKECHROOT:
        # fakechroot é uma solução limitada mas pode ajudar
        return ["fakechroot"]
    return []

def _run_prefix(cmd: List[str], cwd: Optional[str] = None, env: Optional[dict] = None,
                check: bool = True) -> Tuple[int, str, str]:
    """
    Executa 'cmd' já contendo o prefixo se necessário. Usa utils.run() para log.
    Retorna (rc, stdout, stderr).
    """
    rc, out, err = utils.run(cmd, cwd=cwd, env=env, check=False)
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, cmd, out, err)
    return rc, out, err

# ----------------------------------------------------------------------
# API principal: run
# ----------------------------------------------------------------------
def run(cmd: List[str], cwd: Optional[str] = None, env: Optional[dict] = None,
        check: bool = True, use_fakeroot: bool = True) -> Tuple[int, str, str]:
    """
    Executa um comando preferencialmente dentro do mecanismo fakeroot/proot.
    - cmd: lista de strings (ex: ["bash", "-c", "make install"])
    - cwd, env: idem
    - check: se True, lança CalledProcessError em rc != 0
    - use_fakeroot: se False, executa diretamente (útil para debugging)
    Retorna (rc, stdout, stderr)
    """
    prefix = _build_prefix() if use_fakeroot else []
    if prefix:
        full = prefix + cmd
        logger.debug("Executando com prefix %s: %s", prefix[0], " ".join(cmd))
        return _run_prefix(full, cwd=cwd, env=env, check=check)
    else:
        # fallback: não há fakeroot — executa normalmente, mas com WARNING
        logger.warn("Nenhum fakeroot/proot disponível — executando sem emulação: %s", " ".join(cmd))
        return _run_prefix(cmd, cwd=cwd, env=env, check=check)

# ----------------------------------------------------------------------
# Context manager para agrupar execuções em modo fakeroot
# ----------------------------------------------------------------------
class FakerootContext:
    """
    Context manager para executar múltiplos comandos com as mesmas opções.
    Exemplo:
        with FakerootContext(pkg_name="foo") as f:
            f.run(["bash","-c","make install"])
            f.run(["/usr/bin/strip","..."])
    """
    def __init__(self, pkg_name: Optional[str] = None, binds: Optional[List[str]] = None,
                 env: Optional[dict] = None, use_fakeroot: bool = True):
        self.pkg_name = pkg_name or "ibuild"
        self.binds = binds or []
        self.env = env or {}
        self.use_fakeroot = use_fakeroot and is_available()
        self.prefix = _build_prefix() if self.use_fakeroot else []
        self._active = False

    def __enter__(self):
        logger.debug("Entrando em FakerootContext (pkg=%s) use_fakeroot=%s", self.pkg_name, self.use_fakeroot)
        self._active = True
        return self

    def run(self, cmd: List[str], cwd: Optional[str] = None, check: bool = True) -> Tuple[int, str, str]:
        if not self._active:
            raise RuntimeError("Contexto fakeroot não ativo")
        full = (self.prefix + cmd) if self.use_fakeroot else cmd
        logger.debug("FakerootContext.run: %s", " ".join(full))
        return _run_prefix(full, cwd=cwd, env=self.env, check=check)

    def __exit__(self, exc_type, exc, tb):
        self._active = False
        logger.debug("Saindo de FakerootContext (pkg=%s)", self.pkg_name)
        return False  # não suprimir exceções

# ----------------------------------------------------------------------
# Ownership recording / simulation
# ----------------------------------------------------------------------
def _ownership_manifest_path(dest_dir: str) -> str:
    return os.path.join(dest_dir, ".ibuild_ownership.json")

def simulate_chown_record(target_path: str, uid: int, gid: int, mode: Optional[int] = None):
    """
    Registra intenção de chown/chmod para target_path em ownership manifest no dest root.
    O arquivo .ibuild_ownership.json fica no root mais próximo (procura config.install_root).
    """
    # achar root para manifesto (prefer dest root)
    dest_root = config.get("install_root") or "/usr/local"
    man = _ownership_manifest_path(dest_root)
    try:
        data = {}
        if os.path.isfile(man):
            with open(man, "r", encoding="utf-8") as f:
                data = json.load(f)
    except Exception:
        data = {}

    rel = os.path.relpath(target_path, dest_root)
    data[rel] = {"uid": uid, "gid": gid, "mode": mode}
    with open(man, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    logger.debug("Recorded simulated ownership for %s -> %s", rel, data[rel])

def apply_ownership_records(dest_root: str) -> Tuple[int, int]:
    """
    Tenta aplicar ownership manifest (.ibuild_ownership.json) sob dest_root.
    - Aplica chown/chmod somente se o processo tiver permissão (uid == 0 ou realuid can chown).
    - Retorna (applied_count, failed_count)
    """
    man = _ownership_manifest_path(dest_root)
    if not os.path.isfile(man):
        return (0, 0)

    applied = 0
    failed = 0
    try:
        with open(man, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error("Não foi possível ler ownership manifest: %s", e)
        return (0, 0)

    can_chown = (os.geteuid() == 0)
    for rel, info in data.items():
        path = os.path.join(dest_root, rel)
        try:
            if info.get("mode") is not None:
                os.chmod(path, info["mode"])
            if can_chown:
                os.chown(path, info["uid"], info["gid"])
                applied += 1
            else:
                logger.debug("Sem permissão para chown %s -> uid=%s gid=%s", path, info["uid"], info["gid"])
                failed += 1
        except Exception as e:
            logger.warn("Falha ao aplicar ownership para %s: %s", path, e)
            failed += 1

    # se tudo aplicado (ou não), removemos o manifesto para evitar reexecução
    try:
        os.remove(man)
    except Exception:
        pass
    return (applied, failed)

# ----------------------------------------------------------------------
# High level helpers: extract and install using fakeroot when possible
# ----------------------------------------------------------------------
def extract_tar_in_fakeroot(artifact_path: str, dest_dir: str, preserve_owner: bool = True):
    """
    Extrai um tarball para dest_dir. Tenta preservar o owner/mode:
    - se fakeroot/proot disponível: executa `tar -xpf <artifact> -C dest_dir` sob o prefixo (preserva owners)
    - se não disponível: extrai normalmente e registra ownership intent no manifest (simulate_chown_record)
    """
    os.makedirs(dest_dir, exist_ok=True)
    logger.info("Extraindo %s → %s (preserve_owner=%s)", artifact_path, dest_dir, preserve_owner)

    if is_available():
        # usar tar sob fakeroot/proot; usamos -p para preservar permissões, -f para arquivo, -C para dir
        cmd = ["tar", "-xpf", artifact_path, "-C", dest_dir]
        run(cmd, cwd=None, env=None, check=True)
        return

    # fallback: extract normally, then record intended ownerships from archive
    logger.warn("Nenhuma ferramenta fakeroot disponível — extraindo e registrando owners (manifest)")
    with utils.run(["tar", "-tzf", artifact_path], check=True)[1] as _:
        pass
    # More robust: open tarfile and read members
    import tarfile as _tar
    with _tar.open(artifact_path, "r:*") as tf:
        for member in tf.getmembers():
            # extract member
            try:
                tf.extract(member, path=dest_dir)
                full = os.path.join(dest_dir, member.name)
                # if member has uid/gid, record it
                uid = getattr(member, "uid", None) or getattr(member, "uname", None)
                gid = getattr(member, "gid", None) or getattr(member, "gname", None)
                # member.uid/gid are ints in tarfile; uname/gname are strings — handle carefully
                try:
                    if isinstance(member.uid, int):
                        simulate_chown_record(os.path.join(dest_dir, member.name), member.uid, member.gid, member.mode)
                except Exception:
                    # ignore if no numeric uid
                    pass
            except Exception as e:
                logger.warn("Falha ao extrair membro %s: %s", member.name, e)

def install_with_fakeroot(artifact_path: str, dest_dir: str, overwrite: bool = False) -> dict:
    """
    Extrai o artifact em dest_dir simulando install com root:
    - retorna dict com info: {"installed": True, "applied": N, "failed": M}
    - se existir um mecanismo fakeroot, owners/modes serão aplicados nativamente
    - se não, grava .ibuild_ownership.json para pós-aplicação manual
    """
    if not os.path.isfile(artifact_path):
        raise FileNotFoundError(artifact_path)
    os.makedirs(dest_dir, exist_ok=True)
    logger.info("Instalando %s em %s via fakeroot", artifact_path, dest_dir)
    if is_available():
        # executar tar com preservação
        cmd = ["tar", "-xpf", artifact_path, "-C", dest_dir]
        run(cmd, cwd=None, check=True)
        # se houver manifest of ownership apply it (some tar may include owners)
        applied, failed = apply_ownership_records(dest_dir)
        return {"installed": True, "applied_ownership": applied, "failed_ownership": failed}
    else:
        # fallback: extract via python tar, record ownerships via simulate_chown_record
        import tarfile as _tar
        extracted = []
        with _tar.open(artifact_path, "r:*") as tf:
            for m in tf.getmembers():
                try:
                    tf.extract(m, path=dest_dir)
                    extracted.append(m.name)
                    if hasattr(m, "uid") and isinstance(m.uid, int):
                        simulate_chown_record(os.path.join(dest_dir, m.name), m.uid, m.gid, m.mode)
                except Exception as e:
                    logger.warn("Erro extraindo %s: %s", m.name, e)
        # write a small manifest of extracted files
        manifest = os.path.join(dest_dir, ".ibuild_extracted_files.json")
        try:
            with open(manifest, "w", encoding="utf-8") as f:
                json.dump({"files": extracted}, f, indent=2)
        except Exception:
            pass
        logger.warn("Instalação feita sem fakeroot; ownership registrados em %s", _ownership_manifest_path(dest_dir))
        return {"installed": True, "extracted_count": len(extracted), "ownership_manifest": _ownership_manifest_path(dest_dir)}

# ----------------------------------------------------------------------
# Utility: safe chown wrapper (aplica somente se for root)
# ----------------------------------------------------------------------
def safe_chown(path: str, uid: int, gid: int):
    """
    Tenta aplicar chown somente se for possível (processo root).
    """
    try:
        if os.geteuid() == 0:
            os.chown(path, uid, gid)
            return True
        else:
            logger.debug("safe_chown: sem permissão para chown %s", path)
            return False
    except Exception as e:
        logger.warn("safe_chown erro em %s: %s", path, e)
        return False

# ----------------------------------------------------------------------
# Exemplo de uso (documentação)
# ----------------------------------------------------------------------
__doc__ += """
Exemplos de uso:

from ibuild1.0.modules_py import fakeroot

# simples execução de um comando sob fakeroot/proot (se disponível)
fakeroot.run(["bash","-c","echo hello > /tmp/hello_as_root"])

# context manager
with fakeroot.FakerootContext(pkg_name="mypkg") as f:
    f.run(["bash","-c","make install DESTDIR=/tmp/ibuild_fake_root"])

# extrair e instalar artefato no DESTDIR
fakeroot.install_with_fakeroot("/path/to/pkg-1.0.tar.gz", "/tmp/install_dest")

# aplicar ownership manifest (pode precisar de root)
fakeroot.apply_ownership_records("/tmp/install_dest")
"""

# fim do fakeroot.py
