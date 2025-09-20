#!/usr/bin/env bash
# sandbox.sh - isolamento para builds no ibuild

set -euo pipefail

source "$(dirname "$0")/utils.sh"
source "$(dirname "$0")/hooks.sh"

SANDBOX_BASE="/var/lib/ibuild/sandbox"
SANDBOX_CACHE="/var/cache/ibuild/sandbox"
mkdir -p "$SANDBOX_BASE" "$SANDBOX_CACHE"

# ============================================================
# Preparar sandbox rootfs
# ============================================================

sandbox_prepare_rootfs() {
    local pkg="$1"
    local rootfs="$SANDBOX_BASE/$pkg"

    # Se já existir, limpa
    rm -rf "$rootfs"
    mkdir -p "$rootfs"

    # Base mínima: /usr, /bin, /lib, /etc
    mkdir -p "$rootfs"/{usr,bin,lib,etc,tmp,build,package}
    chmod 1777 "$rootfs/tmp"

    # Montar cache de fontes compartilhado
    mkdir -p "$SANDBOX_CACHE/sources"
    mount --bind /var/cache/ibuild/sources "$rootfs/build/sources"

    echo "$rootfs"
}

# ============================================================
# Entrar no sandbox com systemd-nspawn
# ============================================================

sandbox_enter() {
    local srcdir="$1"
    local pkg="$(basename "$srcdir")"
    local rootfs
    rootfs="$(sandbox_prepare_rootfs "$pkg")"

    log ">> Iniciando sandbox para $pkg"

    hooks_run_phase sandbox pre

    # Copiar source para dentro do sandbox
    mkdir -p "$rootfs/build/$pkg"
    rsync -a "$srcdir"/ "$rootfs/build/$pkg/"

    # Iniciar container
    systemd-nspawn \
        --quiet \
        --ephemeral \
        --directory="$rootfs" \
        --setenv=DESTDIR=/package \
        --setenv=PATH=/usr/bin:/bin \
        /bin/bash <<'EOF'
        echo "[sandbox] Ambiente isolado pronto."
EOF

    hooks_run_phase sandbox post
}

# ============================================================
# Executar comandos dentro do sandbox
# ============================================================

sandbox_exec() {
    local pkg="$1"
    shift
    local cmd=("$@")

    local rootfs="$SANDBOX_BASE/$pkg"
    [ -d "$rootfs" ] || { log "ERRO: sandbox não encontrado para $pkg"; exit 1; }

    systemd-nspawn \
        --quiet \
        --directory="$rootfs" \
        --setenv=DESTDIR=/package \
        --setenv=PATH=/usr/bin:/bin \
        "${cmd[@]}"
}

# ============================================================
# Sair e destruir sandbox
# ============================================================

sandbox_exit() {
    local pkg="$1"
    local rootfs="$SANDBOX_BASE/$pkg"
    if [ -d "$rootfs" ]; then
        log ">> Limpando sandbox $pkg"
        umount -f "$rootfs/build/sources" 2>/dev/null || true
        rm -rf "$rootfs"
    fi
}
