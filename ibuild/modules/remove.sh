#!/usr/bin/env bash
# modules/remove.sh - remoção de pacotes no ibuild

set -euo pipefail

source "$(dirname "$0")/utils.sh"
source "$(dirname "$0")/hooks.sh"

PKG_DIR="/var/lib/ibuild/packages"
BIN_DIR="/var/cache/ibuild/packages-bin"
LOG_DIR="/var/log/ibuild"
mkdir -p "$PKG_DIR" "$BIN_DIR" "$LOG_DIR"

REMOVE_LOG="$LOG_DIR/remove.log"

# ============================================================
# Remover pacote
# ============================================================
remove_pkg() {
    local pkg="$1"

    local metafile="$PKG_DIR/$pkg/.meta"
    local filesfile="$PKG_DIR/$pkg/.files"

    # Se não existir localmente, tenta fallback no BIN_DIR
    if [ ! -f "$filesfile" ]; then
        log ">> Manifesto não encontrado em $filesfile, tentando fallback..."
        if [ -f "$BIN_DIR/$pkg.meta" ]; then
            eval "$(grep -E '^(name|version)=' "$BIN_DIR/$pkg.meta")"
            filesfile="$BIN_DIR/${name}-${version}.files"
        fi
    fi

    [ -f "$filesfile" ] || { log "ERRO: Nenhum manifesto encontrado para $pkg"; exit 1; }

    hooks_run_phase remove pre

    log ">> Removendo pacote $pkg"
    while read -r f; do
        [ -z "$f" ] && continue

        # Evitar remoção de diretórios críticos
        case "$f" in
            /|/usr|/usr/*|/bin|/bin/*|/lib|/lib/*|/etc|/etc/*)
                log "AVISO: Ignorando arquivo crítico: $f"
                continue
                ;;
        esac

        if [ -f "/$f" ] || [ -L "/$f" ]; then
            sudo rm -f "/$f"
            echo "REMOVED: /$f" >> "$REMOVE_LOG"
        elif [ -d "/$f" ]; then
            sudo rmdir --ignore-fail-on-non-empty "/$f" 2>/dev/null || true
        fi
    done < "$filesfile"

    # Limpar registro local
    rm -rf "$PKG_DIR/$pkg"

    hooks_run_phase remove post

    log ">> Remoção concluída: $pkg"
}

# CLI
remove_main() {
    local pkg="$1"
    shift || true
    remove_pkg "$pkg" "$@"
}
