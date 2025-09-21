#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update.py - Notificador de novas versões para pacotes do ibuild (super evoluído)

- Varre todos os .meta no repositório (/usr/ibuild).
- Detecta versões mais recentes (http/https/ftp/git/GitHub/GitLab).
- Gera relatório JSON + TXT com resumo.
- Notifica via notify-send com total e lista de pacotes.
"""

import os
import json
import subprocess
import requests
import re
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

META_DIR = "/usr/ibuild"
OUTPUT_JSON = "/var/lib/ibuild/updates.json"
OUTPUT_TXT = "/var/lib/ibuild/updates.txt"

# ---------- utilidades ----------

def scan_meta_dir(meta_dir=META_DIR):
    metas = []
    for root, _, files in os.walk(meta_dir):
        for f in files:
            if f.endswith(".meta") or f.endswith(".json"):
                path = os.path.join(root, f)
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        meta = json.load(fh)
                        metas.append(meta)
                except Exception as e:
                    print(f"[WARN] Não consegui ler {path}: {e}")
    return metas

def normalize_version(v: str) -> list[int]:
    try:
        return [int(x) for x in re.findall(r"\d+", v)]
    except Exception:
        return [0]

def pick_latest(versions: list[str]) -> str | None:
    if not versions:
        return None
    return sorted(set(versions), key=normalize_version)[-1]

# ---------- verificadores ----------

def check_http_version(url):
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            matches = re.findall(r"\d+\.\d+(\.\d+)?", r.text)
            return pick_latest(matches)
    except Exception as e:
        print(f"[WARN] Falha HTTP {url}: {e}")
    return None

def check_git_version(url):
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", url],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            tags = []
            for line in result.stdout.splitlines():
                if "refs/tags" in line:
                    tag = line.split("refs/tags/")[-1].strip("^{}")
                    if re.match(r"v?\d+(\.\d+)+", tag):
                        tags.append(tag.lstrip("v"))
            return pick_latest(tags)
    except Exception as e:
        print(f"[WARN] Falha GIT {url}: {e}")
    return None

def check_github_version(url):
    try:
        parts = url.split("github.com/")[-1].split("/")
        if len(parts) < 2:
            return None
        owner, repo = parts[0], parts[1].replace(".git", "")
        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
        r = requests.get(api_url, timeout=10, headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            data = r.json()
            tag = data.get("tag_name")
            if tag:
                return tag.lstrip("v")
    except Exception as e:
        print(f"[WARN] Falha GitHub {url}: {e}")
    return None

def check_gitlab_version(url):
    try:
        parts = url.split("gitlab.com/")[-1].split("/")
        if len(parts) < 2:
            return None
        owner, repo = parts[0], parts[1].replace(".git", "")
        api_url = f"https://gitlab.com/api/v4/projects/{owner}%2F{repo}/releases"
        r = requests.get(api_url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, list):
                tag = data[0].get("tag_name")
                if tag:
                    return tag.lstrip("v")
    except Exception as e:
        print(f"[WARN] Falha GitLab {url}: {e}")
    return None

# ---------- orquestração ----------

def check_latest_version(meta):
    src = meta.get("source")
    if not src:
        return None
    if isinstance(src, dict):
        url = src.get("url")
    elif isinstance(src, list) and src:
        url = src[0].get("url")
    else:
        return None

    if not url:
        return None

    if "github.com" in url:
        return check_github_version(url)
    if "gitlab.com" in url:
        return check_gitlab_version(url)

    scheme = urlparse(url).scheme
    if scheme in ("http", "https", "ftp"):
        return check_http_version(url)
    elif url.endswith(".git"):
        return check_git_version(url)
    return None

def generate_report(results):
    total = len(results)
    updates = [r for r in results if r.get("latest") and r["latest"] != r["current"]]
    up_to_date = total - len(updates)

    summary = {
        "total": total,
        "updates": len(updates),
        "up_to_date": up_to_date
    }

    # JSON
    os.makedirs(os.path.dirname(OUTPUT_JSON), exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "packages": results}, f, indent=2, ensure_ascii=False)

    # TXT
    with open(OUTPUT_TXT, "w", encoding="utf-8") as f:
        f.write("=== Ibuild Update Report ===\n")
        f.write(f"Total pacotes: {total}\n")
        f.write(f"Atualizados: {up_to_date}\n")
        f.write(f"Novas versões: {len(updates)}\n\n")
        for r in results:
            f.write(f"{r['name']}: {r['current']} -> {r['latest'] or 'desconhecida'}\n")

    print(f"[INFO] Relatório gerado em {OUTPUT_JSON} e {OUTPUT_TXT}")
    return summary, updates

def notify_updates(summary, updates):
    if updates:
        names = [r["name"] for r in updates]
        preview = ", ".join(names[:5])
        more = f" e +{len(names)-5}" if len(names) > 5 else ""
        msg = f"{summary['updates']}/{summary['total']} pacotes desatualizados: {preview}{more}"
        subprocess.run(["notify-send", "Ibuild Update", msg])
    else:
        msg = f"Todos atualizados ({summary['total']} pacotes)"
        subprocess.run(["notify-send", "Ibuild Update", msg])

def main():
    metas = scan_meta_dir()
    results = []

    with ThreadPoolExecutor(max_workers=8) as ex:
        future_map = {ex.submit(check_latest_version, m): m for m in metas}
        for fut in as_completed(future_map):
            meta = future_map[fut]
            latest = fut.result()
            results.append({
                "name": meta.get("name"),
                "current": meta.get("version"),
                "latest": latest,
                "url": meta.get("source", {}).get("url") if isinstance(meta.get("source"), dict) else None
            })
            print(f"{meta.get('name')}: {meta.get('version')} -> {latest or 'desconhecida'}")

    summary, updates = generate_report(results)
    notify_updates(summary, updates)

if __name__ == "__main__":
    main()
