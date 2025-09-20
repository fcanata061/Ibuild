#!/usr/bin/env bash
# remove.sh - remoção de pacotes

source "$(dirname "$0")/utils.sh"

remove_pkg() {
    local pkg="$1"

    if ! pkg_installed "$pkg"; then
        log "Pacote '$pkg' não está instalado"
        return 1
    fi

    # ler lista de arquivos
    local meta
    meta="$(pkg_meta_file "$pkg")"
    source "$meta"

    # aqui assumimos que foi registrado "files=" no meta (a implementar no build)
    if [ -n "${files:-}" ]; then
        for f in $files; do
            sudo rm -f "$f"
        done
    fi

    rm -f "$meta"
    log "Pacote '$pkg' removido"
}

remove_main() {
    local pkg="$1"
    remove_pkg "$pkg"
}
