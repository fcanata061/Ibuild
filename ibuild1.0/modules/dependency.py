#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
modules/dependency.py — Resolver de dependências evoluído para Ibuild

Funcionalidades principais:
- Parsing de especificadores de versão (usa 'packaging' quando disponível)
- Modelo PackageRequirement / PackageCandidate
- Index persistente de "provides" (lib/virtual -> pacotes)
- Resolução com backtracking + heurísticas (prioriza versões estáveis, providers locais)
- Lockfile (dependency.lock.json) para reprodução das resoluções
- Interface de diagnóstico (explain) e verbose tracing
- Ordenação topológica final (build/install order)
- Proteções contra explosion do espaço de busca com timeouts e limits
- API compatível: DependencyResolver.resolve(requests) -> ResolveResult
"""

from __future__ import annotations
import os
import json
import time
import shutil
import math
import heapq
import logging
from dataclasses import dataclass, field
from typing import (
    Optional, List, Dict, Tuple, Set, Iterable, Any, Callable
)

# Tenta usar packaging.version/SpecifierSet para comparações robustas
try:
    from packaging.version import Version, InvalidVersion
    from packaging.specifiers import SpecifierSet, InvalidSpecifier
    HAS_PACKAGING = True
except Exception:
    HAS_PACKAGING = False

# logger local
logger = logging.getLogger("ibuild.dependency")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------
# Helpers de versão e spec parsing
# ---------------------------------------------------------------------

def parse_version(v: str):
    """Tenta parsear versão com packaging, fallback para string."""
    if not v:
        return None
    if HAS_PACKAGING:
        try:
            return Version(v)
        except InvalidVersion:
            # fallback: keep raw string
            return v
    return v

def parse_specifier(spec: str):
    """
    Retorna um SpecifierSet se packaging disponível, ou uma simple callable fallback.
    """
    if not spec:
        return None
    if HAS_PACKAGING:
        try:
            return SpecifierSet(spec)
        except InvalidSpecifier:
            return None
    # fallback rudimentar: supports "==x", ">=x", "<=x", "<x", ">x"
    ops = []
    for part in spec.split(","):
        part = part.strip()
        if part.startswith("=="):
            ops.append(("==", part[2:]))
        elif part.startswith(">="):
            ops.append((">=", part[2:]))
        elif part.startswith("<="):
            ops.append(("<=", part[2:]))
        elif part.startswith(">"):
            ops.append((">", part[1:]))
        elif part.startswith("<"):
            ops.append(("<", part[1:]))
        else:
            ops.append(("==", part))
    def checker(ver) -> bool:
        # if packaging not available, compare lexicographically
        for op, vv in ops:
            if op == "==":
                if str(ver) != vv: return False
            elif op == ">=":
                if str(ver) < vv: return False
            elif op == "<=":
                if str(ver) > vv: return False
            elif op == ">":
                if str(ver) <= vv: return False
            elif op == "<":
                if str(ver) >= vv: return False
        return True
    return checker

def spec_matches_version(spec, version: Optional[str]) -> bool:
    """Testa se version satisfaz spec (SpecifierSet ou callable)."""
    if spec is None:
        return True
    if version is None:
        return False
    if HAS_PACKAGING and isinstance(spec, SpecifierSet):
        try:
            return parse_version(version) in spec
        except Exception:
            return False
    if callable(spec):
        return spec(version)
    # fallback simple equality
    return str(spec) == str(version)

# ---------------------------------------------------------------------
# Modelos de dados
# ---------------------------------------------------------------------

@dataclass
class PackageCandidate:
    """
    Representa uma opção concreta do pacote disponível no repositório:
      name: nome do pacote (unique id)
      version: versão string
      provides: lista de provides (virtuals / libs)
      depends: lista de requirements (tuples ou strings)
      conflicts: lista de pacotes conflitantes
      meta: dicionário cru do .meta (opcional)
    """
    name: str
    version: Optional[str] = None
    provides: List[str] = field(default_factory=list)
    depends: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    optional: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def id(self) -> str:
        return f"{self.name}-{self.version}" if self.version else self.name

    def satisfies(self, requirement: "PackageRequirement") -> bool:
        """Verifica se este candidate satisfaz um dado PackageRequirement."""
        if requirement.name != self.name and requirement.name not in self.provides and requirement.name not in self.meta.get("provides", []):
            return False
        if requirement.specifier is None:
            return True
        return spec_matches_version(requirement.specifier, self.version)

@dataclass
class PackageRequirement:
    """
    Expressa uma necessidade: nome (pode ser virtual), e optional specifier string.
    Ex: name="libfoo.so", specifier=">=1.2,<2.0"
    """
    name: str
    raw: str = ""   # raw textual form
    specifier: Optional[Any] = None  # SpecifierSet or callable

    @classmethod
    def from_string(cls, s: str) -> "PackageRequirement":
        """Cria um PackageRequirement a partir de uma string tipo 'name>=1.2,<2.0'."""
        s = (s or "").strip()
        if not s:
            raise ValueError("Empty requirement string")
        # heurística: split first non-alpha char from name to spec
        # but better: split at first space or first one of comparator chars
        # try to find comparator
        comps = ["==", ">=", "<=", ">", "<", "~=", "!="]
        idx = None
        for c in comps:
            pos = s.find(c)
            if pos != -1:
                idx = pos
                break
        if idx is None:
            # maybe 'name spec' or just 'name'
            if " " in s:
                parts = s.split(None, 1)
                name = parts[0].strip()
                spec = parts[1].strip()
                spec_parsed = parse_specifier(spec)
                return cls(name=name, raw=s, specifier=spec_parsed)
            else:
                return cls(name=s, raw=s, specifier=None)
        # find where spec starts by scanning until comparator char occurrence
        # fallback: separate letters/digits from others
        # simple approach: name chars are alnum, dash, underscore, dot
        name = ""
        spec = ""
        for i, ch in enumerate(s):
            if ch.isalnum() or ch in "-_.":
                name += ch
            else:
                spec = s[i:].strip()
                break
        spec_parsed = parse_specifier(spec)
        return cls(name=name, raw=s, specifier=spec_parsed)

# ---------------------------------------------------------------------
# Storage / index (provides index and candidate index)
# ---------------------------------------------------------------------

class RepoIndex:
    """
    Index simples para os pacotes disponíveis no repositório.

    - candidates_by_name: name -> list[PackageCandidate] (different versions)
    - provides_index: provide_name -> set(package_name)
    - persistent backing (json) para provides_index e candidate metadata (light)
    """

    def __init__(self, repo_dir: Optional[str] = None, index_file: Optional[str] = None):
        self.repo_dir = repo_dir or (os.getenv("IBUILD_REPO_DIR") or "/usr/ibuild")
        self.index_file = index_file or os.path.join(self.repo_dir, "dependency_index.json")
        self.candidates_by_name: Dict[str, List[PackageCandidate]] = {}
        self.provides_index: Dict[str, Set[str]] = {}
        self._loaded = False
        # Try to load persistent index if present
        self.load()

def _safe_load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None
        # Parte 2/3 — continua RepoIndex com construção de índice e seleção

# continue class RepoIndex methods
def _candidate_from_meta(meta: Dict[str, Any], name_hint: Optional[str] = None) -> PackageCandidate:
    """
    Converte dicionário meta (carregado de .meta) para PackageCandidate.
    Espera que meta contenha ao menos 'name' e opcionalmente 'version'.
    """
    name = meta.get("name") or name_hint or meta.get("pkg") or meta.get("package")
    version = meta.get("version")
    provides = meta.get("provides", []) or []
    # normalize provides as strings
    provides = [str(p) for p in provides]
    depends = meta.get("depends", []) or meta.get("dependencies", []) or []
    depends = [str(d) for d in depends]
    optional = meta.get("optional_dependencies", []) or meta.get("optional", []) or []
    conflicts = meta.get("conflicts", []) or []
    return PackageCandidate(
        name=str(name),
        version=str(version) if version is not None else None,
        provides=provides,
        depends=depends,
        conflicts=[str(c) for c in conflicts],
        optional=[str(o) for o in optional],
        meta=meta
    )

def _parse_meta_file(path: str) -> Optional[PackageCandidate]:
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            m = yaml.safe_load(fh)
        if not isinstance(m, dict):
            return None
        return _candidate_from_meta(m)
    except Exception:
        # try json
        try:
            with open(path, "r", encoding="utf-8") as fh:
                m = json.load(fh)
            if isinstance(m, dict):
                return _candidate_from_meta(m)
        except Exception:
            return None

# patching RepoIndex methods into class
def _repoindex_load(self: RepoIndex):
    if self._loaded:
        return
    # try load index file
    data = _safe_load_json(self.index_file)
    if data and isinstance(data, dict):
        try:
            # reconstruct provides_index and candidates (minimal)
            prov = data.get("provides_index", {})
            self.provides_index = {k: set(v) for k, v in prov.items()}
            cand_raw = data.get("candidates", {})
            self.candidates_by_name = {}
            for name, clist in cand_raw.items():
                arr = []
                for c in clist:
                    arr.append(PackageCandidate(
                        name=c.get("name"),
                        version=c.get("version"),
                        provides=c.get("provides", []),
                        depends=c.get("depends", []),
                        conflicts=c.get("conflicts", []),
                        optional=c.get("optional", []),
                        meta=c.get("meta", {})
                    ))
                self.candidates_by_name[name] = arr
            self._loaded = True
            return
        except Exception:
            logger.exception("Failed to reconstruct index from file; will rebuild")

    # if no index_file or failed, build by scanning .meta
    self.provides_index = {}
    self.candidates_by_name = {}
    # walk repo_dir
    for root, _, files in os.walk(self.repo_dir):
        for fn in files:
            if fn.endswith(".meta") or fn.endswith(".yml") or fn.endswith(".yaml") or fn.endswith(".json"):
                full = os.path.join(root, fn)
                cand = _parse_meta_file(full)
                if cand:
                    self.candidates_by_name.setdefault(cand.name, []).append(cand)
                    # add provides
                    for p in cand.provides:
                        self.provides_index.setdefault(p, set()).add(cand.name)
                    # also add the package name as provider for itself
                    self.provides_index.setdefault(cand.name, set()).add(cand.name)
    # persist lightweight index
    try:
        tosave = {
            "provides_index": {k: list(v) for k, v in self.provides_index.items()},
            "candidates": {}
        }
        for name, clist in self.candidates_by_name.items():
            tosave["candidates"][name] = [{
                "name": c.name,
                "version": c.version,
                "provides": c.provides,
                "depends": c.depends,
                "conflicts": c.conflicts,
                "optional": c.optional,
                "meta": c.meta
            } for c in clist]
        _ensure_dir(os.path.dirname(self.index_file) or ".")
        with open(self.index_file, "w", encoding="utf-8") as fh:
            json.dump(tosave, fh, indent=2)
    except Exception:
        logger.exception("Failed write index file")
    self._loaded = True

# attach methods
RepoIndex.load = _repoindex_load

def _repoindex_find_candidates(self: RepoIndex, req: PackageRequirement) -> List[PackageCandidate]:
    """
    Retorna lista de PackageCandidate que potencialmente satisfazem a requirement.
    A ordem é heurística: preferir versões mais novas (se detectáveis) e providers equal name first.
    """
    self.load()
    out: List[PackageCandidate] = []
    # providers by virtual name
    provs = set()
    if req.name in self.provides_index:
        provs.update(self.provides_index.get(req.name, set()))
    # also consider exact name
    provs.add(req.name)
    seen = set()
    # collect candidates
    for pname in provs:
        clist = self.candidates_by_name.get(pname) or []
        for cand in clist:
            if cand.id() in seen:
                continue
            if cand.satisfies(req):
                out.append(cand)
                seen.add(cand.id())
    # heuristic sort: prefer same-name packages, prefer higher version if parsable, prefer locally available (repo_dir)
    def cand_score(c: PackageCandidate):
        score = 0
        if c.name == req.name:
            score += 1000
        # version score: newer -> higher
        v = c.version
        if v:
            pv = parse_version(v)
            if HAS_PACKAGING and isinstance(pv, Version):
                # convert to comparable number (major*1e6 + minor*1e3 + micro)
                try:
                    score += int(pv.release[0]) * 1000000
                except Exception:
                    score += 0
            else:
                # lexicographic fallback
                try:
                    score += int("".join([x for x in v if x.isdigit()])) if any(ch.isdigit() for ch in v) else 0
                except Exception:
                    score += 0
        # prefer packages whose meta is in repo (we don't have remote data here, so neutral)
        return -score  # negative because we want smaller sort key -> higher priority
    out.sort(key=cand_score)
    return out

RepoIndex.find_candidates = _repoindex_find_candidates

def _repoindex_find_best(self: RepoIndex, req: PackageRequirement) -> Optional[PackageCandidate]:
    """Retorna primeiro candidato (melhor heurística) ou None."""
    cands = self.find_candidates(req)
    return cands[0] if cands else None

RepoIndex.find_best = _repoindex_find_best
# Parte 3/3 — DependencyResolver e API pública

class ResolveResult:
    def __init__(self, ok: bool, chosen: Optional[Dict[str, PackageCandidate]] = None, order: Optional[List[str]] = None, issues: Optional[List[str]] = None):
        self.ok = ok
        self.chosen = chosen or {}  # map name -> candidate
        self.order = order or []    # topological build order (ids)
        self.issues = issues or []

class DependencyResolver:
    """
    Resolver com backtracking e heurísticas.
    - repo: RepoIndex
    - lockfile: path to dependency.lock.json (optional)
    - max_steps: limit for backtracking attempts to avoid explosion
    - verbose: logging/trace
    """

    def __init__(self, repo: Optional[RepoIndex] = None, lockfile: Optional[str] = None, max_steps: int = 10000, verbose: bool = False):
        self.repo = repo or RepoIndex()
        self.lockfile = lockfile or os.path.join(self.repo.repo_dir, "dependency.lock.json")
        self.max_steps = int(max_steps)
        self.verbose = verbose
        self._steps = 0
        # load lock if exists
        self._lock_data = None
        if os.path.exists(self.lockfile):
            try:
                self._lock_data = _safe_load_json(self.lockfile)
            except Exception:
                self._lock_data = None

    # ----------------------
    # Utility: apply lock
    # ----------------------
    def _apply_lock(self, requests: List[PackageRequirement]) -> Optional[Dict[str, PackageCandidate]]:
        """
        If lockfile contains exact mapping for the requested root set, return mapping name->candidate
        The lock key is sorted list of request names (without spec)
        """
        if not self._lock_data:
            return None
        key = ",".join(sorted([r.name for r in requests]))
        entry = self._lock_data.get(key)
        if not entry:
            return None
        chosen = {}
        for name, info in entry.items():
            # info contains name, version maybe
            cand = self.repo.find_best(PackageRequirement(name=name, specifier=parse_specifier(f"=={info.get('version')}") if info.get("version") else None))
            if cand:
                chosen[name] = cand
            else:
                # lock stale
                return None
        return chosen

    def _save_lock(self, requests: List[PackageRequirement], chosen: Dict[str, PackageCandidate]) -> None:
        """Persiste lock para as requests. Keyed por root set."""
        try:
            if self._lock_data is None:
                self._lock_data = {}
            key = ",".join(sorted([r.name for r in requests]))
            entry = {}
            for k, c in chosen.items():
                entry[k] = {"name": c.name, "version": c.version}
            self._lock_data[key] = entry
            with open(self.lockfile, "w", encoding="utf-8") as fh:
                json.dump(self._lock_data, fh, indent=2)
            if self.verbose:
                logger.info("Saved lock for key %s -> %s", key, list(entry.keys()))
        except Exception:
            logger.exception("Failed to write lockfile")

    # ----------------------
    # Core: resolve
    # ----------------------
    def resolve(self, requests: Iterable[PackageRequirement], allow_optional: bool = True, prefer_locked: bool = True, timeout: Optional[int] = None) -> ResolveResult:
        """
        Resolve a set of root requirements.
        Returns ResolveResult with chosen candidates and build order.
        """

        start_ts = time.time()
        reqs = [r if isinstance(r, PackageRequirement) else PackageRequirement.from_string(str(r)) for r in requests]
        if self.verbose:
            logger.info("Resolving requests: %s", [r.raw or r.name for r in reqs])

        # try apply lock
        if prefer_locked:
            locked = self._apply_lock(reqs)
            if locked:
                # verify consistency (no conflicts)
                ok, issues = self._verify_selection(locked)
                if ok:
                    order = self._topological_order(locked)
                    return ResolveResult(True, locked, order, [])
                # else fallthrough to re-resolve
                if self.verbose:
                    logger.info("lock found but invalid, re-resolving (%s)", issues)

        # prepare search structures
        chosen: Dict[str, PackageCandidate] = {}
        visiting: Set[str] = set()
        failures: List[str] = []
        # flatten root dependencies to process in priority queue (heuristic)
        # We'll implement DFS with backtracking and step limit
        roots = reqs

        self._steps = 0
        deadline = time.time() + timeout if timeout else None

        # build a helper recursive backtracking resolver
        def backtrack(index: int, active_requests: List[PackageRequirement]) -> Optional[Dict[str, PackageCandidate]]:
            # check step limit
            self._steps += 1
            if self._steps > self.max_steps:
                raise RuntimeError("Max resolution steps exceeded")
            if deadline and time.time() > deadline:
                raise RuntimeError("Dependency resolution timed out")

            if index >= len(active_requests):
                # all root requirements satisfied; but we must ensure transitive deps are satisfied
                ok, msg = self._verify_selection(chosen, allow_optional=allow_optional)
                if ok:
                    return dict(chosen)
                else:
                    if self.verbose:
                        logger.debug("verify_selection failed at leaf: %s", msg)
                    return None

            req = active_requests[index]
            # if already chosen a provider for that virtual/name, check spec
            already = next((c for n, c in chosen.items() if (n == req.name or req.name in c.provides)), None)
            if already:
                if already.satisfies(req):
                    # proceed to next root
                    return backtrack(index + 1, active_requests)
                else:
                    # choice incompatible; fail this branch
                    return None

            # get candidate list (heuristic ordered)
            cands = self.repo.find_candidates(req)
            if not cands:
                # no candidate found — attempt providers fuzzy match (partial) and record failure
                failures.append(f"no_candidate_for:{req.raw or req.name}")
                if self.verbose:
                    logger.debug("No candidates for req %s", req.raw or req.name)
                return None

            # try candidates in order
            for cand in cands:
                # quick conflicts check against already chosen
                conflict = False
                for chosen_name, chosen_c in list(chosen.items()):
                    # if cand conflicts with chosen_c or vice-versa
                    if cand.name in chosen_c.conflicts or chosen_c.name in cand.conflicts:
                        conflict = True
                        break
                if conflict:
                    continue
                # choose cand
                chosen_key = cand.name
                prev = chosen.get(chosen_key)
                chosen[chosen_key] = cand
                # add its transitive dependencies to active_requests if not already present
                trans_reqs: List[PackageRequirement] = []
                for d in cand.depends:
                    pr = PackageRequirement.from_string(d)
                    # skip optional if flag disabled
                    if pr.name in cand.optional and not allow_optional:
                        continue
                    # if already satisfied by chosen, skip
                    sat = any((pr.name == n) or (pr.name in c.provides) for n, c in chosen.items())
                    if not sat:
                        trans_reqs.append(pr)
                # Build new active_requests list: include transitive requirements right after current index
                new_active = active_requests[:index+1] + trans_reqs + active_requests[index+1:]
                try:
                    res = backtrack(index + 1, new_active)
                except RuntimeError:
                    # propagate timeout or steps limit
                    raise
                if res is not None:
                    return res
                # backtrack: undo
                if prev is not None:
                    chosen[chosen_key] = prev
                else:
                    chosen.pop(chosen_key, None)
                # continue trying next candidate
            # exhausted cands -> failure
            return None

        try:
            result_map = backtrack(0, roots)
        except RuntimeError as e:
            return ResolveResult(False, {}, [], [str(e)])

        if result_map is None:
            issues = failures or ["unable_to_resolve"]
            return ResolveResult(False, {}, [], issues)

        # Verify final selection thoroughly (conflicts, transitive, optional rules)
        ok, vissues = self._verify_selection(result_map, allow_optional=allow_optional)
        if not ok:
            return ResolveResult(False, {}, [], vissues)

        # compute topological order
        order = self._topological_order(result_map)
        # save lock
        try:
            self._save_lock(reqs, result_map)
        except Exception:
            pass

        return ResolveResult(True, result_map, order, [])

    # ----------------------
    # verification & topological order
    # ----------------------
    def _verify_selection(self, chosen_map: Dict[str, PackageCandidate], allow_optional: bool = True) -> Tuple[bool, List[str]]:
        """
        Verifica se o conjunto escolhido satisfaz todas dependências transitiveis and has no conflicts.
        """
        issues: List[str] = []
        # Build quick provides map
        provides = {}
        for name, cand in chosen_map.items():
            for p in cand.provides + [cand.name]:
                provides.setdefault(p, []).append(cand)

        # for each chosen candidate, check its depends
        for name, cand in chosen_map.items():
            for d in cand.depends:
                pr = PackageRequirement.from_string(d)
                # check any provider in chosen_map provides pr
                sat = False
                for p, clist in provides.items():
                    if p == pr.name or pr.name in p:
                        # at least one candidate providing p must also satisfy version
                        for candidate in clist:
                            if candidate.satisfies(pr):
                                sat = True
                                break
                        if sat:
                            break
                if not sat:
                    # check if optional
                    if pr.name in cand.optional and allow_optional:
                        continue
                    issues.append(f"unsatisfied:{cand.name}->{pr.raw or pr.name}")
            # conflicts
            for c in cand.conflicts:
                # if any chosen candidate matches conflict name -> issue
                for other_name, other_cand in chosen_map.items():
                    if other_name == c or c in other_cand.provides:
                        issues.append(f"conflict:{cand.name}~{other_name}")
        return (len(issues) == 0, issues)

    def _topological_order(self, chosen_map: Dict[str, PackageCandidate]) -> List[str]:
        """
        Retorna ordem topológica de build/instalação (ids). Usa Kahn com dependências restritas ao chosen set.
        """
        # build graph nodes = chosen candidate ids
        nodes = {c.id(): c for c in chosen_map.values()}
        deps_map: Dict[str, Set[str]] = {nid: set() for nid in nodes.keys()}  # node -> set of node ids it depends on
        name_to_id = {c.name: c.id() for c in chosen_map.values()}

        for nid, cand in nodes.items():
            for d in cand.depends:
                pr = PackageRequirement.from_string(d)
                # find chosen provider for pr
                provider_id = None
                for cname, ccand in chosen_map.items():
                    if ccand.satisfies(pr):
                        provider_id = ccand.id()
                        break
                if provider_id and provider_id in nodes:
                    deps_map[nid].add(provider_id)
        # Kahn algorithm
        indeg = {nid: 0 for nid in nodes}
        for nid, deps in deps_map.items():
            for d in deps:
                indeg[nid] += 1
        q = [nid for nid, deg in indeg.items() if deg == 0]
        order: List[str] = []
        while q:
            n = q.pop(0)
            order.append(n)
            # remove edges from n
            for m, deps in deps_map.items():
                if n in deps:
                    deps.remove(n)
                    indeg[m] -= 1
                    if indeg[m] == 0:
                        q.append(m)
        if len(order) != len(nodes):
            # cycle detected; try heuristic: break cycles by arbitrary order of names
            # append remaining nodes in deterministic order
            remaining = [nid for nid in nodes if nid not in order]
            remaining.sort()
            order.extend(remaining)
        return order

    # ----------------------
    # Diagnostics / explain
    # ----------------------
    def explain(self, requests: Iterable[PackageRequirement], depth: int = 2) -> Dict[str, Any]:
        """
        Gera relatório explicativo: candidate choices, providers, why failed, suggestions.
        """
        reqs = [r if isinstance(r, PackageRequirement) else PackageRequirement.from_string(str(r)) for r in requests]
        report = {"requests": [r.raw or r.name for r in reqs], "candidates": {}, "providers": {}, "tips": []}
        for r in reqs:
            cands = self.repo.find_candidates(r)
            report["candidates"][r.name] = [c.id() for c in cands]
            provs = []
            for c in cands:
                provs.extend(c.provides)
            report["providers"][r.name] = sorted(set(provs))
            if not cands:
                report["tips"].append(f"No candidates found for {r.raw or r.name}. Check .meta or provides index.")
        # top-level suggestions
        if not report["tips"]:
            report["tips"].append("If resolution fails, try: rebuild lib index, add missing provides to .meta, or pin versions in .meta.")
        return report

# -----------------------
# Public API convenience
# -----------------------

def resolve_requirements(requirements: Iterable[str], repo_dir: Optional[str] = None,
                         lockfile: Optional[str] = None, max_steps: int = 20000, verbose: bool = False) -> Dict[str, Any]:
    """
    Conveniência: resolve a partir de strings e retorna dict com results.
    """
    repo = RepoIndex(repo_dir=repo_dir)
    dr = DependencyResolver(repo=repo, lockfile=lockfile, max_steps=max_steps, verbose=verbose)
    req_objs = [PackageRequirement.from_string(s) for s in requirements]
    res = dr.resolve(req_objs)
    out = {"ok": res.ok, "issues": res.issues}
    if res.ok:
        out["chosen"] = {k: {"name": c.name, "version": c.version} for k, c in res.chosen.items()}
        out["order"] = res.order
    else:
        out["explain"] = dr.explain(req_objs)
    return out

# module exports
__all__ = [
    "PackageCandidate",
    "PackageRequirement",
    "RepoIndex",
    "DependencyResolver",
    "ResolveResult",
    "resolve_requirements",
        ]
