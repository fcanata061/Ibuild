#!/usr/bin/env bash
# install.sh - instalação de pacotes no ibuild (evoluído)

set -euo pipefail

source "$(dirname "$0")/utils.sh"
source "$(dirname "$0")/hooks.sh"
source "$(dirname "$0")/sandbox.sh"

PKG_DIR="/var/lib/ibuild/packages"
CACHE_DIR="/var/cache/ibuild/src"
LOG_ROOT="$LOG_DIR"

mkdir -p "$CACHE_DIR"

# ============================================================
# Baixa e verifica fonte
# ============================================================
_fetch_source() {
    local name="$1"
    local version="$2"
    local source="$3"
    local sha256="${4:-}"

    local cached="$CACHE_DIR/${name}-${version}.tar.gz"

    if [ ! -f "$cached" ]; then
        log "Baixando fonte de $name-$version"
        wget -q -O "$cached" "$source"
    else
        log "Usando fonte em cache: $cached"
    fi

    # Verificação de integridade
    if [ -n "$sha256" ]; then
        echo "$sha256  $cached" | sha256sum -c - || {
            log "Erro: checksum não confere para $name"
            exit 1
        }
    fi

    local srcdir="/tmp/ibuild-src/$name-$version"
    rm -rf "$srcdir"
    mkdir -p "$srcdir"
    tar xf "$cached" -C "$srcdir" --strip-components=1
    echo "$srcdir"
}

# ============================================================
# Roda build dentro ou fora do sandbox
# ============================================================
_run_build() {
    local name="$1"
    local build_cmd="$2"
    local sandbox="${3:-}"

    if [ -n "$sandbox" ]; then
        log "Rodando build no sandbox: $sandbox"
        ./ibuild sandbox exec "$sandbox" "$build_cmd"
    else
        log "Rodando build no host"
        eval "$build_cmd"
    fi
}

# ============================================================
# Checa dependências
# ============================================================
_check_deps() {
    local depends="$1"

    [ -z "$depends" ] && return 0
    for dep in $(echo "$depends" | tr ',' ' '); do
        if ! pkg_installed "$dep"; then
            log "Dependência não instalada: $dep"
            log "Use: ibuild install $dep"
            exit 1
        fi
    done
}

# ============================================================
# Instala pacote
# ============================================================
install_pkg() {
    local pkg="$1"
    local sandbox="${2:-}"
    local force="${3:-0}"

    local meta="$PKG_DIR/$pkg.meta"
    [ -f "$meta" ] || { log "Pacote '$pkg' não encontrado"; return 1; }

    # carrega variáveis do pacote
    source "$meta"  # name, version, source, depends, build, sha256?, hooks...

    if pkg_installed "$name" && [ "$force" -eq 0 ]; then
        log "Pacote '$name' já está instalado (use --force para reinstalar)"
        return 0
    fi

    _check_deps "$depends"

    log "=== Instalando pacote $name-$version ==="

    local srcdir; srcdir=$(_fetch_source "$name" "$version" "$source" "${sha256:-}")
    cd "$srcdir"

    local logdir="$LOG_ROOT/$name"
    mkdir -p "$logdir"
    local build_log="$logdir/build.log"
    local hooks_log="$logdir/hooks.log"
    : >"$build_log"
    : >"$hooks_log"

    # pasta temporária para rastrear arquivos
    local destdir="/tmp/ibuild-dest/$name"
    rm -rf "$destdir"
    mkdir -p "$destdir"

    (
        hooks_run configure pre "$name" | tee -a "$hooks_log"

        log "[BUILD] Executando etapa de build"
        eval "$build" 2>&1

        hooks_run configure post "$name" | tee -a "$hooks_log"

        hooks_run install pre "$name" | tee -a "$hooks_log"

        log "[BUILD] make install DESTDIR=$destdir"
        make install DESTDIR="$destdir" 2>&1

        hooks_run install post "$name" | tee -a "$hooks_log"
    ) >>"$build_log" 2>&1

    # Copiar arquivos do DESTDIR para sistema
    log "Copiando arquivos para /"
    sudo cp -a "$destdir"/* /

    # Registrar metadados
    local meta_out; meta_out="$(pkg_meta_file "$name")"
    {
        echo "name=$name"
        echo "version=$version"
        echo "depends=$depends"
        echo "source=$source"
        [ -n "${sha256:-}" ] && echo "sha256=$sha256"
        echo -n "files="
        (cd "$destdir" && find . -type f | tr '\n' ' ')
    } >"$meta_out"

    log "Pacote $name-$version instalado com sucesso"
    log "Logs disponíveis em $logdir"
}

# ============================================================
# Entry point
# ============================================================
install_main() {
    local force=0
    local sandbox=""
    local pkg=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --force) force=1 ;;
            --sandbox) sandbox="$2"; shift ;;
            *) pkg="$1" ;;
        esac
        shift
    done

    [ -z "$pkg" ] && { log "Uso: ibuild install <pacote> [--sandbox <sb>] [--force]"; exit 1; }

    install_pkg "$pkg" "$sandbox" "$force"
}
