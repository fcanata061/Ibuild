#!/usr/bin/env python3
# cli.py — Ibuild CLI completo (atualizado: add update + healthcheck)
from __future__ import annotations
import argparse
import sys
import os
import json
import time
from concurrent.futures import ThreadPoolExecutor

# importa módulos do diretório modules/
from modules import (
    build as build_mod,
    package as package_mod,
    upgrade as upgrade_mod,
    rollback as rollback_mod,
    dependency as dep_mod,
    meta as meta_mod,
    sandbox as sb_mod,
    log as log_mod,
    config as config_mod,
    utils,
    update as update_mod,
    healthcheck as health_mod,
)

# ANSI colors
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

def color(text: str, col: str) -> str:
    return f"{C.get(col,'')}{text}{C['reset']}"

logger = log_mod.get_logger("cli")

def _print_json_or_plain(data, as_json: bool):
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if isinstance(data, dict):
            for k, v in data.items():
                print(f"{color(k, 'cyan')}: {v}")
        elif isinstance(data, list):
            for it in data:
                print(it)
        else:
            print(data)

def _setup_logging(verbose: bool):
    if verbose:
        log_mod.set_level("debug")
    else:
        log_mod.set_level("info")

# -----------------------
# Command handlers
# -----------------------

def cmd_build(args):
    try:
        artifact, meta = build_mod.build_package(
            args.pkg,
            category=args.category,
            resolve_deps=not args.no_deps,
            include_optional=args.include_optional,
            jobs=getattr(args, "jobs", None),
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
            m = meta_mod.load_meta(args.pkg, args.category)
            art = f"{config_mod.get('cache_dir')}/packages/{m['name']}-{m.get('version')}.tar.gz"
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
        if ok:
            print(color("[OK] Pacote removido", "green"))
            return 0
        else:
            print(color("[WARN] Nada foi removido", "yellow"))
            return 1
    except Exception as e:
        print(color(f"[ERRO] Remoção falhou: {e}", "red"), file=sys.stderr)
        return 2

def cmd_list(args):
    pkgs = package_mod.list_installed()
    for p in pkgs:
        name = p.get("name")
        version = p.get("version", "?")
        print(f"{color(name,'cyan')} {color(version,'magenta')}")
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
        inst = package_mod.query_package(args.pkg) is not None
        print(color(f"Pacote: {m['name']} {m.get('version','?')}", "cyan"))
        print(color("Status: instalado", "green") if inst else color("Status: não instalado", "yellow"))
        print(f"Descrição: {m.get('description', '(sem descrição)')}")
        print(f"Categoria: {m.get('category','?')}")
        print("Dependências:", m.get("dependencies", []))
        print("Optional:", m.get("optional_dependencies", []))
        return 0
    except Exception as e:
        print(color(f"[ERRO] Info falhou: {e}", "red"), file=sys.stderr)
        return 2

def cmd_verify(args):
    # integra com modules.healthcheck
    fix = args.fix
    report = health_mod.healthcheck(autofix=fix)
    health_mod.generate_report(report)
    total = report["summary"].get("total_packages", 0)
    affected = report["summary"].get("affected_packages", 0)
    broken_links = report["summary"].get("broken_symlinks", 0)

    if affected == 0 and broken_links == 0:
        print(color(f"✅ Sistema íntegro ({total} pacotes verificados)", "green"))
        return 0
    else:
        print(color(f"❌ {affected} pacotes afetados, {broken_links} links quebrados (de {total})", "red"))
        for pkg in report.get("packages", []):
            print(color(f"[{pkg['name']}]:", "yellow"))
            for issue in pkg.get("issues", []):
                sev = issue.get("severity", "UNKNOWN")
                col = "red" if sev in ("CRITICAL", "HIGH") else "yellow"
                print(color(f" - {issue['type']} ({sev}): {issue['details']}", col))
                print(f"   Sugestão: {issue['suggestion']}")
            if pkg.get("fixed"):
                for fix_msg in pkg["fixed"]:
                    print(color(f"   🔧 Corrigido: {fix_msg}", "blue"))
        if report.get("broken_symlinks"):
            print(color("\nLinks quebrados:", "yellow"))
            for l in report["broken_symlinks"]:
                print(f"  {l['path']} -> {l['target']}")
                if l.get("fixed"):
                    print(color("    🔧 Corrigido: link removido", "blue"))
        # exit code 1 indicates problems found
        return 1

def cmd_logs(args):
    log_dir = config_mod.get("log_dir") or os.path.join(os.getcwd(), "logs")
    if not os.path.isdir(log_dir):
        print(color(f"[ERRO] Diretório de log não existe: {log_dir}", "red"))
        return 1
    logs = [fn for fn in os.listdir(log_dir) if fn.endswith(".log")]
    print(color("=== Logs disponíveis ===", "magenta"))
    for l in logs:
        print(color(l, "cyan"))
    return 0

def cmd_log(args):
    log_dir = config_mod.get("log_dir") or os.path.join(os.getcwd(), "logs")
    log_file = os.path.join(log_dir, f"{args.name}.log")
    if not os.path.isfile(log_file):
        print(color(f"[ERRO] Log {args.name} não encontrado: {log_file}", "red"))
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
                print(color("\n== Parado ==", "magenta"))
    else:
        with open(log_file, "r", encoding="utf-8") as f:
            print(f.read())
    return 0

def cmd_update(args):
    """
    Executa modules.update.main(), gera report e apresenta resumo.
    --no-notify : não enviar notify-send
    --bar : imprime "updates/total" (útil para status bars)
    """
    # run the module (which will generate the OUTPUT files and notifications by default)
    try:
        # allow update_mod to accept options if implemented; else call main()
        if hasattr(update_mod, "main"):
            update_mod.main()
        else:
            # fallback: try run_update()
            update_mod.run_update()
    except Exception as e:
        print(color(f"[ERRO] Falha ao executar update: {e}", "red"), file=sys.stderr)
        return 2

    # read the standard output JSON if exists
    output_json = getattr(update_mod, "OUTPUT_JSON", "/var/lib/ibuild/updates.json")
    if os.path.isfile(output_json):
        try:
            with open(output_json, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            print(color(f"[ERRO] Falha ao ler relatório de update: {e}", "red"), file=sys.stderr)
            return 2
    else:
        print(color("[WARN] Relatório de atualização não encontrado.", "yellow"))
        data = None

    if args.bar:
        # print counts in a compact form for status bar: "updates/total" or "0/0" if unknown
        if data and "summary" in data:
            s = data["summary"]
            print(f"{s.get('updates',0)}/{s.get('total',0)}")
        else:
            # try to compute from packages list
            if data and "packages" in data:
                total = len(data["packages"])
                updates = sum(1 for p in data["packages"] if p.get("latest") and p.get("latest") != p.get("current"))
                print(f"{updates}/{total}")
            else:
                print("0/0")
        return 0

    # Default pretty output
    if data:
        summary = data.get("summary", {})
        print(color("=== Update Summary ===", "magenta"))
        print(f"Total packages: {summary.get('total', '?')}")
        print(f"Updates available: {summary.get('updates', '?')}")
        print(f"Up-to-date: {summary.get('up_to_date', '?')}")
        if summary.get("updates", 0) > 0:
            print(color("Pacotes com novas versões:", "yellow"))
            packs = data.get("packages", [])
            for p in packs:
                if p.get("latest") and p.get("latest") != p.get("current"):
                    print(f" - {color(p['name'],'cyan')}: {p['current']} -> {color(p['latest'],'green')}")
        return 0

    print(color("[WARN] Sem dados de atualização para mostrar.", "yellow"))
    return 0

def cmd_pipeline(args):
    try:
        print(color("[INFO] Pipeline: baixar → extrair → patch → build → instalar", "blue"))
        if args.url:
            src = utils.download(args.url, dest_dir=args.dest or "/tmp")
            extracted = utils.extract_archive(src, dest_dir=args.dest or "/tmp")
            if args.patch:
                utils.apply_patch(extracted, args.patch)
            # if pkg is given we still call build on the meta entry
        artifact, meta = build_mod.build_package(args.pkg, category=args.category, resolve_deps=True, include_optional=False, jobs=getattr(args,'jobs',None))
        package_mod.install_package(artifact, dest_dir=args.dest or None, overwrite=True, upgrade=args.upgrade)
        print(color("[OK] Pipeline concluído", "green"))
        return 0
    except Exception as e:
        print(color(f"[ERRO] Pipeline falhou: {e}", "red"), file=sys.stderr)
        return 2

# -----------------------
# Argument parser
# -----------------------

def build_parser():
    p = argparse.ArgumentParser(prog="ibuild", description="Ibuild - Gerenciador de pacotes")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    p.add_argument("--json", action="store_true", help="JSON output")
    sub = p.add_subparsers(dest="command")

    sb = sub.add_parser("build", aliases=["b"], help="Build a package from .meta")
    sb.add_argument("pkg")
    sb.add_argument("--category", default=None)
    sb.add_argument("--no-deps", action="store_true")
    sb.add_argument("--include-optional", action="store_true")
    sb.add_argument("--jobs", "-j", type=int, default=None)
    sb.add_argument("--keep-sandbox", action="store_true")
    sb.set_defaults(func=cmd_build)

    si = sub.add_parser("install", aliases=["i"], help="Install artifact or package")
    si.add_argument("pkg", nargs="?")
    si.add_argument("--artifact", "-a", default=None)
    si.add_argument("--category", default=None)
    si.add_argument("--dest", default=None)
    si.add_argument("--overwrite", action="store_true")
    si.add_argument("--upgrade", action="store_true")
    si.set_defaults(func=cmd_install)

    sr = sub.add_parser("remove", aliases=["rm"], help="Remove installed package")
    sr.add_argument("pkg")
    sr.add_argument("--purge", action="store_true")
    sr.set_defaults(func=cmd_remove)

    sl = sub.add_parser("list", aliases=["ls"], help="List installed packages")
    sl.set_defaults(func=cmd_list)

    ss = sub.add_parser("search", aliases=["s"], help="Search packages")
    ss.add_argument("pattern")
    ss.set_defaults(func=cmd_search)

    si2 = sub.add_parser("info", help="Show package info")
    si2.add_argument("pkg")
    si2.add_argument("--category", default=None)
    si2.set_defaults(func=cmd_info)

    sv = sub.add_parser("verify", help="Health check (integrity). Use --fix to attempt repairs")
    sv.add_argument("--fix", "-f", action="store_true", help="Attempt automatic fixes")
    sv.set_defaults(func=cmd_verify)

    slog = sub.add_parser("logs", help="List log files")
    slog.set_defaults(func=cmd_logs)

    slg = sub.add_parser("log", help="Show specific log (with --follow)")
    slg.add_argument("name")
    slg.add_argument("--follow", "-f", action="store_true")
    slg.set_defaults(func=cmd_log)

    sup = sub.add_parser("update", aliases=["upd"], help="Scan repository for newer upstream versions and notify")
    sup.add_argument("--no-notify", action="store_true", help="Do not send desktop notification")
    sup.add_argument("--bar", action="store_true", help="Print 'updates/total' for status bars")
    sup.set_defaults(func=cmd_update)

    su = sub.add_parser("upgrade", aliases=["up"], help="Upgrade a package (resolve, build, install)")
    su.add_argument("pkg")
    su.set_defaults(func=lambda args: (upgrade_mod.upgrade_package(args.pkg, commit=True), 0)[1])

    srb = sub.add_parser("rollback", aliases=["rb"], help="Rollback last or a specific package")
    srb.add_argument("--last", action="store_true")
    srb.add_argument("--pkg", default=None)
    srb.add_argument("--version", default=None)
    srb.set_defaults(func=lambda args: (rollback_mod.rollback_pkg_to_version(args.pkg, args.version, commit=True) if not args.last else rollback_mod.rollback_last(commit=True), 0)[1])

    srd = sub.add_parser("revdep", aliases=["rd"], help="Reverse dependency check / fix")
    srd.add_argument("--fix", action="store_true")
    srd.set_defaults(func=lambda args: (rollback_mod.revdep_fix(fix=args.fix, dry_run=not args.fix), 0)[1])

    sor = sub.add_parser("orphan", aliases=["or"], help="Detect/remove orphan packages")
    sor.add_argument("--force", action="store_true")
    sor.set_defaults(func=lambda args: (rollback_mod.remove_orphans(dry_run=False, force=args.force), 0)[1])

    sh = sub.add_parser("history", aliases=["h"], help="Show rollback/upgrade history")
    sh.add_argument("--n", type=int, default=50)
    sh.set_defaults(func=lambda args: (rollback_mod.history(n=args.n), 0)[1])

    sp = sub.add_parser("pipeline", aliases=["all"], help="Fetch → patch → build → install pipeline")
    sp.add_argument("pkg")
    sp.add_argument("--url", default=None)
    sp.add_argument("--patch", default=None)
    sp.add_argument("--category", default=None)
    sp.add_argument("--dest", default=None)
    sp.add_argument("--upgrade", action="store_true")
    sp.set_defaults(func=cmd_pipeline)

    return p

def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    _setup_logging(getattr(args, "verbose", False))
    result = args.func(args)
    # if handler returned an int exit code, use it
    if isinstance(result, int):
        sys.exit(result)
    # some lambdas used above return tuple; ensure exit 0
    sys.exit(0)

if __name__ == "__main__":
    main()
