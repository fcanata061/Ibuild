import os
import shutil
import subprocess

from ibuild1.0.modules_py import config, log, utils


class SandboxError(Exception):
    pass


def sandbox_root(pkg_name: str) -> str:
    """Diretório raiz do sandbox para um pacote"""
    return os.path.join(config.get("cache_dir"), "sandbox", pkg_name)


def create_sandbox(pkg_name: str, binds: list[str] | None = None, keep: bool = False):
    """
    Cria sandbox para um pacote.
    binds: lista de diretórios para montar dentro do sandbox (somente leitura).
    keep: se True, não remove sandbox automaticamente após falha.
    """
    root = sandbox_root(pkg_name)

    if os.path.exists(root):
        log.warn("Sandbox %s já existe, removendo...", pkg_name)
        shutil.rmtree(root)

    os.makedirs(root, exist_ok=True)
    log.info("Sandbox criado em %s", root)

    # Estrutura básica
    for sub in ["build", "install", "tmp", "logs"]:
        os.makedirs(os.path.join(root, sub), exist_ok=True)

    # Criar log local do sandbox
    with open(os.path.join(root, "logs", "sandbox.log"), "w") as f:
        f.write(f"[ibuild] sandbox {pkg_name} criado\n")

    # Bind mounts reais se disponíveis
    if binds:
        bindfile = os.path.join(root, "binds.txt")
        with open(bindfile, "w") as f:
            f.write("\n".join(binds))
        log.info("Binds registrados: %s", ", ".join(binds))

        if shutil.which("unshare") and shutil.which("mount"):
            log.info("Sistema suporta namespaces → binds reais podem ser usados")
        else:
            log.warn("Sistema não suporta unshare/mount → binds apenas registrados")


def _sandbox_env(pkg_name: str, extra_env: dict | None = None) -> dict:
    """Ambiente controlado para builds"""
    root = sandbox_root(pkg_name)
    env = os.environ.copy()

    env.update({
        "DESTDIR": os.path.join(root, "install"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "CFLAGS": "-O2 -pipe",
        "LDFLAGS": "-Wl,-O1 -Wl,--as-needed"
    })

    if extra_env:
        env.update(extra_env)

    return env


def run_in_sandbox(pkg_name: str, cmd: list[str],
                   cwd: str | None = None,
                   env: dict | None = None,
                   memory_limit: str | None = None,
                   cpu_limit: int | None = None,
                   phase: str = "build") -> tuple[int, str, str]:
    """
    Executa comando dentro do sandbox (via fakeroot).
    Registra stdout/stderr em log global + sandbox.log.
    """
    root = sandbox_root(pkg_name)
    if not os.path.isdir(root):
        raise SandboxError(f"Sandbox não existe: {root}")

    sandbox_env = _sandbox_env(pkg_name, env)
    sandbox_cwd = cwd or os.path.join(root, "build")

    # Montar comando com prlimit/ulimit
    limit_cmd = []
    if shutil.which("prlimit"):
        if memory_limit:
            limit_cmd += ["prlimit", f"--as={memory_limit}"]
        if cpu_limit:
            limit_cmd += ["--cpu={}".format(cpu_limit)]
        if limit_cmd:
            limit_cmd.append("--")
    elif memory_limit or cpu_limit:
        u = []
        if memory_limit:
            u += ["ulimit", "-v", memory_limit]
        if cpu_limit:
            u += ["ulimit", "-t", str(cpu_limit)]
        if u:
            limit_cmd = ["bash", "-c", " ".join(u) + " && exec \"$@\"", "bash"]

    final_cmd = ["fakeroot"] + limit_cmd + cmd

    log_phase = f"[{pkg_name}:{phase}]"
    log.info("%s Executando: %s", log_phase, " ".join(cmd))

    rc, out, err = utils.run(final_cmd, cwd=sandbox_cwd, env=sandbox_env, check=False)

    # Salvar em log local
    with open(os.path.join(root, "logs", "sandbox.log"), "a") as f:
        f.write(f"\n{log_phase} CMD: {' '.join(cmd)}\n")
        f.write(f"{log_phase} RC={rc}\n")
        if out:
            f.write(f"{log_phase} STDOUT:\n{out}\n")
        if err:
            f.write(f"{log_phase} STDERR:\n{err}\n")

    if rc != 0:
        raise SandboxError(f"Falha no sandbox {pkg_name}, fase {phase}: {cmd}")

    return rc, out, err


def destroy_sandbox(pkg_name: str, force: bool = False):
    """Remove sandbox completamente"""
    root = sandbox_root(pkg_name)
    if os.path.isdir(root):
        shutil.rmtree(root)
        log.info("Sandbox %s removido", pkg_name)
    elif force:
        log.warn("Sandbox %s não existia, ignorando", pkg_name)
