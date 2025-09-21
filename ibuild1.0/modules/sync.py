import os

from ibuild1.0.modules_py import config, log, utils


class SyncError(Exception):
    pass


def repo_path() -> str:
    """Retorna caminho do repositório local"""
    return config.get("repo_dir")


def sync_repo(remote_url: str, branch: str = "main"):
    """
    Sincroniza repositório de pacotes:
      - Clona se não existir
      - Dá pull se já existir
    """
    repo_dir = repo_path()

    if not os.path.exists(repo_dir) or not os.path.isdir(os.path.join(repo_dir, ".git")):
        log.info("Clonando repositório %s → %s", remote_url, repo_dir)
        utils.ensure_dir(os.path.dirname(repo_dir))
        rc, _, _ = utils.run(["git", "clone", "--branch", branch, remote_url, repo_dir], check=False)
        if rc != 0:
            raise SyncError(f"Falha ao clonar repositório {remote_url}")
    else:
        log.info("Atualizando repositório em %s", repo_dir)
        rc, _, _ = utils.run(["git", "-C", repo_dir, "fetch", "--all"], check=False)
        if rc != 0:
            raise SyncError("Falha ao buscar atualizações")

        rc, _, _ = utils.run(["git", "-C", repo_dir, "reset", "--hard", f"origin/{branch}"], check=False)
        if rc != 0:
            raise SyncError(f"Falha ao resetar para branch {branch}")

        log.info("Repositório sincronizado em %s", repo_dir)


def checkout(branch: str):
    """Troca de branch dentro do repositório"""
    repo_dir = repo_path()
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        raise SyncError("Repositório não inicializado")

    rc, _, _ = utils.run(["git", "-C", repo_dir, "checkout", branch], check=False)
    if rc != 0:
        raise SyncError(f"Falha ao trocar para branch {branch}")

    log.info("Checkout realizado: %s", branch)


def current_commit() -> str:
    """Retorna hash do commit atual"""
    repo_dir = repo_path()
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        raise SyncError("Repositório não inicializado")

    rc, out, _ = utils.run(["git", "-C", repo_dir, "rev-parse", "HEAD"], check=False)
    if rc != 0:
        raise SyncError("Falha ao obter commit atual")
    return out.strip()
