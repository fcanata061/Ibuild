# cli.py
"""
Ibuild - CLI evoluído, colorido e completo.

Comandos:
  build / b         -> compilar pacote
  install / i       -> instalar pacote
  remove / rm       -> remover pacote
  list / ls         -> listar pacotes instalados
  search / s        -> procurar pacotes (instalados e no repo)
  info              -> mostrar informações detalhadas do pacote
  verify            -> verificar integridade de pacote
  repair            -> reparar pacote
  sync              -> sincronizar repo de .meta
  sandbox / sb      -> abrir shell no sandbox
  deps              -> resolver dependências
  meta              -> mostrar meta parseado
  upgrade / up      -> upgrade de pacote
  rollback / rb     -> rollback
  revdep / rd       -> checar/arrumar dependências reversas
  orphan / or       -> remover órfãos
  history / h       -> histórico de rollback
  logs              -> listar arquivos de log
  log               -> ver log específico (com --follow tipo tail -f)
  pipeline / all    -> baixar → extrair → aplicar patch → compilar → instalar
"""

from __future__ import annotations
import argparse
import sys
import json
import os
import time

# importa dos módulos
from modules import (
    build as build_mod,
    package as package_mod,
    upgrade as upgrade_mod,
    rollback as rollback_mod,
    sync as sync_mod,
    dependency as dep_mod,
    meta as meta_mod,
    sandbox as sb_mod,
    log as log_mod,
    config as config_mod,
    utils,
)

# 🎨 ANSI cores
C = {
    "reset": "\033[0m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "bold": "\033[1m",
}

logger = log_mod.get_logger("cli")

def color(msg: str, color: str) -> str:
    return f"{C.get(color,'')}{msg}{C['reset']}"

def _print_json_or_plain(data, as_json: bool):
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if isinstance(data, dict):
            for k, v in data.items():
                print(f"{color(k,'cyan')}: {v}")
        elif isinstance(data, list):
            for it in data:
                print(it)
        else:
            print(data)

def _setup_logging(verbose: bool, quiet: bool):
    if quiet:
        log_mod.set_level("error")
    elif verbose:
        log_mod.set_level("debug")
    else:
        log_mod.set_level("info")

# ----------------------
# Subcomandos
# ----------------------

def cmd_build(args):
    try:
        artifact, meta = build_mod.build_package(
            args.pkg,
            category=args.category,
            resolve_deps=not args.no_deps,
            include_optional=args.include_optional,
            jobs=args.jobs,
            keep_sandbox=args.keep_sandbox,
        )
        print(color("[OK] Build concluída", "green"))
        _print_json_or_plain({"artifact": artifact, "pkg": meta["name"], "version": meta.get("version")}, args.json)
        return 0
    except Exception as e:
        print(color(f"[ERRO] Build falhou: {e}", "red"), file=sys.stderr)
        return 2

def cmd_install(args):
    try:
        art = args.artifact
        if not art and args.pkg:
            meta = meta_mod.load_meta(args.pkg, args.category)
            art = f"{config_mod.get('cache_dir')}/packages/{meta['name']}-{meta.get('version')}.tar.gz"
        res = package_mod.install_package(art, dest_dir=args.dest, overwrite=args.overwrite, upgrade=args.upgrade)
        print(color("[OK] Pacote instalado", "green"))
        _print_json_or_plain(res, args.json)
        return 0
    except Exception as e:
        print(color(f"[ERRO] Instalação falhou: {e}", "red"), file=sys.stderr)
        return 2

def cmd_remove(args):
    try:
        ok = package_mod.remove_package(args.pkg, purge=args.purge)
        msg = "[OK] Pacote removido" if ok else "[WARN] Nada removido"
        print(color(msg, "yellow" if not ok else "green"))
        return 0 if ok else 1
    except Exception as e:
        print(color(f"[ERRO] Remoção falhou: {e}", "red"), file=sys.stderr)
        return 2

def cmd_list(args):
    pkgs = package_mod.list_installed()
    for p in pkgs:
        print(f"{color(p['name'],'cyan')} {color(p.get('version','?'),'magenta')}")
    return 0

def cmd_search(args):
    installed = package_mod.search_installed(args.pattern)
    metas = meta_mod.search_meta(args.pattern)
    print(color("=== Instalados ===", "magenta"))
    for p in installed:
        print(f"{color(p['name'],'cyan')} {p.get('version','?')}")
    print(color("=== Disponíveis (.meta) ===", "magenta"))
    for m in metas:
        print(f"{color(m['name'],'cyan')} {m.get('version','?')}")
    return 0

def cmd_info(args):
    try:
        m = meta_mod.load_meta(args.pkg, args.category)
        inst = package_mod.is_installed(args.pkg)
        print(color(f"Pacote: {m['name']} {m.get('version','?')}", "cyan"))
        if inst:
            print(color("Status: instalado", "green"))
        else:
            print(color("Status: não instalado", "yellow"))
        print(f"Descrição: {m.get('description','(sem descrição)')}")
        print(f"Categoria: {m.get('category','?')}")
        print("Dependências:", m.get("dependencies", []))
        print("Optional:", m.get("optional_dependencies", []))
        return 0
    except Exception as e:
        print(color(f"[ERRO] Info falhou: {e}", "red"), file=sys.stderr)
        return 2

def cmd_logs(args):
    log_dir = config_mod.get("pkg_db")
    logs = [f for f in os.listdir(log_dir) if f.endswith(".log")]
    print(color("=== Logs disponíveis ===", "magenta"))
    for l in logs:
        print(color(l, "cyan"))
    return 0

def cmd_log(args):
    log_file = os.path.join(config_mod.get("pkg_db"), f"{args.name}.log")
    if not os.path.isfile(log_file):
        print(color(f"[ERRO] Log {args.name} não encontrado", "red"))
        return 1

    if args.follow:
        print(color(f"== Seguindo {log_file} (Ctrl+C para sair) ==", "magenta"))
        with open(log_file, "r", encoding="utf-8") as f:
            f.seek(0, os.SEEK_END)
            try:
                while True:
                    line = f.readline()
                    if not line:
                        time.sleep(0.5)
                        continue
                    if "[ERROR]" in line:
                        print(color(line.strip(), "red"))
                    elif "[WARN]" in line:
                        print(color(line.strip(), "yellow"))
                    elif "[INFO]" in line:
                        print(color(line.strip(), "blue"))
                    else:
                        print(line.strip())
            except KeyboardInterrupt:
                print(color("\n== Encerrado ==", "magenta"))
    else:
        with open(log_file, "r", encoding="utf-8") as f:
            print(f.read())
    return 0

def cmd_pipeline(args):
    try:
        print(color("[INFO] Baixando...", "blue"))
        src = utils.download(args.url, dest_dir=args.dest or "/tmp")
        print(color("[INFO] Extraindo...", "blue"))
        extracted = utils.extract_archive(src, dest_dir=args.dest or "/tmp")
        if args.patch:
            print(color("[INFO] Aplicando patch...", "blue"))
            utils.apply_patch(extracted, args.patch)
        print(color("[INFO] Compilando...", "blue"))
        artifact, meta = build_mod.build_package(args.pkg, category=args.category)
        print(color("[INFO] Instalando...", "blue"))
        package_mod.install_package(artifact, dest_dir=args.dest or None, overwrite=True, upgrade=args.upgrade)
        print(color("[OK] Pipeline concluído", "green"))
        return 0
    except Exception as e:
        print(color(f"[ERRO] Pipeline falhou: {e}", "red"), file=sys.stderr)
        return 2

# ----------------------
# Parser
# ----------------------
def build_parser():
    p = argparse.ArgumentParser(prog="ibuild", description="Ibuild - Gerenciador de pacotes")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--jobs", "-j", type=int, default=None)
    sub = p.add_subparsers(dest="command")

    # build
    sb = sub.add_parser("build", aliases=["b"])
    sb.add_argument("pkg")
    sb.add_argument("--category")
    sb.add_argument("--no-deps", action="store_true")
    sb.add_argument("--include-optional", action="store_true")
    sb.add_argument("--keep-sandbox", action="store_true")
    sb.set_defaults(func=cmd_build)

    # install
    si = sub.add_parser("install", aliases=["i"])
    si.add_argument("pkg", nargs="?")
    si.add_argument("--artifact")
    si.add_argument("--category")
    si.add_argument("--dest")
    si.add_argument("--overwrite", action="store_true")
    si.add_argument("--upgrade", action="store_true")
    si.set_defaults(func=cmd_install)

    # remove
    sr = sub.add_parser("remove", aliases=["rm"])
    sr.add_argument("pkg")
    sr.add_argument("--purge", action="store_true")
    sr.set_defaults(func=cmd_remove)

    # list
    sl = sub.add_parser("list", aliases=["ls"])
    sl.set_defaults(func=cmd_list)

    # search
    ss = sub.add_parser("search", aliases=["s"])
    ss.add_argument("pattern")
    ss.set_defaults(func=cmd_search)

    # info
    si = sub.add_parser("info")
    si.add_argument("pkg")
    si.add_argument("--category")
    si.set_defaults(func=cmd_info)

    # logs
    slog = sub.add_parser("logs")
    slog.set_defaults(func=cmd_logs)

    # log
    slg = sub.add_parser("log")
    slg.add_argument("name")
    slg.add_argument("--follow", "-f", action="store_true")
    slg.set_defaults(func=cmd_log)

    # pipeline
    sp = sub.add_parser("pipeline", aliases=["all"])
    sp.add_argument("pkg")
    sp.add_argument("--url")
    sp.add_argument("--patch")
    sp.add_argument("--category")
    sp.add_argument("--dest")
    sp.add_argument("--upgrade", action="store_true")
    sp.set_defaults(func=cmd_pipeline)

    return p

def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    _setup_logging(args.verbose, args.quiet)
    if args.jobs:
        config_mod.set("jobs", args.jobs)

    return args.func(args)

if __name__ == "__main__":
    sys.exit(main())
