#!/usr/bin/env bash
# hooks.sh - sistema avançado de hooks do ibuild

source "$(dirname "$0")/utils.sh"

GLOBAL_HOOKS_DIR="/var/lib/ibuild/hooks"

mkdir -p "$GLOBAL_HOOKS_DIR"

# ============================================================
# Execução de um hook genérico
# ============================================================
_execute_hook() {
    local hook_name="$1"   # ex: hook_pre_configure
    local pkg="$2"
    local critical_var="${hook_name}_critical"
    local logf="$LOG_DIR/${pkg}.hooks.log"

    # 1. hook global (script em /var/lib/ibuild/hooks)
    local script_global="$GLOBAL_HOOKS_DIR/${hook_name}.sh"
    if [ -x "$script_global" ]; then
        log "Hook global: $hook_name ($script_global)"
        if ! "$script_global" "$pkg" >>"$logf" 2>&1; then
            log "ERRO no hook global $hook_name"
            [ "${!critical_var:-false}" = true ] && return 1
        fi
    fi

    # 2. hook definido no .meta (variável shell)
    if declare -p "$hook_name" &>/dev/null; then
        log "Hook meta: $hook_name"
        if ! eval "${!hook_name}" >>"$logf" 2>&1; then
            log "ERRO no hook meta $hook_name"
            [ "${!critical_var:-false}" = true ] && return 1
        fi
    fi

    # 3. hook local (diretório hooks/ do pacote fonte)
    local srcdir="/tmp/ibuild-src/$pkg/hooks"
    local script_local="$srcdir/${hook_name}.sh"
    if [ -x "$script_local" ]; then
        log "Hook local: $hook_name ($script_local)"
        if ! "$script_local" "$pkg" >>"$logf" 2>&1; then
            log "ERRO no hook local $hook_name"
            [ "${!critical_var:-false}" = true ] && return 1
        fi
    fi
}

# ============================================================
# API pública
# ============================================================
hooks_run() {
    local phase="$1"   # configure, install, remove
    local timing="$2"  # pre, post
    local pkg="${3:-unknown}"

    case "$phase" in
        configure|install|remove) ;;
        *) log "Fase inválida: $phase"; return 1 ;;
    esac

    case "$timing" in
        pre|post) ;;
        *) log "Timing inválido: $timing"; return 1 ;;
    esac

    local hook_var="hook_${timing}_${phase}"
    _execute_hook "$hook_var" "$pkg"
}

hooks_list() {
    local pkg="$1"

    echo "Hooks carregados para $pkg:"
    for h in hook_pre_configure hook_post_configure \
             hook_pre_install hook_post_install \
             hook_pre_remove hook_post_remove; do
        if declare -p "$h" &>/dev/null; then
            echo "  $h -> ${!h}"
        fi
    done

    echo "Hooks globais em $GLOBAL_HOOKS_DIR:"
    ls -1 "$GLOBAL_HOOKS_DIR" 2>/dev/null | grep '\.sh$' || true

    local srcdir="/tmp/ibuild-src/$pkg/hooks"
    if [ -d "$srcdir" ]; then
        echo "Hooks locais em $srcdir:"
        ls -1 "$srcdir" | grep '\.sh$' || true
    fi
}

hooks_run_manual() {
    local pkg="$1"
    local phase="$2"
    local timing="$3"
    hooks_run "$phase" "$timing" "$pkg"
}
