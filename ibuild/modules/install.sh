#!/usr/bin/env bash
# install.sh - instalação de pacotes

source "$(dirname "$0")/utils.sh"

PKG_DIR="/var/lib/ibuild/packages"

install_pkg() {
    local pkg="$1"
    local meta="$PKG_DIR/$pkg.meta"

    [ -f "$meta" ] || { log "Pacote '$pkg' não encontrado em $PKG_DIR"; return 1; }

    source "$meta"  # carrega vars: name, version, source, build, depends

    if pkg_installed "$name"; then
        log "Pacote '$name' já está instalado"
        return 0
    fi

    log "Instalando pacote $name-$version"

    # baixar fonte
    mkdir -p /tmp/ibuild-src
    cd /tmp/ibuild-src
    wget -O "$name-$version.tar.gz" "$source"
    tar xf "$name-$version.tar.gz"

    # rodar build
    local logf="$LOG_DIR/$name.log"
    (
        eval "$build"
    ) >"$logf" 2>&1

    # registrar metadados
    cat >"$(pkg_meta_file "$name")" <<EOF
name=$name
version=$version
depends=$depends
source=$source
EOF

    log "Pacote $name instalado com sucesso"
}

install_main() {
    local pkg="$1"
    install_pkg "$pkg"
}
