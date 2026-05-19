Paquete Centralizado Solo Arquitectura
Este paquete contiene solo el gate de arquitectura para publicarlo inicialmente en bch-dryrun/arq_datos_code_review y consumirlo desde master.

Contenido
.github/workflows/reusable-architecture-gate.yml: reusable workflow central solo para arquitectura.
config/defaults/review-gates.yml: configuracion minima con arquitectura habilitada.
config/defaults/requirements-architecture.txt: dependencias Python del gate.
scripts/: wrapper, runner y merge de configuracion.
policies/architecture/: policy pack activo del agente.
examples/workflow-stub/: stub consumidor apuntando a bch-dryrun/arq_datos_code_review@master.
examples/test-scenarios/06_universal_real_pr_vmc_complete.json: escenario completo de prueba con repos de ejemplo en bch-dryrun.
Publicacion sugerida
Crear o usar el repositorio bch-dryrun/arq_datos_code_review.
Subir el contenido de esta carpeta y dejar disponible el reusable en master.
En un repo consumidor de prueba de bch-dryrun, copiar examples/workflow-stub/.github/workflows/org-pr-gates.yml.
Si quieres override local, copiar tambien examples/workflow-stub/.code-review/repo-config.yml.
Abrir un PR en uno de los repos de ejemplo con el JSON del escenario dentro del body entre <!-- AUTOEDD_INPUT_JSON_START --> y <!-- AUTOEDD_INPUT_JSON_END -->.
Repos de ejemplo para la prueba end-to-end
bch-dryrun/tls_parametros_sgt
bch-dryrun/sgt_stage_cu1
bch-dryrun/sgt_universal_cu1
Observaciones
Este paquete no incluye jobs de calidad ni seguridad.
El nombre AutoEdd se conserva solo en el gate de arquitectura.
El modelo por defecto del workflow reusable queda en openai/gpt-4.1 porque openai/gpt-5-mini sigue excediendo el limite de request actual.
El policy pack de delivery es repo-local: cada PR evalua solo el repositorio actual y el rulepack decide que reglas aplican a tls, stage o universal dentro de un mismo arquetipo.
En stage_dataflow, la regla de tecnologia aplica a tls y stage; la validacion de templates/schema/*.json aplica solo a stage; las validaciones de .ini aplican solo a tls.
En universal_dbt, la regla de tecnologia aplica a tls y universal; las validaciones de .ini aplican solo a tls; las validaciones de carpeta raiz y profiles.yml aplican solo a universal.