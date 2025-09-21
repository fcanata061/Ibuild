#!/usr/bin/env bash
# ibuild/modules/build.sh
# Evolução: suporte completo a hooks inline no .meta

set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$MODULE_DIR/utils.sh"
source "$MODULE_DIR/sandbox.sh"
source "$MODULE_DIR/hooks.sh"

PKG_DIR="${PKG_DIR:-/var/lib/ibuild/packages}"
BUILD_ROOT="${BUILD_ROOT:-/var/cache/ibuild/build}"
DESTDIR="${DESTDIR:-/var/cache/ibuild/dest}"
PKG_OUT="${PKG_OUT:-/var/cache/ibuild/packages}"

ensure_dir "$BUILD_ROOT" "$DESTDIR" "$PKG_OUT"

# ============================================================
# Helpers
# ============================================================

_meta_get() {
    local meta="$1" key="$2"
    grep -E "^${key}=" "$meta" | cut -d= -f2- || true
}

_hook_exec() {
    local meta="$1" hook="$2" default_cmd="$3"

    # hooks no .meta têm prioridade
    local cmd
    cmd="$(_meta_get "$meta" "hook_${hook}")"

    if [ -n "$cmd" ]; then
        log "[hook:$hook] executando comando do meta"
        sandbox_exec bash -c "$cmd"
    elif [ -n "$default_cmd" ]; then
        log "[hook:$hook] executando comando padrão"
        sandbox_exec bash -c "$default_cmd"
    fi
}

# ============================================================
# Build pipeline
# ============================================================

build_main() {
    local pkg="$1"
    local pkg_path="$PKG_DIR/$pkg"
    local meta="$pkg_path/$pkg.meta"

    if [ ! -f "$meta" ]; then
        err "Meta file não encontrado para $pkg"
        exit 1
    fi

    # Carrega variáveis principais do .meta
    eval "$(grep -E '^(name|version|source|checksum|build|rundep|builddep)=' "$meta" || true)"

    # Define diretórios
    export SRC_DIR="$BUILD_ROOT/$pkg/src"
    export BUILD_DIR="$BUILD_ROOT/$pkg/build"
    export PKG_DESTDIR="$DESTDIR/$pkg"

    ensure_dir "$SRC_DIR" "$BUILD_DIR" "$PKG_DESTDIR"

    # --- Fetch ---
    _hook_exec "$meta" pre_fetch ""
    _hook_exec "$meta" fetch "
        if [ -n \"\$source\" ]; then
            fname=\$(basename \"\$source\")
            log \"Baixando fonte \$source\"
            curl -L -o \"$BUILD_ROOT/$pkg/\$fname\" \"\$source\" || true
            if [ -f \"$BUILD_ROOT/$pkg/\$fname\" ]; then
                tar -xf \"$BUILD_ROOT/$pkg/\$fname\" -C \"$SRC_DIR\" --strip-components=1
            fi
        fi
    "
    _hook_exec "$meta" post_fetch ""

    # --- Patch ---
    _hook_exec "$meta" pre_patch ""
    _hook_exec "$meta" patch "
        if [ -d \"$pkg_path/patches\" ]; then
            for p in \"$pkg_path\"/patches/*.patch; do
                [ -f \"\$p\" ] || continue
                log \"Aplicando patch \$p\"
                patch -d \"$SRC_DIR\" -p1 < \"\$p\"
            done
        fi
    "
    _hook_exec "$meta" post_patch ""

    # --- Configure ---
    _hook_exec "$meta" pre_configure "mkdir -p \"$BUILD_DIR\""
    _hook_exec "$meta" configure "
        cd \"$BUILD_DIR\"
        \"$SRC_DIR/configure\" --prefix=/usr
    "
    _hook_exec "$meta" post_configure ""

    # --- Build ---
    _hook_exec "$meta" pre_build ""
    _hook_exec "$meta" build "
        make -C \"$BUILD_DIR\" -j\$(nproc)
    "
    _hook_exec "$meta" post_build ""

    # --- Install ---
    _hook_exec "$meta" pre_install ""
    _hook_exec "$meta" install "
        make -C \"$BUILD_DIR\" DESTDIR=\"$PKG_DESTDIR\" install
    "
    _hook_exec "$meta" post_install ""

    # --- Package ---
    log "Empacotando $pkg-$version"
    (
        cd "$PKG_DESTDIR"
        tar -c . | zstd -19 -o "$PKG_OUT/$pkg-$version.tar.zst"
        find . -type f | sort > "$PKG_OUT/$pkg-$version.files"
        cp "$meta" "$PKG_OUT/$pkg-$version.meta"
    )

    ok "$pkg-$version construído, instalado e empacotado em $PKG_OUT"
}
