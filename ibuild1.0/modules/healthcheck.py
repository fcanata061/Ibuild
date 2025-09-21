#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
healthcheck.py - Auditor e reparador de integridade do ibuild (evoluído)

Funções:
- Diagnostica pacotes quebrados, libs ausentes, links e permissões.
- Gera relatório detalhado (JSON + TXT).
- Notifica via notify-send.
- Pode corrigir problemas simples com --fix.
"""

import os
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

PKG_DB = "/var/lib/ibuild/pkgdb.json"
OUTPUT_JSON = "/var/log/ibuild/healthcheck.json"
OUTPUT_TXT = "/var/log/ibuild/healthcheck.txt"

# ---------- utilidades ----------

def load_pkgdb():
    if not os.path.isfile(PKG_DB):
        return []
    with open(PKG_DB, "r", encoding="utf-8") as f:
        return json.load(f)

def run_cmd(cmd):
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False

def fix_permissions(path, issue):
    if "não-executável" in issue:
        try:
            os.chmod(path, 0o755)
            return True
        except Exception:
            return False
    if "não-legível" in issue:
        try:
            os.chmod(path, 0o644)
            return True
        except Exception:
            return False
    return False

def fix_symlink(path):
    try:
        os.unlink(path)
        return True
    except Exception:
        return False

def repair_package(pkg_name):
    return run_cmd(["ibuild", "repair", pkg_name])

# ---------- verificadores ----------

def check_manifest(pkg):
    return [f for f in pkg.get("files", []) if not os.path.exists(f)]

def check_ldd(pkg):
    missing_libs = []
    for f in pkg.get("files", []):
        if os.path.isfile(f) and os.access(f, os.X_OK):
            try:
                out = subprocess.check_output(["ldd", f], text=True, stderr=subprocess.DEVNULL)
                for line in out.splitlines():
                    if "not found" in line:
                        lib = line.strip().split()[0]
                        missing_libs.append((f, lib))
            except Exception:
                pass
    return missing_libs

def check_symlinks(root="/usr"):
    broken = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                target = os.readlink(path)
                abs_target = os.path.join(os.path.dirname(path), target)
                if not os.path.exists(abs_target):
                    broken.append((path, target))
    return broken

def check_permissions(pkg):
    bad = []
    for f in pkg.get("files", []):
        if os.path.isfile(f):
            if f.startswith("/usr/bin") and not os.access(f, os.X_OK):
                bad.append((f, "não-executável"))
            if f.startswith("/usr/lib") and not os.access(f, os.R_OK):
                bad.append((f, "não-legível"))
    return bad

# ---------- análise ----------

def analyze_package(pkg, autofix=False):
    pkg_report = {"name": pkg["name"], "issues": [], "fixed": []}

    missing = check_manifest(pkg)
    if missing:
        issue = {
            "type": "missing_files",
            "severity": "HIGH",
            "details": missing,
            "suggestion": f"ibuild repair {pkg['name']}"
        }
        if autofix:
            if repair_package(pkg["name"]):
                pkg_report["fixed"].append("repair executed")
        pkg_report["issues"].append(issue)

    libs = check_ldd(pkg)
    if libs:
        issue = {
            "type": "missing_libs",
            "severity": "CRITICAL",
            "details": libs,
            "suggestion": f"ibuild revdep {pkg['name']}"
        }
        pkg_report["issues"].append(issue)

    perms = check_permissions(pkg)
    if perms:
        issue = {
            "type": "bad_perms",
            "severity": "LOW",
            "details": perms,
            "suggestion": "chmod +x ou ajustar permissões"
        }
        if autofix:
            for f, why in perms:
                if fix_permissions(f, why):
                    pkg_report["fixed"].append(f"fixed perms {f}")
        pkg_report["issues"].append(issue)

    return pkg_report if pkg_report["issues"] else None

# ---------- relatório ----------

def healthcheck(autofix=False):
    pkgdb = load_pkgdb()
    results = {"packages": [], "broken_symlinks": [], "summary": {}}

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(analyze_package, pkg, autofix): pkg for pkg in pkgdb}
        for fut in as_completed(futures):
            rep = fut.result()
            if rep:
                results["packages"].append(rep)

    broken = check_symlinks("/usr")
    if broken:
        for path, target in broken:
            entry = {"path": path, "target": target, "suggestion": "ibuild fix-links"}
            if autofix and fix_symlink(path):
                entry["fixed"] = True
            results["broken_symlinks"].append(entry)

    total = len(pkgdb)
    affected = len(results["packages"])
    results["summary"] = {
        "total_packages": total,
        "affected_packages": affected,
        "broken_symlinks": len(broken),
    }

    return results

def generate_report(report):
    os.makedirs(os.path.dirname(OUTPUT_JSON), exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    with open(OUTPUT_TXT, "w", encoding="utf-8") as f:
        f.write("=== Ibuild Healthcheck Report ===\n")
        f.write(f"Total pacotes: {report['summary']['total_packages']}\n")
        f.write(f"Pacotes afetados: {report['summary']['affected_packages']}\n")
        f.write(f"Links quebrados: {report['summary']['broken_symlinks']}\n\n")
        for pkg in report["packages"]:
            f.write(f"[{pkg['name']}]\n")
            for issue in pkg["issues"]:
                f.write(f" - {issue['type']} ({issue['severity']}): {issue['details']}\n")
                f.write(f"   Sugestão: {issue['suggestion']}\n")
            if pkg.get("fixed"):
                f.write(f"   Corrigido: {pkg['fixed']}\n")
        if report["broken_symlinks"]:
            f.write("\nLinks quebrados:\n")
            for l in report["broken_symlinks"]:
                f.write(f"  {l['path']} -> {l['target']}\n")
                if l.get("fixed"):
                    f.write("    Corrigido: link removido\n")
    print(f"[INFO] Relatório gerado em {OUTPUT_JSON} e {OUTPUT_TXT}")

def notify(report):
    affected = report["summary"]["affected_packages"]
    broken_links = report["summary"]["broken_symlinks"]
    total = report["summary"]["total_packages"]
    if affected or broken_links:
        msg = f"{affected} pacotes e {broken_links} links com problemas (de {total})"
    else:
        msg = f"Sistema íntegro ({total} pacotes verificados)"
    subprocess.run(["notify-send", "Ibuild Healthcheck", msg])

def main():
    autofix = "--fix" in sys.argv
    report = healthcheck(autofix=autofix)
    generate_report(report)
    notify(report)

if __name__ == "__main__":
    main()
