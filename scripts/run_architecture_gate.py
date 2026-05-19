#!/usr/bin/env python3
"""Wrapper del gate de arquitectura AutoEdd.

El wrapper resuelve el lifecycle del check: extrae el input desde el PR,
valida contratos, ejecuta el runner agente y publica el resultado final.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


BLOCKING_RESULTS = {"FAIL", "INPUT_INCOMPLETO", "ARQUETIPO_INVALIDO"}
SUPPORTED_BACKEND_MODE = "agent-subprocess"


# Carga un YAML y normaliza documentos vacios a un diccionario vacio.
def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data or {}


# Carga un archivo JSON contractual desde disco.
def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_github_event_pull_request() -> dict[str, Any]:
    event_path = os.getenv("GITHUB_EVENT_PATH", "")
    if not event_path:
        return {}
    path = Path(event_path)
    if not path.exists():
        return {}
    try:
        event_payload = load_json(path)
    except (OSError, json.JSONDecodeError):
        return {}
    pull_request = event_payload.get("pull_request")
    return pull_request if isinstance(pull_request, dict) else {}


def write_output(path_value: str | None, content: str) -> None:
    # El workflow consume estos artefactos después para comentar el PR y registrar evidencia.
    if not path_value:
        return
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read_output(path_value: str | None) -> str:
    if not path_value:
        return ""
    path = Path(path_value)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def build_raw_response_details(path_value: str | None) -> dict[str, Any]:
    raw_response = read_output(path_value)
    if not raw_response:
        return {}
    return {
        "raw_response_path": path_value,
        "raw_response_excerpt": raw_response[:4000],
    }


def extract_json_from_pr_body(body: str, start_marker: str, end_marker: str) -> dict[str, Any]:
    # El gate solo acepta input estructurado dentro de los markers acordados en el body del PR.
    if not body.strip():
        raise ValueError("pull request body is empty")
    start_index = body.find(start_marker)
    end_index = body.find(end_marker)
    if start_index == -1 or end_index == -1 or end_index <= start_index:
        raise ValueError("required JSON markers not found in pull request body")
    raw_json = body[start_index + len(start_marker):end_index].strip()
    if not raw_json:
        raise ValueError("JSON block between markers is empty")
    return json.loads(raw_json)


# Verifica que todos los archivos declarados del policy pack existan en el root esperado.
def validate_policy_pack(base_dir: Path, files: list[str]) -> list[str]:
    return [str(base_dir / filename) for filename in files if not (base_dir / filename).exists()]


def resolve_policy_pack_files(base_dir: Path, configured_files: list[str]) -> list[str]:
    resolved = list(dict.fromkeys(configured_files))
    flow_path = base_dir / "agent_evaluation_flow.yml"
    if not flow_path.exists():
        return resolved

    flow_document = load_yaml(flow_path).get("agent_evaluation_flow", {})
    authoritative_sources = flow_document.get("authoritative_sources", {})
    for key in ("input_contract", "rulepack", "ini_field_mapping"):
        candidate = authoritative_sources.get(key)
        if isinstance(candidate, str) and candidate not in resolved:
            resolved.append(candidate)
    return resolved


def validate_against_schema(payload: dict[str, Any], schema_path: Path) -> list[str]:
    # El wrapper nunca delega si el request o la respuesta rompen el contrato versionado.
    schema = load_json(schema_path)
    validator = Draft202012Validator(schema)
    return [error.message for error in sorted(validator.iter_errors(payload), key=lambda item: item.path)]


def validate_result_semantics(result: dict[str, Any]) -> list[str]:
    evidence = result.get("rule_evidence") or []
    pass_count = sum(1 for item in evidence if str(item.get("status", "")).upper() == "PASS")
    fail_count = sum(1 for item in evidence if str(item.get("status", "")).upper() == "FAIL")
    result_value = str(result.get("result", "")).upper()
    category = str(result.get("category", "")).upper()

    errors: list[str] = []
    if result_value == "PASS" and fail_count > 0:
        errors.append("El resultado final no puede ser PASS si existe evidencia FAIL.")

    if result_value in BLOCKING_RESULTS and fail_count == 0 and pass_count > 0:
        errors.append("El resultado final no puede ser bloqueante si toda la evidencia reportada esta en PASS.")

    if category == "MINIMUM_INPUT_NOT_MET" and fail_count == 0:
        errors.append("La categoria MINIMUM_INPUT_NOT_MET requiere al menos una evidencia FAIL que respalde el faltante o incumplimiento.")

    if result.get("applies_edd_traditional") and fail_count == 0:
        errors.append("No se puede marcar applies_edd_traditional=true sin evidencia FAIL o WARN que lo justifique.")

    return errors


def summarize_evidence_details(details: dict[str, Any]) -> str | None:
    if not details:
        return None

    if details.get("missing_paths"):
        return f"Rutas faltantes: {', '.join(str(item) for item in details['missing_paths'])}"

    path = details.get("path") or details.get("input_path")
    expected = details.get("expected") or details.get("expected_value") or details.get("expected_dataset")
    found = details.get("found") or details.get("found_or_missing") or details.get("actual_value_or_missing") or details.get("actual_schema_or_missing")
    if path and expected is not None:
        return f"Ruta: {path} | Esperado: {expected} | Encontrado: {found}"

    file_path = details.get("ini_file_path") or details.get("repo_file_path")
    if file_path and expected is not None:
        return f"Archivo: {file_path} | Esperado: {expected} | Encontrado: {found}"

    if file_path:
        return f"Archivo: {file_path}"

    compact = json.dumps(details, ensure_ascii=False, sort_keys=True)
    return compact if compact != "{}" else None


def collect_result_values(result: dict[str, Any], plural_key: str, singular_key: str) -> list[str]:
    values = result.get(plural_key)
    if isinstance(values, list):
        return [str(item).strip() for item in values if str(item).strip()]

    value = result.get(singular_key)
    return [str(value).strip()] if value not in (None, "") and str(value).strip() else []


def build_rulepack_catalog(rulepack: dict[str, Any]) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    archetypes_by_stage_technology: dict[tuple[str, str], str] = {}
    rule_descriptions: dict[str, str] = {}

    for stage_name, stage_cfg in (rulepack.get("stages") or {}).items():
        technologies = (stage_cfg or {}).get("technologies") or {}
        for technology_name, technology_cfg in technologies.items():
            archetype_id = str((technology_cfg or {}).get("archetype_id") or "").strip()
            if archetype_id:
                archetypes_by_stage_technology[(str(stage_name), str(technology_name))] = archetype_id
            for rule in (technology_cfg or {}).get("rules") or []:
                rule_id = str((rule or {}).get("id") or "").strip()
                description = str((rule or {}).get("description") or "").strip()
                if rule_id and description:
                    rule_descriptions[rule_id] = description

    return archetypes_by_stage_technology, rule_descriptions


def resolve_display_archetypes(
    result: dict[str, Any],
    evaluated_stages: list[str],
    technologies: list[str],
    rulepack: dict[str, Any] | None,
) -> list[str]:
    fallback_archetypes = collect_result_values(result, "archetype_ids", "archetype_id")
    if not rulepack:
        return fallback_archetypes

    archetypes_by_stage_technology, _ = build_rulepack_catalog(rulepack)
    resolved_archetypes: list[str] = []
    remaining_technologies = [technology for technology in technologies]

    for stage_name in evaluated_stages:
        matched_archetype = None
        for technology_name in list(remaining_technologies):
            matched_archetype = archetypes_by_stage_technology.get((stage_name, technology_name))
            if matched_archetype:
                remaining_technologies.remove(technology_name)
                break
        if not matched_archetype:
            stage_candidates = [
                archetype_id
                for (candidate_stage, _technology), archetype_id in archetypes_by_stage_technology.items()
                if candidate_stage == stage_name
            ]
            if len(stage_candidates) == 1:
                matched_archetype = stage_candidates[0]
        if matched_archetype and matched_archetype not in resolved_archetypes:
            resolved_archetypes.append(matched_archetype)

    return resolved_archetypes or fallback_archetypes


def build_validation_summary(
    result: dict[str, Any],
    evaluated_stages: list[str],
    technologies: list[str],
    archetype_ids: list[str],
) -> str:
    parts: list[str] = []
    repo_type = result.get("repo_type")
    if repo_type:
        parts.append(f"repo tipo {repo_type}")
    if evaluated_stages:
        parts.append(f"etapas {', '.join(evaluated_stages)}")
    if technologies:
        parts.append(f"tecnologias {', '.join(technologies)}")
    if archetype_ids:
        parts.append(f"arquetipos {', '.join(archetype_ids)}")

    evidence = result.get("rule_evidence") or []
    pass_count = sum(1 for item in evidence if str(item.get("status", "")).upper() == "PASS")
    warn_count = sum(1 for item in evidence if str(item.get("status", "")).upper() == "WARN")
    fail_count = sum(1 for item in evidence if str(item.get("status", "")).upper() == "FAIL")
    if pass_count:
        parts.append(f"{pass_count} reglas validadas")
    if warn_count:
        parts.append(f"{warn_count} advertencias")
    if fail_count:
        parts.append(f"{fail_count} fallas")

    if not parts:
        return str(result.get("message", "n/a"))

    prefix = "Se valido" if str(result.get("result", "")).upper() == "PASS" else "Se evaluo"
    summary = f"{prefix} " + "; ".join(parts) + "."
    if pass_count and warn_count == 0 and fail_count == 0:
        summary += " Sin observaciones."
    return summary


def build_edd_decision_message(result: dict[str, Any]) -> str:
    result_value = str(result.get("result", "")).upper()
    if result_value == "PASS":
        return "🥇 Aplica validacion automatica de EDD"
    return "⚠️ 🚫 Aplica EDD tradicional con equipo de arquitectura de datos"


def build_markdown_summary(result: dict[str, Any], pr_url: str, rulepack: dict[str, Any] | None = None) -> str:
    # Este markdown es la única vista humana persistida en el comentario del PR.
    evaluated_stages = collect_result_values(result, "evaluated_stages", "evaluated_stage")
    technologies = collect_result_values(result, "technologies", "technology")
    archetype_ids = resolve_display_archetypes(result, evaluated_stages, technologies, rulepack)
    result_value = str(result.get("result", "n/a"))
    summary_text = build_validation_summary(result, evaluated_stages, technologies, archetype_ids)
    edd_decision_text = build_edd_decision_message(result)
    result_icon = {
        "PASS": "✅",
        "FAIL": "❌",
        "INPUT_INCOMPLETO": "⚠️",
        "ARQUETIPO_INVALIDO": "🚫",
    }.get(result_value, "ℹ️")
    lines = [
        "<!-- autoedd-architecture-gate -->",
        "## AutoEdd Architecture Gate",
        f"- 🔎 PR: {pr_url or 'n/a'}",
        f"- 🤖 Dictamen EDD: {edd_decision_text}",
        f"- {result_icon} Resultado final: {result_value}",
        f"- 🏷️ Codigo tecnico: {result.get('category', 'n/a')}",
        f"- 📦 Tipo de repositorio evaluado: {result.get('repo_type') or 'n/a'}",
        f"- 🧭 Etapas evaluadas o motivo de no evaluacion: {', '.join(evaluated_stages) if evaluated_stages else result.get('message', 'n/a')}",
        f"- 🛠️ Tecnologias evaluadas: {', '.join(technologies) if technologies else 'no informadas por el agente'}",
        f"- 🧬 Arquetipos seleccionados: {', '.join(archetype_ids) if archetype_ids else 'no informados por el agente'}",
        f"- 📝 Detalle validado: {summary_text}",
    ]
    if result.get("applies_edd_traditional"):
        lines.append("- 🧱 Observacion: aplica EDD tradicional")
    bindings = result.get("bindings") or {}
    if bindings:
        lines.append(f"- 🪢 Bindings resueltos: {json.dumps(bindings, ensure_ascii=False, sort_keys=True)}")
    evidence = result.get("rule_evidence") or []
    _, rule_descriptions = build_rulepack_catalog(rulepack or {})
    passed_evidence = [item for item in evidence if item.get("status") == "PASS"]
    relevant_evidence = [item for item in evidence if item.get("status") in {"FAIL", "WARN"}]
    if passed_evidence:
        lines.append("")
        lines.append("### Reglas validadas")
        for item in passed_evidence:
            rule_id = str(item.get("rule_id") or "sin_regla")
            description = rule_descriptions.get(rule_id) or item.get("message") or "Validacion conforme"
            lines.append(f"- ✅ {rule_id}: {description}")
    if relevant_evidence:
        lines.append("")
        lines.append("### Evidencia relevante")
        for item in relevant_evidence:
            status = str(item.get("status", "")).upper()
            marker = "❌" if status == "FAIL" else "⚠️"
            lines.append(f"- {marker} {item.get('rule_id')}: {item.get('message')}")
            summary = summarize_evidence_details(item.get("details") or {})
            if summary:
                lines.append(f"  {summary}")
    return "\n".join(lines) + "\n"


def build_error_result(category: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    # Los errores del wrapper también respetan el contrato para no romper el flujo del job.
    return {
        "contract_version": "1.0",
        "result": "FAIL" if category != "INVALID_PULL_REQUEST_BODY_INPUT" else "INPUT_INCOMPLETO",
        "category": category,
        "message": message,
        "repo_type": None,
        "evaluated_stage": None,
        "evaluated_stages": None,
        "technology": None,
        "technologies": None,
        "archetype_id": None,
        "archetype_ids": None,
        "applies_edd_traditional": False,
        "blocking": True,
        "bindings": {},
        "metadata": {"evaluation_backend": "wrapper"},
        "rule_evidence": [
            {
                "rule_id": category.lower(),
                "status": "FAIL",
                "message": message,
                "details": details or {},
            }
        ],
    }


# Construye el request contractual mínimo que el runner agente puede usar como contexto autorizado.
def build_agent_request(payload: dict[str, Any], policy_files: list[str], pr_url: str) -> dict[str, Any]:
    authorized_policy_files = []
    for filename in policy_files:
        suffix = Path(filename).suffix.lower()
        content_type = "json" if suffix == ".json" else "markdown" if suffix == ".md" else "yaml"
        authorized_policy_files.append({"path": filename, "content_type": content_type})

    # La policy resuelve el tipo de repositorio desde pull_request.url, que proviene del workflow del PR actual.
    agent_pr_url = str(pr_url)

    # El request limita explícitamente las fuentes que el runner agente puede usar.
    return {
        "contract_version": "1.0",
        "request_id": str(uuid.uuid4()),
        "pull_request": {
            "url": agent_pr_url,
            "body_source": "pull_request_body",
            "head_sha": os.getenv("PR_HEAD_SHA"),
        },
        "input_payload": payload,
        "authorized_policy_files": authorized_policy_files,
        "execution_rules": {
            "allowed_results": ["PASS", "FAIL", "INPUT_INCOMPLETO", "ARQUETIPO_INVALIDO"],
            "blocking_results": sorted(BLOCKING_RESULTS),
            "must_not_hallucinate": True,
            "must_cite_evidence": True,
        },
    }


# Ejecuta el runner agente por subprocess y recupera la respuesta JSON escrita a disco.
def run_agent_backend(agent_command: str, request_path: Path, response_path: Path, prompt_path: Path, context_path: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env["AUTOEDD_AGENT_REQUEST_PATH"] = str(request_path)
    env["AUTOEDD_AGENT_RESPONSE_PATH"] = str(response_path)
    env["AUTOEDD_AGENT_PROMPT_PATH"] = str(prompt_path)
    env["AUTOEDD_AGENT_CONTEXT_PATH"] = str(context_path)
    env.setdefault("AUTOEDD_AGENT_RAW_RESPONSE_PATH", ".code-review-effective/architecture-agent-raw-response.txt")
    env.setdefault("AUTOEDD_PLATFORM_ROOT", str(Path.cwd()))
    env.setdefault("AUTOEDD_POLICY_ROOT", str(prompt_path.parent))
    # El runner agente siempre responde vía archivo JSON contractual para mantener el wrapper estable.
    subprocess.run(agent_command, shell=True, check=True, env=env)
    return load_json(response_path)


# Resuelve desde configuración qué variable de entorno contiene el comando del runner agente.
def resolve_agent_command(config: dict[str, Any]) -> str | None:
    backend_cfg = config.get("review_gates", {}).get("architecture", {}).get("backend", {})
    command_env_name = backend_cfg.get("agent_command_env", "AUTOEDD_AGENT_COMMAND")
    return os.getenv(command_env_name)


# Orquesta el flujo completo del gate: input PR, contratos, ejecución del agente y artefactos finales.
def main() -> int:
    parser = argparse.ArgumentParser(description="Ejecuta el gate de arquitectura AutoEdd en modo hibrido.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--platform-root", required=True)
    args = parser.parse_args()

    platform_root = Path(args.platform_root)
    config_path = Path(args.config)
    config = load_yaml(config_path)
    architecture = config.get("review_gates", {}).get("architecture", {})
    policy_pack = architecture.get("policy_pack", {})
    backend_cfg = architecture.get("backend", {})
    base_dir = platform_root / policy_pack.get("base_dir", "policies/architecture")
    rulepack_document = load_yaml(base_dir / "rulepack.yml") if (base_dir / "rulepack.yml").exists() else {}
    files = resolve_policy_pack_files(base_dir, policy_pack.get("files", []))
    missing = validate_policy_pack(base_dir, files)
    event_pull_request = load_github_event_pull_request()
    pr_url = str(event_pull_request.get("html_url") or os.getenv("PR_URL", ""))

    if missing:
        result = build_error_result("MISSING_POLICY_PACK", "Faltan archivos requeridos del policy pack centralizado.", {"missing_files": missing})
        write_output(os.getenv("AUTOEDD_RESULT_JSON"), json.dumps(result, ensure_ascii=False, indent=2))
        write_output(os.getenv("AUTOEDD_RESULT_MD"), build_markdown_summary(result, pr_url, rulepack_document))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    input_source = architecture.get("input_source", {})
    if input_source.get("mode") != "pull_request_body":
        result = build_error_result("UNSUPPORTED_INPUT_SOURCE", "El gate centralizado solo soporta input_source.mode=pull_request_body.")
        write_output(os.getenv("AUTOEDD_RESULT_JSON"), json.dumps(result, ensure_ascii=False, indent=2))
        write_output(os.getenv("AUTOEDD_RESULT_MD"), build_markdown_summary(result, pr_url, rulepack_document))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    markers = input_source.get("markers", {})
    start_marker = markers.get("start", "<!-- AUTOEDD_INPUT_JSON_START -->")
    end_marker = markers.get("end", "<!-- AUTOEDD_INPUT_JSON_END -->")
    pr_body = str(event_pull_request.get("body") or os.getenv("PR_BODY", ""))
    try:
        payload = extract_json_from_pr_body(pr_body, start_marker, end_marker)
    except (ValueError, json.JSONDecodeError) as exc:
        result = build_error_result(
            "INVALID_PULL_REQUEST_BODY_INPUT",
            "No fue posible obtener un JSON valido desde el body del PR.",
            {"reason": str(exc), "required_start_marker": start_marker, "required_end_marker": end_marker},
        )
        write_output(os.getenv("AUTOEDD_RESULT_JSON"), json.dumps(result, ensure_ascii=False, indent=2))
        write_output(os.getenv("AUTOEDD_RESULT_MD"), build_markdown_summary(result, pr_url, rulepack_document))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    pr_url = str(pr_url)
    request_contract_path = platform_root / backend_cfg.get("agent_request_contract", "policies/architecture/agent_request_contract.schema.json")
    response_contract_path = platform_root / backend_cfg.get("agent_response_contract", "policies/architecture/agent_response_contract.schema.json")
    prompt_path = platform_root / backend_cfg.get("agent_prompt_file", "policies/architecture/agent_system_prompt.md")
    context_path = platform_root / backend_cfg.get("agent_context_file", "policies/architecture/agent_context.md")

    request_payload = build_agent_request(payload=payload, policy_files=files, pr_url=pr_url)
    request_errors = validate_against_schema(request_payload, request_contract_path)
    if request_errors:
        result = build_error_result("INVALID_AGENT_REQUEST", "El request generado para el agente no cumple el contrato estricto.", {"errors": request_errors})
        write_output(os.getenv("AUTOEDD_RESULT_JSON"), json.dumps(result, ensure_ascii=False, indent=2))
        write_output(os.getenv("AUTOEDD_RESULT_MD"), build_markdown_summary(result, pr_url, rulepack_document))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    agent_request_path = Path(os.getenv("AUTOEDD_AGENT_REQUEST_PATH", ".code-review-effective/architecture-agent-request.json"))
    agent_request_path.parent.mkdir(parents=True, exist_ok=True)
    agent_request_path.write_text(json.dumps(request_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    backend_mode = backend_cfg.get("mode", SUPPORTED_BACKEND_MODE)
    agent_command = resolve_agent_command(config)
    response_path = Path(os.getenv("AUTOEDD_AGENT_RESPONSE_PATH", ".code-review-effective/architecture-agent-response.json"))
    raw_response_path = os.getenv("AUTOEDD_AGENT_RAW_RESPONSE_PATH", ".code-review-effective/architecture-agent-raw-response.txt")
    if backend_mode != SUPPORTED_BACKEND_MODE:
        result = build_error_result(
            "UNSUPPORTED_BACKEND",
            "La arquitectura vigente solo soporta el modo agent-subprocess sobre GitHub Models.",
            {"backend_mode": backend_mode, "supported_backend_mode": SUPPORTED_BACKEND_MODE},
        )
    elif not agent_command:
        result = build_error_result("MISSING_AGENT_COMMAND", "No se definio AUTOEDD_AGENT_COMMAND para ejecutar el backend del agente.")
    else:
        # Toda la evaluacion semantica ocurre en el runner agente; el wrapper solo orquesta y valida.
        result = run_agent_backend(
            agent_command=agent_command,
            request_path=agent_request_path,
            response_path=response_path,
            prompt_path=prompt_path,
            context_path=context_path,
        )

    response_errors = validate_against_schema(result, response_contract_path)
    semantic_errors = validate_result_semantics(result) if not response_errors else []
    if response_errors or semantic_errors:
        error_details = {"errors": response_errors + semantic_errors}
        error_details.update(build_raw_response_details(raw_response_path))
        result = build_error_result(
            "INVALID_AGENT_RESPONSE",
            "La salida del backend de arquitectura no cumple el contrato estricto.",
            error_details,
        )

    result.setdefault("metadata", {})
    result["metadata"].setdefault("policy_mode", "hybrid-wrapper")
    result["metadata"].setdefault("backend_mode", backend_mode)
    result_json = json.dumps(result, ensure_ascii=False, indent=2)
    summary = build_markdown_summary(result, pr_url, rulepack_document)
    write_output(os.getenv("AUTOEDD_RESULT_JSON"), result_json)
    write_output(os.getenv("AUTOEDD_RESULT_MD"), summary)
    print(result_json)
    print(summary)
    return 0 if result.get("result") == "PASS" else 1


# Punto de entrada para ejecución directa del wrapper desde CLI o workflow.
if __name__ == "__main__":
    raise SystemExit(main())