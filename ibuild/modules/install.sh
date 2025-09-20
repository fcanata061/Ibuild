#!/usr/bin/env bash
# install.sh - instalação de pacotes no ibuild

set -euo pipefail

source "$(dirname "$0")/utils.sh"
source "$(dirname "$0")/hooks.sh"
source "$(dirname "$0")/sandbox.sh"
source "$(dirname "$0")/dependency.sh"
source "$(dirname "$0")/build.sh"

PKG_DIR="/var/lib/ibuild/packages"
LOG_DIR="/var/log/ibuild"
BIN_DIR="/var/cache/ibuild/packages-bin"

mkdir -p "$PKG_DIR" "$LOG_DIR" "$BIN_DIR"

# ============================================================
# Funções auxiliares
# ============================================================

install_from_bin() {
    local pkg="$1"
    local meta="$PKG_DIR/$pkg.meta"
    [ -f "$meta" ] || { log "ERRO: metadados de $pkg não encontrados"; exit 1; }

    source "$meta"

    local archive_zst="$BIN_DIR/${pkg}-${version}.tar.zst"
    local archive_xz="$BIN_DIR/${pkg}-${version}.tar.xz"
    local archive=""

    if [ -f "$archive_zst" ]; then
        archive="$archive_zst"
    elif [ -f "$archive_xz" ]; then
        archive="$archive_xz"
    fi

    [ -n "$archive" ] || { log "Nenhum pacote binário disponível para $pkg-$version"; return 1; }

    log ">> Instalando $pkg-$version a partir do pacote binário"
    hooks_run_phase install pre

    local logf="$LOG_DIR/$pkg-install.log"

    if [[ "$archive" == *.zst ]]; then
        zstd -dc "$archive" | tar -xf - -C /
    else
        xz -dc "$archive" | tar -xf - -C /
    fi >>"$logf" 2>&1

    hooks_run_phase install post

    log "Instalação de $pkg-$version concluída (binário)"
}

install_from_source() {
    local pkg="$1"
    log ">> Nenhum pacote binário para $pkg, compilando do source..."
    build_pkg "$pkg"
    install_from_bin "$pkg"
}

# ============================================================
# Instalação de dependências
# ============================================================

install_deps() {
    local pkg="$1"
    local meta="$PKG_DIR/$pkg.meta"
    [ -f "$meta" ] || { log "Metadados de $pkg não encontrados"; exit 1; }

    local deps
    deps="$(get_rundeps "$pkg")"
    if [ -n "$deps" ]; then
        log "Resolvendo dependências de runtime para $pkg: $deps"
        for dep in $deps; do
            if [ ! -f "$PKG_DIR/$dep.meta" ]; then
                log "Dependência '$dep' não instalada, instalando..."
                install_pkg "$dep"
            fi
        done
    fi
}

# ============================================================
# Instalação principal
# ============================================================

install_pkg() {
    local pkg="$1"
    local meta="$PKG_DIR/$pkg.meta"

    [ -f "$meta" ] || { log "Pacote '$pkg' não encontrado"; exit 1; }
    source "$meta"

    # Se já instalado, pula
    if [ -f "$PKG_DIR/$pkg.files" ]; then
        log "Pacote '$pkg' já instalado"
        return 0
    fi

    # 1. Dependências de runtime
    install_deps "$pkg"

    # 2. Tenta binário, senão compila
    if ! install_from_bin "$pkg"; then
        install_from_source "$pkg"
    fi

    # 3. Registrar arquivos
    tar -tf "$BIN_DIR/${pkg}-${version}.tar."* | sort > "$PKG_DIR/$pkg.files"
    log "Arquivos registrados em $PKG_DIR/$pkg.files"
}

# ============================================================
# Entry point
# ============================================================

install_main() {
    local pkg="${1:-}"
    [ -n "$pkg" ] || { log "Uso: ibuild install <pacote>"; exit 1; }
    install_pkg "$pkg"
}
