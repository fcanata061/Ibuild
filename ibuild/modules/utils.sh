#!/usr/bin/env bash
# modules/utils.sh
# Utilitários comuns para ibuild

set -euo pipefail

# Cores (desativadas se NO_COLOR=1)
if [ "${NO_COLOR:-0}" -eq 0 ] && [ -t 2 ]; then
    C_RESET="\033[0m"
    C_RED="\033[31m"
    C_GREEN="\033[32m"
    C_YELLOW="\033[33m"
    C_BLUE="\033[34m"
else
    C_RESET=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""
fi

log() {
    printf "${C_BLUE}[ibuild]${C_RESET} %s\n" "$*" >&2
}

warn() {
    printf "${C_YELLOW}[ibuild:WARN]${C_RESET} %s\n" "$*" >&2
}

ok() {
    printf "${C_GREEN}[ibuild:OK]${C_RESET} %s\n" "$*" >&2
}

die() {
    printf "${C_RED}[ibuild:ERRO]${C_RESET} %s\n" "$*" >&2
    exit 1
}

ensure_dir() {
    local d="$1"
    [ -d "$d" ] || mkdir -p "$d"
}

# Executa comando visível, falha se der erro
run_cmd() {
    log "Executando: $*"
    "$@" || die "Falha no comando: $*"
}
