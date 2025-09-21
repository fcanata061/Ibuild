# dependency.py
"""
Módulo avançado para resolução de dependências de pacotes .meta.

Funcionalidades:
- Carrega .meta via meta.load_meta
- Construção de grafo com support para:
    * dependencies (obrigatórias)
    * optional dependencies (opcionais)
    * provides (virtual packages)
    * conflicts
    * version constraints (com packaging, se disponível)
- Resolução por topo-sort (dependências primeiro), com opção reverse=True
- Detecção de ciclos com relatório do caminho
- Cache em memória por execução
- API principal: resolve(pkg_names, include_optional=False, prefer_provided=True, reverse=False)
"""

from __future__ import annotations

import os
from collections import defaultdict, deque, OrderedDict
from typing import List, Dict, Tuple, Set, Optional, Any

from ibuild1.0.modules_py import meta, log, sync

# try usar packaging para version handling; se não disponível, usamos fallback simples
try:
    from packaging.version import Version, InvalidVersion
    from packaging.specifiers import SpecifierSet, InvalidSpecifier
    _HAS_PACKAGING = True
except Exception:
    _HAS_PACKAGING = False
    # implementaremos fallback comparador simples (string equality or naive numeric split)


class DependencyError(Exception):
    pass


class CycleError(DependencyError):
    def __init__(self, cycle_path: List[str]):
        super().__init__(f"Ciclo detectado: {' -> '.join(cycle_path)}")
        self.cycle_path = cycle_path


class ConflictError(DependencyError):
    def __init__(self, pkg: str, reason: str):
        super().__init__(f"Conflito para {pkg}: {reason}")
        self.pkg = pkg
        self.reason = reason


# cache por execução para acelerar
_META_CACHE: Dict[str, dict] = {}
_PROVIDES_INDEX: Dict[str, Set[str]] = {}  # provide -> set of pkg names that provide it


def _load_meta_cached(pkg_name: str, category: Optional[str] = None) -> dict:
    key = f"{category or '_'}::{pkg_name}"
    if key in _META_CACHE:
        return _META_CACHE[key]
    m = meta.load_meta(pkg_name, category)
    _META_CACHE[key] = m
    return m


def _build_provides_index(search_categories: Optional[List[str]] = None):
    """
    Reconstrói índice de 'provides' lendo todos os metadados existentes.
    Se search_categories for None, varre todas as categories do repo.
    """
    global _PROVIDES_INDEX
    _PROVIDES_INDEX = defaultdict(set)
    repo_dir = meta.config.get("repo_dir")
    cats = search_categories or meta.list_categories()
    for cat in cats:
        try:
            pkgs = meta.list_packages(cat)
        except Exception:
            continue
        for p in pkgs:
            try:
                m = _load_meta_cached(p, cat)
            except Exception:
                continue
            provides = m.get("provides", []) or []
            for prov in provides:
                _PROVIDES_INDEX[prov].add(p)
    # converter para dict normal
    _PROVIDES_INDEX = dict(_PROVIDES_INDEX)


def _version_satisfies(candidate_version: str, constraint: Optional[str]) -> bool:
    """
    Verifica se candidate_version satisfaz constraint.
    Constraint pode ser None, string tipo '>=1.2.0,<2.0' (PEP440-ish) ou simples '1.2.0'.
    """
    if constraint in (None, "", []):
        return True
    if not _HAS_PACKAGING:
        # fallback simples: se constraint contém operadores, ignorar e exigir igualdade
        try:
            if any(op in constraint for op in [">=", "<=", ">", "<", "~", "^"]):
                # sem packaging, não suportamos specifiers complexos; retornar True apenas para evitar bloquear
                log.warn("Sem 'packaging' instalado — constraints avançadas não são avaliadas, assumindo True para '%s'", constraint)
                return True
            else:
                # igualdade simples
                return candidate_version == constraint
        except Exception:
            return True

    try:
        ver = Version(candidate_version)
    except InvalidVersion:
        log.warn("Versão inválida format: %s, tratando como string", candidate_version)
        # fallback equality
        return constraint == candidate_version

    try:
        spec = SpecifierSet(str(constraint))
    except InvalidSpecifier:
        # talvez seja "1.2.3" — tratar como ==1.2.3
        try:
            spec = SpecifierSet(f"=={constraint}")
        except Exception:
            log.warn("Constraint inválida: %s", constraint)
            return True

    return ver in spec


def _normalize_pkg_id(pkg_name: str, version: Optional[str]) -> str:
    return f"{pkg_name}@{version or '0'}"


# -----------------------------------------------------------
# Construção do grafo e resolução
# -----------------------------------------------------------

def _expand_dep_spec(dep_spec: Any) -> Tuple[str, Optional[str], bool]:
    """
    Normaliza uma entrada de dependência que pode vir em vários formatos:
    - "pkgname"
    - "pkgname==1.2.3"  (string com operador simples)
    - {"name":"pkg","version":">=1.2.0","optional": True}
    Retorna: (name, version_constraint, optional)
    """
    if isinstance(dep_spec, str):
        # checar se há operador '=='
        if "==" in dep_spec:
            name, ver = dep_spec.split("==", 1)
            return name.strip(), ver.strip(), False
        # checar se é 'name>=1.2'
        for op in [">=", "<=", ">", "<", "~=", "!="]:
            if op in dep_spec:
                # retornamos a string completa como constraint, nome antes do operador
                parts = dep_spec.split(op, 1)
                name = parts[0].strip()
                constraint = dep_spec[len(name):].strip()
                return name, constraint, False
        # senão só nome
        return dep_spec.strip(), None, False

    if isinstance(dep_spec, dict):
        name = dep_spec.get("name") or dep_spec.get("pkg") or dep_spec.get("package")
        ver = dep_spec.get("version") or dep_spec.get("constraint")
        optional = bool(dep_spec.get("optional") or dep_spec.get("optional", False))
        return name, ver, optional

    raise DependencyError(f"Formato de dependência não suportado: {dep_spec}")


def _resolve_candidate_for_provide(virtual_name: str, constraint: Optional[str]) -> Optional[Tuple[str, dict]]:
    """
    Para um 'provides' virtual, retorna um candidato (pkg_name, meta) que satisfaça a constraint.
    Estratégia: usar _PROVIDES_INDEX e escolher o pacote com maior versão que satisfaça (se houver version).
    """
    if not _PROVIDES_INDEX:
        _build_provides_index()

    candidates = _PROVIDES_INDEX.get(virtual_name, set())
    if not candidates:
        return None

    best = None
    best_ver = None
    for p in candidates:
        try:
            m = _load_meta_cached(p)
            v = str(m.get("version", "0"))
        except Exception:
            continue
        if _version_satisfies(v, constraint):
            if best is None:
                best = (p, m)
                best_ver = v
            else:
                # escolher maior versão
                if _HAS_PACKAGING:
                    try:
                        if Version(v) > Version(best_ver):
                            best = (p, m)
                            best_ver = v
                    except Exception:
                        pass
                else:
                    # fallback lexicográfico
                    if v > best_ver:
                        best = (p, m)
                        best_ver = v
    return best


def _gather_dependencies(initial_pkgs: List[str], include_optional: bool = False,
                         prefer_provided: bool = True) -> Tuple[Dict[str, dict], Dict[str, Set[str]]]:
    """
    Retorna:
      - metas: dict pkg_name -> meta dict (a versão carregada)
      - graph: adj list, chave: pkg_name, valor: set(dependents)
    Observação: grafo orientado de dependency -> dependent (para topo-sort facilitar)
    """
    metas: Dict[str, dict] = {}
    graph: Dict[str, Set[str]] = defaultdict(set)  # dependency -> set(dependent)
    # Keep track of edges for reverse traversal
    edges_forward: Dict[str, Set[str]] = defaultdict(set)  # dependent -> set(dependency)

    queue = deque(initial_pkgs)
    seen: Set[str] = set()

    while queue:
        pname = queue.popleft()
        if pname in seen:
            continue
        seen.add(pname)

        # support if initial name refers to virtual provided name: try direct load first, else treat as virtual
        try:
            m = _load_meta_cached(pname)
            metas[pname] = m
        except Exception:
            # tentar provided candidates (virtual)
            if prefer_provided:
                cand = _resolve_candidate_for_provide(pname, None)
                if cand:
                    real_name, real_meta = cand
                    metas[real_name] = real_meta
                    pname = real_name  # continuar com o real
                else:
                    raise DependencyError(f"Pacote ou provide não encontrado: {pname}")
            else:
                raise DependencyError(f"Pacote não encontrado: {pname}")

        # obter dependências do meta
        mdeps = metas[pname].get("dependencies", []) or []
        # algumas metas podem ter 'optional' grouped or marked; já suportado pelo _expand_dep_spec
        for raw in mdeps:
            dep_name, constraint, optional = _expand_dep_spec(raw)
            if optional and not include_optional:
                log.debug("Pulando optional dep %s para %s", dep_name, pname)
                continue

            # se o dep_name é virtual (colocar aqui a heurística)
            resolved_name = None
            try:
                # tentar achar pacote com esse nome
                _ = _load_meta_cached(dep_name)
                resolved_name = dep_name
            except Exception:
                # não existe pacote com esse nome — tentar provides
                candidate = _resolve_candidate_for_provide(dep_name, constraint)
                if candidate:
                    resolved_name = candidate[0]
                    metas[resolved_name] = candidate[1]
                else:
                    raise DependencyError(f"Dependência não encontrada: {dep_name} (requerido por {pname})")

            # checar versão
            if constraint and not _version_satisfies(str(metas[resolved_name].get("version", "0")), constraint):
                raise ConflictError(resolved_name, f"Versão {metas[resolved_name].get('version')} não satisfaz {constraint} (requerido por {pname})")

            # registrar aresta dependency -> dependent
            graph[resolved_name].add(pname)
            edges_forward[pname].add(resolved_name)

            # enfileirar dependency para expandir suas próprias deps
            if resolved_name not in seen:
                queue.append(resolved_name)

    return metas, graph


def _topo_sort(graph: Dict[str, Set[str]]) -> List[str]:
    """
    Recebe um grafo orientado dependency -> set(dependents)
    e retorna uma lista topológica onde dependências aparecem antes dos dependents.
    Algoritmo baseado em Kahn, invertendo sentido para facilitar.
    """
    # construir in-degree (usando edges reversed)
    indeg = defaultdict(int)
    nodes = set()
    for dep, dependents in graph.items():
        nodes.add(dep)
        for d in dependents:
            nodes.add(d)
            indeg[d] += 1
    # nodes with indeg 0 are leaves (no one depends on them) -> but queremos nodes que have no dependencies?
    # dado nosso grafo dependency -> dependents, indeg counts how many dependencies point to node as dependent.
    q = deque([n for n in nodes if indeg[n] == 0])
    ordered: List[str] = []

    processed = 0
    # For Kahn we need also reverse adjacency: node -> dependencies
    reverse_adj = defaultdict(set)
    for dep, dependents in graph.items():
        for d in dependents:
            reverse_adj[d].add(dep)

    while q:
        n = q.popleft()
        ordered.append(n)
        processed += 1
        # remover n das arestas -> para cada dependent of n, diminuir indeg
        for dependent in graph.get(n, []):
            indeg[dependent] -= 1
            if indeg[dependent] == 0:
                q.append(dependent)

    if processed != len(nodes):
        # há ciclo: identificar caminho
        # fallback simples: encontrar ciclo por DFS
        cycle = _find_cycle(nodes, graph)
        raise CycleError(cycle)

    # ordered: dependencies-first (mais básicos primeiro), adequado para build
    return ordered


def _find_cycle(nodes: Set[str], graph: Dict[str, Set[str]]) -> List[str]:
    """
    Detecta um ciclo no grafo; retorna um ciclo como lista de nodes.
    Usa DFS com color marking.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    parent = {}

    def dfs(u):
        color[u] = GREY
        for v in graph.get(u, []):
            if color.get(v, WHITE) == WHITE:
                parent[v] = u
                res = dfs(v)
                if res:
                    return res
            elif color.get(v) == GREY:
                # ciclo encontrado: reconstruir caminho v -> ... -> v
                path = [v]
                cur = u
                while cur != v and cur in parent:
                    path.append(cur)
                    cur = parent[cur]
                path.append(v)
                path.reverse()
                return path
        color[u] = BLACK
        return None

    for n in nodes:
        if color[n] == WHITE:
            p = dfs(n)
            if p:
                return p
    return ["<unknown-cycle>"]


# -----------------------------------------------------------
# API pública
# -----------------------------------------------------------

def resolve(pkg_names: List[str],
            include_optional: bool = False,
            prefer_provided: bool = True,
            reverse: bool = False) -> Tuple[List[str], Dict[str, dict]]:
    """
    Resolve dependências para a(s) pacotes dado(s) em pkg_names.

    Retorna:
      - ordered_ids: lista de pkg identifiers (pkg_name) na ordem de build/install.
          Por padrão: dependências aparecem antes de quem depende (útil para build).
          Se reverse=True, a lista é invertida.
      - metas: dict pkg_name -> meta dict (com campo _pkg_dir, _meta_path, _patches)

    Parâmetros:
      - include_optional: se True, considera optional deps
      - prefer_provided: se True, tenta mapear nomes virtuais para provedores
      - reverse: se True, inverte a ordem final
    """
    if not pkg_names:
        return [], {}

    log.info("Iniciando resolução de dependências para: %s", ", ".join(pkg_names))
    # reconstruir index de provides para garantir fresh
    _build_provides_index()

    metas, graph = _gather_dependencies(pkg_names, include_optional=include_optional, prefer_provided=prefer_provided)
    log.debug("Grafo construído com %d nós", len(metas))

    ordered = _topo_sort(graph)
    # filtrar apenas metas que carregamos (pode haver nodes vazios)
    # garantir que todos initial pkg_names estejam incluídos
    final = [n for n in ordered if n in metas]

    # append initial packages if missing
    for p in pkg_names:
        # se p era virtual e mapeado para real, encontrar o real na metas
        if p not in metas:
            # procurar provider
            cand = _resolve_candidate_for_provide(p, None)
            if cand:
                real = cand[0]
                if real not in final:
                    final.append(real)
        else:
            if p not in final:
                final.append(p)

    if reverse:
        final.reverse()

    log.info("Resolveu ordem com %d pacotes", len(final))
    return final, metas


def list_dependency_tree(pkg_name: str, metas: Optional[Dict[str, dict]] = None) -> Dict[str, List[str]]:
    """
    Retorna uma representação do dependency tree:
      { pkg: [list of dependencies] }
    Usa metas se fornecido, senão carrega o meta para pkg_name.
    """
    if metas is None:
        metas = {}

    def build_for(p):
        if p in metas:
            m = metas[p]
        else:
            m = _load_meta_cached(p)
            metas[p] = m
        deps = []
        for raw in m.get("dependencies", []) or []:
            name, _, optional = _expand_dep_spec(raw)
            if optional:
                continue
            # resolve provide if needed
            try:
                _ = _load_meta_cached(name)
                deps.append(name)
            except Exception:
                cand = _resolve_candidate_for_provide(name, None)
                if cand:
                    deps.append(cand[0])
        return deps

    tree = {}
    # breadth-first gather starting at pkg_name
    queue = deque([pkg_name])
    seen = set()
    while queue:
        p = queue.popleft()
        if p in seen:
            continue
        seen.add(p)
        tree[p] = build_for(p)
        for d in tree[p]:
            if d not in seen:
                queue.append(d)
    return tree


def has_conflict(pkg_list: List[str], metas: Dict[str, dict]) -> Optional[Tuple[str, str]]:
    """
    Verifica conflitos básicos (campo 'conflicts' em metas).
    Retorna (pkg, conflict_with) se encontrar, senão None.
    """
    present = set(pkg_list)
    for p in pkg_list:
        m = metas.get(p)
        if not m:
            continue
        for c in m.get("conflicts", []) or []:
            # c pode ser nome ou dict
            cname, _, _ = _expand_dep_spec(c)
            # se conflict refers to a provide, expand providers
            if cname in present:
                return (p, cname)
            # check provides: if any present package provides cname
            provs = _PROVIDES_INDEX.get(cname, set())
            if provs & present:
                return (p, list(provs & present)[0])
    return None
