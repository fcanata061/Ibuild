#!/usr/bin/env bash
# modules/upgrade.sh
# Atualização de pacotes (upgrade) no ibuild

set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Carregar utils
# shellcheck disable=SC1090
source "$MODULE_DIR/utils.sh"
source "$MODULE_DIR/dependency.sh"
source "$MODULE_DIR/build.sh"
source "$MODULE_DIR/install.sh"
source "$MODULE_DIR/remove.sh"
source "$MODULE_DIR/sync.sh"

PKG_DIR="${PKG_DIR:-/var/lib/ibuild/packages}"
BIN_DIR="${BIN_DIR:-/var/cache/ibuild/packages-bin}"
STATE_DIR="${STATE_DIR:-/var/lib/ibuild/state}"
LOG_DIR="${LOG_DIR:-/var/log/ibuild}"

ensure_dir "$PKG_DIR" "$BIN_DIR" "$STATE_DIR" "$LOG_DIR"

# ============================================================
# Helpers
# ============================================================

_pkg_installed_version() {
    local pkg="$1"
    local state_file="$STATE_DIR/$pkg.version"
    [ -f "$state_file" ] && cat "$state_file" || echo "none"
}

_pkg_repo_version() {
    local pkg="$1"
    local meta="$PKG_DIR/$pkg/$pkg.meta"
    [ -f "$meta" ] || die "Meta de $pkg não encontrada em $meta"
    grep -E '^version=' "$meta" | cut -d= -f2
}

_pkg_needs_upgrade() {
    local pkg="$1"
    local installed repo
    installed="$(_pkg_installed_version "$pkg")"
    repo="$(_pkg_repo_version "$pkg")"
    if [ "$installed" = "none" ]; then
        echo "install"
    elif [ "$installed" != "$repo" ]; then
        echo "upgrade"
    else
        echo "uptodate"
    fi
}

_mark_installed() {
    local pkg="$1" ver="$2"
    echo "$ver" | sudo tee "$STATE_DIR/$pkg.version" >/dev/null
    ok "$pkg marcado como instalado na versão $ver"
}

# ============================================================
# Upgrade core
# ============================================================

_upgrade_pkg() {
    local pkg="$1"
    local action
    action="$(_pkg_needs_upgrade "$pkg")"

    case "$action" in
        install)
            log ">> Instalando novo pacote: $pkg"
            install_pkg "$pkg"
            _mark_installed "$pkg" "$(_pkg_repo_version "$pkg")"
            ;;
        upgrade)
            log ">> Atualizando pacote: $pkg"
            local old_ver new_ver
            old_ver="$(_pkg_installed_version "$pkg")"
            new_ver="$(_pkg_repo_version "$pkg")"

            log "Versão instalada: $old_ver"
            log "Nova versão:      $new_ver"

            # backup opcional
            local backup_dir="$STATE_DIR/backups/$pkg-$old_ver"
            ensure_dir "$backup_dir"
            if [ -d "/usr/local/$pkg" ]; then
                log ">> Backup de /usr/local/$pkg em $backup_dir"
                sudo rsync -a "/usr/local/$pkg/" "$backup_dir/"
            fi

            # remover e reinstalar
            remove_pkg "$pkg"
            install_pkg "$pkg"

            _mark_installed "$pkg" "$new_ver"
            ok "$pkg atualizado de $old_ver para $new_ver"
            ;;
        uptodate)
            ok "$pkg já está na versão mais recente"
            ;;
        *)
            die "Erro interno: ação desconhecida $action"
            ;;
    esac
}

# ============================================================
# CLI
# ============================================================

upgrade_usage() {
    cat <<EOF
Uso:
  ibuild upgrade [pacote...]
  ibuild upgrade --all
  ibuild upgrade --check

Opções:
  --all     Atualiza todos os pacotes instalados
  --check   Apenas mostra pacotes que têm upgrade disponível
EOF
}

upgrade_main() {
    local mode="specific"
    local pkgs=()

    while [ $# -gt 0 ]; do
        case "$1" in
            --all) mode="all"; shift ;;
            --check) mode="check"; shift ;;
            --help) upgrade_usage; return 0 ;;
            *) pkgs+=("$1"); shift ;;
        esac
    done

    if [ "$mode" = "all" ]; then
        mapfile -t pkgs < <(ls "$STATE_DIR"/*.version 2>/dev/null | xargs -n1 basename | sed 's/\.version//')
    fi

    if [ ${#pkgs[@]} -eq 0 ] && [ "$mode" != "check" ]; then
        upgrade_usage
        return 1
    fi

    case "$mode" in
        specific|all)
            for pkg in "${pkgs[@]}"; do
                _upgrade_pkg "$pkg"
            done
            ;;
        check)
            log "Verificando pacotes instalados..."
            for f in "$STATE_DIR"/*.version; do
                [ -f "$f" ] || continue
                local pkg; pkg="$(basename "$f" .version)"
                local act; act="$(_pkg_needs_upgrade "$pkg")"
                if [ "$act" = "upgrade" ]; then
                    local old new
                    old="$(_pkg_installed_version "$pkg")"
                    new="$(_pkg_repo_version "$pkg")"
                    printf "%-20s %s -> %s\n" "$pkg" "$old" "$new"
                fi
            done
            ;;
    esac
}
