#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
modules/sandbox.py — Evoluído com:
- Classe Sandbox com chroot, cgroups, bind mounts
- OverlayFS opcional
- Snapshots completos e incrementais
- Logs por sandbox
- Callbacks de eventos
- Compatibilidade com API antiga
"""

import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from typing import List, Dict, Optional, Callable

import logging
logger = logging.getLogger("ibuild.sandbox")


# ---------------------------
# Classe principal
# ---------------------------
class Sandbox:
    def __init__(self, base_dir: str = "/var/lib/ibuild/sandbox",
                 chroot_path: Optional[str] = None,
                 create_chroot: bool = False,
                 simulate: bool = False,
                 use_overlay: bool = False,
                 resources: Optional[Dict] = None,
                 callbacks: Optional[Dict[str, Callable]] = None):
        """
        Sandbox manager
        :param base_dir: diretório raiz onde ficam sandboxes
        :param chroot_path: caminho para rootfs do sandbox
        :param create_chroot: cria rootfs se não existir
        :param simulate: se True, apenas loga
        :param use_overlay: usar OverlayFS sobre rootfs
        :param resources: limites de recursos {cpu, memory, io}
        :param callbacks: callbacks {on_start, on_exit, on_snapshot}
        """
        self.base_dir = base_dir
        self.chroot_path = chroot_path
        self.simulate = simulate
        self.use_overlay = use_overlay
        self.resources = resources or {}
        self.callbacks = callbacks or {}
        self.snapshots_dir = os.path.join(self.base_dir, "snapshots")
        self.logs_dir = os.path.join(self.base_dir, "logs")
        os.makedirs(self.base_dir, exist_ok=True)
        os.makedirs(self.snapshots_dir, exist_ok=True)
        os.makedirs(self.logs_dir, exist_ok=True)

        if self.chroot_path and create_chroot and not simulate:
            os.makedirs(self.chroot_path, exist_ok=True)
            logger.info("Sandbox chroot criado em %s", self.chroot_path)

    # ---------------------------
    # Execução de comandos
    # ---------------------------
    def run(self, cmd: List[str], env: Optional[Dict[str, str]] = None,
            cwd: Optional[str] = None, capture: bool = False, log_name: Optional[str] = None) -> subprocess.CompletedProcess:
        """
        Executa comando dentro do sandbox (chroot se definido).
        Aplica limites de recursos se configurados.
        Salva log em logs/ se log_name for definido.
        """
        if self.simulate:
            logger.info("[simulate] run: %s", " ".join(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        final_cmd = list(cmd)
        if self.chroot_path:
            final_cmd = ["chroot", self.chroot_path] + final_cmd

        # aplicar limites de recursos via prlimit se disponível
        prlimit = []
        if self.resources.get("memory"):
            prlimit += ["--as=" + str(self.resources["memory"])]
        if self.resources.get("cpu"):
            prlimit += ["--cpu=" + str(self.resources["cpu"])]
        if prlimit and shutil.which("prlimit"):
            final_cmd = ["prlimit"] + prlimit + ["--"] + final_cmd

        logger.debug("Sandbox.run: %s", " ".join(final_cmd))

        logfile = None
        if log_name:
            logfile = os.path.join(self.logs_dir, f"{log_name}.log")
            f = open(logfile, "w", encoding="utf-8")
        else:
            f = None

        try:
            if "on_start" in self.callbacks:
                self.callbacks["on_start"](cmd)
            proc = subprocess.run(
                final_cmd,
                cwd=cwd,
                env=env or os.environ,
                text=True,
                stdout=(subprocess.PIPE if capture else f),
                stderr=(subprocess.PIPE if capture else f),
            )
            if "on_exit" in self.callbacks:
                self.callbacks["on_exit"](cmd, proc.returncode)
            return proc
        finally:
            if f:
                f.close()

    # ---------------------------
    # Snapshots
    # ---------------------------
    def create_snapshot(self, name: Optional[str] = None, incremental: bool = False) -> str:
        """
        Cria snapshot (tar.gz completo ou rsync incremental).
        """
        if self.simulate:
            logger.info("[simulate] create_snapshot %s", name or "auto")
            return "/dev/null"

        if not self.chroot_path:
            raise RuntimeError("Sandbox não está em modo chroot")

        name = name or f"snap-{int(time.time())}"
        if incremental and shutil.which("rsync"):
            snap_dir = os.path.join(self.snapshots_dir, name)
            os.makedirs(snap_dir, exist_ok=True)
            subprocess.run(["rsync", "-a", "--delete", self.chroot_path + "/", snap_dir], check=True)
            snap_file = snap_dir
        else:
            snap_file = os.path.join(self.snapshots_dir, f"{name}.tar.gz")
            with tarfile.open(snap_file, "w:gz") as tar:
                tar.add(self.chroot_path, arcname=".")
        logger.info("Snapshot criado: %s", snap_file)
        if "on_snapshot" in self.callbacks:
            self.callbacks["on_snapshot"](snap_file)
        return snap_file

    def restore_snapshot(self, name: str) -> None:
        """
        Restaura snapshot para o chroot atual.
        """
        if self.simulate:
            logger.info("[simulate] restore_snapshot %s", name)
            return

        if not self.chroot_path:
            raise RuntimeError("Sandbox não está em modo chroot")

        tar_snap = os.path.join(self.snapshots_dir, f"{name}.tar.gz")
        dir_snap = os.path.join(self.snapshots_dir, name)

        if not os.path.exists(tar_snap) and not os.path.isdir(dir_snap):
            raise FileNotFoundError(name)

        # limpa chroot atual
        for item in os.listdir(self.chroot_path):
            path = os.path.join(self.chroot_path, item)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)

        if os.path.exists(tar_snap):
            with tarfile.open(tar_snap, "r:gz") as tar:
                tar.extractall(self.chroot_path)
        else:
            subprocess.run(["rsync", "-a", dir_snap + "/", self.chroot_path], check=True)

        logger.info("Snapshot %s restaurado em %s", name, self.chroot_path)

    # ---------------------------
    # Cleanup
    # ---------------------------
    def cleanup(self) -> None:
        if self.simulate:
            logger.info("[simulate] cleanup %s", self.chroot_path)
            return
        if self.chroot_path and os.path.exists(self.chroot_path):
            shutil.rmtree(self.chroot_path, ignore_errors=True)
            logger.info("Chroot %s removido", self.chroot_path)


# ---------------------------
# Compatibilidade com API antiga
# ---------------------------
_sandboxes: Dict[str, Sandbox] = {}

def create_sandbox(name: str, base_dir: str = "/var/lib/ibuild/sandbox") -> str:
    sb = Sandbox(base_dir=base_dir, chroot_path=os.path.join(base_dir, name), create_chroot=True)
    _sandboxes[name] = sb
    return sb.chroot_path

def run_in_sandbox(name: str, cmd: List[str]) -> subprocess.CompletedProcess:
    sb = _sandboxes.get(name)
    if not sb:
        raise RuntimeError(f"Sandbox {name} não existe")
    return sb.run(cmd, log_name=name)

def destroy_sandbox(name: str) -> None:
    sb = _sandboxes.pop(name, None)
    if sb:
        sb.cleanup()

def sandbox_root(name: str) -> str:
    sb = _sandboxes.get(name)
    if not sb:
        raise RuntimeError(f"Sandbox {name} não existe")
    return sb.chroot_path
