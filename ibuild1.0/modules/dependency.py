# dependency.py (versão evoluída: busca com backtracking, heurísticas, quebra de ciclos)
"""
Resolução avançada de dependências para Ibuild.

Principais características:
- Formatos suportados em .meta dependencies:
    - "pkg"
    - "pkg==1.2.3"
    - "pkg>=1.2"
    - {"name":"pkg", "version":">=1.2", "optional": True}
    - ["alt1", "alt2==2.0", {..}]  -> alternativas (qualquer uma satisfaz)
- Supports "provides", "conflicts"
- Usa packaging para versão quando disponível, fallback simples caso contrário.
- Monta grafo e usa backtracking/Memo para encontrar conjunto consistente.
- Tenta resolver ciclos: se ciclo detectado, tenta:
    1) usar optional deps para romper;
    2) escolher diferentes provedores;
    3) falha com explicação detalhada.
- API principal: resolve(pkgs, include_optional=False, prefer_provided=True, reverse=False)
"""

from __future__ import annotations
import os
import logging
from collections import defaultdict, deque, OrderedDict
from typing import List, Dict, Tuple, Set, Optional, Any

from ibuild1.0.modules_py import meta, log as iblog, utils, sync

# tentar usar packaging para version handling
try:
    from packaging.version import Version, InvalidVersion
    from packaging.specifiers import SpecifierSet, InvalidSpecifier
    _HAS_PACKAGING = True
except Exception:
    _HAS_PACKAGING = False

# logging local
logger = iblog.get_logger("dependency")

# Erros
class DependencyError(Exception):
    pass

class CycleError(DependencyError):
    def __init__(self, cycle_path: List[str]):
        super().__init__(f"Ciclo detectado: {' -> '.join(cycle_path)}")
        self.cycle_path = cycle_path

class ConflictError(DependencyError):
    def __init__(self, reason: str):
        super().__init__(f"Conflito: {reason}")
        self.reason = reason

# caches por execução
_META_CACHE: Dict[str, dict] = {}
_PROVIDES_INDEX: Dict[str, Set[str]] = {}

def _load_meta_cached(pkg_name: str, category: Optional[str] = None) -> dict:
    key = f"{category or '_'}::{pkg_name}"
    if key in _META_CACHE:
        return _META_CACHE[key]
    m = meta.load_meta(pkg_name, category)
    _META_CACHE[key] = m
    return m

def _build_provides_index(force: bool=False):
    global _PROVIDES_INDEX
    if _PROVIDES_INDEX and not force:
        return
    _PROVIDES_INDEX = defaultdict(set)
    repo = meta.config.get("repo_dir")
    try:
        cats = meta.list_categories()
    except Exception:
        cats = []
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
            provs = m.get("provides", []) or []
            for pr in provs:
                _PROVIDES_INDEX[pr].add(p)
    _PROVIDES_INDEX = dict(_PROVIDES_INDEX)

def _version_satisfies(candidate_version: str, constraint: Optional[str]) -> bool:
    if not constraint or constraint == "":
        return True
    if _HAS_PACKAGING:
        try:
            ver = Version(str(candidate_version))
            spec = SpecifierSet(str(constraint))
            return ver in spec
        except Exception:
            # fallback para igualdade
            return str(candidate_version) == str(constraint)
    else:
        # sem packing: tratar "==" e simples comparações lexicográficas básicas
        if "==" in constraint:
            return str(candidate_version) == constraint.split("==",1)[1].strip()
        # se operadores complexos, aceitar e avisar (não bloquear)
        if any(op in constraint for op in [">=", "<=", ">", "<", "~=", "!="]):
            logger.warn("Sem 'packaging' — constraints complexas não avaliadas, assumindo True para constraint '%s'", constraint)
            return True
        return str(candidate_version) == str(constraint)

def _expand_dep_item(item: Any) -> List[Tuple[str, Optional[str], bool]]:
    """
    Aceita:
    - str -> "pkg" ou "pkg==1.2"
    - dict -> {"name":.., "version":.., "optional": bool}
    - list -> alternativas: cada entry processada e retornada como alternativa
    Retorna lista de (name, version_constraint_or_None, optional_flag)
    Se forem alternativas (lista), cada alternativa aparece separada.
    """
    if isinstance(item, str):
        # detectar operadores simples
        if "==" in item:
            name, ver = item.split("==",1)
            return [(name.strip(), ver.strip(), False)]
        for op in [">=", "<=", ">", "<", "~=", "!="]:
            if op in item:
                # guardar como constraint inteira (ex: ">=1.2,<2.0")
                # buscar nome até primeiro espaço/op
                # simplificar: separar nome pelo primeiro caractere que não seja alfanum/._- 
                # mas aqui assumimos "name<op>..."
                # tentar split por primeira aparição de op (pode haver composições)
                # fallback: assume name antes do op
                idx = item.find(op)
                name = item[:idx].strip()
                constraint = item[len(name):].strip()
                return [(name, constraint, False)]
        return [(item.strip(), None, False)]

    if isinstance(item, dict):
        name = item.get("name") or item.get("pkg") or item.get("package")
        constraint = item.get("version") or item.get("constraint")
        optional = bool(item.get("optional", False))
        if not name:
            raise DependencyError(f"Dependência dict sem 'name': {item}")
        return [(name, constraint, optional)]

    if isinstance(item, list):
        res = []
        for alt in item:
            res.extend(_expand_dep_item(alt))
        return res

    raise DependencyError(f"Formato de dependência inválido: {item}")

def _resolve_virtual(name: str, constraint: Optional[str]) -> Optional[Tuple[str, dict]]:
    """
    Para um nome virtual (provide), retorna um candidato (pkgname, meta) que satisfaça constraint.
    Estratégia: procura providers e escolhe o de maior versão compatível.
    """
    if not _PROVIDES_INDEX:
        _build_provides_index()
    providers = _PROVIDES_INDEX.get(name, set())
    if not providers:
        return None
    best = None
    best_ver = None
    for p in providers:
        try:
            m = _load_meta_cached(p)
        except Exception:
            continue
        v = str(m.get("version", "0"))
        if _version_satisfies(v, constraint):
            if best is None:
                best = (p, m)
                best_ver = v
            else:
                try:
                    if _HAS_PACKAGING and Version(v) > Version(best_ver):
                        best = (p, m); best_ver = v
                    elif not _HAS_PACKAGING and v > best_ver:
                        best = (p, m); best_ver = v
                except Exception:
                    pass
    return best

# ---------------------------
# Backtracking resolver
# ---------------------------

def _normalize_initial(pkgs: List[str]) -> List[str]:
    # se passar pkg@ver, separar (não usado aqui por ora). Mantemos nomes simples.
    return list(pkgs)

def _collect_all_candidates(name: str, constraint: Optional[str], prefer_provided: bool) -> List[Tuple[str, dict]]:
    """
    Retorna lista de candidatos reais para o requisito `name`.
    1) tenta pacote literal com esse nome (se existir)
    2) se falhar e prefer_provided=True, tenta providers (virtual)
    3) retorna lista possivelmente vazia
    """
    candidates = []
    try:
        m = _load_meta_cached(name)
        candidates.append((name, m))
    except Exception:
        # não existe pacote literal
        pass

    # se constraint não satisfeita pelo literal, ele será filtrado depois
    if prefer_provided:
        prov = _resolve_virtual(name, constraint)
        if prov:
            # prov é single best candidate; também varrer todos providers poderia ser mais completo.
            candidates.append(prov)
        else:
            # tentar listar TODOS providers (não apenas melhor) para backtracking
            if _PROVIDES_INDEX:
                all_provs = _PROVIDES_INDEX.get(name, set())
                for p in all_provs:
                    try:
                        candidates.append((p, _load_meta_cached(p)))
                    except Exception:
                        continue
    return candidates

def _satisfies_conflicts(chosen_set: Set[str], candidate_name: str, candidate_meta: dict) -> Optional[str]:
    """
    Verifica se ao adicionar candidate_name ao conjunto chosen_set causa conflito.
    Retorna None se ok, ou string com razão do conflito se houver.
    Usa campo 'conflicts' nas metas.
    """
    # verificar conflicts do candidate contra os já escolhidos
    for c in candidate_meta.get("conflicts", []) or []:
        # c pode ser string/dict/list; expandir
        for nm, _, _ in _expand_dep_item(c):
            # se nome exato presente, conflito
            if nm in chosen_set:
                return f"{candidate_name} conflita com {nm}"
            # se nm for virtual e algum chosen provide isso, também conflito
            provs = _PROVIDES_INDEX.get(nm, set())
            if provs & chosen_set:
                return f"{candidate_name} conflita com provider {list(provs & chosen_set)[0]} (virtual {nm})"
    # verificar conflicts existentes no chosen_set contra o candidate
    for existing in list(chosen_set):
        try:
            m = _load_meta_cached(existing)
        except Exception:
            continue
        for c in m.get("conflicts", []) or []:
            for nm, _, _ in _expand_dep_item(c):
                if nm == candidate_name:
                    return f"{existing} conflita com {candidate_name}"
                provs = _PROVIDES_INDEX.get(nm, set())
                if candidate_name in provs:
                    return f"{existing} conflita com provider {candidate_name} (virtual {nm})"
    return None

def _candidate_version_ok(candidate_meta: dict, constraint: Optional[str]) -> bool:
    if not constraint:
        return True
    return _version_satisfies(str(candidate_meta.get("version", "0")), constraint)

def _gather_deps_from_meta(pkg_name: str, pkg_meta: dict, include_optional: bool) -> List[Any]:
    """Retorna lista bruta de dependências do meta (como declaradas)"""
    deps = pkg_meta.get("dependencies", []) or []
    # alguns projetos usam 'depends' ou 'requires' alternativamente
    if not deps:
        for alt in ("depends", "requires"):
            if alt in pkg_meta:
                deps = pkg_meta[alt] or []
                break
    # filtrar optional aqui? não — manter para avaliação no resolver
    return deps

def _resolve_with_backtracking(initial: List[str],
                               include_optional: bool = False,
                               prefer_provided: bool = True,
                               max_depth: int = 1000) -> Tuple[List[str], Dict[str, dict]]:
    """
    Algoritmo central:
    - tentativa de construir chosen_set (set de nomes reais) que satisfaça todas dependências.
    - trabalha em DFS: pegar próxima necessidade não satisfeita, tentar candidatos (literal + providers).
    - usa memo (failed states) para podar.
    - se ciclo detectado, tenta heurísticas para romper (usar optional, trocar provider).
    Retorna (ordered_list, metas) se sucesso, ou lança DependencyError/ConflictError/CycleError.
    """

    _build_provides_index()  # garantir index
    metas: Dict[str, dict] = {}
    # chosen_set: pacotes reais selecionados
    chosen_set: Set[str] = set()
    # requirements: mapping pkg -> list of raw deps (for later expansion)
    requirements: Dict[str, List[Any]] = {}

    # prepare initial metas & queue
    initial_norm = _normalize_initial(initial)
    queue = deque(initial_norm)

    # pre-load initial meta candidates (if virtual, will resolve later)
    while queue:
        name = queue.popleft()
        # if exists literal, add to metas, else leave to resolver as virtual
        try:
            m = _load_meta_cached(name)
            metas[name] = m
        except Exception:
            # virtual, do nothing for now
            pass

    # We'll maintain a list of "needs": each need is tuple(name, constraint, required_by, optional_flag)
    # Start by converting initial inputs to needs (name may be virtual)
    initial_needs: List[Tuple[str, Optional[str], Optional[str], bool]] = []
    for name in initial_norm:
        initial_needs.append((name, None, None, False))

    # helper structures for backtracking
    memo_failed_states: Set[str] = set()  # fingerprint of states that falharam
    # fingerprint: sorted chosen_set + sorted remaining needs

    def fingerprint(chosen: Set[str], needs: List[Tuple[str, Optional[str], Optional[str], bool]]) -> str:
        return "|".join(sorted(chosen)) + "::" + "|".join(f"{n}:{c}:{opt}" for (n,c,_,opt) in needs)

    # build function to expand needs from a chosen package
    def expand_needs_from_pkg(name: str) -> List[Tuple[str, Optional[str], str, bool]]:
        # returns list of (dep_name, constraint, required_by, optional_flag)
        try:
            m = metas.get(name) or _load_meta_cached(name)
            metas[name] = m
        except Exception:
            raise DependencyError(f"Meta não encontrado para {name}")

        deps_raw = _gather_deps_from_meta(name, metas[name], include_optional)
        res = []
        for raw in deps_raw:
            for dep_name, constraint, optional in _expand_dep_item(raw):
                if optional and not include_optional:
                    continue
                # append
                res.append((dep_name, constraint, name, optional))
        return res

    # main DFS search
    def dfs(needs: List[Tuple[str, Optional[str], Optional[str], bool]], chosen: Set[str], depth: int=0) -> bool:
        if depth > max_depth:
            raise DependencyError("Profundidade máxima de resolução atingida")

        # limpar necessidades já satisfeitas
        new_needs = []
        for (name, constraint, req_by, optional) in needs:
            # Se name já foi escolhido (ou provided por chosen), validar versão constraint
            satisfied = False
            # se name é literal e está em chosen, verificar versão
            if name in chosen:
                m = _load_meta_cached(name)
                if _candidate_version_ok(m, constraint):
                    satisfied = True
                else:
                    # chosen package não satisfaz constraint -> esta necessidade falha
                    return False
            else:
                # se any chosen provides 'name'
                provs = _PROVIDES_INDEX.get(name, set())
                if provs & chosen:
                    # verificar provider version
                    prov_name = list(provs & chosen)[0]
                    pm = _load_meta_cached(prov_name)
                    if _candidate_version_ok(pm, constraint):
                        satisfied = True
                    else:
                        return False
            if not satisfied:
                new_needs.append((name, constraint, req_by, optional))

        needs = new_needs

        if not needs:
            return True  # todas satisfeitas

        # fingerprint check
        fp = fingerprint(chosen, needs)
        if fp in memo_failed_states:
            return False

        # escolher next need — heurística: pick the one with fewest candidates (MRV)
        best_need = None
        best_cands = None
        for need in needs:
            name, constraint, req_by, optional = need
            cands = _collect_all_candidates(name, constraint, prefer_provided=True)
            # filtrar por version ok
            cands = [(n,m) for (n,m) in cands if _candidate_version_ok(m, constraint)]
            # se none e optional -> pular
            if not cands and optional:
                continue
            if best_need is None or len(cands) < len(best_cands):
                best_need = need
                best_cands = cands

        if best_need is None:
            # nenhum candidato encontrado para necessidades não opcionais -> falha
            # compilar mensagem de erro
            unsat = ", ".join(f"{n}" for (n,_,_,opt) in needs if not opt)
            memo_failed_states.add(fp)
            raise DependencyError(f"Dependência(s) não satisfeita(s): {unsat}")

        # tentar candidatos em ordem preferencial (ordenar por versão decrescente)
        name, constraint, req_by, optional = best_need
        cands = best_cands or []
        # heurística: ordena por versão desc (preferir mais recente)
        def cand_key(pair):
            try:
                v = str(pair[1].get("version","0"))
                if _HAS_PACKAGING:
                    return Version(v)
                return v
            except Exception:
                return str(pair[0])
        cands.sort(key=lambda p: cand_key(p), reverse=True)

        # se houver nenhuma candidato e optional True -> pular
        if not cands:
            if optional:
                # consider as satisfied by skipping
                remaining = [x for x in needs if x != best_need]
                return dfs(remaining, chosen, depth+1)
            memo_failed_states.add(fp)
            return False

        # tentar cada candidato
        for candidate_name, candidate_meta in cands:
            # checar conflitos imediatos
            conflict_reason = _satisfies_conflicts(chosen, candidate_name, candidate_meta)
            if conflict_reason:
                logger.debug("Candidato %s rejeitado por conflito: %s", candidate_name, conflict_reason)
                continue

            # adiciona candidato e expande suas próprias dependências
            chosen.add(candidate_name)
            # carregar suas deps
            new_reqs = []
            try:
                deps_of_candidate = _gather_deps_from_meta(candidate_name, candidate_meta, include_optional)
                for raw in deps_of_candidate:
                    for dname, dconstraint, dopt in _expand_dep_item(raw):
                        if dopt and not include_optional:
                            continue
                        new_reqs.append((dname, dconstraint, candidate_name, dopt))
                # construir next needs: (needs - best_need) + new_reqs
                next_needs = [n for n in needs if n != best_need] + new_reqs
                # check cycles: if candidate is equal to req_by or candidate causes trivial cycle
                # DFS recursion
                ok = dfs(next_needs, chosen, depth+1)
                if ok:
                    return True
            except CycleError as ce:
                # tentativa de quebrar ciclo: se alguma dependência é optional, tentar pular
                logger.debug("CycleError detectado ao tentar candidato %s: %s", candidate_name, ce)
                # continuar tentando outros candidatos
                pass
            except DependencyError as de:
                logger.debug("DependencyError ao expandir %s: %s", candidate_name, de)
                # falhou, tentar próximo candidato
                pass

            # rollback
            chosen.discard(candidate_name)

        # nenhum candidato funcionou -> memoize e retornar False
        memo_failed_states.add(fp)
        return False

    # Start search
    initial_needs = []
    for n in initial_norm:
        initial_needs.append((n, None, None, False))
    success = dfs(initial_needs, chosen_set)
    if not success:
        raise DependencyError("Não foi possível resolver dependências com as heurísticas atuais")

    # se sucesso, construir metas dict para todos chosen_set
    result_metas = {}
    for p in chosen_set:
        result_metas[p] = _load_meta_cached(p)

    # ordenar por topo-sort (dependências primeiro)
    # construir graph dependency->dependents para chosen_set
    graph = defaultdict(set)
    for p in chosen_set:
        m = result_metas[p]
        for raw in _gather_deps_from_meta(p, m, include_optional):
            for dname, dconstraint, dopt in _expand_dep_item(raw):
                # map to real chosen provider
                if dname in chosen_set:
                    graph[dname].add(p)
                else:
                    # if provided by chosen_set
                    provs = _PROVIDES_INDEX.get(dname, set())
                    inter = provs & chosen_set
                    if inter:
                        # use one provider (arbitrarily)
                        graph[list(inter)[0]].add(p)

    # topo sort
    ordered = _topo_sort(graph)

    # filtrar apenas chosen_set e manter ordem
    final_order = [n for n in ordered if n in chosen_set]

    return final_order, result_metas

# -----------------------------------------------------
# utilitários: topo sort / ciclo detect / explain
# -----------------------------------------------------
def _topo_sort(graph: Dict[str, Set[str]]) -> List[str]:
    # graph: dependency -> set(dependents)
    indeg = defaultdict(int)
    nodes = set()
    for dep, depset in graph.items():
        nodes.add(dep)
        for d in depset:
            nodes.add(d)
            indeg[d] += 1
    q = deque([n for n in nodes if indeg[n] == 0])
    out = []
    processed = 0
    while q:
        n = q.popleft()
        out.append(n)
        processed += 1
        for dependent in graph.get(n, []):
            indeg[dependent] -= 1
            if indeg[dependent] == 0:
                q.append(dependent)
    if processed != len(nodes):
        cycle = _find_cycle(nodes, graph)
        raise CycleError(cycle)
    return out

def _find_cycle(nodes: Set[str], graph: Dict[str, Set[str]]) -> List[str]:
    WHITE, GREY, BLACK = 0,1,2
    color = {n: WHITE for n in nodes}
    parent = {}
    def dfs(u):
        color[u] = GREY
        for v in graph.get(u, []):
            if color[v] == WHITE:
                parent[v] = u
                res = dfs(v)
                if res:
                    return res
            elif color[v] == GREY:
                # reconstruir ciclo
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
    return ["<unknown>"]

def explain_failure(pkgs: List[str], include_optional=False, prefer_provided=True) -> str:
    """
    Rodar resolução e retornar string explicativa se falhar.
    """
    try:
        resolve(pkgs, include_optional=include_optional, prefer_provided=prefer_provided)
        return "Resolved successfully (no failure to explain)."
    except Exception as e:
        return f"Falha ao resolver: {e}"

# -----------------------------------------------------
# API pública
# -----------------------------------------------------
def resolve(pkgs: List[str],
            include_optional: bool = False,
            prefer_provided: bool = True,
            reverse: bool = False) -> Tuple[List[str], Dict[str, dict]]:
    """
    Resolve dependências complexas usando backtracking. Retorna (ordered_list, metas).
    ordered_list: dependências primeiro (pronto para build). Se reverse=True, inverte.
    """
    if not pkgs:
        return [], {}
    # garantir index de provides
    _build_provides_index(force=True)
    ordered, metas = _resolve_with_backtracking(pkgs, include_optional=include_optional, prefer_provided=prefer_provided)
    if reverse:
        ordered.reverse()
    logger.info("Resolve final: %s", ", ".join(ordered))
    return ordered, metas
