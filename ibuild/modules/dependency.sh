#!/usr/bin/env bash
# dependency.sh - resolução de dependências para ibuild
# Suporte: builddeps, rundeps, ordenação topológica e reversa

set -euo pipefail
source "$(dirname "$0")/utils.sh"

PKG_DIR="/var/lib/ibuild/packages"

# ============================================================
# Carrega metadados de um pacote
# ============================================================
_load_meta_var() {
    local pkg="$1"
    local var="$2"
    local meta="$PKG_DIR/$pkg.meta"
    [ -f "$meta" ] || return 0
    source "$meta"
    eval "echo \${$var:-}"
}

get_builddeps() { _load_meta_var "$1" builddeps; }
get_rundeps()   { _load_meta_var "$1" rundeps; }
get_depends()   { _load_meta_var "$1" depends; }

# ============================================================
# Constrói grafo de dependências (direto)
# ============================================================
build_dep_graph() {
    local pkg="$1"
    local mode="${2:-all}" # all | build | run
    declare -A graph=()

    _dfs() {
        local node="$1"
        local deps=""

        case "$mode" in
            all) deps="$(get_builddeps "$node") $(get_rundeps "$node") $(get_depends "$node")" ;;
            build) deps="$(get_builddeps "$node")" ;;
            run) deps="$(get_rundeps "$node") $(get_depends "$node")" ;;
        esac

        for dep in $deps; do
            graph["$node"]+="$dep "
            if [ -z "${graph[$dep]+x}" ]; then
                _dfs "$dep"
            fi
        done
    }

    _dfs "$pkg"

    # printa arestas
    for k in "${!graph[@]}"; do
        for v in ${graph[$k]}; do
            echo "$k -> $v"
        done
    done
}

# ============================================================
# Ordenação topológica
# ============================================================
toposort() {
    declare -A indegree graph
    local edges=()

    while read -r from arrow to; do
        graph["$from"]+="$to "
        ((indegree["$to"]++))
        : $((indegree["$from"]+=0))
    done

    local queue=()
    for n in "${!indegree[@]}"; do
        if [ "${indegree[$n]}" -eq 0 ]; then
            queue+=("$n")
        fi
    done

    local order=()
    while [ "${#queue[@]}" -gt 0 ]; do
        local n="${queue[0]}"
        queue=("${queue[@]:1}")
        order+=("$n")
        for m in ${graph[$n]:-}; do
            ((indegree["$m"]--))
            if [ "${indegree[$m]}" -eq 0 ]; then
                queue+=("$m")
            fi
        done
    done

    if [ "${#order[@]}" -ne "${#indegree[@]}" ]; then
        log "Erro: ciclo detectado em dependências"
        return 1
    fi

    printf "%s\n" "${order[@]}"
}

# ============================================================
# Resolução completa de dependências
# ============================================================
resolve_deps() {
    local pkg="$1"
    local mode="${2:-all}" # all | build | run
    local edges
    edges="$(build_dep_graph "$pkg" "$mode")"
    echo "$edges" | toposort
}

# ============================================================
# Resolução reversa (quem depende de quem)
# ============================================================
reverse_deps() {
    local target="$1"
    for f in "$PKG_DIR"/*.meta; do
        [ -e "$f" ] || continue
        local dep_pkg
        dep_pkg="$(basename "$f" .meta)"
        local all="$(get_builddeps "$dep_pkg") $(get_rundeps "$dep_pkg") $(get_depends "$dep_pkg")"
        for d in $all; do
            if [ "$d" = "$target" ]; then
                echo "$dep_pkg"
            fi
        done
    done
}

# ============================================================
# Helper para integração
# ============================================================
deps_install_order() {
    local pkg="$1"
    resolve_deps "$pkg" all
}

deps_remove_order() {
    local pkg="$1"
    local order
    order="$(resolve_deps "$pkg" all | tac)"
    echo "$order"
}
