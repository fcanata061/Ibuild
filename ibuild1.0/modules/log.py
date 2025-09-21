import logging
import os
import subprocess
import traceback
from logging.handlers import RotatingFileHandler
from datetime import datetime

from ibuild1.0.modules_py import config

# -------------------------
# Configuração inicial
# -------------------------
LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

_root_logger = logging.getLogger("ibuild")
_root_logger.setLevel(logging.DEBUG)  # captura tudo


class ColorFormatter(logging.Formatter):
    """Formata mensagens com cores para o console"""
    COLORS = {
        logging.DEBUG: "\033[36m",   # ciano
        logging.INFO: "\033[32m",    # verde
        logging.WARNING: "\033[33m", # amarelo
        logging.ERROR: "\033[31m",   # vermelho
        logging.CRITICAL: "\033[41m" # fundo vermelho
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        module = f"[{record.name}]" if record.name != "ibuild" else ""
        msg = super().format(record)
        return f"{color}[{ts}] {record.levelname.lower():<8}{module}{self.RESET} {msg}"


def _setup_handlers():
    """Configura handlers globais"""
    if _root_logger.handlers:
        return  # já configurado

    # Console
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(ColorFormatter("%(message)s"))
    _root_logger.addHandler(ch)

    # Arquivo
    log_dir = config.get("log_dir")
    os.makedirs(log_dir, exist_ok=True)
    logfile = os.path.join(log_dir, "ibuild.log")

    fh = RotatingFileHandler(logfile, maxBytes=10 * 1024 * 1024, backupCount=5)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        "%Y-%m-%d %H:%M:%S"
    ))
    _root_logger.addHandler(fh)


_setup_handlers()


# -------------------------
# API pública
# -------------------------
def get_logger(name: str = "ibuild"):
    """Obtém sub-logger (ex.: log.get_logger("sandbox"))"""
    return _root_logger.getChild(name)


def set_level(level: str):
    """Altera nível global"""
    lvl = LEVELS.get(level.lower())
    if lvl is None:
        raise ValueError(f"Nível inválido: {level}")
    for handler in _root_logger.handlers:
        handler.setLevel(lvl)


def exception(msg: str):
    """Loga erro com traceback completo"""
    tb = traceback.format_exc()
    _root_logger.error("%s\n%s", msg, tb)


def run_cmd(cmd: list[str], cwd: str | None = None, env: dict | None = None):
    """
    Executa comando externo registrando stdout/stderr em tempo real.
    Retorna (returncode, stdout, stderr).
    """
    logger = get_logger("cmd")
    logger.info("Executando: %s", " ".join(cmd))

    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    stdout_lines, stderr_lines = [], []
    for line in process.stdout:
        line = line.rstrip()
        stdout_lines.append(line)
        logger.debug("[stdout] %s", line)
    for line in process.stderr:
        line = line.rstrip()
        stderr_lines.append(line)
        logger.warning("[stderr] %s", line)

    process.wait()
    rc = process.returncode

    if rc != 0:
        logger.error("Comando falhou com código %s", rc)

    return rc, "\n".join(stdout_lines), "\n".join(stderr_lines)


# Atalhos simples (sem precisar chamar get_logger)
def debug(msg, *args, **kwargs): _root_logger.debug(msg, *args, **kwargs)
def info(msg, *args, **kwargs): _root_logger.info(msg, *args, **kwargs)
def warn(msg, *args, **kwargs): _root_logger.warning(msg, *args, **kwargs)
def error(msg, *args, **kwargs): _root_logger.error(msg, *args, **kwargs)
def critical(msg, *args, **kwargs): _root_logger.critical(msg, *args, **kwargs)
