#!/usr/bin/env bash
# sb-indep.sh - sandbox independente, apenas systemd-nspawn

set -euo pipefail

BASE_DIR="/var/lib/sbmanager"

log() { printf '[*] %s\n' "$*"; }

sandbox_create() {
    local name="$1"
    local dir="$BASE_DIR/$name"

    if [ -d "$dir" ]; then
        log "Sandbox '$name' já existe em $dir"
        return 1
    fi

    log "Criando sandbox em $dir"
    mkdir -p "$dir"/{bin,sbin,lib,lib64,usr,etc,var,run,tmp,build}
    chmod 1777 "$dir/tmp"
    log "Sandbox vazio criado. (precisa compilar e instalar pacotes base depois)"
}

sandbox_enter() {
    local name="$1"
    local dir="$BASE_DIR/$name"

    if [ ! -d "$dir" ]; then
        log "Sandbox '$name' não existe"
        return 1
    fi

    log "Entrando no sandbox '$name'..."
    sudo systemd-nspawn -D "$dir" --bind="$dir/build":/build /bin/bash --login
}

sandbox_build() {
    local name="$1"
    shift
    local dir="$BASE_DIR/$name"

    if [ ! -d "$dir" ]; then
        log "Sandbox '$name' não existe"
        return 1
    fi
    if [ $# -eq 0 ]; then
        log "Uso: $0 build <sandbox> <comando...>"
        return 1
    fi

    local cmd="$*"
    local unit="sb-${name}-build.service"

    log "Rodando build no sandbox '$name' como unidade $unit"
    sudo systemd-run --unit="$unit" --property=Slice=machine.slice \
        /usr/bin/systemd-nspawn -D "$dir" \
        --bind="$dir/build":/build \
        /bin/bash -lc "$cmd"
}

sandbox_remove() {
    local name="$1"
    local dir="$BASE_DIR/$name"

    log "Removendo sandbox '$name'"
    sudo rm -rf "$dir"
}

case "${1:-}" in
    create) sandbox_create "$2" ;;
    enter)  sandbox_enter "$2" ;;
    build)  shift; sandbox_build "$@" ;;
    remove) sandbox_remove "$2" ;;
    *)
        echo "Uso: $0 {create|enter|build|remove} <sandbox> [comando]"
        ;;
esac
