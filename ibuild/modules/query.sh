#!/usr/bin/env bash
# query.sh - inspeção e listagem de pacotes no ibuild

set -euo pipefail

source "$(dirname "$0")/utils.sh"
source "$(dirname "$0")/dependency.sh"

PKG_DIR="/var/lib/ibuild/packages"

# ============================================================
# Funções de query
# ============================================================

query_list() {
    for f in "$PKG_DIR"/*.meta; do
        [ -e "$f" ] || continue
        local pkg; pkg="$(basename "$f" .meta)"
        echo "$pkg"
    done | sort
}

query_info() {
    local pkg="$1"
    local meta="$PKG_DIR/$pkg.meta"
    [ -f "$meta" ] || { log "Pacote '$pkg' não encontrado"; exit 1; }
    source "$meta"
    echo "Name:     $name"
    echo "Version:  $version"
    [ -n "${builddeps:-}" ] && echo "BuildDeps: $builddeps"
    [ -n "${rundeps:-}" ] && echo "RunDeps:   $rundeps"
    [ -n "${depends:-}" ] && echo "Depends:   $depends"
}

query_files() {
    local pkg="$1"
    local meta="$PKG_DIR/$pkg.meta"
    [ -f "$meta" ] || { log "Pacote '$pkg' não encontrado"; exit 1; }
    source "$meta"
    echo "$files" | tr ' ' '\n' | sort
}

query_depends() {
    local pkg="$1"
    echo "Dependências diretas de $pkg:"
    echo "Build: $(get_builddeps "$pkg")"
    echo "Run:   $(get_rundeps "$pkg")"
    echo "Other: $(get_depends "$pkg")"
}

query_rdepends() {
    local pkg="$1"
    echo "Pacotes que dependem de $pkg:"
    reverse_deps "$pkg" | sort || true
}

query_tree() {
    local pkg="$1"
    echo "Árvore de dependências de $pkg:"
    resolve_deps "$pkg" all | sed 's/^/  /'
}

query_orphans() {
    echo "Pacotes órfãos:"
    for f in "$PKG_DIR"/*.meta; do
        [ -e "$f" ] || continue
        local pkg; pkg="$(basename "$f" .meta)"
        local revs; revs="$(reverse_deps "$pkg" || true)"
        if [ -z "$revs" ]; then
            echo "$pkg"
        fi
    done | sort
}

query_search() {
    local regex="$1"
    query_list | grep -E "$regex" || true
}

# ============================================================
# Entry point
# ============================================================
query_main() {
    local cmd="${1:-}"
    shift || true

    case "$cmd" in
        list)      query_list ;;
        info)      query_info "$@" ;;
        files)     query_files "$@" ;;
        depends)   query_depends "$@" ;;
        rdepends)  query_rdepends "$@" ;;
        tree)      query_tree "$@" ;;
        orphans)   query_orphans ;;
        search)    query_search "$@" ;;
        *)
            cat <<EOF
Uso: ibuild query <comando> [args]

Comandos disponíveis:
  list                 - lista pacotes instalados
  info <pkg>           - mostra informações do pacote
  files <pkg>          - lista arquivos instalados pelo pacote
  depends <pkg>        - mostra dependências diretas
  rdepends <pkg>       - mostra quem depende do pacote
  tree <pkg>           - mostra árvore de dependências
  orphans              - lista pacotes órfãos
  search <regex>       - procura pacotes por nome
EOF
            ;;
    esac
}
