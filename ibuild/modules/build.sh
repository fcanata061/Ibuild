#!/usr/bin/env bash
# modules/build.sh - construção de pacotes no ibuild com sandbox

set -euo pipefail

source "$(dirname "$0")/utils.sh"
source "$(dirname "$0")/hooks.sh"
source "$(dirname "$0")/sandbox.sh"

PKG_DIR="/var/lib/ibuild/packages"
SRC_CACHE="/var/cache/ibuild/sources"
BIN_DIR="/var/cache/ibuild/packages-bin"
mkdir -p "$PKG_DIR" "$SRC_CACHE" "$BIN_DIR"

# ============================================================
# Aplicar patches (rodando dentro do sandbox)
# ============================================================
apply_patches() {
    local pkg="$1"
    local srcdir="/build/$pkg"
    local patches=("$srcdir/patches"/*.patch)

    [ -d "$srcdir/patches" ] || return 0

    for patch in "${patches[@]}"; do
        [ -f "$patch" ] || continue
        log ">> Aplicando patch: $(basename "$patch")"
        (cd "$srcdir" && patch -p1 < "$patch")
    done
}

# ============================================================
# Construção principal
# ============================================================
build_pkg() {
    local pkg="$1"
    local metafile="$PKG_DIR/$pkg/.meta"

    [ -f "$metafile" ] || { log "ERRO: .meta não encontrado para $pkg"; exit 1; }

    # Carrega .meta
    local name version source checksum build rundep builddep
    eval "$(grep -E '^(name|version|source|checksum|build|rundep|builddep)=' "$metafile")"

    local srcfile="$SRC_CACHE/${source##*/}"
    local workdir="$PKG_DIR/$pkg"
    local pkgfile="$BIN_DIR/${name}-${version}.tar.zst"

    hooks_run_phase build pre

    # ========================================================
    # 1. Preparar sandbox
    # ========================================================
    sandbox_prepare_rootfs "$pkg" --bind-ro "/usr:/hostusr,/lib:/hostlib"
    rm -rf "$workdir" && mkdir -p "$workdir"
    tar -xf "$srcfile" -C "$workdir" --strip-components=1
    sandbox_copy_source "$pkg" "$workdir"

    hooks_run_phase sandbox pre

    # ========================================================
    # 2. Executar build dentro do sandbox
    # ========================================================
    sandbox_exec "$pkg" -- bash -c "
        set -e
        cd /build/$pkg
        echo '[sandbox] Aplicando patches...'
        $(declare -f apply_patches)
        apply_patches '$pkg'
        echo '[sandbox] Iniciando build...'
        $build
        echo '[sandbox] Instalando em DESTDIR=/package'
        make install DESTDIR=/package
    "

    hooks_run_phase sandbox post

    # ========================================================
    # 3. Empacotar resultado
    # ========================================================
    log '>> Empacotando resultado'
    (cd "$SANDBOX_BASE/$pkg/package" && sudo tar -c . | zstd -19 -T0 > "$pkgfile")

    # Gerar manifesto de arquivos
    local manifest="$BIN_DIR/${name}-${version}.files"
    sudo tar -tf "$pkgfile" | sort > "$manifest"

    # Copiar .meta
    cp "$metafile" "$BIN_DIR/${name}-${version}.meta"

    # ========================================================
    # 4. Limpar sandbox
    # ========================================================
    sandbox_destroy "$pkg"

    hooks_run_phase build post

    log ">> Build de $name-$version finalizado"
    log "   Pacote: $pkgfile"
    log "   Manifesto: $manifest"
}

# CLI
build_main() {
    local pkg="$1"
    shift || true
    build_pkg "$pkg" "$@"
}
