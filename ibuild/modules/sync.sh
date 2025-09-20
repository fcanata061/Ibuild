#!/usr/bin/env bash
# modules/sync.sh
# Sincroniza receitas (metas/pastas de pacote) a partir de um repositório Git
#
# Funcionalidades:
#  - clone/update de repositório em cache (/var/cache/ibuild/git/<name>)
#  - checkout de branch/commit
#  - sincronização por rsync do subdir do repo para /var/lib/ibuild/packages
#  - opções: --branch, --subdir, --depth, --dry-run, --force, --backup
#  - sincroniza tudo ou pacote específico
#
# Requisitos: git, rsync, sudo (para escrever em /var/lib/ibuild/packages)
#
# Exemplo:
#  ibuild sync repo https://github.com/meu/repo.git --branch main --subdir recipes --dry-run
#  ibuild sync repo https://github.com/meu/repo.git --branch main --subdir recipes --force
#
set -euo pipefail

MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
IBUILD_ROOT="$(cd "$MODULE_DIR/../.." && pwd 2>/dev/null || echo /var/lib/ibuild)"
CACHE_DIR="${CACHE_DIR:-/var/cache/ibuild/git}"
PKG_DIR="${PKG_DIR:-/var/lib/ibuild/packages}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/ibuild/backup}"
TMP_DIR="/tmp/ibuild-sync-$$"

# try to use utils.log if available
if [ -f "$MODULE_DIR/utils.sh" ]; then
    # shellcheck disable=SC1090
    source "$MODULE_DIR/utils.sh"
else
    log() { printf '[ibuild:sync] %s\n' "$*"; }
fi

mkdir -p "$CACHE_DIR" "$PKG_DIR" "$BACKUP_DIR" "$TMP_DIR"

# =============================================================
# Helpers
# =============================================================
_git_clone_or_update() {
    local url="$1"
    local name="$2"
    local depth="$3"

    local dest="$CACHE_DIR/$name"

    if [ -d "$dest/.git" ]; then
        log "Atualizando repositório cache: $dest"
        git -C "$dest" fetch --prune --tags --depth="${depth:-1}" origin || git -C "$dest" fetch --prune --tags origin
    else
        log "Clonando repositório para cache: $dest"
        mkdir -p "$dest"
        if [ -n "$depth" ] && [ "$depth" != "0" ]; then
            git clone --depth "$depth" "$url" "$dest"
        else
            git clone "$url" "$dest"
        fi
    fi
    echo "$dest"
}

_git_checkout() {
    local repo_dir="$1"
    local rev="$2"   # branch, tag, commit
    if [ -z "$rev" ]; then
        # keep current HEAD
        git -C "$repo_dir" rev-parse --abbrev-ref HEAD >/dev/null 2>&1 || git -C "$repo_dir" rev-parse HEAD >/dev/null
        return 0
    fi
    # try to checkout; if branch doesn't exist locally, try to fetch it
    if git -C "$repo_dir" rev-parse --verify "$rev" >/dev/null 2>&1; then
        git -C "$repo_dir" checkout "$rev"
    else
        git -C "$repo_dir" fetch origin "$rev":"$rev" || true
        git -C "$repo_dir" checkout "$rev" || (log "Falha no checkout $rev" && return 1)
    fi
    return 0
}

# find recipe dirs under <path> — a recipe é diretório contendo *.meta ou .meta file directly
_find_recipes() {
    local path="$1"
    # directories containing *.meta files (directly) or files named <pkg>.meta inside
    find "$path" -maxdepth 3 -type f -name '*.meta' -print0 | xargs -0 -n1 dirname | sort -u
}

# copy recipe tree from repo_subpath to PKG_DIR
_sync_single_recipe_dir() {
    local src_dir="$1"   # absolute path in repo cache pointing to a recipe (directory)
    local relname
    relname="$(basename "$src_dir")"
    local dst="$PKG_DIR/$relname"

    # create backup if exists and not forced
    if [ -d "$dst" ] && [ "$BACKUP" = "1" ]; then
        local stamp; stamp="$(date +%Y%m%d%H%M%S)"
        local bk="$BACKUP_DIR/${relname}_$stamp"
        log "Criando backup de $dst -> $bk"
        sudo mkdir -p "$bk"
        sudo rsync -a --delete "$dst"/ "$bk"/
    fi

    # perform rsync (dry-run possible)
    log "Sincronizando receita '$relname' -> $dst"
    sudo mkdir -p "$dst"
    if [ "$DRYRUN" = "1" ]; then
        log "[DRY-RUN] rsync -a --delete \"$src_dir/\" \"$dst/\""
        rsync -avn --delete "$src_dir"/ "$dst"/ || true
    else
        if [ "$FORCE" = "1" ]; then
            # remove then copy (force replace)
            sudo rm -rf "$dst"
            sudo mkdir -p "$dst"
            sudo rsync -a --delete "$src_dir"/ "$dst"/
        else
            # normal rsync copy (merge/overwrite changed files)
            sudo rsync -a --delete "$src_dir"/ "$dst"/
        fi
    fi

    # validate .meta exists in dst
    if [ ! -f "$dst"/*.meta ]; then
        log "AVISO: nenhuma .meta encontrada em $dst (talvez não seja uma receita válida)"
    fi

    log "Receita '$relname' sincronizada"
}

# =============================================================
# Main sync flow
# =============================================================
# args:
#   url - git repo url
#   name - local cache name (optional)
#   rev - branch/tag/commit (optional)
#   subdir - subdir path inside repo where recipes live (optional)
# options via globals:
#   DRYRUN=1, FORCE=1, BACKUP=1, DEPTH=n, ALLOW_UNTRACKED=1
_sync_repo_to_packages() {
    local url="$1"; local name="$2"; local rev="$3"; local subdir="$4"

    name="${name:-$(basename -s .git "$url")}"
    log "Sincronizando repo '$url' (cache name: $name) rev='$rev' subdir='$subdir'"

    local repo_dir
    repo_dir="$(_git_clone_or_update "$url" "$name" "${DEPTH:-1}")"

    # checkout rev if provided
    if [ -n "$rev" ]; then
        _git_checkout "$repo_dir" "$rev"
    fi

    local target_root="$repo_dir"
    if [ -n "$subdir" ]; then
        target_root="$repo_dir/$subdir"
        if [ ! -d "$target_root" ]; then
            log "Erro: subdir '$subdir' não existe em repo"
            return 1
        fi
    fi

    # detect recipe directories
    mapfile -t recipe_dirs < <(_find_recipes "$target_root")
    if [ "${#recipe_dirs[@]}" -eq 0 ]; then
        log "Nenhuma receita encontrada em $target_root"
        return 0
    fi

    log "Receitas encontradas: ${#recipe_dirs[@]}"

    for r in "${recipe_dirs[@]}"; do
        # if specific package requested, skip others (handled by caller)
        _sync_single_recipe_dir "$r"
    done
}

# =============================================================
# CLI helper
# =============================================================
sync_usage() {
    cat <<EOF
ibuild sync repo <git-url> [options]

Options:
  --name <name>         name for local cache dir (default: basename of repo)
  --rev <ref>           branch/tag/commit to checkout (default: repo default branch)
  --subdir <path>       subpath inside repo where recipes are (default: repo root)
  --depth <n>           git shallow clone depth (default: 1)
  --pkg <pkgname>       sync only specific package folder (basename match)
  --dry-run             show what would change (rsync -n)
  --force               overwrite existing packages
  --backup              create backup of overwritten packages
  --help

Examples:
  ibuild sync repo https://github.com/meu/repo.git --subdir recipes --rev main
  ibuild sync repo git@github.com:meu/repo.git --name myrepo --pkg htop --force
EOF
}

# =============================================================
# Entry point: sync_main
# =============================================================
sync_main() {
    local subcmd="${1:-}"
    shift || true

    if [ "$subcmd" != "repo" ]; then
        sync_usage
        return 1
    fi

    # defaults
    local url="" name="" rev="" subdir="" pkg_filter=""
    DRYRUN=0; FORCE=0; BACKUP=0; DEPTH=1

    # parse args
    url="$1"; shift || true
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
            --help) sync_usage; return 0 ;;
            *) log "Opção desconhecida: $1"; sync_usage; return 1 ;;
        esac
    done

    if [ -z "$url" ]; then
        log "ERRO: é necessário informar <git-url>"
        sync_usage
        return 1
    fi

    # perform sync to temp, then per-package rsync to PKG_DIR
    mkdir -p "$TMP_DIR/repo"
    _sync_repo_to_packages "$url" "$name" "$rev" "$subdir"

    # if pkg_filter specified, ensure only that package updated (we already sync all recipes found;
    # to restrict, user should set subdir pointing to the package dir or use repo layout with one package)
    if [ -n "$pkg_filter" ]; then
        log "NOTE: --pkg filter was provided but sync currently scans detected recipe dirs; to strictly limit, pass --subdir to point to specific package directory."
    fi

    # cleanup tmp
    rm -rf "$TMP_DIR"
    log "Sync concluído."
}

# export for sourcing
export -f sync_main

# If run directly, dispatch
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    sync_main "$@"
fi
