#!/usr/bin/env bash
# modules/update.sh
# Verifica novas versões de pacotes (http, https, ftp, git) e notifica

set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$MODULE_DIR/utils.sh"

PKG_DIR="${PKG_DIR:-/var/lib/ibuild/packages}"
LOG_DIR="${LOG_DIR:-/var/log/ibuild}"
REPORT_FILE="$LOG_DIR/update-report.json"

ensure_dir "$LOG_DIR"

# ============================================================
# Helpers
# ============================================================

_get_meta_field() {
    local meta="$1" field="$2"
    grep -E "^${field}=" "$meta" | cut -d= -f2-
}

_notify_update() {
    local pkg="$1" old="$2" new="$3" src="$4"
    if command -v notify-send >/dev/null 2>&1; then
        notify-send -u normal -a "ibuild" "Atualização disponível" \
            "$pkg: $old → $new\n$src"
    fi
    ok "[$pkg] atualização encontrada: $old → $new"
}

# ============================================================
# Fetchers
# ============================================================

_fetch_http() {
    local url="$1" regex="$2"
    local html
    if ! html="$(curl -L -s --max-time 20 "$url")"; then
        warn "Falha ao baixar $url"
        echo "unknown"
        return 1
    fi
    echo "$html" | grep -oE "$regex" | sort -V | tail -n1 || echo "unknown"
}

_fetch_ftp() {
    local url="$1" regex="$2"
    local list
    if ! list="$(curl -s --max-time 20 "$url")"; then
        warn "Falha ao listar FTP $url"
        echo "unknown"
        return 1
    fi
    echo "$list" | grep -oE "$regex" | sort -V | tail -n1 || echo "unknown"
}

_fetch_git() {
    local repo="$1" regex="$2"
    local tmpdir
    tmpdir="$(mktemp -d)"
    if ! git ls-remote --tags "$repo" >"$tmpdir/tags.txt" 2>/dev/null; then
        warn "Falha ao acessar repo git $repo"
        rm -rf "$tmpdir"
        echo "unknown"
        return 1
    fi
    local ver
    ver="$(grep -oE "$regex" "$tmpdir/tags.txt" | sort -V | tail -n1 || true)"
    rm -rf "$tmpdir"
    [ -n "$ver" ] && echo "$ver" || echo "unknown"
}

# ============================================================
# Main
# ============================================================

update_main() {
    local updates=()
    log ">> Procurando atualizações em $PKG_DIR"

    for meta in "$PKG_DIR"/*/*.meta; do
        [ -f "$meta" ] || continue
        local pkg; pkg="$(basename "$(dirname "$meta")")"

        local cur_ver src regex
        cur_ver="$(_get_meta_field "$meta" "version")"
        src="$(_get_meta_field "$meta" "source")"
        regex="$(_get_meta_field "$meta" "regex")"

        if [ -z "$src" ] || [ -z "$regex" ]; then
            warn "[$pkg] sem source/regex definidos, ignorando"
            continue
        fi

        log "[$pkg] verificando em $src"

        local proto latest
        proto="$(echo "$src" | cut -d: -f1)"

        case "$proto" in
            http|https)
                latest="$(_fetch_http "$src" "$regex")"
                ;;
            ftp)
                latest="$(_fetch_ftp "$src" "$regex")"
                ;;
            git|git+http|git+https|git+ssh)
                local clean_repo="${src#git+}"
                latest="$(_fetch_git "$clean_repo" "$regex")"
                ;;
            *)
                warn "[$pkg] protocolo $proto não suportado"
                latest="unknown"
                ;;
        esac

        if [ "$latest" != "unknown" ] && [ "$latest" != "$cur_ver" ]; then
            _notify_update "$pkg" "$cur_ver" "$latest" "$src"
            updates+=("{\"package\":\"$pkg\",\"current\":\"$cur_ver\",\"latest\":\"$latest\",\"source\":\"$src\"}")
        else
            log "[$pkg] já está na versão $cur_ver"
        fi
    done

    {
        echo "["
        printf "  %s\n" "$(IFS=,; echo "${updates[*]}")"
        echo "]"
    } > "$REPORT_FILE"

    ok "Relatório salvo em $REPORT_FILE"
}
