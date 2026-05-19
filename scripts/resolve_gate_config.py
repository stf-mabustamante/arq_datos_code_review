#!/usr/bin/env python3
"""Resuelve la configuracion efectiva combinando baseline central y override local."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


# Carga un YAML y normaliza archivos vacios o nulos a un diccionario vacio.
def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data or {}


# Mezcla recursivamente el override sobre la base, preservando valores base cuando el override viene nulo.
def deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = deep_merge(merged.get(key), value)
        return merged
    return override if override is not None else base


# Resuelve la configuracion efectiva desde CLI, aplica override opcional y escribe el resultado final.
def main() -> int:
    parser = argparse.ArgumentParser(description="Combina configuracion base y override de repositorio.")
    parser.add_argument("--base-config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--override-config")
    args = parser.parse_args()

    base_config = load_yaml(Path(args.base_config))
    effective_config = base_config

    if args.override_config and Path(args.override_config).exists():
        override_config = load_yaml(Path(args.override_config))
        effective_config = deep_merge(base_config, override_config)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(effective_config, handle, sort_keys=False)

    print(output_path.as_posix())
    return 0


# Punto de entrada para ejecucion directa desde CLI.
if __name__ == "__main__":
    raise SystemExit(main())