# upgrade.py
"""
Módulo de upgrade do Ibuild (evoluído).

Responsabilidades:
 - resolver dependências do pacote alvo (dependency.resolve)
 - construir (build) as versões necessárias na ordem correta
 - instalar temporariamente dentro de um sandbox para validação
 - se commit=True, aplicar as instalações no sistema (com opção de usar fakeroot)
 - prover rollback básico em caso de falha no commit
 - apoiar dry-run, keep_sandbox, jobs, include_optional

API pública:
 - upgrade_package(pkg_name, category=None, commit=False, resolve_deps=True,
                   include_optional=False, jobs=None, keep_sandbox=False, dry_run=False)
"""

from __future__ import annotations
import os
import time
import shutil
from typing import Dict, List, Optional, Tuple

from ibuild1.0.modules_py import (
    dependency,
    build as build_mod,
    package as package_mod,
    sandbox as sb_mod,
    fakeroot as fr_mod,
    log,
    config,
    utils,
    meta,
)

logger = log.get_logger("upgrade")


class UpgradeError(Exception):
    pass


def _timestamp() -> str:
    return time.strftime("%Y%m%d%H%M%S")


def _sb_name_for(pkg_name: str) -> str:
    return f"upgrade-{pkg_name}-{_timestamp()}"


def _ensure_install_root() -> str:
    # destino real para commit (padrão config.install_root ou /usr/local)
    return config.get("install_root") or "/usr/local"


def _build_packages_in_order(order: List[str],
                             metas: Dict[str, dict],
                             jobs: Optional[int] = None,
                             keep_sandbox_builds: bool = False,
                             dry_run: bool = False) -> Dict[str, str]:
    """
    Para cada pacote em 'order' (dependências primeiro), executa build.build_package(...)
    Retorna dict: pkg_name -> artifact_path.
    Se dry_run=True, não executa builds, apenas tenta localizar artifacts no cache.
    """
    artifacts: Dict[str, str] = {}
    for pkg in order:
        m = metas.get(pkg) or meta.load_meta(pkg)
        logger.info("Preparando build para %s@%s", m["name"], m.get("version"))
        if dry_run:
            # tentar inferir artifact path (cache dir)
            art = os.path.join(config.get("cache_dir"), "packages", f"{m['name']}-{m.get('version','0')}.tar.gz")
            logger.info("dry_run: assumindo artefato (se existir): %s", art)
            artifacts[pkg] = art
            continue

        try:
            art_path, meta_returned = build_mod.build_package(
                pkg,
                category=None,
                resolve_deps=False,  # já resolvido
                include_optional=False,
                jobs=jobs,
                keep_sandbox=keep_sandbox_builds,
                stages=None  # executar todas por padrão
            )
            artifacts[pkg] = art_path
            logger.info("Build concluído: %s -> %s", pkg, art_path)
        except Exception as e:
            logger.exception("Falha ao construir %s: %s", pkg, e)
            raise UpgradeError(f"Falha ao construir {pkg}: {e}") from e
    return artifacts


def _install_artifacts_in_sandbox(artifacts: Dict[str, str], sb_name: str) -> Dict[str, dict]:
    """
    Instala cada artifact dentro do sandbox `sb_name` usando fakeroot.install_with_fakeroot.
    Retorna dict pkg -> install_result (o dict retornado por fakeroot.install_with_fakeroot).
    """
    results = {}
    sb_install_root = os.path.join(sb_mod.sandbox_root(sb_name), "install")
    os.makedirs(sb_install_root, exist_ok=True)

    for pkg, art in artifacts.items():
        logger.info("Instalando %s no sandbox %s (artefato=%s)", pkg, sb_name, art)
        if not os.path.isfile(art):
            logger.warn("Artefato não encontrado para %s: %s (pulando)", pkg, art)
            results[pkg] = {"installed": False, "reason": "missing_artifact", "artifact": art}
            continue
        try:
            # fakeroot.install_with_fakeroot tenta usar ferramentas apropriadas,
            # e registra ownership quando necessário.
            res = fr_mod.install_with_fakeroot(art, sb_install_root)
            logger.debug("Instalação sandbox %s -> %s", pkg, res)
            results[pkg] = res
        except Exception as e:
            logger.exception("Falha ao instalar %s no sandbox: %s", pkg, e)
            results[pkg] = {"installed": False, "reason": str(e)}
            # abortamos a sequência de sandbox installs pois algo grave ocorreu
            raise UpgradeError(f"Falha ao instalar {pkg} no sandbox: {e}") from e
    return results


def _commit_installs(artifacts: Dict[str, str], commit_method: str = "package_install") -> Tuple[List[str], List[Tuple[str, str]]]:
    """
    Aplica as instalações no sistema a partir dos artifacts.
    commit_method:
      - "package_install": usa package.install_package(artifact, upgrade=True) -> atualizar pkg_db
      - "fakeroot_direct": usa fakeroot.install_with_fakeroot para o install_root (não altera pkg_db automaticamente)
    Retorna (succeeded, failed_list[(pkg,reason)]).
    """
    succeeded: List[str] = []
    failed: List[Tuple[str, str]] = []
    install_root = _ensure_install_root()

    for pkg, art in artifacts.items():
        if not os.path.isfile(art):
            logger.error("Commit: artefato não encontrado para %s: %s", pkg, art)
            failed.append((pkg, "missing_artifact"))
            continue
        try:
            if commit_method == "package_install":
                # package.install_package fará upgrade (se upgrade=True) e gravará manifest/installed.meta
                package_mod.install_package(art, dest_dir=install_root, overwrite=True, upgrade=True)
                logger.info("Commit: package.install_package OK para %s", pkg)
            else:
                # fakeroot direto: extrai no install_root, não atualiza pkg_db —
                # portanto, também escrevemos o installed.meta manualmente
                fr_mod.install_with_fakeroot(art, install_root)
                # gerar installed meta via leitura do artifact / heurística
                # reutilizamos package.install_package path to register meta (call with upgrade)
                package_mod.install_package(art, dest_dir=install_root, overwrite=True, upgrade=True)
                logger.info("Commit: installed via fakeroot for %s", pkg)
            succeeded.append(pkg)
        except Exception as e:
            logger.exception("Erro ao aplicar commit para %s: %s", pkg, e)
            failed.append((pkg, str(e)))
            # tentativa de rollback parcial: remover o pacote que falhou (se was partially installed)
            try:
                package_mod.remove_package(pkg, purge=True)
            except Exception:
                logger.warn("Rollback parcial falhou para %s", pkg)
    return succeeded, failed


def upgrade_package(pkg_name: str,
                    category: Optional[str] = None,
                    commit: bool = False,
                    resolve_deps: bool = True,
                    include_optional: bool = False,
                    jobs: Optional[int] = None,
                    keep_sandbox: bool = False,
                    dry_run: bool = False) -> dict:
    """
    Fluxo principal de upgrade.

    Parâmetros:
      - pkg_name: nome do pacote alvo (pode ser virtual; dependency resolver tentará providers)
      - category: categoria opcional para meta.load_meta quando necessário
      - commit: se True, aplica as novas versões no sistema (usa package.install_package)
      - resolve_deps: se True, resolve dependências automaticamente
      - include_optional: incluir optional deps na resolução
      - jobs: número de jobs para build
      - keep_sandbox: se True, não destrói sandbox ao final
      - dry_run: se True, não faz builds nem commits, apenas simula passos

    Retorna um relatório dict com chaves:
      - "order": lista de pacotes resolvidos
      - "artifacts": mapping pkg -> artifact path (or assumed path in dry-run)
      - "sandbox_install": mapping pkg -> result (do fakeroot.install_with_fakeroot)
      - "commit": {"succeeded": [...], "failed": [...] } (se commit=True)
      - "sandbox": sandbox name used
    """
    logger.info("Iniciando upgrade para %s (commit=%s, resolve_deps=%s)", pkg_name, commit, resolve_deps)
    report = {"order": [], "artifacts": {}, "sandbox_install": {}, "commit": None, "sandbox": None}

    # 1) resolver dependências
    if resolve_deps:
        try:
            order, metas = dependency.resolve([pkg_name], include_optional=include_optional, prefer_provided=True)
            logger.info("Ordem resolvida: %s", ", ".join(order))
        except Exception as e:
            logger.exception("Falha ao resolver dependências para %s: %s", pkg_name, e)
            raise UpgradeError(f"Falha na resolução de dependências: {e}") from e
    else:
        # apenas o pacote alvo (e carregar meta)
        try:
            m = meta.load_meta(pkg_name, category)
            order = [m["name"]]
            metas = {m["name"]: m}
        except Exception as e:
            logger.exception("Falha ao carregar meta para %s: %s", pkg_name, e)
            raise UpgradeError(f"Meta não encontrado para {pkg_name}: {e}") from e

    report["order"] = order

    # 2) build todos os pacotes na ordem (dependências primeiro)
    artifacts = _build_packages_in_order(order, metas, jobs=jobs, keep_sandbox_builds=False, dry_run=dry_run)
    report["artifacts"] = artifacts

    # 3) criar sandbox para validação de instalação
    sb_name = _sb_name_for(pkg_name)
    report["sandbox"] = sb_name
    sb_mod.create_sandbox(sb_name, binds=[config.get("repo_dir")], keep=keep_sandbox)

    try:
        # 4) instalar todos os artifacts no sandbox
        sandbox_results = _install_artifacts_in_sandbox(artifacts, sb_name)
        report["sandbox_install"] = sandbox_results

        # opcional: executar testes rápidos pós-instalação no sandbox (hooks ou smoke tests)
        # se desejar, poderia rodar pkg_meta['post_install_test'] aqui. O build pipeline já executa checks.

        if dry_run:
            logger.info("dry_run=True → pulando commit. Relatório gerado.")
            return report

        if commit:
            # 5) commit: aplicar no sistema
            logger.info("Commit solicitado → aplicando instalações no sistema")
            succ, failed = _commit_installs(artifacts, commit_method="package_install")
            report["commit"] = {"succeeded": succ, "failed": failed}

            if failed:
                logger.error("Alguns commits falharam: %s", failed)
                raise UpgradeError(f"Falha ao aplicar commit para alguns pacotes: {failed}")

        logger.info("Upgrade finalizado com sucesso para %s", pkg_name)
        return report

    except Exception as e:
        logger.exception("Erro no processo de upgrade para %s: %s", pkg_name, e)
        raise

    finally:
        # cleanup do sandbox se necessário
        if keep_sandbox:
            logger.info("keep_sandbox=True → mantendo sandbox %s para inspeção", sb_name)
        else:
            try:
                sb_mod.destroy_sandbox(sb_name)
            except Exception:
                logger.warn("Falha ao remover sandbox %s no finally", sb_name)
