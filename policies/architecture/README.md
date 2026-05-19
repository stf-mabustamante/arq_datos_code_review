Policy Pack De Arquitectura
Esta carpeta es la fuente autorizada para el gate de arquitectura AutoEdd en el modelo actual wrapper + runner en GitHub Models. El wrapper solo orquesta el ciclo del check; la semantica de negocio debe vivir aqui.

Capas del modelo
agent_evaluation_flow.yml: flujo de decision, elegibilidad, aplicabilidad y seleccion de arquetipo.
contract_input.yml: contrato minimo esperado desde el JSON embebido en el PR.
rulepack.yml: arquetipos, reglas y evidencia exigida por etapa y tecnologia.
ini_field_mapping_for_agents.json: mapping estructurado autorizado para interpretar .ini TLS en runtime.
agent_request_contract.schema.json: contrato estricto del request que recibe el backend agente.
agent_response_contract.schema.json: contrato estricto de la respuesta estructurada.
agent_system_prompt.md: instrucciones invariantes del agente intermedio.
agent_context.md: contexto compacto autorizado para evitar heuristicas fuera del policy pack.
Regla de mantenimiento
Si cambia una regla de arquitectura, primero debe cambiar este directorio. Solo se toca Python cuando hace falta incorporar una nueva capacidad generica del wrapper o del runner de GitHub Models.

Convenciones activas del rulepack
Un mismo arquetipo puede mezclar reglas compartidas y reglas exclusivas por tipo de repositorio usando applies_when.repo_types.
stage_dataflow ya opera con esa forma: comparte la validacion de tecnologia entre orchestration_tls y raw_to_stage_repo, ejecuta templates/schema/*.json solo en raw_to_stage_repo y deja las reglas .ini solo para orchestration_tls.
universal_dbt sigue el mismo patron: comparte la validacion de tecnologia entre orchestration_tls y stage_to_universal_repo, deja las reglas .ini solo para orchestration_tls y valida en stage_to_universal_repo la carpeta raiz /<tabla_destino> y /<tabla_destino>/models/<tabla_destino>/profiles.yml en la ruta <tabla_destino>.outputs.productions.projects.
Cuando se agregue una regla nueva, primero se debe decidir si aplica a tls, stage, universal o a mas de uno; esa decision debe quedar declarada en rulepack.yml, no codificada en Python.
Restricciones
No inventar archivos, rutas ni campos fuera del pack.
No agregar logica de negocio en el workflow.
No mover reglas de negocio al wrapper o al runner si pueden declararse en rulepack.yml, agent_context.md o agent_system_prompt.md.