#!/usr/bin/env bash
# dependency.sh - sistema de resolução de dependências para ibuild
#
# Funcionalidades:
#  - monta grafo de dependências (run / build)
#  - ordenação topológica (resolução)
#  - detecção de ciclos e mensagem com ciclo identificado
#  - modos: resolve, builddep, rundep, graph, topo, reverse
#
# Uso (via dispatcher ibuild):
#   ibuild dependency resolve <pkg> [--type run|build]
#   ibuild dependency builddep <pkg> [--install]
#   ibuild dependency rundep <pkg> -- <comando...>
#   ibuild dependency graph
#
# Requisitos: bash >= 4 (associative arrays)

set -euo pipefail

# Ajuste de caminho (garante que ache ibuild e modules)
IBUILD_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MODULES_DIR="$IBUILD_DIR/modules"

source "$MODULES_DIR/utils.sh"   # fornece pkg_meta_file, pkg_installed, log

PKG_DIR="/var/lib/ibuild/packages"

# ============================================================
# Leitura de meta (retorna valores sem executar o arquivo)
# ============================================================
# Extrai o campo de um arquivo .meta (pattern like ^field=...)
# retorna vazio se não existir
_meta_field() {
    local metafile="$1"
    local field="$2"
    if [ ! -f "$metafile" ]; then
        echo ""
        return
    fi
    # Usa shell parsing mínimo: pega valor após = e remove aspas se houver
    awk -F'=' -v f="$field" '
      $1==f {
         sub(/^[ \t]*/, "", $2);
         sub(/[ \t]*$/, "", $2);
         # remove leading/trailing quotes if present
         if ($2 ~ /^".*"$/ || $2 ~ /^\047.*\047$/) {
            print substr($2,2,length($2)-2);
         } else {
            print $2;
         }
      }
    ' "$metafile"
}

# Normaliza lista "a,b,c" -> array (separador vírgula ou espaço)
_split_deps_to_array() {
    local raw="$1"
    local -n _out=$2
    _out=()
    [ -z "$raw" ] && return
    # Replace commas by spaces, collapse spaces
    raw="$(echo "$raw" | tr ',' ' ' | tr -s ' ')"
    for token in $raw; do
        token="${token//[[:space:]]/}"
        [ -n "$token" ] && _out+=("$token")
    done
}

# ============================================================
# Construção do grafo
# Representação:
#   nodes -> associative array (node -> 1)
#   edges -> adjacency list: edges["pkg"]="dep1 dep2 ..."
# ============================================================
declare -A _nodes
declare -A _edges

_graph_reset() {
    _nodes=()
    _edges=()
}

# Adiciona nó (idempotente)
_graph_add_node() {
    local n="$1"
    _nodes["$n"]=1
    # ensure edges entry exists
    if [ -z "${_edges[$n]+x}" ]; then
        _edges["$n"]=""
    fi
}

# Adiciona aresta pkg -> dep (pkg depende de dep)
_graph_add_edge() {
    local pkg="$1"
    local dep="$2"
    _graph_add_node "$pkg"
    _graph_add_node "$dep"
    # append dep if not present
    local cur="${_edges[$pkg]}"
    for e in $cur; do
        [ "$e" = "$dep" ] && return
    done
    _edges["$pkg"]="$cur $dep"
}

# Constrói o grafo a partir de todos os .meta (tipo: run | build)
# build: olhar campo "build_depends" ou "build-depends" (aceita ambos)
# run: olhar campo "depends"
_graph_build_from_meta() {
    local type="$1"  # run|build
    _graph_reset

    for meta in "$PKG_DIR"/*.meta; do
        [ -f "$meta" ] || continue
        local name
        name="$(_meta_field "$meta" "name")"
        # if no explicit name, fallback to basename without .meta
        if [ -z "$name" ]; then
            name="$(basename "$meta" .meta)"
        fi
        _graph_add_node "$name"

        local raw_deps=""
        if [ "$type" = "run" ]; then
            raw_deps="$(_meta_field "$meta" "depends")"
        else
            raw_deps="$(_meta_field "$meta" "build_depends")"
            [ -z "$raw_deps" ] && raw_deps="$(_meta_field "$meta" "build-depends")"
        fi

        local arr=()
        _split_deps_to_array "$raw_deps" arr
        for d in "${arr[@]}"; do
            _graph_add_edge "$name" "$d"
        done
    done
}

# ============================================================
# Topological sort (DFS)
# Returns list of nodes in topological order (pkg dependencies first)
# Detects cycles and prints them to stderr and returns 1
# ============================================================
_topo_order() {
    local -a order=()
    declare -A state  # 0=unseen, 1=visiting, 2=done
    declare -a stack_trace=()

    _dfs_visit() {
        local node="$1"
        state["$node"]=1
        stack_trace+=("$node")
        # iterate neighbours
        local neighs="${_edges[$node]}"
        for n in $neighs; do
            if [ -z "${state[$n]+x}" ] || [ "${state[$n]}" -eq 0 ]; then
                _dfs_visit "$n" || return 1
            elif [ "${state[$n]}" -eq 1 ]; then
                # cycle detected, construct readable cycle
                local cycle=()
                local i
                for ((i=${#stack_trace[@]}-1; i>=0; i--)); do
                    cycle=("${stack_trace[$i]}" "${cycle[@]}")
                    if [ "${stack_trace[$i]}" = "$n" ]; then
                        break
                    fi
                done
                echo "CICLO DETECTADO: ${cycle[*]} -> $n" >&2
                return 1
            fi
        done
        state["$node"]=2
        order+=("$node")
        # pop stack_trace
        unset 'stack_trace[${#stack_trace[@]}-1]'
    }

    # visit all nodes
    local node
    for node in "${!_nodes[@]}"; do
        if [ -z "${state[$node]+x}" ] || [ "${state[$node]}" -eq 0 ]; then
            _dfs_visit "$node" || return 1
        fi
    done

    # print order (dependencies first). We want topological order where if A depends on B, B appears before A.
    printf "%s\n" "${order[@]}"
    return 0
}

# ============================================================
# Resolve dependencies for a specific package:
# computes subgraph reachable from pkg, then topo-sorts that subgraph
# ============================================================
_resolve_for_pkg() {
    local pkg="$1"
    local type="$2"  # run|build

    # rebuild full graph of requested type
    _graph_build_from_meta "$type"

    # if pkg not known, add node (maybe meta file missing)
    _graph_add_node "$pkg"

    # compute reachable nodes from pkg (DFS)
    declare -A visited=()
    declare -a stack=("$pkg")
    visited["$pkg"]=1
    while [ ${#stack[@]} -gt 0 ]; do
        local cur="${stack[-1]}"
        stack=("${stack[@]::${#stack[@]}-1}")
        local neighs="${_edges[$cur]}"
        for n in $neighs; do
            if [ -z "${visited[$n]+x}" ]; then
                visited["$n"]=1
                stack+=("$n")
            fi
        done
    done

    # build subgraph containing only visited nodes
    declare -A saved_nodes
    declare -A saved_edges
    for n in "${!visited[@]}"; do
        saved_nodes["$n"]=1
        saved_edges["$n"]="${_edges[$n]}"
    done

    # replace global structures temporarily
    local -n _global_nodes_ref=_nodes
    local -n _global_edges_ref=_edges
    local -A tmp_nodes tmp_edges
    tmp_nodes=()
    tmp_edges=()
    # set _nodes/_edges to saved ones temporarily
    _nodes=()
    _edges=()
    for n in "${!saved_nodes[@]}"; do
        _nodes["$n"]=1
        _edges["$n"]="${saved_edges[$n]}"
    done

    # now topo sort; result printed to stdout
    if ! _topo_order; then
        # restore original nodes/edges from saved (we have them in tmp? but safer to rebuild original)
        _graph_build_from_meta "$type"
        return 1
    fi

    # restore original graph
    _graph_build_from_meta "$type"
    return 0
}

# ============================================================
# Public API functions
# ============================================================
_dep_resolve_print() {
    local pkg="$1"
    local type="$2"
    if ! _resole_out="$(mktemp)"; then :; fi
    if ! _res=$( _resolve_for_pkg "$pkg" "$type" 2> >(cat >&2) ); then
        # _resolve_for_pkg already wrote cycle info to stderr
        return 1
    fi
}

# Resolve and print ordered list
dependency_resolve_print() {
    local pkg="$1"
    local type="${2:-run}"
    if ! _resolve_for_pkg "$pkg" "$type"; then
        return 1
    fi
    # _topo_order already printed nodes (one per line) — but it printed all nodes in subgraph.
    # We want to filter to only nodes reachable from pkg (already done).
    # However _topo_order printed in order dependencies-first; but the caller wants often exclude the target itself?
    # We'll print as-is (deps first, last line will be pkg).
}

# Install build dependencies (calls ibuild install unless --dry-run)
dependency_builddep_install() {
    local pkg="$1"
    local dry_run="${2:-0}"
    # resolve build graph
    if ! out="$( _resolve_for_pkg "$pkg" "build" )"; then
        log "Falha ao resolver build-deps para $pkg"
        return 1
    fi
    # capture into array
    mapfile -t order < <( _resolve_for_pkg "$pkg" "build" )
    # order has dependencies first, pkg last; we need only deps excluding pkg itself
    local to_install=()
    for node in "${order[@]}"; do
        [ "$node" = "$pkg" ] && continue
        to_install+=("$node")
    done

    if [ ${#to_install[@]} -eq 0 ]; then
        log "Nenhuma build-dep encontrada para $pkg"
        return 0
    fi

    log "Build-deps na ordem (instalar nesta ordem): ${to_install[*]}"

    for d in "${to_install[@]}"; do
        if pkg_installed "$d"; then
            log "Já instalado: $d"
            continue
        fi
        if [ "$dry_run" -eq 1 ]; then
            log "[DRY-RUN] ibuild install $d"
        else
            log "Instalando build-dep: $d"
            # chama o dispatcher ibuild para instalar
            if ! "$IBUILD_DIR/ibuild" install "$d"; then
                log "Erro ao instalar $d"
                return 1
            fi
        fi
    done
    return 0
}

# Ensure runtime deps installed, then run a command (or open shell) optionally in sandbox
dependency_rundep_and_exec() {
    local pkg="$1"
    shift || true
    local cmd=("$@")  # command to run
    # resolve runtime deps
    mapfile -t order < <( _resolve_for_pkg "$pkg" "run" )
    # keep only deps excluding pkg itself
    local to_install=()
    for node in "${order[@]}"; do
        [ "$node" = "$pkg" ] && continue
        to_install+=("$node")
    done

    log "Runtime deps a garantir: ${to_install[*]}"

    for d in "${to_install[@]}"; do
        if pkg_installed "$d"; then
            log "Já instalado: $d"
            continue
        fi
        log "Instalando runtime-dep: $d"
        if ! "$IBUILD_DIR/ibuild" install "$d"; then
            log "Erro ao instalar runtime-dep $d"; return 1
        fi
    done

    # tudo pronto — executa comando
    if [ ${#cmd[@]} -eq 0 ]; then
        # abre shell
        /bin/bash --login
    else
        "${cmd[@]}"
    fi
}

# Print graph (edges)
dependency_graph_print() {
    _graph_build_from_meta "run"
    echo "Grafo runtime (pkg -> deps):"
    for k in "${!_edges[@]}"; do
        echo "$k -> ${_edges[$k]}"
    done
}

# Print graph for build deps
dependency_graph_print_build() {
    _graph_build_from_meta "build"
    echo "Grafo build (pkg -> build-deps):"
    for k in "${!_edges[@]}"; do
        echo "$k -> ${_edges[$k]}"
    done
}

# ============================================================
# CLI: dependency_main
# ============================================================
dependency_usage() {
    cat <<EOF
ibuild dependency <cmd> ...

Comandos:
  resolve <pkg> [--type run|build]    - imprime ordem topológica (deps primeiro)
  builddep <pkg> [--install] [--dry]  - resolve build-deps; instala (se --install)
  rundep <pkg> -- <command...>        - garante runtime deps instaladas e executa comando
  graph [run|build]                   - imprime grafo
  help
EOF
}

dependency_main() {
    local cmd="${1:-}"
    shift || true
    case "$cmd" in
        resolve)
            local pkg="${1:-}"
            local type="run"
            shift || true
            while [ $# -gt 0 ]; do
                case "$1" in
                    --type) type="$2"; shift 2 ;;
                    *) pkg="$1"; shift ;;
                esac
            done
            [ -n "$pkg" ] || { echo "Uso: dependency resolve <pkg> [--type run|build]"; return 1; }
            if ! _resolve_for_pkg "$pkg" "$type"; then
                return 1
            fi
            ;;
        builddep)
            local pkg="${1:-}"; shift || true
            local do_install=0; local dry=0
            while [ $# -gt 0 ]; do
                case "$1" in
                    --install) do_install=1; shift ;;
                    --dry) dry=1; shift ;;
                    *) [ -z "$pkg" ] && pkg="$1"; shift || true ;;
                esac
            done
            [ -n "$pkg" ] || { echo "Uso: dependency builddep <pkg> [--install] [--dry]"; return 1; }
            if [ "$do_install" -eq 1 ]; then
                dependency_builddep_install "$pkg" "$dry"
            else
                # just print resolved order
                _resolve_for_pkg "$pkg" "build"
            fi
            ;;
        rundep)
            local pkg="${1:-}"; shift || true
            # expect '--' followed by command
            if [ "$#" -gt 0 ] && [ "$1" = "--" ]; then shift; fi
            if [ -z "$pkg" ]; then
                echo "Uso: dependency rundep <pkg> -- <command...>"
                return 1
            fi
            dependency_rundep_and_exec "$pkg" "$@"
            ;;
        graph)
            local type="${1:-run}"
            if [ "$type" = "build" ]; then
                dependency_graph_print_build
            else
                dependency_graph_print
            fi
            ;;
        help|""|-h|--help)
            dependency_usage
            ;;
        *)
            echo "Comando desconhecido: $cmd"; dependency_usage; return 1 ;;
    esac
}
