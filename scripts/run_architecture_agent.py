#!/usr/bin/env python3
"""Backend agente real para el gate de arquitectura AutoEdd.

Lee el request contractual generado por el wrapper, recopila el contexto autorizado
y consulta GitHub Models para obtener una decision estructurada conforme al
contrato de respuesta.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


MAX_FILE_BYTES = 800
MAX_FILES_PER_REPO = 12
MAX_POLICY_FILE_BYTES = 3_000
DEFAULT_MODEL = "azure-openai/gpt-5"
DEFAULT_BASE_URL = "https://models.github.ai/inference"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_API_VERSION = "2022-11-28"
INCLUDED_POLICY_FILES = {
    "agent_evaluation_flow.yml",
    "contract_input.yml",
    "rulepack.yml",
    "ini_field_mapping_for_agents.json",
}


# Carga el request o respuesta contractual en formato JSON.
def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


# Lee un archivo de texto UTF-8 completo desde disco.
def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path | None, content: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# Carga YAML y normaliza documentos vacios a un diccionario vacio.
def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data or {}


# Limpia sufijos descriptivos de key paths para dejarlos como patrones glob utilizables.
def normalize_key_path(pattern: str) -> str:
    return pattern.split(" (", 1)[0].strip()


def extract_universal_destination_tables(request_payload: dict[str, Any]) -> list[str]:
    input_payload = request_payload.get("input_payload", {})
    properties = input_payload.get("properties", {})
    mapping = properties.get("stage_to_universal_mapeo", {})
    destino_items = mapping.get("destino", [])
    if not isinstance(destino_items, list):
        return []

    table_names: list[str] = []
    seen_tables: set[str] = set()
    for item in destino_items:
        if not isinstance(item, dict):
            continue
        table_name = str(item.get("tabla", "")).strip()
        if not table_name or table_name in seen_tables:
            continue
        seen_tables.add(table_name)
        table_names.append(table_name)
    return table_names


def resolve_key_paths(repo_type_name: str, repo_type_cfg: dict[str, Any], request_payload: dict[str, Any]) -> list[str]:
    raw_key_paths = [str(path) for path in repo_type_cfg.get("key_paths", [])]
    if repo_type_name != "stage_to_universal_repo":
        return raw_key_paths

    table_names = extract_universal_destination_tables(request_payload)
    if not table_names:
        return raw_key_paths

    resolved_paths: list[str] = []
    for raw_path in raw_key_paths:
        if "{{table_name}}" not in raw_path:
            resolved_paths.append(raw_path)
            continue
        for table_name in table_names:
            resolved_paths.append(raw_path.replace("{{table_name}}", table_name))
    return resolved_paths


# Filtra binarios simples detectando bytes nulos en una muestra inicial del archivo.
def is_text_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            sample = handle.read(2048)
    except OSError:
        return False
    return b"\x00" not in sample


# Recoge una muestra acotada de archivos del repo según los glob patterns autorizados.
def read_repo_files(repo_root: Path, patterns: list[str]) -> dict[str, Any]:
    collected_files: list[dict[str, str]] = []
    missing_patterns: list[str] = []
    seen_paths: set[Path] = set()

    for raw_pattern in patterns:
        pattern = normalize_key_path(raw_pattern)
        matches = list(repo_root.glob(pattern))
        if not matches:
            missing_patterns.append(pattern)
            continue
        for match in matches:
            if len(collected_files) >= MAX_FILES_PER_REPO:
                break
            if match.is_dir() or match in seen_paths or not is_text_file(match):
                continue
            try:
                content = match.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = match.read_text(encoding="latin-1")
            seen_paths.add(match)
            # El runner manda evidencia curada para caber en la ventana de contexto de GitHub Models.
            collected_files.append(
                {
                    "path": match.relative_to(repo_root).as_posix(),
                    "content": content[:MAX_FILE_BYTES],
                }
            )
    return {"files": collected_files, "missing_patterns": missing_patterns}


# Carga solo los documentos de policy explícitamente autorizados para este request del agente.
def load_authorized_policy_contents(policy_root: Path, request_payload: dict[str, Any]) -> list[dict[str, str]]:
    documents: list[dict[str, str]] = []
    for item in request_payload.get("authorized_policy_files", []):
        relative_path = item.get("path")
        if not relative_path or str(relative_path) not in INCLUDED_POLICY_FILES:
            continue
        path = policy_root / relative_path
        content = load_text(path)
        # Solo se adjuntan las policies mínimas que el agente necesita para decidir sin exceder el límite del endpoint.
        documents.append(
            {
                "path": relative_path,
                "content_type": str(item.get("content_type", "text")),
                "content": content[:MAX_POLICY_FILE_BYTES],
            }
        )
    return documents


def resolve_authoritative_rulepack_path(policy_root: Path, request_payload: dict[str, Any]) -> Path:
    flow_document = load_yaml(policy_root / "agent_evaluation_flow.yml").get("agent_evaluation_flow", {})
    authoritative_sources = flow_document.get("authoritative_sources", {})
    configured_rulepack = str(authoritative_sources.get("rulepack", "rulepack.yml")).strip() or "rulepack.yml"
    authorized_paths = {str(item.get("path", "")).strip() for item in request_payload.get("authorized_policy_files", []) if item.get("path")}
    if configured_rulepack not in authorized_paths:
        raise FileNotFoundError("El rulepack configurado no fue autorizado en el request del agente.")
    return policy_root / configured_rulepack


def resolve_current_repo_root(policy_root: Path) -> Path:
    platform_root = os.getenv("AUTOEDD_PLATFORM_ROOT")
    if platform_root:
        return Path(platform_root)
    return policy_root.parents[1]


def parse_pull_request_repo_name(pr_url: str) -> str | None:
    parsed = urlparse(pr_url)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    return parts[1]


def resolve_current_repo_type(rulepack: dict[str, Any], request_payload: dict[str, Any]) -> str | None:
    pr_url = str(request_payload.get("pull_request", {}).get("url", ""))
    repo_name = parse_pull_request_repo_name(pr_url)
    if not repo_name:
        return None

    for repo_type_name, repo_type_cfg in rulepack.get("repo_types", {}).items():
        match_glob = str(repo_type_cfg.get("repository_name_identification", {}).get("match_glob", "")).strip()
        if match_glob and fnmatch(repo_name.lower(), match_glob.lower()):
            return str(repo_type_name)
    return None


# Construye snapshots mínimos del repositorio actual usando el rulepack como guía.
def load_repository_snapshots(policy_root: Path, request_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rulepack = load_yaml(resolve_authoritative_rulepack_path(policy_root, request_payload))
    repo_types = rulepack.get("repo_types", {})
    repo_type_name = resolve_current_repo_type(rulepack, request_payload)
    if not repo_type_name:
        return []
    repo_type_cfg = repo_types.get(repo_type_name, {})
    key_paths = resolve_key_paths(repo_type_name, repo_type_cfg, request_payload)
    if not key_paths:
        return []
    repo_root = resolve_current_repo_root(policy_root)
    snapshot = read_repo_files(repo_root, [str(path) for path in key_paths])
    return [
        {
            "name": "current_pr_repository",
            "url": str(request_payload.get("pull_request", {}).get("url", "")),
            "repo_type": repo_type_name,
            "files": snapshot["files"],
        }
    ]


# Ensambla el prompt final para chat completions con request, policies y snapshots autorizados.
def build_chat_messages(system_prompt: str, agent_context: str, request_payload: dict[str, Any], policy_docs: list[dict[str, str]], repo_snapshots: list[dict[str, Any]]) -> list[dict[str, str]]:
    # Se serializa un request compacto porque el endpoint de GitHub Models aplica límites estrictos al cuerpo.
    compact_request = {
        "contract_version": request_payload.get("contract_version"),
        "pull_request": request_payload.get("pull_request"),
        "input_payload": request_payload.get("input_payload"),
        "execution_rules": request_payload.get("execution_rules"),
    }
    user_sections = [
        "Debes resolver la evaluacion AutoEdd y devolver exclusivamente un JSON valido contra el contrato de respuesta.",
        "## Request contractual",
        json.dumps(compact_request, ensure_ascii=False, separators=(",", ":")),
        "## Contexto adicional del agente",
        agent_context[:3000],
        "## Policy files autorizados",
    ]
    for document in policy_docs:
        user_sections.append(f"### {document['path']} ({document['content_type']})")
        user_sections.append(document["content"])
    user_sections.append("## Repository snapshots autorizados")
    user_sections.append(json.dumps(repo_snapshots, ensure_ascii=False, separators=(",", ":")))
    user_sections.append("## Contrato de salida esperado")
    user_sections.append(
        "Para TLS con multiples etapas habilitadas debes completar evaluated_stages, technologies y archetype_ids; "
        "no dejes tecnologias o arquetipos en null si fueron realmente resueltos. El campo message debe resumir brevemente que se valido."
    )
    user_sections.append(
        "Dentro de un mismo arquetipo, ejecuta solo las reglas cuyo applies_when.repo_types incluya el tipo de repositorio actual; si una regla no declara applies_when, tratala como compartida."
    )
    user_sections.append(
        json.dumps(
            {
                "contract_version": "1.0",
                "result": "PASS|FAIL|INPUT_INCOMPLETO|ARQUETIPO_INVALIDO",
                "category": "string",
                "message": "string",
                "repo_type": "stage|universal|tls|null",
                "evaluated_stage": "stage|universal|raw|null",
                "evaluated_stages": ["stage|universal|raw"],
                "technology": "string|null",
                "technologies": ["string"],
                "archetype_id": "string|null",
                "archetype_ids": ["string"],
                "applies_edd_traditional": "boolean",
                "blocking": "boolean",
                "bindings": {},
                "metadata": {},
                "rule_evidence": [
                    {
                        "rule_id": "string",
                        "status": "PASS|FAIL|WARN",
                        "message": "string",
                        "details": {},
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    user_sections.append("## Instruccion final")
    user_sections.append(
        "Responde solo un objeto JSON. No agregues markdown, comentarios, ni explicacion adicional fuera del JSON."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(user_sections)},
    ]


# Invoca el endpoint de GitHub Models compatible con chat completions y devuelve el texto crudo.
def call_chat_completions(model: str, base_url: str, auth_token: str, messages: list[dict[str, str]]) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
    }
    api_version = os.getenv("AUTOEDD_AGENT_API_VERSION", DEFAULT_API_VERSION)
    # GitHub Models expone una API compatible con chat completions, pero autenticada con credenciales GitHub.
    request = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_token}",
            "X-GitHub-Api-Version": api_version,
        },
    )
    timeout = int(os.getenv("AUTOEDD_AGENT_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    return str(body["choices"][0]["message"]["content"])


# Extrae detalle útil del error HTTP del proveedor para dejar trazabilidad diagnóstica en el resultado.
def build_http_error_details(exc: urllib.error.HTTPError, model: str, base_url: str) -> dict[str, Any]:
    detail = exc.read().decode("utf-8", errors="replace")
    headers = {key: value for key, value in exc.headers.items()}
    return {
        "status": exc.code,
        "reason": detail[:4000],
        "http_reason": str(exc.reason or ""),
        "url": f"{base_url.rstrip('/')}/chat/completions",
        "model": model,
        "response_headers": headers,
    }


# Extrae un objeto JSON desde la respuesta del modelo incluso si viene envuelto en fences o texto adicional.
def extract_json_object(raw_text: str) -> dict[str, Any]:
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # Algunos modelos siguen envolviendo la respuesta en texto adicional; aquí se rescata el objeto JSON contractual.
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


# Construye una respuesta contractual de error para fallas de ejecución del runner agente.
def build_error_result(category: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "result": "FAIL",
        "category": category,
        "message": message,
        "repo_type": None,
        "evaluated_stage": None,
        "technology": None,
        "archetype_id": None,
        "applies_edd_traditional": False,
        "blocking": True,
        "bindings": {},
        "metadata": {"evaluation_backend": "agent-subprocess"},
        "rule_evidence": [
            {
                "rule_id": category.lower(),
                "status": "FAIL",
                "message": message,
                "details": details or {},
            }
        ],
    }


# Persiste la respuesta contractual del agente en la ruta esperada por el wrapper.
def write_response(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# Ejecuta el ciclo completo del runner: carga contexto, consulta el modelo y serializa la respuesta.
def main() -> int:
    response_path = Path(os.environ["AUTOEDD_AGENT_RESPONSE_PATH"])
    request_path = Path(os.environ["AUTOEDD_AGENT_REQUEST_PATH"])
    prompt_path = Path(os.environ["AUTOEDD_AGENT_PROMPT_PATH"])
    context_path = Path(os.environ["AUTOEDD_AGENT_CONTEXT_PATH"])
    policy_root = Path(os.getenv("AUTOEDD_POLICY_ROOT", str(prompt_path.parent)))
    raw_response_env = os.getenv("AUTOEDD_AGENT_RAW_RESPONSE_PATH", "")
    raw_response_path = Path(raw_response_env) if raw_response_env else None

    try:
        request_payload = load_json(request_path)
        system_prompt = load_text(prompt_path)
        agent_context = load_text(context_path)
        policy_docs = load_authorized_policy_contents(policy_root, request_payload)
        repo_snapshots = load_repository_snapshots(policy_root, request_payload)

        model = os.getenv("AUTOEDD_AGENT_MODEL", DEFAULT_MODEL)
        auth_token = (
            os.getenv("AUTOEDD_AGENT_TOKEN")
            or os.getenv("GITHUB_TOKEN")
            or os.getenv("AUTOEDD_GITHUB_TOKEN")
        )
        base_url = os.getenv("AUTOEDD_AGENT_BASE_URL", DEFAULT_BASE_URL)
        if not auth_token:
            result = build_error_result(
                "AGENT_CONFIGURATION_ERROR",
                "No se encontro un token GitHub utilizable para ejecutar GitHub Models.",
                {"required_env": ["GITHUB_TOKEN|AUTOEDD_AGENT_TOKEN", "AUTOEDD_AGENT_MODEL", "AUTOEDD_AGENT_BASE_URL"]},
            )
            write_response(response_path, result)
            return 0

        # A partir de aquí toda la decisión de arquitectura depende del agente y no de reglas embebidas en Python.
        messages = build_chat_messages(system_prompt, agent_context, request_payload, policy_docs, repo_snapshots)
        raw_response = call_chat_completions(model=model, base_url=base_url, auth_token=auth_token, messages=messages)
        write_text(raw_response_path, raw_response)
        result = extract_json_object(raw_response)
        if isinstance(result, dict):
            result.setdefault("metadata", {})
            result["metadata"].setdefault("evaluation_backend", "agent-subprocess")
            result["metadata"].setdefault("model", model)
            result["metadata"].setdefault("base_url", base_url)
            result["metadata"].setdefault("provider", "github-models")
            if raw_response_path:
                result["metadata"].setdefault("raw_response_path", str(raw_response_path))
        write_response(response_path, result)
        return 0
    except urllib.error.HTTPError as exc:
        result = build_error_result(
            "AGENT_HTTP_ERROR",
            "El backend agente no pudo obtener una respuesta valida del proveedor del modelo.",
            build_http_error_details(exc, model=model, base_url=base_url),
        )
        write_response(response_path, result)
        return 0
    except Exception as exc:  # noqa: BLE001
        result = build_error_result(
            "AGENT_EXECUTION_ERROR",
            "El backend agente fallo antes de producir una respuesta contractual.",
            {"reason": str(exc)},
        )
        write_response(response_path, result)
        return 0


    # Punto de entrada para ejecución directa del runner agente.
if __name__ == "__main__":
    raise SystemExit(main())