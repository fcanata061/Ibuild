#!/usr/bin/env bash
# modules/sandbox.sh
# Sandbox aprimorado para ibuild usando systemd-nspawn
# - Cria rootfs por pacote em /var/lib/ibuild/sandbox/<pkg>
# - Isola build usando systemd-nspawn (ephemeral)
# - Suporta exec não-interativo e shell interativo
# - Bind do cache de fontes, opção de mount readonly do host
#
# Uso (via ibuild):
#   ibuild sandbox prepare <pkg> [--bind-ro /usr:/hostusr,/lib:/hostlib] [--allow-network]
#   ibuild sandbox shell <pkg>   # shell interativo dentro do sandbox
#   ibuild sandbox exec <pkg> -- <comando...>   # executa comando dentro do sandbox
#   ibuild sandbox destroy <pkg>                # limpa o rootfs
#   ibuild sandbox status <pkg>                 # checa se existe rootfs
#
# Observações:
# - systemd-nspawn é executado via sudo (requer privilégios)
# - por padrão a sandbox usa --private-network (sem rede). Passe --allow-network para permitir rede.
# - por padrão não monta host /usr /bin /lib — mas você pode passar binds só-leitura via --bind-ro
#
set -euo pipefail

# Dependências/paths globais
IBUILD_MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IBUILD_ROOT="$(cd "$IBUILD_MODULE_DIR/../.." && pwd)" || IBUILD_ROOT="/var/lib/ibuild"
SANDBOX_BASE="${SANDBOX_BASE:-/var/lib/ibuild/sandbox}"
SRC_CACHE="${SRC_CACHE:-/var/cache/ibuild/sources}"

mkdir -p "$SANDBOX_BASE" "$SRC_CACHE"

# Logging mínimo
log() { printf '[ibuild:sandbox] %s\n' "$*"; }

# Helper: copia recursiva (rsync preferido)
_copy_tree() {
    local src="$1"; local dst="$2"
    if command -v rsync &>/dev/null; then
        rsync -aH --numeric-ids --delete "$src"/ "$dst"/
    else
        # fallback simples
        rm -rf "$dst"
        mkdir -p "$(dirname "$dst")"
        cp -a "$src" "$dst"
    fi
}

# Prepara rootfs mínimo para o pacote
# args: pkg_name [--bind-ro <comma-separated host:target>] [--allow-network]
# Retorna path do rootfs
sandbox_prepare_rootfs() {
    local pkg="$1"
    shift || true

    local allow_network=0
    local bind_ro_list=""
    # parse opts
    while [ $# -gt 0 ]; do
        case "$1" in
            --allow-network) allow_network=1; shift ;;
            --bind-ro) bind_ro_list="$2"; shift 2 ;;
            *) log "Aviso: opção desconhecida $1"; shift ;;
        esac
    done

    local rootfs="$SANDBOX_BASE/$pkg"
    log "Preparando rootfs em: $rootfs"
    sudo rm -rf "$rootfs"
    sudo mkdir -p "$rootfs"/{bin,boot,dev,etc,home,lib,lib64,mnt,opt,proc,root,run,sbin,sys,tmp,usr,var}
    sudo chmod 1777 "$rootfs/tmp"

    # criar diretórios específicos do ibuild
    sudo mkdir -p "$rootfs/build"
    sudo mkdir -p "$rootfs/package"

    # montar cache de fontes dentro do rootfs/build/sources via bind - será feito no runtime com --bind
    # mas deixamos a pasta criada
    sudo mkdir -p "$rootfs/build/sources"
    # não montamos aqui — usaremos --bind ao executar systemd-nspawn

    # Se bind_ro_list for fornecido, criamos os destinos
    if [ -n "$bind_ro_list" ]; then
        IFS=',' read -ra binds <<< "$bind_ro_list"
        for b in "${binds[@]}"; do
            # formato hostpath:targetpath (ex: /usr:/hostusr)
            local hostpath="${b%%:*}"
            local targetpath="${b#*:}"
            if [ -z "$hostpath" ] || [ -z "$targetpath" ]; then
                log "Formato inválido em --bind-ro: $b (esperado host:target)"
                continue
            fi
            sudo mkdir -p "$rootfs/$targetpath"
        done
    fi

    # Set permission (host-owned rootfs; systemd-nspawn will take care de mapear users)
    sudo chown root:root "$rootfs"
    log "Rootfs pronto: $rootfs"
    echo "$rootfs"
}

# Internal: build the systemd-nspawn common flags
# args: <pkg> <allow_network> <bind_ro_list>
_sandbox_nspawn_flags() {
    local pkg="$1"; local allow_network="$2"; local bind_ro_list="$3"
    local flags=( --ephemeral --as-pid2 --quiet --directory="$SANDBOX_BASE/$pkg" --machine="ibuild-${pkg}" --setenv=DEBIAN_FRONTEND=noninteractive )

    # Networking
    if [ "$allow_network" -eq 1 ]; then
        # default: allow host network (no --private-network)
        :
    else
        flags+=( --private-network )
    fi

    # basic safety
    flags+=( --private-users=pick --capability=CAP_CHOWN,CAP_DAC_OVERRIDE,CAP_FOWNER,CAP_FSETID,CAP_KILL --register=no )

    # bind the sources cache readonly into /build/sources
    flags+=( --bind="$SRC_CACHE":/build/sources )

    # bind-ro list (hostpath:target)
    if [ -n "$bind_ro_list" ]; then
        IFS=',' read -ra binds <<< "$bind_ro_list"
        for b in "${binds[@]}"; do
            local hostpath="${b%%:*}"
            local targetpath="${b#*:}"
            # mount read-only
            flags+=( --bind-ro="$hostpath":"$targetpath" )
        done
    fi

    printf '%s\0' "${flags[@]}"
}

# Abre shell interativo dentro do sandbox
# usage: sandbox_shell <pkg> [--allow-network] [--bind-ro host:target,...]
sandbox_shell() {
    local pkg="$1"; shift || true
    local allow_network=0; local bind_ro_list=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --allow-network) allow_network=1; shift ;;
            --bind-ro) bind_ro_list="$2"; shift 2 ;;
            *) log "Opção desconhecida: $1"; shift ;;
        esac
    done

    local rootfs="$SANDBOX_BASE/$pkg"
    if [ ! -d "$rootfs" ]; then
        log "Rootfs não encontrado para $pkg. Rode: ibuild sandbox prepare $pkg"
        return 1
    fi

    log "Abrindo shell interativo no sandbox $pkg (rede permitido: $allow_network)"
    # Build flags
    IFS=$'\0' read -r -d '' -a flags < <(_sandbox_nspawn_flags "$pkg" "$allow_network" "$bind_ro_list" && printf '\0')
    # Launch interactive shell (use sudo)
    sudo systemd-nspawn "${flags[@]}" -- /bin/bash --login
}

# Executa comando não interativo dentro do sandbox
# usage: sandbox_exec <pkg> [--allow-network] [--bind-ro host:target,...] -- <cmd...>
sandbox_exec() {
    local pkg="$1"; shift || true
    local allow_network=0; local bind_ro_list=""
    # parse until -- then command remains
    while [ $# -gt 0 ]; do
        case "$1" in
            --allow-network) allow_network=1; shift ;;
            --bind-ro) bind_ro_list="$2"; shift 2 ;;
            --) shift; break ;;
            *) log "Opção desconhecida: $1"; shift ;;
        esac
    done

    if [ $# -eq 0 ]; then
        log "Uso: sandbox_exec <pkg> [--allow-network] [--bind-ro host:target,...] -- <comando...>"
        return 1
    fi

    local rootfs="$SANDBOX_BASE/$pkg"
    [ -d "$rootfs" ] || { log "Rootfs não encontrado para $pkg. Rode: ibuild sandbox prepare $pkg"; return 1; }

    # prepare bind flags etc.
    IFS=$'\0' read -r -d '' -a flags < <(_sandbox_nspawn_flags "$pkg" "$allow_network" "$bind_ro_list" && printf '\0')

    # run the command inside ephemeral container; keep environment variable DESTDIR=/package
    log "Executando em sandbox $pkg: $*"
    sudo systemd-nspawn "${flags[@]}" --setenv=DESTDIR=/package -- /bin/bash -c "$*"
    return $?
}

# Copia um diretório de fonte (host) para a build tree dentro do sandbox
# usage: sandbox_copy_source <pkg> <host_src_dir>
sandbox_copy_source() {
    local pkg="$1"; local host_src="$2"
    local rootfs="$SANDBOX_BASE/$pkg"
    [ -d "$rootfs" ] || { log "Rootfs não existe: $rootfs"; return 1; }
    sudo mkdir -p "$rootfs/build/$pkg"
    log "Copiando source para sandbox: $host_src -> $rootfs/build/$pkg/"
    # copy as root (preserve)
    if command -v rsync &>/dev/null; then
        sudo rsync -aH --numeric-ids "$host_src"/ "$rootfs/build/$pkg/"
    else
        sudo cp -a "$host_src"/ "$rootfs/build/$pkg/"
    fi
}

# Destroi/limpa sandbox (remove rootfs)
sandbox_destroy() {
    local pkg="$1"
    local rootfs="$SANDBOX_BASE/$pkg"
    if [ -d "$rootfs" ]; then
        log "Destruindo sandbox $pkg em $rootfs"
        # nenhum mount persistente criado por este script; mas tente limpar mountpoints seguros
        sudo rm -rf "$rootfs"
        log "Sandbox removido"
    else
        log "Sandbox $pkg não existe"
    fi
}

# Status simples
sandbox_status() {
    local pkg="$1"
    local rootfs="$SANDBOX_BASE/$pkg"
    if [ -d "$rootfs" ]; then
        log "Sandbox existe: $rootfs"
        ls -la "$rootfs" | sed -n '1,10p'
        return 0
    else
        log "Sandbox $pkg não existe"
        return 1
    fi
}

# CLI entrypoint (chamado por ibuild)
sandbox_main() {
    local cmd="${1:-}"
    shift || true
    case "$cmd" in
        prepare)
            local pkg="${1:-}"; shift || true
            [ -n "$pkg" ] || { echo "Uso: ibuild sandbox prepare <pkg> [--bind-ro host:target,...] [--allow-network]"; return 1; }
            sandbox_prepare_rootfs "$pkg" "$@"
            ;;
        shell)
            local pkg="${1:-}"; shift || true
            [ -n "$pkg" ] || { echo "Uso: ibuild sandbox shell <pkg> [--bind-ro ...] [--allow-network]"; return 1; }
            sandbox_shell "$pkg" "$@"
            ;;
        exec)
            local pkg="${1:-}"; shift || true
            [ -n "$pkg" ] || { echo "Uso: ibuild sandbox exec <pkg> [--bind-ro ...] [--allow-network] -- <comando...>"; return 1; }
            sandbox_exec "$pkg" "$@"
            ;;
        copy)
            local pkg="${1:-}"; local src="${2:-}"
            [ -n "$pkg" ] && [ -n "$src" ] || { echo "Uso: ibuild sandbox copy <pkg> <host_src_dir>"; return 1; }
            sandbox_copy_source "$pkg" "$src"
            ;;
        destroy)
            local pkg="${1:-}"; shift || true
            [ -n "$pkg" ] || { echo "Uso: ibuild sandbox destroy <pkg>"; return 1; }
            sandbox_destroy "$pkg"
            ;;
        status)
            local pkg="${1:-}"; shift || true
            [ -n "$pkg" ] || { echo "Uso: ibuild sandbox status <pkg>"; return 1; }
            sandbox_status "$pkg"
            ;;
        *)
            cat <<EOF
ibuild sandbox <cmd> ...

Comandos:
  prepare <pkg> [--bind-ro host:target,...] [--allow-network]   - cria rootfs mínimo
  copy <pkg> <host_src_dir>                                    - copia fontes para sandbox
  shell <pkg> [--bind-ro ...] [--allow-network]                - abre shell interativo
  exec <pkg> [--bind-ro ...] [--allow-network] -- <cmd...>     - executa comando dentro do sandbox
  destroy <pkg>                                                - remove sandbox
  status <pkg>                                                 - mostra info
EOF
            ;;
    esac
}

# Export functions for other modules if sourced
# (quando incluído via "source modules/sandbox.sh", exporta funções)
export -f sandbox_prepare_rootfs sandbox_shell sandbox_exec sandbox_copy_source sandbox_destroy sandbox_status
