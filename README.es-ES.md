# ChatGPT Export Archiver

Idioma: English | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

ChatGPT Export Archiver es una herramienta local, pensada para la privacidad, que convierte los ZIP exportados desde OpenAI / ChatGPT en un archivo SQLite buscable. Mantiene el export original fuera del navegador, admite importaciones incrementales repetibles, ofrece exportación y búsqueda desde CLI, e incluye una Web UI local en React para navegar e importar ZIP desde el navegador.

## Qué hace este proyecto

- Importa `conversations.json` desde un ZIP exportado por OpenAI / ChatGPT o desde un directorio ya extraído.
- Conserva metadatos de conversaciones, mapping nodes, roles de mensajes, texto, marcas de tiempo, enlaces de padres y advertencias de importación.
- Admite importación incremental. Al reimportar un export más reciente en la misma base de datos, actualiza las conversaciones que cambiaron sin duplicar de forma intencionada los datos sin cambios.
- Crea un índice opcional FTS5 para búsqueda por CLI.
- Crea índices Web opcionales de búsqueda por subcadenas para acelerar la búsqueda en el navegador.
- Exporta conversaciones como Markdown, TXT o ambos.
- Incluye `verify`, `stats` e `inspect`, con una salida prudente que no imprime el texto de los chats.
- Incluye una Web UI local que puede arrancar aunque todavía no exista una base de datos y permite importar ZIP desde el navegador.
- Separa los logs de la salida estructurada de comandos y evita registrar títulos, snippets, raw JSON o cuerpos de mensajes.

## Privacidad

Todo se ejecuta localmente. La base de datos, los archivos exportados, las copias temporales subidas, la Web UI y los logs permanecen en tu máquina salvo que los muevas o publiques tú. La CLI imprime deliberadamente IDs, recuentos, marcas de tiempo y estados, no fragmentos de mensajes. Los summaries de la CLI y los logs no imprimen cuerpos de conversación, títulos, snippets, raw JSON, rutas completas de entrada/salida ni nombres reales de ZIP; el summary de importación informa solo el tipo de entrada, por ejemplo `source zip`. La Web UI está pensada para uso local y se enlaza por defecto a `127.0.0.1`.

En los summaries de importación, `valid_conversations` cuenta los elementos conversation de entrada que se parsearon correctamente antes de fusionar ids duplicados. Cuando se fusionan ids duplicados, puede ser mayor que los recuentos finales de cambios en base de datos: `inserted_conversations`, `updated_conversations` o `unchanged_conversations`.

`inspect` y los errores del scanner no imprimen por defecto nombres reales de ZIP ni rutas completas. Los comandos CLI que requieren una base de datos existente, como `verify`, `stats`, `search` y `export`, informan `database_not_found` cuando la ruta de la base de datos es incorrecta y no crean un archivo SQLite vacío. La búsqueda Web usa los índices trigram opcionales como capa de candidatos cuando están disponibles, y luego sigue aplicando los filtros de subcadena normalizados, de modo que las consultas cortas, símbolos y casos sin soporte trigram vuelven de forma segura al fallback.

`--delete-input-on-success` solo se ejecuta después de que la transacción principal de importación haya terminado correctamente. Si la entrada explícita es un symlink, elimina el symlink indicado en la línea de comandos, no el ZIP real al que apunta.

La base de datos y los Markdown / TXT exportados pueden contener conversaciones privadas. Trata `archive/*.db`, los archivos exportados y tus ZIP originales de ChatGPT como datos sensibles.

## Requisitos

- Python 3.10 o más reciente.
- SQLite con JSON1 y FTS5 habilitados. La mayoría de builds actuales de Python en macOS, Windows y Linux ya los incluyen.
- Node.js y npm solo si quieres reconstruir la Web UI en React o ejecutar comprobaciones de frontend. La entrega runnable incluye `webui/dist`, así que el uso local normal de la Web UI no requiere reconstruir el frontend.
- Para usar la subida de ZIP desde la Web UI, instala las dependencias de `requirements-web.txt`.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements-web.txt
```

En Windows PowerShell:

```bash
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-web.txt
```

En Windows cmd.exe:

```bash
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -r requirements-web.txt
```

## Inicio rápido

Coloca el ZIP exportado por ChatGPT fuera del repositorio y ejecuta el comando de importación seguro más rápido. Omite el hash de entrada y reconstruye FTS una sola vez al final, lo que suele ser mucho más rápido en archivos grandes que mantener FTS fila por fila.

```bash
NEW_ZIP="$HOME/Downloads/chatgpt_export/chatgpt_export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Equivalente en Windows PowerShell:

```bash
$env:NEW_ZIP = "$env:USERPROFILE\Downloads\chatgpt-export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$env:NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Equivalente en Windows cmd.exe:

```bash
set NEW_ZIP=%USERPROFILE%\Downloads\chatgpt-export.zip
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "%NEW_ZIP%" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Arranca la Web UI local:

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

Si todavía no existe una base de datos, la Web UI arranca igualmente y muestra un estado vacío con un panel de importación. Puedes elegir un ZIP exportado por ChatGPT en el navegador; el backend escribe una copia temporal local, lo importa y luego ejecuta automáticamente `verify`, `stats` y `web-index`.

```bash
python chatgpt_archive.py web --port 8787
```

## Flujo CLI habitual

Inspecciona un export sin imprimir contenido de chat:

```bash
python chatgpt_archive.py inspect --input "$NEW_ZIP"
```

Crea explícitamente una base de datos vacía:

```bash
python chatgpt_archive.py init --db archive/chatgpt_archive.db
```

Importa con la ruta recomendada para archivos grandes:

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

Verifica la coherencia estructural:

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

Muestra recuentos estructurados y límites temporales:

```bash
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Busca texto de mensajes mediante la ruta de búsqueda de CLI. Imprime conversation IDs, node IDs y roles, no snippets:

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db --limit 20 "python sqlite"
```

Exporta la conversación como Markdown, TXT o ambos formatos en la misma ejecución. `--format md` escribe archivos de cuerpo Markdown y actualiza el manifest, `--format txt` escribe archivos de cuerpo en plain text y actualiza el manifest, y `--format all` escribe ambos formatos de cuerpo y actualiza el manifest:

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format txt --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format all --out exports
```

Exporta un rango de fechas y reescribe archivos existentes si hace falta. Los límites de fecha de `--from` y `--to` solo aceptan `YYYY-MM-DD`:

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports --from 2024-01-01 --to 2024-12-31 --force
```

El summary de exportación informa recuentos de archivos de cuerpo. `written` cuenta archivos Markdown/TXT cuyos bytes finales cambiaron, y `skipped_unchanged` cuenta archivos Markdown/TXT sin cambios. Los manifest se actualizan cuando hace falta, pero no se incluyen en esos dos recuentos.

Reconstruye los índices opcionales de búsqueda Web:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

Arranca la Web UI:

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

## Modos de importación

El comando recomendado para archivos grandes es:

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

Si quieres que SQLite dedique tiempo adicional a ordenar estadísticas del planner y el índice FTS después de importar, usa:

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --optimize-after-import --optimize-fts-after-import
```

`--delete-input-on-success` está desactivado por defecto. Úsalo solo si ya tienes otra copia de seguridad del ZIP. El borrado se ejecuta únicamente después de que la transacción principal de importación haya terminado con éxito. Si el borrado funciona, la CLI imprime `deleted_input True` sin ruta. Si el borrado falla, la importación sigue siendo correcta, el run queda como `finished`, se guarda un warning estructurado `delete_input_failed`, y la CLI imprime solo `delete_input_failed True` y el tipo de excepción.

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --delete-input-on-success
```

Las importaciones incrementales son una ruta normal de uso. Al importar un export más reciente en la misma base de datos, se actualizan las conversaciones cambiadas y se conserva el resto del archivo.

## Flujo de la Web UI

La Web UI es una aplicación React local servida por FastAPI. La ruta recomendada es servir los archivos preconstruidos de `webui/dist` incluidos en el árbol runnable.

```bash
python chatgpt_archive.py web --port 8787
```

La Web UI puede usarse de dos formas. Si la base de datos ya existe, pásala de forma explícita o usa la ruta por defecto. Si no existe, arranca la Web UI igualmente y usa el panel de importación para subir un ZIP de ChatGPT. Las importaciones subidas se serializan para que solo haya un writer SQLite en el proceso.

Tras una importación Web correcta, el backend ejecuta el mismo import pipeline que la CLI y luego ejecuta `verify`, `stats` y `web-index`. El ZIP subido es una copia temporal del lado del servidor y se limpia de forma independiente del archivo original en tu disco.


## Checklist de aceptación de la Web UI

Usa esta lista cuando cambies la ruta Web o prepares una entrega runnable:

- Arranca la Web UI sin base de datos y confirma que sirve el contrato de estado vacío.
- Importa desde el navegador un ZIP pequeño de ChatGPT y confirma que el job termina.
- Confirma que el backend ejecuta `verify`, `stats` y `web-index` después de la importación subida.
- Recarga la página y confirma que las conversaciones se pueden listar y abrir.
- Reimporta un ZIP más reciente y confirma que la ruta incremental sigue funcionando.

La ruta Web de una entrega runnable no debería necesitar `webui/node_modules`, porque los assets React ya construidos se sirven desde `webui/dist`.

## Sintaxis de búsqueda

La búsqueda CLI usa la sintaxis segura del proyecto, no texto de consulta SQLite sin procesar. Puedes usar palabras normales, frases entre comillas, exclusiones `-term`, `OR` y filtros como `role:user`, `source:zip`, `path:current`, `path:all`, `scope:title` y `scope:message`. Imprime conversation IDs, node IDs y roles, no snippets.

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

La búsqueda Web usa índices opcionales normalized trigram creados por `web-index`. Está pensada para búsquedas prácticas por subcadena en el navegador. Si esos índices opcionales faltan o se dañan, reconstruye:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

## Verificación e índices Web opcionales

`verify` revisa la integridad de SQLite y la coherencia propia del proyecto, incluidos current nodes faltantes, enlaces de padres rotos, conversaciones vacías y ciclos de padres.

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

Si `PRAGMA integrity_check` informa de un inverted index FTS5 malformado en `web_message_trigram` o `web_title_trigram`, los datos principales de conversación pueden seguir siendo estructuralmente válidos mientras que el índice opcional de búsqueda Web está dañado. En ese caso `verify` informa `optional_web_index_error true` y muestra una pista de recuperación. Reconstruye los índices Web opcionales con:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

El diagnóstico es conservador. Solo se marca como problema opcional de índice Web cuando todos los errores de integrity-check pueden atribuirse a esas tablas opcionales o a sus FTS5 shadow tables.

## Logging

Los niveles de log son `debug`, `info`, `warning`, `error` y `none`. El nivel por defecto es `warning`. Los niveles más detallados incluyen los niveles más silenciosos. Los logs no incluyen títulos, snippets, raw JSON ni cuerpos de mensajes.

Las opciones de logging pueden ir antes o después del subcomando:

```bash
python chatgpt_archive.py --log-level debug web
python chatgpt_archive.py web --log-level debug
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --log-level info --log-file logs/import.log
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --json-logs --log-file logs/import.jsonl
```

Mantén los logs JSON en ubicaciones ignoradas como `logs/`. Los archivos `*.jsonl` son artefactos locales de log y delivery clean los rechaza.

Los campos de tiempo de importación incluyen `source_scan_seconds`, `parse_and_upsert_seconds`, `fts_rebuild_seconds`, `finalize_commit_seconds`, `close_seconds`, `legacy_pre_commit_seconds`, `wall_total_seconds` y `total_import_seconds`. `total_import_seconds` es el wall time de extremo a extremo, incluido commit final y close.

Después de que la transacción de importación haya terminado correctamente, las actualizaciones posteriores del summary son best-effort. `summary_update_after_commit_failed`, `import_connection_close_failed` y `summary_update_after_close_failed` son advertencias, no motivos para marcar como fallida una importación correcta.

## Desarrollo y comprobaciones de aceptación

Ejecuta las comprobaciones de Python y limpia artefactos seguros antes del primer delivery clean:

```bash
python -m compileall chatgpt_archive.py chatgpt_export_archiver tests tools
python -m unittest discover -s tests -v
python tools/clean_generated_artifacts.py --fail-on-blocked
python tools/check_delivery_clean.py --mode runnable .
```

Construye la Web UI y ejecuta smoke tests:

```bash
cd webui
npm ci
npm run typecheck
npm run build
npm run test:python-resolution
npm run test:dom
cd ..
python tools/clean_generated_artifacts.py --fail-on-blocked
python tools/check_delivery_clean.py --mode runnable .
```

`clean_generated_artifacts.py` es multiplataforma y conserva `webui/dist`. Solo elimina archivos que se pueden regenerar con seguridad. No elimina bases de datos, ZIP, sidecars de SQLite, `archive/`, `exports/` ni `logs/`; si delivery clean sigue informando esas rutas sensibles, muévelas fuera de la raíz del proyecto o elimínalas manualmente. Los comandos de aceptación usan `--fail-on-blocked`, de modo que los restos sensibles detienen el flujo de entrega inmediatamente.

En Windows PowerShell o cmd, usa comillas dobles para las search queries que contienen espacios, por ejemplo `"python sqlite"` o `"role:user path:current python -pandas"`. Los comandos de Python, Web, Web index, typecheck, build, cleanup y delivery-check anteriores funcionan en macOS, Windows y Linux cuando Python y Node están en el `PATH`. Si Windows usa el Python launcher, ejecuta el helper de limpieza con `py -3 tools/clean_generated_artifacts.py --fail-on-blocked`.

Para comprobar un ZIP de entrega:

```bash
python tools/check_delivery_clean.py --mode runnable path/to/delivery.zip
```

## Notas de entrega

Una entrega runnable debe incluir las fuentes Python, tests, documentación, `requirements-web.txt` y `webui/dist`. No debe incluir `webui/node_modules`, `webui/tsconfig.tsbuildinfo`, directorios de caché o bytecode de Python, cachés de coverage/typecheck, `.DS_Store`, `__MACOSX`, `Thumbs.db`, `Desktop.ini`, `.gitignore.md`, logs temporales, logs locales de aceptación, `*.log`, `*.ndjson`, `*.jsonl`, `archive/`, `exports/`, ningún `*.zip`, `conversations*.json`, bases de datos reales como `*.db`, `*.sqlite` y `*.sqlite3`, ni sidecars de SQLite como `*.db-journal`, `*.sqlite-wal`, `*.sqlite-shm`, `*.sqlite-journal`, `*.sqlite3-wal`, `*.sqlite3-shm` y `*.sqlite3-journal`. La comprobación de directorio permite el `.git` propio de la raíz objetivo para que un Git clone normal pueda verificarse, pero rechaza `.git` anidados; en un ZIP de entrega cualquier entrada `.git` falla.

Una entrega source-only puede omitir `webui/dist`, pero entonces habrá que reconstruir el frontend antes de servir la React UI completa.

## Guía del árbol de código

```text
chatgpt_archive.py                 CLI entry point
chatgpt_export_archiver/cli.py     CLI commands and reusable import pipeline
chatgpt_export_archiver/db.py      SQLite schema, import helpers, verify, stats, FTS helpers
chatgpt_export_archiver/web_app.py FastAPI app factory and static UI serving
chatgpt_export_archiver/web_api.py Web API routes
chatgpt_export_archiver/web_db.py  Web query helpers and optional trigram index builder
chatgpt_export_archiver/web_jobs.py Web ZIP import job manager
webui/                             React frontend source and built dist files
tests/                             Python unit and integration tests
tools/                             Delivery and support scripts
```

## Resumen de la base de datos

La base principal almacena conversaciones, mapping nodes, import runs y warnings. La tabla FTS de CLI es `message_fts`. Las tablas auxiliares opcionales para búsqueda Web incluyen `web_message_norm`, `web_title_norm`, `web_message_trigram` y `web_title_trigram`, además de las shadow tables de SQLite FTS5.

El proyecto evita cambiar el schema de la base de datos durante pequeñas correcciones de robustez, salvo que exista una migración planificada y documentada explícitamente.

## Límites conocidos

- Es una herramienta local de archivo, no un servicio de sincronización en la nube.
- La Web UI está pensada para uso local. No la expongas a redes no confiables sin añadir tus propios controles de acceso.
- El parser sigue el formato de exportación de OpenAI / ChatGPT observado hasta ahora. Si cambia el formato de origen, actualiza `inspect` y las pruebas antes de confiar en una nueva ruta de importación.
- Los archivos muy grandes pueden tardar en importarse, reconstruir FTS y crear índices Web trigram. Para importaciones grandes, prefiere la ruta `--rebuild-fts`.
