#!/usr/bin/env bash
# remove.sh - remoção de pacotes no ibuild (versão evoluída)

set -euo pipefail

source "$(dirname "$0")/utils.sh"
source "$(dirname "$0")/hooks.sh"
source "$(dirname "$0")/dependency.sh"

PKG_DIR="/var/lib/ibuild/packages"
LOG_ROOT="$LOG_DIR"

# ============================================================
# Remove um pacote individual
# ============================================================
_remove_pkg() {
    local pkg="$1"
    local force="$2"
    local dry_run="$3"

    local meta="$PKG_DIR/$pkg.meta"
    [ -f "$meta" ] || { log "Pacote '$pkg' não está instalado"; return 0; }

    source "$meta"  # name, version, files, essential?

    # Pacotes essenciais só saem com --force
    if [ "${essential:-0}" -eq 1 ] && [ "$force" -eq 0 ]; then
        log "Pacote '$name' é essencial e não pode ser removido (use --force)"
        exit 1
    fi

    log "=== Removendo pacote $name-$version ==="

    local logdir="$LOG_ROOT/$name"
    mkdir -p "$logdir"
    local remove_log="$logdir/remove.log"
    local hooks_log="$logdir/hooks.log"
    local removed_files="$logdir/removed_files.log"
    : >"$remove_log"
    : >"$hooks_log"
    : >"$removed_files"

    if [ "$dry_run" -eq 1 ]; then
        log "[DRY-RUN] Pacote $name seria removido"
        echo "$files" | tr ' ' '\n' >>"$removed_files"
        return 0
    fi

    (
        hooks_run remove pre "$name" | tee -a "$hooks_log"

        if [ -n "${files:-}" ]; then
            log "[REMOVE] Apagando arquivos"
            for f in $files; do
                sudo rm -f "/$f" 2>/dev/null || true
                echo "/$f" >>"$removed_files"
            done
        else
            log "[WARN] Nenhuma lista de arquivos encontrada no meta"
        fi

        hooks_run remove post "$name" | tee -a "$hooks_log"
    ) >>"$remove_log" 2>&1

    # Apagar metadados
    rm -f "$meta"

    log "Pacote $name removido com sucesso"
}

# ============================================================
# Ordem reversa de remoção
# ============================================================
remove_pkg() {
    local pkg="$1"
    local force="$2"
    local recursive="$3"
    local dry_run="$4"
    local autoremove="$5"

    log "Resolvendo ordem de remoção para $pkg"

    local pkgs=()

    if [ "$recursive" -eq 1 ]; then
        # inclui dependentes recursivamente
        pkgs="$(deps_remove_order "$pkg")"
    else
        pkgs="$pkg"
    fi

    for p in $pkgs; do
        # checar dependentes antes de remover
        if [ "$force" -eq 0 ] && [ "$recursive" -eq 0 ]; then
            local revs
            revs="$(reverse_deps "$p" || true)"
            if [ -n "$revs" ]; then
                log "Erro: não é seguro remover '$p'. Pacotes dependentes: $revs"
                log "Use --force ou --recursive"
                exit 1
            fi
        fi

        _remove_pkg "$p" "$force" "$dry_run"
    done

    if [ "$autoremove" -eq 1 ]; then
        log "Verificando pacotes órfãos..."
        for f in "$PKG_DIR"/*.meta; do
            [ -e "$f" ] || continue
            local q; q="$(basename "$f" .meta)"
            local revs; revs="$(reverse_deps "$q" || true)"
            if [ -z "$revs" ]; then
                log "Órfão detectado: $q"
                [ "$dry_run" -eq 0 ] && _remove_pkg "$q" "$force" "$dry_run"
            fi
        done
    fi
}

# ============================================================
# Entry point
# ============================================================
remove_main() {
    local force=0
    local recursive=0
    local dry_run=0
    local autoremove=0
    local pkg=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --force) force=1 ;;
            --recursive) recursive=1 ;;
            --dry-run) dry_run=1 ;;
            --autoremove) autoremove=1 ;;
            *) pkg="$1" ;;
        esac
        shift
    done

    [ -z "$pkg" ] && { log "Uso: ibuild remove <pacote> [--force] [--recursive] [--dry-run] [--autoremove]"; exit 1; }

    remove_pkg "$pkg" "$force" "$recursive" "$dry_run" "$autoremove"
}
