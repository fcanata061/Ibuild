#!/usr/bin/env bash
# ibuild/modules/remove.sh
# Evoluído: suporte a hooks inline no .meta + dry-run + checagem de deps

set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$MODULE_DIR/utils.sh"
source "$MODULE_DIR/hooks.sh"
source "$MODULE_DIR/dependency.sh"

PKG_DIR="${PKG_DIR:-/var/lib/ibuild/packages}"
BIN_DIR="${BIN_DIR:-/var/cache/ibuild/packages}"
LOG_DIR="${LOG_DIR:-/var/log/ibuild}"
REMOVE_LOG="$LOG_DIR/remove.log"

ensure_dir "$LOG_DIR"

DRY_RUN=false

# ============================================================
# Helpers
# ============================================================

_meta_get() {
    local meta="$1" key="$2"
    grep -E "^${key}=" "$meta" | cut -d= -f2- || true
}

_hook_exec() {
    local meta="$1" hook="$2" default_cmd="$3"

    local cmd
    cmd="$(_meta_get "$meta" "hook_${hook}")"

    if [ -n "$cmd" ]; then
        log "[hook:$hook] executando do meta"
        eval "$cmd"
    elif [ -n "$default_cmd" ]; then
        log "[hook:$hook] executando padrão"
        eval "$default_cmd"
    fi
}

# ============================================================
# Remove pipeline
# ============================================================

remove_main() {
    local pkg="$1"
    local meta="$PKG_DIR/$pkg/$pkg.meta"

    if [ ! -f "$meta" ]; then
        err "Meta file não encontrado para $pkg"
        exit 1
    fi

    eval "$(grep -E '^(name|version|rundep|builddep)=' "$meta" || true)"

    local manifest
    manifest="$PKG_DIR/$pkg/.files"

    if [ ! -f "$manifest" ]; then
        manifest="$(ls "$BIN_DIR"/$pkg-*.files 2>/dev/null | head -n1 || true)"
    fi

    if [ ! -f "$manifest" ]; then
        err "Manifesto de arquivos não encontrado para $pkg"
        exit 1
    fi

    # --- Reverse dependency check ---
    local revdeps
    revdeps="$(dependency_reverse_check "$pkg")"
    if [ -n "$revdeps" ]; then
        warn "Pacotes dependem de $pkg: $revdeps"
        warn "Abortando remoção."
        exit 1
    fi

    # --- pre_remove hook ---
    _hook_exec "$meta" pre_remove "hooks_run_phase remove pre"

    # --- remove hook ---
    _hook_exec "$meta" remove "
        while read -r f; do
            [ -n \"\$f\" ] || continue
            target=\"/\${f#./}\"
            if [ -e \"\$target\" ]; then
                if \$DRY_RUN; then
                    echo \"[dry-run] Removeria: \$target\"
                else
                    rm -rf \"\$target\"
                    echo \"Removido: \$target\"
                fi
            fi
        done < \"$manifest\"
    "

    # --- post_remove hook ---
    _hook_exec "$meta" post_remove "hooks_run_phase remove post"

    # --- limpeza final ---
    if ! \$DRY_RUN; then
        rm -rf "$PKG_DIR/$pkg"
        echo "$(date '+%F %T') - Removed $pkg-$version" >> "$REMOVE_LOG"
        ok "$pkg-$version removido"
    else
        ok "[dry-run] Nenhum arquivo removido"
    fi
}

# ============================================================
# CLI
# ============================================================

usage() {
    echo "Uso: $0 [--dry-run] <pacote>"
    exit 1
}

main() {
    local args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run) DRY_RUN=true ;;
            -h|--help) usage ;;
            *) args+=("$1") ;;
        esac
        shift
    done
    [ ${#args[@]} -eq 0 ] && usage
    remove_main "${args[0]}"
}

main "$@"
