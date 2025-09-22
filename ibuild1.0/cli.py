#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cli.py — CLI atualizado do Ibuild (versão com integração: meta, update, healthcheck, toolchain)

Cole as partes 1/3, 2/3 e 3/3 na ordem em um único arquivo cli.py.
"""

from __future__ import annotations
import argparse
import sys
import os
import json
import time
import tempfile
import shutil
import subprocess
from typing import Optional, Any

# importa módulos do projeto (espera que existam em modules/)
try:
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
        utils as utils_mod,
        update as update_mod,
        healthcheck as health_mod,
        toolchain as toolchain_mod,
        packager as packager_mod,
        fakeroot as fakeroot_mod,
    )
except Exception:
    # fallback placeholders para evitar ImportError ao abrir o arquivo; chamadas reais
    # às funções faltantes irão lançar em tempo de execução se os módulos não existirem.
    build_mod = package_mod = upgrade_mod = rollback_mod = dep_mod = None
    meta_mod = sb_mod = log_mod = config_mod = utils_mod = update_mod = health_mod = toolchain_mod = packager_mod = fakeroot_mod = None

# ANSI colors simples
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
    return f"{C.get(col, '')}{text}{C['reset']}"

# logger helper (uses modules.log if available)
if log_mod is not None:
    logger = log_mod.get_logger("cli")
else:
    import logging
    logger = logging.getLogger("cli")
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO)

# Small helpers
def safe_import_name(module, name, default=None):
    if module is None:
        return default
    return getattr(module, name, default)

def _print_json_or_plain(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if isinstance(data, dict):
            for k,v in data.items():
                print(f"{color(str(k), 'cyan')}: {v}")
        elif isinstance(data, list):
            for item in data:
                print(item)
        else:
            print(data)

def _setup_logging(verbose: bool) -> None:
    if log_mod is not None:
        log_mod.set_level("debug" if verbose else "info")
    else:
        if verbose:
            logger.setLevel("DEBUG")
        else:
            logger.setLevel("INFO")

# ---------------------------
# Command handlers (parte 1)
# ---------------------------

def cmd_build(args):
    """
    ibuild build <pkg> [--category CAT] [--no-deps] [--include-optional] [-j N] [--keep-sandbox]
    """
    try:
        jobs = getattr(args, "jobs", None)
        sandbox = getattr(args, "sandbox", False)
        artifact, meta = build_mod.build_package(
            args.pkg if hasattr(args, "pkg") else args.package,
            category=getattr(args, "category", None),
            resolve_deps=not getattr(args, "no_deps", False),
            include_optional=getattr(args, "include_optional", False),
            jobs=jobs,
            keep_sandbox=getattr(args, "keep_sandbox", False),
            sandbox=sandbox,
        )
        print(color("[OK] Build concluída", "green"))
        _print_json_or_plain({"artifact": artifact, "pkg": meta.get("name"), "version": meta.get("version")}, getattr(args, "json", False))
        return 0
    except Exception as e:
        logger.exception("Build falhou")
        print(color(f"[ERRO] Build falhou: {e}", "red"), file=sys.stderr)
        return 2

def cmd_install(args):
    """
    ibuild install <pkg> [--artifact ART] [--category CAT] [--dest DIR] [--overwrite] [--upgrade]
    """
    try:
        art = getattr(args, "artifact", None)
        pkg = getattr(args, "pkg", None) or getattr(args, "package", None)
        if not art and pkg:
            # tenta carregar meta para descobrir artifact path
            try:
                m = meta_mod.load_meta(pkg, getattr(args, "category", None))
                cache = config_mod.get("cache_dir") if config_mod else None
                art = os.path.join(cache, "packages", f"{m['name']}-{m.get('version')}.tar.gz") if cache else None
            except Exception:
                art = None
        res = package_mod.install_package(art, dest_dir=getattr(args, "dest", None), overwrite=getattr(args, "overwrite", False), upgrade=getattr(args, "upgrade", False))
        print(color("[OK] Pacote instalado", "green"))
        _print_json_or_plain(res, getattr(args, "json", False))
        return 0
    except Exception as e:
        logger.exception("Install falhou")
        print(color(f"[ERRO] Instalação falhou: {e}", "red"), file=sys.stderr)
        return 2

def cmd_remove(args):
    """
    ibuild remove <pkg> [--purge]
    """
    try:
        ok = package_mod.remove_package(args.pkg if hasattr(args, "pkg") else args.package, purge=getattr(args, "purge", False))
        if ok:
            print(color("[OK] Pacote removido", "green"))
            return 0
        else:
            print(color("[WARN] Nada foi removido", "yellow"))
            return 1
    except Exception as e:
        logger.exception("Remove falhou")
        print(color(f"[ERRO] Remoção falhou: {e}", "red"), file=sys.stderr)
        return 2

def cmd_list(args):
    """
    ibuild list
    """
    try:
        pkgs = package_mod.list_installed()
    except Exception:
        pkgs = []
        logger.debug("package_mod.list_installed não disponível ou falhou")
    for p in pkgs:
        name = p.get("name")
        version = p.get("version", "?")
        print(f"{color(name, 'cyan')} {color(version, 'magenta')}")
    return 0

def cmd_search(args):
    """
    ibuild search <pattern>
    """
    try:
        installed = package_mod.search_installed(args.pattern)
    except Exception:
        installed = []
    try:
        metas = meta_mod.search_meta(args.pattern)
    except Exception:
        metas = []
    print(color("=== Instalados ===", "magenta"))
    for p in installed:
        print(f"{color(p['name'], 'cyan')} {p.get('version','?')}")
    print(color("=== Disponíveis (.meta) ===", "magenta"))
    for m in metas:
        print(f"{color(m['name'], 'cyan')} {m.get('version','?')}")
    return 0

def cmd_info(args):
    """
    ibuild info <pkg>
    """
    try:
        m = meta_mod.load_meta(args.pkg if hasattr(args, "pkg") else args.package, getattr(args, "category", None))
        inst = package_mod.query_package(m["name"]) if package_mod else None
        print(color(f"Pacote: {m['name']} {m.get('version','?')}", "cyan"))
        print(color("Status: instalado", "green") if inst else color("Status: não instalado", "yellow"))
        print(f"Descrição: {m.get('description','(sem descrição)')}")
        print(f"Categoria: {m.get('category','?')}")
        print("Dependências:", m.get("dependencies", []))
        print("Optional:", m.get("optional_dependencies", []))
        if m.get("_patches"):
            print("Patches:", m["_patches"])
        return 0
    except Exception as e:
        logger.exception("Info falhou")
        print(color(f"[ERRO] Info falhou: {e}", "red"), file=sys.stderr)
        return 2
# Parte 2/3 — continuação: update, verify(healthcheck), logs, meta-create, pipeline, other helpers
def cmd_verify(args):
    """
    ibuild verify [--fix]
    """
    fix = getattr(args, "fix", False)
    try:
        report = health_mod.healthcheck(autofix=fix)
        health_mod.generate_report(report)
    except Exception as e:
        logger.exception("Healthcheck falhou")
        print(color(f"[ERRO] Healthcheck falhou: {e}", "red"), file=sys.stderr)
        return 2

    total = report.get("summary", {}).get("total_packages", 0)
    affected = report.get("summary", {}).get("affected_packages", 0)
    broken_links = report.get("summary", {}).get("broken_symlinks", 0)

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
                print(color(f" - {issue['type']} ({sev}): {issue.get('details')}", col))
                print(f"   Sugestão: {issue.get('suggestion')}")
            if pkg.get("fixed"):
                for fix_msg in pkg["fixed"]:
                    print(color(f"   🔧 Corrigido: {fix_msg}", "blue"))
        if report.get("broken_symlinks"):
            print(color("\nLinks quebrados:", "yellow"))
            for l in report["broken_symlinks"]:
                print(f"  {l['path']} -> {l['target']}")
                if l.get("fixed"):
                    print(color("    🔧 Corrigido: link removido", "blue"))
        return 1

def cmd_update(args):
    """
    ibuild update [--bar] [--no-notify]
    """
    try:
        # prefer main() if implemented
        if update_mod is None:
            raise RuntimeError("modules.update não disponível")
        if hasattr(update_mod, "main"):
            # if update_mod.main accepts options, it may ignore args; we call it simple
            update_mod.main()
        else:
            # fallback to run_update or check_updates
            if hasattr(update_mod, "run_update"):
                update_mod.run_update()
            elif hasattr(update_mod, "check_updates"):
                update_mod.check_updates()
            else:
                raise RuntimeError("update module não expõe main/run_update/check_updates")
    except Exception as e:
        logger.exception("Update falhou")
        print(color(f"[ERRO] Falha ao executar update: {e}", "red"), file=sys.stderr)
        return 2

    # read output JSON if available
    output_json = getattr(update_mod, "OUTPUT_JSON", "/var/lib/ibuild/updates.json")
    data = None
    if os.path.isfile(output_json):
        try:
            with open(output_json, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = None

    if getattr(args, "bar", False):
        if data and "summary" in data:
            s = data["summary"]
            print(f"{s.get('updates',0)}/{s.get('total',0)}")
        else:
            if data and "packages" in data:
                total = len(data["packages"])
                updates = sum(1 for p in data["packages"] if p.get("latest") and p.get("latest") != p.get("current"))
                print(f"{updates}/{total}")
            else:
                print("0/0")
        return 0

    if data:
        s = data.get("summary", {})
        print(color("=== Update Summary ===", "magenta"))
        print(f"Total packages: {s.get('total','?')}")
        print(f"Updates available: {s.get('updates','?')}")
        print(f"Up-to-date: {s.get('up_to_date','?')}")
        if s.get("updates", 0) > 0:
            print(color("Pacotes com novas versões:", "yellow"))
            for p in data.get("packages", []):
                if p.get("latest") and p.get("latest") != p.get("current"):
                    print(f" - {color(p['name'], 'cyan')}: {p['current']} -> {color(p['latest'], 'green')}")
        return 0

    print(color("[WARN] Sem dados de atualização para mostrar.", "yellow"))
    return 0

def cmd_logs(args):
    """
    ibuild logs
    """
    log_dir = config_mod.get("log_dir") if config_mod else os.path.join(os.getcwd(), "logs")
    if not os.path.isdir(log_dir):
        print(color(f"[ERRO] Diretório de log não existe: {log_dir}", "red"))
        return 1
    logs = [fn for fn in os.listdir(log_dir) if fn.endswith(".log")]
    print(color("=== Logs disponíveis ===", "magenta"))
    for l in logs:
        print(color(l, "cyan"))
    return 0

def cmd_log(args):
    """
    ibuild log <name> [--follow]
    """
    log_dir = config_mod.get("log_dir") if config_mod else os.path.join(os.getcwd(), "logs")
    log_file = os.path.join(log_dir, f"{args.name}.log")
    if not os.path.isfile(log_file):
        print(color(f"[ERRO] Log {args.name} não encontrado: {log_file}", "red"))
        return 1

    if getattr(args, "follow", False):
        print(color(f"== Seguindo {log_file} (Ctrl+C para sair) ==", "magenta"))
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                f.seek(0, os.SEEK_END)
                while True:
                    line = f.readline()
                    if not line:
                        time.sleep(0.5)
                        continue
                    if "[ERROR]" in line:
                        print(color(line.rstrip(), "red"))
                    elif "[WARN]" in line:
                        print(color(line.rstrip(), "yellow"))
                    elif "[INFO]" in line:
                        print(color(line.rstrip(), "blue"))
                    else:
                        print(line.rstrip())
        except KeyboardInterrupt:
            print(color("\n== Parado ==", "magenta"))
        return 0
    else:
        with open(log_file, "r", encoding="utf-8") as f:
            print(f.read())
        return 0

def cmd_config(args):
    """
    ibuild config get <key>
    ibuild config set <key> <value> [--system]
    ibuild config list
    ibuild config reset [--system]
    """
    from modules import config as cfg
    act = args.action
    if act == "get":
        if not args.key:
            print("Uso: ibuild config get <chave>")
            return 1
        print(cfg.get(args.key))
        return 0
    elif act == "set":
        if not args.key or args.value is None:
            print("Uso: ibuild config set <chave> <valor> [--system]")
            return 1
        cfg.set(args.key, args.value, system=args.system)
        print(f"[OK] Configuração '{args.key}' definida para '{args.value}' ({'global' if args.system else 'usuário'})")
        return 0
    elif act == "list":
        allcfg = cfg.all()
        for k, v in allcfg.items():
            print(f"{k}: {v}")
        return 0
    elif act == "reset":
        cfg.reset(system=args.system)
        print(f"[OK] Configuração restaurada para padrões {'globais' if args.system else 'de usuário'}")
        return 0
    else:
        print("Ação desconhecida:", act)
        return 1

def cmd_runtime(args):
    """
    Gerenciamento de runtimes (Python, Ruby, Java, Node, Go, PHP, Perl)
    """
    from modules import runtime
    lang = args.language

    if args.action == "list":
        detailed = args.detailed
        versions = runtime.list_runtimes(lang, detailed=detailed)
        if detailed:
            for v in versions:
                print(f"{v['version']}  [{v['status']}] {'(default)' if v['default'] else ''}")
        else:
            print(" ".join(versions) if versions else f"Nenhuma versão instalada de {lang}")

    elif args.action == "install":
        runtime.install_runtime(lang, args.version)

    elif args.action == "use":
        runtime.set_default(lang, args.version, user=args.user)

    elif args.action == "remove":
        runtime.remove_runtime(lang, args.version)

    elif args.action == "validate":
        runtime.validate_runtime(lang, args.version)

    elif args.action == "repair":
        runtime.repair_runtime(lang)

    elif args.action == "diagnose":
        report = runtime.diagnose_runtime(lang)
        print(f"\nDiagnóstico {lang}")
        print(f"Versão default: {report['default']}")
        for v in report["versions"]:
            status = "OK ✅" if v["ok"] else "BROKEN ❌"
            print(f"- {v['version']} → {status}")

    else:
        print("Uso: ibuild runtime [list|install|use|remove|validate|repair|diagnose]")
        return 1

    return 0

def cmd_meta_create(args):
    """
    ibuild meta-create <pkg> <category> [--version V] [--maintainer M] [--description D]
    """
    try:
        pkg_dir = meta_mod.create_meta(pkg_name=args.pkg if hasattr(args, "pkg") else args.package, category=args.category if hasattr(args, "category") else args.cat,
                                       version=getattr(args, "version", "1.0.0"),
                                       maintainer=getattr(args, "maintainer", "unknown"),
                                       description=getattr(args, "description", ""))
        print(color(f"[OK] Meta criado em {pkg_dir}", "green"))
        return 0
    except Exception as e:
        logger.exception("create_meta falhou")
        print(color(f"[ERRO] Não foi possível criar meta: {e}", "red"), file=sys.stderr)
        return 2

def cmd_pipeline(args):
    """
    ibuild pipeline <pkg> [--url URL] [--patch PATCH] [--category CAT] [--dest DIR] [--upgrade]
    """
    try:
        print(color("[INFO] Pipeline: baixar → extrair → patch → build → instalar", "blue"))
        if getattr(args, "url", None):
            src = utils_mod.download(args.url, dest_dir=getattr(args, "dest", "/tmp"))
            extracted = utils_mod.extract_archive(src, dest_dir=getattr(args, "dest", "/tmp"))
            if getattr(args, "patch", None):
                utils_mod.apply_patch(extracted, args.patch)
        pkg = getattr(args, "pkg", None) or getattr(args, "package", None)
        artifact, meta = build_mod.build_package(pkg, category=getattr(args, "category", None), resolve_deps=True, include_optional=False, jobs=getattr(args, "jobs", None))
        package_mod.install_package(artifact, dest_dir=getattr(args, "dest", None), overwrite=True, upgrade=getattr(args, "upgrade", False))
        print(color("[OK] Pipeline concluído", "green"))
        return 0
    except Exception as e:
        logger.exception("Pipeline falhou")
        print(color(f"[ERRO] Pipeline falhou: {e}", "red"), file=sys.stderr)
        return 2
# end Parte 2/3
# Parte 3/3 — continuação: toolchain commands, parser definition, main()

def cmd_toolchain(args):
    """
    ibuild toolchain [--list] [--verify] [--set-gcc VER] [--set-kernel VER] [--profiles] [--create-profile NAME] [--use-profile NAME] [--rollback] [--jobs N] [--no-sandbox]
    """
    try:
        # listar versões/profiles
        if getattr(args, "list", False):
            data = toolchain_mod.list_versions() if toolchain_mod else {}
            print(color("=== Toolchain status ===", "magenta"))
            _print_json_or_plain(data, False)
            return 0

        if getattr(args, "verify", False):
            ok = toolchain_mod.verify_toolchain() if toolchain_mod else False
            print(color("Toolchain OK" if ok else "Toolchain com erros", "green" if ok else "red"))
            return 0

        if getattr(args, "set_gcc", None):
            toolchain_mod.set_active("gcc", args.set_gcc)
            print(color(f"[OK] GCC ativado: {args.set_gcc}", "green"))
            return 0

        if getattr(args, "set_kernel", None):
            toolchain_mod.set_active("kernel", args.set_kernel)
            print(color(f"[OK] Kernel ativado: {args.set_kernel}", "green"))
            return 0

        if getattr(args, "profiles", False):
            profiles = toolchain_mod.list_profiles() if toolchain_mod else {}
            print(color("=== Perfis ===", "magenta"))
            _print_json_or_plain(profiles, getattr(args, "json", False))
            return 0

        if getattr(args, "create_profile", None):
            toolchain_mod.create_profile(args.create_profile)
            print(color(f"[OK] Profile criado: {args.create_profile}", "green"))
            return 0

        if getattr(args, "use_profile", None):
            toolchain_mod.use_profile(args.use_profile)
            print(color(f"[OK] Profile ativado: {args.use_profile}", "green"))
            return 0

        if getattr(args, "rollback", False):
            snaps = toolchain_mod.list_snapshots()
            if not snaps:
                print(color("Nenhum snapshot encontrado", "yellow"))
                return 1
            toolchain_mod.rollback_snapshot(snaps[-1])
            print(color("[OK] Rollback aplicado (último snapshot)", "green"))
            return 0

        # default: rebuild toolchain
        jobs = getattr(args, "jobs", None)
        sandboxed = not getattr(args, "no_sandbox", False)
        success = toolchain_mod.rebuild_toolchain(jobs=jobs, sandboxed=sandboxed)
        print(color("[OK] Toolchain rebuild concluído" if success else "[ERRO] Toolchain rebuild com falhas", "green" if success else "red"))
        return 0
    except Exception as e:
        logger.exception("Toolchain command falhou")
        print(color(f"[ERRO] Toolchain falhou: {e}", "red"), file=sys.stderr)
        return 2

# -----------------------------------------------------------------------------
# Build argument parser and connect commands
# -----------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="ibuild", description="Ibuild - Gerenciador de pacotes e builds")
    p.add_argument("--verbose", "-v", action="store_true", help="Modo verboso")
    p.add_argument("--json", action="store_true", help="Imprime JSON quando aplicável")
    sub = p.add_subparsers(dest="command")

    # build
    sb = sub.add_parser("build", aliases=["b"], help="Compilar pacote")
    sb.add_argument("pkg")
    sb.add_argument("--category", default=None)
    sb.add_argument("--no-deps", action="store_true")
    sb.add_argument("--include-optional", action="store_true")
    sb.add_argument("--jobs", "-j", type=int, default=None)
    sb.add_argument("--keep-sandbox", action="store_true")
    sb.set_defaults(func=cmd_build)

    # install
    si = sub.add_parser("install", aliases=["i"], help="Instalar pacote")
    si.add_argument("pkg", nargs="?")
    si.add_argument("--artifact", "-a", default=None)
    si.add_argument("--category", default=None)
    si.add_argument("--dest", default=None)
    si.add_argument("--overwrite", action="store_true")
    si.add_argument("--upgrade", action="store_true")
    si.set_defaults(func=cmd_install)

    # remove
    sr = sub.add_parser("remove", aliases=["rm"], help="Remover pacote")
    sr.add_argument("pkg")
    sr.add_argument("--purge", action="store_true")
    sr.set_defaults(func=cmd_remove)

    # list
    sl = sub.add_parser("list", aliases=["ls"], help="Listar pacotes instalados")
    sl.set_defaults(func=cmd_list)

    # search
    ss = sub.add_parser("search", aliases=["s"], help="Buscar pacotes")
    ss.add_argument("pattern")
    ss.set_defaults(func=cmd_search)

    # info
    si2 = sub.add_parser("info", help="Mostrar informações do pacote")
    si2.add_argument("pkg")
    si2.add_argument("--category", default=None)
    si2.set_defaults(func=cmd_info)

    # verify / healthcheck
    sv = sub.add_parser("verify", aliases=["check"], help="Verificar integridade do sistema")
    sv.add_argument("--fix", "-f", action="store_true", help="Tentar corrigir problemas automaticamente")
    sv.set_defaults(func=cmd_verify)

    # runtime
    sr = sub.add_parser("runtime", help="Gerenciar runtimes (Python, Ruby, Java, Node, etc.)")
    sr.add_argument("action", choices=["list", "install", "use", "remove", "validate", "repair", "diagnose"],
                    help="Ação sobre a runtime")
    sr.add_argument("language", help="Linguagem (ex: python, ruby, java, node, go, php, perl)")
    sr.add_argument("version", nargs="?", help="Versão (quando aplicável)")
    sr.add_argument("--user", action="store_true", help="Definir versão apenas para o usuário (não global)")
    sr.add_argument("--detailed", action="store_true", help="Listagem detalhada com status e default")
    sr.set_defaults(func=cmd_runtime)

    # meta-create
    smc = sub.add_parser("meta-create", aliases=["mcreate"], help="Criar novo pacote .meta")
    smc.add_argument("pkg", help="Nome do pacote")
    smc.add_argument("category", help="Categoria do pacote")
    smc.add_argument("--version", "-v", help="Versão inicial", default="1.0.0")
    smc.add_argument("--maintainer", "-m", help="Mantenedor", default=None)
    smc.add_argument("--description", "-d", help="Descrição", default=None)
    smc.set_defaults(func=cmd_meta_create)

    # logs
    slog = sub.add_parser("logs", help="Listar arquivos de log")
    slog.set_defaults(func=cmd_logs)

    # log
    slg = sub.add_parser("log", help="Mostrar conteúdo de um log")
    slg.add_argument("name", help="Nome do log (ex: build, rollback, ...)")
    slg.add_argument("--follow", "-f", action="store_true", help="Seguir (tail -f)")
    slg.set_defaults(func=cmd_log)

    # update
    sup = sub.add_parser("update", aliases=["upd"], help="Verificar atualizações upstream")
    sup.add_argument("--bar", action="store_true", help="Imprimir updates/total para status bars")
    sup.add_argument("--no-notify", action="store_true", help="Não enviar notify-send")
    sup.set_defaults(func=cmd_update)

    # config
    sc = sub.add_parser("config", help="Gerenciar configuração do ibuild")
    sc.add_argument("action", choices=["get","set","list","reset"], help="Ação sobre a configuração")
    sc.add_argument("key", nargs="?", help="Chave da configuração")
    sc.add_argument("value", nargs="?", help="Valor (para set)")
    sc.add_argument("--system", action="store_true", help="Salvar/operar no config global (/etc)")
    sc.set_defaults(func=cmd_config)

    # pipeline
    sp = sub.add_parser("pipeline", aliases=["all"], help="Pipeline: fetch → patch → build → install")
    sp.add_argument("pkg")
    sp.add_argument("--url", default=None)
    sp.add_argument("--patch", default=None)
    sp.add_argument("--category", default=None)
    sp.add_argument("--dest", default=None)
    sp.add_argument("--upgrade", action="store_true")
    sp.add_argument("--jobs", "-j", type=int, default=None)
    sp.set_defaults(func=cmd_pipeline)

    # toolchain
    stc = sub.add_parser("toolchain", help="Gerenciar toolchain (gcc/kernel/profiles)")
    stc.add_argument("--list", action="store_true", help="Listar versões instaladas / status")
    stc.add_argument("--verify", action="store_true", help="Verificar toolchain funcional")
    stc.add_argument("--set-gcc", metavar="VERSAO", help="Ativar versão específica do GCC")
    stc.add_argument("--set-kernel", metavar="VERSAO", help="Ativar versão específica do Kernel")
    stc.add_argument("--profiles", action="store_true", help="Listar perfis disponíveis")
    stc.add_argument("--create-profile", metavar="NOME", help="Criar novo perfil")
    stc.add_argument("--use-profile", metavar="NOME", help="Ativar perfil existente")
    stc.add_argument("--rollback", action="store_true", help="Rollback ao último snapshot")
    stc.add_argument("--jobs", "-j", type=int, default=None)
    stc.add_argument("--no-sandbox", action="store_true")
    stc.set_defaults(func=cmd_toolchain)

    return p

def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    _setup_logging(getattr(args, "verbose", False))

    try:
        rc = args.func(args)
        if isinstance(rc, int):
            sys.exit(rc)
        # if handler returned something else, exit 0
        sys.exit(0)
    except Exception as e:
        logger.exception("Erro ao executar comando")
        print(color(f"[ERRO] {e}", "red"), file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
