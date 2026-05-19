# Contexto de arquitectura para el agente

Recordatorios operativos minimos para el modelo actual `wrapper + agente intermedio sobre GitHub Models`.

## Axiomas operativos

- Si el input no califica para evaluacion automatica, no se revisa ningun repositorio.
- Si el resultado final es `PASS`, el mensaje debe dejar claro que aplica EDD automatico.
- Si el resultado final no es `PASS`, el mensaje debe dejar claro que se debe solicitar EDD tradicional con el equipo de Arquitectura de Datos.
- Si el input no habilita una etapa, esa etapa no se evalua.
- Si el rulepack no define un arquetipo o una regla, no se crea implicitamente.
- Si una regla declara `applies_when.repo_types`, solo se ejecuta cuando el repositorio actual esta incluido en esa lista.
- Para TLS, toda lectura de `.ini` debe apegarse a `ini_field_mapping_for_agents.json`.
- Para TLS, pueden coexistir arquetipos `raw`, `stage` y `universal`; deben evaluarse todos los habilitados por el input.
- El campo top-level `status` del input no participa en la decision.
- Los snapshots de repositorio son curados por el runner: si una ruta no viene en el snapshot, no debe asumirse su contenido.

## Recordatorios por tecnologia

### DBT universal

- Buscar en TLS un `.ini` `UNI` con `TASK=fw_ingesta_tasks.dbt/run:dbt_run`.
- En repositorios `universal`, la tabla destino se resuelve desde `$.properties.stage_to_universal_mapeo.destino[].tabla`.
- En repositorios `universal`, debe existir la carpeta raiz `/<tabla_destino>`.
- En repositorios `universal`, `gcp_project_prod` debe aparecer dentro de `/<tabla_destino>/models/<tabla_destino>/profiles.yml` en la ruta `<tabla_destino>.outputs.productions.projects`.
- En TLS, el directorio DBT relevante puede corroborarse con `MESSAGE.dbt_project` del ini `UNI` cuando este fragmento este disponible.

### Dataflow stage

- La tecnologia se resuelve desde `$.properties.raw_to_stage_technology`.
- En repositorios `stage`, la tabla destino se resuelve desde `$.properties.raw_to_stage_mapeo.table` y debe existir un archivo `templates/schema/*.json` cuyo nombre contenga la tabla sin el prefijo `tbl_` cuando ese prefijo exista.
- Para TLS se debe leer `TASK=fw_ingesta_tasks.dataflow/run:create_job_templated` cuando el rulepack lo declare.
- Los campos `project_id`, `service_account_email`, `schema_path`, `bq_table_id` e `input_file_path` viven dentro de `MESSAGE`.