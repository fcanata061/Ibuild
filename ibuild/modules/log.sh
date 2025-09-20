#!/usr/bin/env bash
# log.sh - visualizar logs

source "$(dirname "$0")/utils.sh"

log_show() {
    local pkg="$1"
    local file="$LOG_DIR/$pkg.log"

    [ -f "$file" ] || { log "Sem log para '$pkg'"; return 1; }

    less "$file"
}

log_main() {
    local pkg="$1"
    log_show "$pkg"
}
