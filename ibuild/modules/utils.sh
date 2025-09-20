#!/usr/bin/env bash
# Funções auxiliares

DB_DIR="/var/lib/ibuild/db"
LOG_DIR="$DB_DIR/logs"
META_DIR="$DB_DIR/installed"

mkdir -p "$DB_DIR" "$LOG_DIR" "$META_DIR"

log() {
    printf "[ibuild] %s\n" "$*"
}

pkg_meta_file() {
    echo "$META_DIR/$1.meta"
}

pkg_installed() {
    [ -f "$(pkg_meta_file "$1")" ]
}
