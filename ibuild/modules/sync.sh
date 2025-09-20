#!/usr/bin/env bash
# modules/sync.sh
# Sincroniza receitas do repo Git com verificação GPG opcional e filtro de tags

set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE_DIR="${CACHE_DIR:-/var/cache/ibuild/git}"
PKG_DIR="${PKG_DIR:-/var/lib/ibuild/packages}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/ibuild/backup}"

# tenta usar utils.sh se existir
if [ -f "$MODULE_DIR/utils.sh" ]; then
    # shellcheck disable=SC1090
    source "$MODULE_DIR/utils.sh"
else
    log() { printf '[ibuild:sync] %s\n' "$*"; }
fi

mkdir -p "$CACHE_DIR" "$PKG_DIR" "$BACKUP_DIR"

# ============================================================
# Git helpers
# ============================================================
_git_clone_or_update() {
    local url="$1" name="$2" depth="$3"
    local dest="$CACHE_DIR/$name"

    if [ -d "$dest/.git" ]; then
        log ">> Atualizando repo em cache: $dest"
        git -C "$dest" fetch --prune --tags --depth="$depth" origin || git -C "$dest" fetch --prune --tags origin
    else
        log ">> Clonando repo para cache: $dest"
        if [ "$depth" -gt 0 ]; then
            git clone --depth "$depth" "$url" "$dest"
        else
            git clone "$url" "$dest"
        fi
    fi
    echo "$dest"
}

_git_checkout() {
    local repo_dir="$1" rev="$2" verify_gpg="$3" tag_pattern="$4"

    if [ -z "$rev" ]; then
        return 0
    fi

    # Se for tag e tag_pattern está definido, validar
    if git -C "$repo_dir" rev-parse -q --verify "refs/tags/$rev" >/dev/null 2>&1; then
        if [ -n "$tag_pattern" ]; then
            if ! [[ "$rev" =~ $tag_pattern ]]; then
                log "ERRO: tag '$rev' não bate com padrão '$tag_pattern'"
                exit 1
            fi
        fi
        log ">> Checando tag $rev"
        git -C "$repo_dir" checkout "tags/$rev"
    else
        log ">> Checando branch/commit $rev"
        git -C "$repo_dir" checkout "$rev" || { log "ERRO: não achei ref '$rev'"; exit 1; }
    fi

    # Verificação GPG se habilitada
    if [ "$verify_gpg" -eq 1 ]; then
        log ">> Verificando assinatura GPG de $rev"
        if git -C "$repo_dir" tag -v "$rev" >/dev/null 2>&1; then
            log "Assinatura GPG válida para tag $rev"
        elif git -C "$repo_dir" log -1 --pretty=format:%G? "$rev" | grep -q "G"; then
            log "Commit $rev tem assinatura GPG válida"
        else
            log "ERRO: assinatura GPG inválida ou ausente em $rev"
            exit 1
        fi
    fi
}

# ============================================================
# Encontrar e sincronizar receitas
# ============================================================
_find_recipes() {
    local path="$1"
    find "$path" -maxdepth 2 -type f -name '*.meta' -exec dirname {} \; | sort -u
}

_sync_recipe() {
    local src="$1"
    local relname; relname="$(basename "$src")"
    local dst="$PKG_DIR/$relname"

    if [ -d "$dst" ] && [ "$BACKUP" -eq 1 ]; then
        local stamp; stamp="$(date +%Y%m%d%H%M%S)"
        local bk="$BACKUP_DIR/${relname}_$stamp"
        log ">> Criando backup: $bk"
        sudo rsync -a "$dst"/ "$bk"/
    fi

    log ">> Sincronizando $relname"
    sudo mkdir -p "$dst"
    if [ "$FORCE" -eq 1 ]; then
        sudo rm -rf "$dst"
        sudo mkdir -p "$dst"
    fi

    if [ "$DRYRUN" -eq 1 ]; then
        rsync -avn --delete "$src"/ "$dst"/
    else
        sudo rsync -a --delete "$src"/ "$dst"/
    fi

    [ -f "$dst"/*.meta ] || log "AVISO: nenhuma .meta em $dst"
}

# ============================================================
# CLI
# ============================================================
sync_usage() {
    cat <<EOF
Uso:
  ibuild sync repo <git-url> [options]

Opções:
  --name <n>         Nome do cache local (default: basename do repo)
  --rev <ref>        Branch/tag/commit para checkout
  --subdir <path>    Subdir no repo (onde ficam receitas)
  --depth <n>        Profundidade clone (default: 1)
  --pkg <nome>       Sincronizar apenas pacote específico
  --dry-run          Apenas mostrar mudanças (rsync -n)
  --force            Sobrescrever receitas existentes
  --backup           Fazer backup antes de sobrescrever
  --verify-gpg       Exigir assinaturas GPG válidas
  --tag-pattern <re> Aceitar apenas tags matching regex (ex: '^v[0-9]')
  --help             Mostrar ajuda
EOF
}

sync_main() {
    local subcmd="${1:-}"
    shift || true

    [ "$subcmd" = "repo" ] || { sync_usage; return 1; }

    local url="$1"; shift || true
    local name="" rev="" subdir="" pkg_filter="" tag_pattern=""
    DRYRUN=0; FORCE=0; BACKUP=0; DEPTH=1; VERIFY_GPG=0

    while [ $# -gt 0 ]; do
        case "$1" in
            --name) name="$2"; shift 2 ;;
            --rev) rev="$2"; shift 2 ;;
            --subdir) subdir="$2"; shift 2 ;;
            --depth) DEPTH="$2"; shift 2 ;;
            --pkg) pkg_filter="$2"; shift 2 ;;
            --dry-run) DRYRUN=1; shift ;;
            --force) FORCE=1; shift ;;
            --backup) BACKUP=1; shift ;;
            --verify-gpg) VERIFY_GPG=1; shift ;;
            --tag-pattern) tag_pattern="$2"; shift 2 ;;
            --help) sync_usage; return 0 ;;
            *) log "Opção desconhecida: $1"; sync_usage; return 1 ;;
        esac
    done

    [ -n "$url" ] || { log "ERRO: precisa do <git-url>"; exit 1; }

    local repo_name="${name:-$(basename -s .git "$url")}"
    local repo_dir; repo_dir="$(_git_clone_or_update "$url" "$repo_name" "$DEPTH")"

    _git_checkout "$repo_dir" "$rev" "$VERIFY_GPG" "$tag_pattern"

    local root="$repo_dir"
    [ -n "$subdir" ] && root="$repo_dir/$subdir"

    mapfile -t recipes < <(_find_recipes "$root")

    for r in "${recipes[@]}"; do
        if [ -n "$pkg_filter" ] && [ "$(basename "$r")" != "$pkg_filter" ]; then
            continue
        fi
        _sync_recipe "$r"
    done

    log ">> Sync concluído."
}
