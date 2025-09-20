#!/usr/bin/env bash
# build.sh - construção e empacotamento de pacotes no ibuild

set -euo pipefail

source "$(dirname "$0")/utils.sh"
source "$(dirname "$0")/hooks.sh"
source "$(dirname "$0")/sandbox.sh"
source "$(dirname "$0")/dependency.sh"

PKG_DIR="/var/lib/ibuild/packages"
LOG_DIR="/var/log/ibuild"
SRC_CACHE="/var/cache/ibuild/sources"
BIN_DIR="/var/cache/ibuild/packages-bin"

mkdir -p "$PKG_DIR" "$LOG_DIR" "$SRC_CACHE" "$BIN_DIR"

# ============================================================
# Helpers: fetch, verify, extract, patch
# ============================================================

fetch_source() {
    local url="$1"
    local pkg="$2"
    local dest="$SRC_CACHE/$(basename "$url")"

    if [ ! -f "$dest" ]; then
        log "Baixando source de $pkg: $url"
        if command -v curl &>/dev/null; then
            curl -L -o "$dest" "$url"
        else
            wget -O "$dest" "$url"
        fi
    else
        log "Source em cache: $dest"
    fi

    echo "$dest"
}

verify_source() {
    local file="$1"
    local expect_hash="${2:-}"
    if [ -n "$expect_hash" ]; then
        local got_hash
        got_hash="$(sha256sum "$file" | awk '{print $1}')"
        if [ "$got_hash" != "$expect_hash" ]; then
            log "ERRO: Hash inválido para $file"
            exit 1
        fi
        log "Hash SHA256 válido para $file"
    fi
}

extract_source() {
    local file="$1"
    local buildroot="$2"
    mkdir -p "$buildroot"
    case "$file" in
        *.tar.gz|*.tgz) tar -xzf "$file" -C "$buildroot" ;;
        *.tar.xz) tar -xJf "$file" -C "$buildroot" ;;
        *.tar.bz2) tar -xjf "$file" -C "$buildroot" ;;
        *.zip) unzip -q "$file" -d "$buildroot" ;;
        *) log "Formato desconhecido: $file"; exit 1 ;;
    esac
}

apply_patches() {
    local srcdir="$1"
    local pkg="$2"
    local patchdir="$PKG_DIR/$pkg/patches"

    if [ -n "${patches[*]:-}" ]; then
        hooks_run_phase patch pre
        for p in "${patches[@]}"; do
            local patchfile="$patchdir/$p"
            [ -f "$patchfile" ] || { log "Patch não encontrado: $patchfile"; exit 1; }
            log "Aplicando patch manual: $p"
            (cd "$srcdir" && patch -p1 < "$patchfile")
        done
        hooks_run_phase patch post
        return
    fi

    if [ -d "$patchdir" ]; then
        local found=0
        hooks_run_phase patch pre
        for p in "$patchdir"/*.patch "$patchdir"/*.diff; do
            [ -e "$p" ] || continue
            found=1
            log "Aplicando patch automático: $(basename "$p")"
            (cd "$srcdir" && patch -p1 < "$p")
        done
        [ "$found" -eq 1 ] && hooks_run_phase patch post
    fi
}

# ============================================================
# Empacotamento
# ============================================================

package_build() {
    local pkg="$1"
    local version="$2"
    local pkgdir="$3"
    local meta="$4"

    hooks_run_phase package pre

    local tarball="$BIN_DIR/${pkg}-${version}.tar"
    local archive

    (cd "$pkgdir" && tar -cf "$tarball" .)

    if command -v zstd &>/dev/null; then
        archive="${tarball}.zst"
        zstd -f "$tarball"
    else
        archive="${tarball}.xz"
        xz -f "$tarball"
    fi

    rm -f "$tarball"

    log "Pacote gerado: $archive"

    tar -tf "$archive" | sort > "$PKG_DIR/$pkg.files"
    cp "$meta" "$PKG_DIR/$pkg.meta"

    hooks_run_phase package post
}

# ============================================================
# Build principal com sandbox
# ============================================================

build_pkg() {
    local pkg="$1"
    local meta="$PKG_DIR/$pkg.meta"
    [ -f "$meta" ] || { log "Pacote '$pkg' não encontrado"; exit 1; }

    source "$meta"

    log ">> Iniciando build do pacote: $name-$version"

    # 1. Resolver deps de build
    local builddeps
    builddeps="$(get_builddeps "$pkg")"
    if [ -n "$builddeps" ]; then
        for dep in $builddeps; do
            if ! [ -f "$PKG_DIR/$dep.meta" ]; then
                log "Dependência de build '$dep' não instalada, construindo..."
                build_pkg "$dep"
            fi
        done
    fi

    # 2. Download + hash
    local srcfile
    srcfile="$(fetch_source "$source" "$pkg")"
    verify_source "$srcfile" "${sha256:-}"

    # 3. Preparar diretórios de build
    local buildroot="/tmp/ibuild-$pkg-build"
    rm -rf "$buildroot"
    mkdir -p "$buildroot"
    extract_source "$srcfile" "$buildroot"
    local srcdir
    srcdir="$(find "$buildroot" -mindepth 1 -maxdepth 1 -type d | head -n1)"

    local pkgdir="$buildroot/pkgdir"
    mkdir -p "$pkgdir"

    # 4. Aplicar patches
    apply_patches "$srcdir" "$pkg"

    # 5. Hooks pré-build
    hooks_run_phase build pre

    # 6. Rodar build dentro do sandbox
    local logf="$LOG_DIR/$pkg-build.log"
    sandbox_prepare_rootfs "$pkg" >/dev/null

    log ">> Rodando build em sandbox..."
    sandbox_exec "$pkg" bash -c "
        cd /build/$pkg &&
        $build DESTDIR=/package
    " >\"$logf\" 2>&1

    log "Build concluído. Log em $logf"

    # 7. Hooks pós-build
    hooks_run_phase build post

    # 8. Copiar resultado do sandbox
    local rootfs="$SANDBOX_BASE/$pkg"
    cp -a \"$rootfs/package/.\" \"$pkgdir/\"

    # 9. Empacotar
    package_build \"$pkg\" \"$version\" \"$pkgdir\" \"$meta\"

    # 10. Limpeza
    sandbox_exit \"$pkg\"
    rm -rf \"$buildroot\"

    log \"Sandbox finalizado\"
}

# ============================================================
# Entry point
# ============================================================

build_main() {
    local pkg=\"${1:-}\"
    [ -n \"$pkg\" ] || { log \"Uso: ibuild build <pacote>\"; exit 1; }
    build_pkg \"$pkg\"
}
