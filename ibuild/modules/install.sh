#!/usr/bin/env bash
# modules/install.sh - instalação de pacotes no ibuild

set -euo pipefail

source "$(dirname "$0")/utils.sh"
source "$(dirname "$0")/hooks.sh"
source "$(dirname "$0")/dependency.sh"
source "$(dirname "$0")/build.sh"

PKG_DIR="/var/lib/ibuild/packages"
BIN_DIR="/var/cache/ibuild/packages-bin"
LOG_DIR="/var/log/ibuild"
mkdir -p "$PKG_DIR" "$BIN_DIR" "$LOG_DIR"

# ============================================================
# Instalar pacote binário
# ============================================================
install_bin_pkg() {
    local pkg="$1"
    local metafile="$BIN_DIR/$pkg.meta"

    [ -f "$metafile" ] || { log "ERRO: .meta não encontrado para $pkg"; exit 1; }

    # Carregar metadados
    local name version rundep
    eval "$(grep -E '^(name|version|rundep)=' "$metafile")"

    local pkgfile="$BIN_DIR/${name}-${version}.tar.zst"
    local manifest="$BIN_DIR/${name}-${version}.files"

    [ -f "$pkgfile" ] || { log "ERRO: pacote binário não encontrado: $pkgfile"; exit 1; }

    hooks_run_phase install pre

    # Resolver dependências de runtime antes
    if [ -n "${rundep:-}" ]; then
        log ">> Resolvendo dependências de runtime: $rundep"
        dependency_resolve "$rundep" | while read -r dep; do
            [ -n "$dep" ] && ibuild install "$dep"
        done
    fi

    # Instalar o pacote no sistema
    log ">> Instalando $name-$version"
    sudo tar --extract --preserve-permissions --same-owner -C / -f "$pkgfile"

    # Salvar lista de arquivos
    mkdir -p "$PKG_DIR/$pkg"
    cp "$metafile" "$PKG_DIR/$pkg/.meta"
    cp "$manifest" "$PKG_DIR/$pkg/.files"

    hooks_run_phase install post

    log ">> Instalação concluída: $name-$version"
}

# ============================================================
# Instalar (binário ou build se necessário)
# ============================================================
install_pkg() {
    local pkg="$1"
    local metafile="$PKG_DIR/$pkg/.meta"

    # Se não existir binário, buildar antes
    if [ ! -f "$BIN_DIR/$pkg.meta" ]; then
        log ">> Pacote binário não encontrado, chamando build: $pkg"
        build_pkg "$pkg"
    fi

    install_bin_pkg "$pkg"
}

# CLI
install_main() {
    local pkg="$1"
    shift || true
    install_pkg "$pkg" "$@"
}
