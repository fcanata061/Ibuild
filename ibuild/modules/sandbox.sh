#!/usr/bin/env bash
# sandbox.sh - módulo de sandbox para ibuild
# Usa systemd-nspawn para isolar compilações

source "$(dirname "$0")/utils.sh"

BASE_DIR="/var/lib/ibuild/sandboxes"

sandbox_create() {
    local name="$1"
    local dir="$BASE_DIR/$name"

    if [ -d "$dir" ]; then
        log "Sandbox '$name' já existe em $dir"
        return 1
    fi

    log "Criando sandbox em $dir"
    sudo mkdir -p "$dir"/{bin,sbin,lib,lib64,usr,etc,var,run,tmp,build}
    sudo chmod 1777 "$dir/tmp"
    log "Sandbox vazio criado"
}

sandbox_enter() {
    local name="$1"
    local dir="$BASE_DIR/$name"

    [ -d "$dir" ] || { log "Sandbox '$name' não existe"; return 1; }

    log "Entrando no sandbox '$name'..."
    sudo systemd-nspawn -D "$dir" --bind="$dir/build":/build /bin/bash --login
}

sandbox_build() {
    local name="$1"
    shift
    local dir="$BASE_DIR/$name"

    [ -d "$dir" ] || { log "Sandbox '$name' não existe"; return 1; }
    [ $# -gt 0 ] || { log "Uso: ibuild sandbox build <nome> <comando>"; return 1; }

    local cmd="$*"
    local unit="ibuild-${name}-build.service"

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

sandbox_main() {
    local action="${1:-}"
    shift || true

    case "$action" in
        create) sandbox_create "$@" ;;
        enter)  sandbox_enter "$@" ;;
        build)  sandbox_build "$@" ;;
        remove) sandbox_remove "$@" ;;
        *)
            echo "Uso: ibuild sandbox {create|enter|build|remove} <args>"
            ;;
    esac
}
