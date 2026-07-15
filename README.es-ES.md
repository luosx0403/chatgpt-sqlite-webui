# ChatGPT Export Archiver

Idioma: [English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

Convierte tu ZIP oficial exportado por ChatGPT en un archivo SQLite privado y buscable.

`Local-first` · `SQLite` · `Privacy-first` · `Fast import` · `Fast Web index` · `Chat-style Web UI` · `Markdown/TXT export`

ChatGPT Export Archiver importa ZIP oficiales de OpenAI / ChatGPT directamente en SQLite, verifica el archivo, crea índices de búsqueda, abre una Web UI local y exporta conversaciones a Markdown o TXT. Está pensado para archivos personales a largo plazo, búsqueda offline, migración a bases de conocimiento y flujos locales que la UI oficial de historial no ofrece como archivos e índices.

## Por Qué Usarlo

- **Local-first y privado.** El ZIP, la base de datos, los exports, las copias temporales subidas, la Web UI y los logs permanecen en tu máquina salvo que los muevas tú.
- **Importación directa de ZIP.** Lee el ZIP oficial exportado por ChatGPT sin descomprimirlo ni fusionar shards manualmente.
- **Preparado para archivos grandes.** La ruta recomendada admite ZIP grandes, importaciones incrementales, reconstrucción FTS diferida e índices Web opcionales optimizados.
- **Lector estilo chat.** La Web UI usa por defecto una disposición similar a ChatGPT: user a la derecha, assistant a la izquierda, y system/internal plegados pero desplegables.
- **La vista técnica clásica sigue disponible.** Usa Settings o `?layout=classic` / `?messageLayout=classic` para volver al diseño anterior por filas.
- **Búsqueda de archivo más controlable.** Para archivos a largo plazo y búsqueda local, SQLite ofrece más control que la UI oficial de historial: filtros role/title/source/scope/exclude, frases, OR, paginación, verify, índices reconstruibles y exportación.
- **Exports portables.** Markdown y TXT se generan de forma determinista y sirven para backups, bases de conocimiento locales, grep offline o migración.

## Captura

Captura segura pendiente. Las capturas deben usar conversaciones synthetic, no títulos reales, snippets, raw JSON, emails ni rutas locales.

## Observaciones Locales De Smoke

Estas son observaciones de ejemplo en una sola máquina local, no una garantía universal:

- Un ZIP real exportado de unos 2.25 GB se importó en unos 98 segundos con la ruta para archivos grandes.
- `verify` sobre ese archivo terminó en unos 4 segundos.
- La reconstrucción del índice Web opcional tras un archivo incremental mayor terminó en unos 106 segundos.
- Una búsqueda Web de mensajes con muchos resultados en la app local Uvicorn respondió en unos 0.3 segundos.

## Qué Hace Este Proyecto

- Importa `conversations.json` y shards `conversations-*.json` desde un ZIP exportado por OpenAI / ChatGPT, un archivo `conversations.json` suelto o un directorio ya extraído.
- Conserva metadatos de conversaciones, mapping nodes, roles de mensajes, texto, marcas de tiempo, enlaces de padres, seguimiento de origen y advertencias de importación.
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

- Python 3.10 o más reciente. Python 3.12 es el objetivo probado para instalaciones Web reproducibles.
- SQLite con JSON1 y FTS5 habilitados. La mayoría de builds actuales de Python en macOS, Windows y Linux ya los incluyen.
- Node.js y npm solo si quieres reconstruir la Web UI en React o ejecutar comprobaciones de frontend. La entrega runnable incluye `webui/dist`, así que el uso local normal de la Web UI no requiere reconstruir el frontend.
- La CLI principal usa solo la biblioteca estándar de Python. Para ejecutar la Web UI, incluida la subida ZIP, instala `requirements-web.txt`; sin ese perfil el comando `web` falla de inmediato con una indicación de instalación.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py312.txt
```

En Windows PowerShell:

```bash
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py312.txt
```

En Windows cmd.exe:

```bash
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py312.txt
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

`--input` puede apuntar al ZIP oficial exportado, a un `conversations.json` suelto o a un directorio exportado ya extraído. Los directorios extraídos pueden contener `conversations.json` o archivos sharded `conversations-*.json`; no unas shards manualmente.

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input conversations.json --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input ./extracted-export/ --no-input-sha256 --rebuild-fts
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

La entrada puede ser un ZIP, un `conversations.json` suelto o un directorio extraído que contenga `conversations.json` o archivos sharded `conversations-*.json`. Scanner discovery ignora macOS metadata paths como `__MACOSX`, archivos AppleDouble `._*` y `.DS_Store`, por lo que esos artifact locales no se convierten en conversation source.

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

El diseño de lectura por defecto es `chat`: los mensajes user se alinean a la derecha, los mensajes assistant a la izquierda y los mensajes system/internal aparecen como notas plegadas. Para usar el diseño técnico anterior por filas, elige `Classic` en Settings o añade `?layout=classic` o `?messageLayout=classic` a la URL de la Web UI.

Todas las rutas comparten la misma regla effective-current para `path=current`: un `current_node` válido que pertenece a la conversación y su cadena de padres tiene prioridad incluso si todos los raw flags son cero; después se elige de forma determinista una cadena hoja utilizable con `is_on_current_path=1`; solo si ninguna existe esa conversación cae a all. La respuesta conserva el significado raw e incluye `current_node_exists`, `current_collection_source`, `current_path_fallback_to_all`, `effective_path` y visibilidad efectiva por nodo. Padres rotos y ciclos terminan con diagnósticos deterministas.

Las acciones de copiar y exportar del lector siguen el contrato visible del lector. `Copiar conversación de la ruta actual` obtiene todas las páginas de la ruta actual del reader y respeta la opción Show internal messages, pero ignora los filtros de búsqueda actuales. `Copiar visibles` copia solo los mensajes visibles ya cargados. Los enlaces de descarga usan la misma ruta actual y la misma opción Show internal. El acceso raw por mensaje es una vista previa raw ampliada con límite; las respuestas truncadas deben renderizar `raw_text` como texto plano de vista previa y la UI solo muestra esa capped preview.

Cuando el reader salta a un hit con `around_node_id`, usa la misma colección paginada que el reader: visible-only rows si Show internal está desactivado, la node collection completa si Show internal está activado y la effective all-node collection para conversaciones dañadas sin current-path node.

La Web UI puede usarse de dos formas. Si la base de datos ya existe, pásala de forma explícita o usa la ruta por defecto. Si no existe, arranca la Web UI igualmente y usa el panel de importación para subir un ZIP de ChatGPT. Las importaciones subidas se serializan para que solo haya un writer SQLite en el proceso.

Tras una importación Web correcta, el backend ejecuta el mismo import pipeline que la CLI y luego ejecuta `verify`, `stats` y `web-index`. El ZIP subido es una copia temporal del lado del servidor y se limpia de forma independiente del archivo original en tu disco.

Si no puede servirse la app React preconstruida, el fallback HTML es una interfaz de emergencia deliberadamente limitada, no un sustituto del reader completo. Tiene menos controles y las descargas excluyen nodos internal salvo petición explícita. Reconstruye `webui/dist` para la UI completa.

## Límites de seguridad de subida Web

Las subidas Web aplican límites de seguridad a nivel de aplicación antes de iniciar el job de importación. Estos se controlan mediante variables de entorno y son independientes del `import` de CLI (que no usa estos límites).

La subida Web reserva un pending slot antes de leer el archivo para que una subida grande no compita con otro writer. Cualquier error posterior a esa reserva, incluida una falla al crear la ruta temporal de subida, debe liberar el slot y limpiar el directorio temporal del servidor; un import job iniciado correctamente toma posesión del slot y de la copia temporal.

Cuando la Web UI está enlazada a una dirección loopback (`127.0.0.1`, `localhost`, `::1`), los valores por defecto permiten archivos grandes de confianza:

| Variable de entorno | Valor local por defecto | Controla |
|---|---|---|
| `CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES` | 20 GiB | Tamaño total del ZIP comprimido subido |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES` | 64 GiB | Tamaño máx. sin comprimir de un solo miembro JSON |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS` | 5,000 | Número máx. de miembros JSON de conversación |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES` | 128 GiB | Total máx. de datos JSON sin comprimir |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO` | 1,000.0 | Ratio máx. de compresión para miembros JSON grandes |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS` | 100,000 | Número máx. total de miembros ZIP |
| `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE` | unset | Ponlo en `local` solo en redes no loopback de confianza para usar valores locales por defecto en límites no definidos |

**Política de enlace remoto.** Un bind no loopback requiere opt-in mediante `CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS=true`, `CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS=true` o `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local`; de lo contrario se rechaza el arranque. Al activarlo, el servidor avisa que expone el archivo y aplica límites remote-safe de 128 MiB ZIP, 256 MiB por JSON member, 512 MiB total sin comprimir, 200 JSON members y 10,000 ZIP members. `ALLOW_REMOTE_UPLOADS` solo relaja límites explícitos; los demás siguen remote-safe. `REMOTE_UPLOAD_PROFILE=local` es solo para una LAN de confianza. No hay autenticación: el límite de confianza es la red y el firewall. La protección Origin/Sec-Fetch de escrituras no vuelve segura una red no confiable.

`/api/schema` informa la política efectiva, incluido el límite multipart body (límite ZIP más overhead acotado). El writer slot y el body cap a nivel receive actúan antes del parseo multipart. El spool del parser puede coexistir con el ZIP temporal del pipeline: reserva casi dos copias comprimidas más el crecimiento de la DB. JSON decode, SQLite y `web-index` consumen RAM, disco y CPU según el tamaño decodificado. Las subidas remotas requieren `Content-Length`; las chunked de loopback siguen limitadas durante streaming.

Para aumentar un límite local para un archivo legítimo grande, define la variable correspondiente antes de iniciar la Web UI:

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES=64424509440  # 60 GiB
python chatgpt_archive.py web --port 8787
```

```powershell
# Windows PowerShell
$env:CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES = 64424509440  # 60 GiB
python chatgpt_archive.py web --port 8787
```

```batch
:: Windows cmd.exe
set CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES=64424509440
python chatgpt_archive.py web --port 8787
```

Para permitir un límite explícito grande de ZIP comprimido en una red interna de confianza y mantener otros límites no definidos en remote-safe:

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS=true
export CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES=10737418240  # 10 GiB
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

```powershell
# Windows PowerShell
$env:CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS = "true"
$env:CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES = 10737418240  # 10 GiB
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

```batch
:: Windows cmd.exe
set CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS=true
set CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES=10737418240
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

Para usar el profile completo de subida local en una red interna de confianza:

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

Define límites más altos solo para archivos locales de confianza. Valores más altos aumentan el riesgo de ZIP bomb, presión de disco y uso de CPU/memoria.


## Checklist de aceptación de la Web UI

Usa esta lista cuando cambies la ruta Web o prepares una entrega runnable:

- Arranca la Web UI sin base de datos y confirma que sirve el contrato de estado vacío.
- Importa desde el navegador un ZIP pequeño de ChatGPT y confirma que el job termina.
- Confirma que el backend ejecuta `verify`, `stats` y `web-index` después de la importación subida.
- Recarga la página y confirma que las conversaciones se pueden listar y abrir.
- Reimporta un ZIP más reciente y confirma que la ruta incremental sigue funcionando.

La ruta Web de una entrega runnable no debería necesitar `webui/node_modules`, porque los assets React ya construidos se sirven desde `webui/dist`.

## Sintaxis de búsqueda

La búsqueda CLI usa sintaxis segura. Los términos normales son substring `contains` normalizado y usan AND; `OR` en mayúsculas crea alternativas. Las comillas conservan frases y `-term`/`-"frase"` excluyen. `word` aplica límites solo a letras ASCII, números y guion bajo; CJK conserva prudentemente `normalized contains`. Los modificadores raw `path:`/`scope:` de la consulta prevalecen sobre los selectores UI.

Las exclusiones son de nivel conversación para resultados de conversaciones: si cualquier título o mensaje dentro del scope y path de búsqueda seleccionados coincide con un fragmento excluido, esa conversation no se devuelve. `/api/search/messages` sigue devolviendo solo hits de mensajes que no contienen el fragmento excluido. `path:current` sigue la ruta del reader por conversación; si un archivo dañado no tiene ningún current-path node, la búsqueda current-path cae al mismo all-node view que muestra el reader.

Los filtros de fecha usan días UTC: inicio inclusivo a `00:00:00Z` y fin exclusivo a `00:00:00Z` del día siguiente. Los timestamps y fechas deterministas de nombres exportados por CLI usan UTC; el navegador muestra su zona local. La búsqueda Web admite 500 caracteres. Cmd/Ctrl+F solo ve filas renderizadas de la lista virtual; usa búsqueda del archivo o copia la conversación completa.

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

La búsqueda Web usa índices opcionales normalized trigram creados por `web-index`. Está pensada para búsquedas prácticas por subcadena en el navegador. Si esos índices opcionales faltan o se dañan, reconstruye:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

Los diagnostics de búsqueda son pistas de rendimiento best-effort. Solo deben informar capas candidatas normalized-safe o fallbacks de escaneo como normalized trigram, normalized scan, normalized title scan o full scan. La presencia de legacy raw FTS puede informarse por separado, pero no debe presentarse como el candidate backend real porque puede omitir texto equivalente tras normalización.

Si ejecutas manualmente `VACUUM`, `VACUUM INTO` o reescribes la base SQLite con una herramienta externa de compactación o backup, vuelve a ejecutar `python chatgpt_archive.py web-index --db <archive.db>` antes de confiar en la búsqueda de la Web UI. El índice Web opcional se reconstruye desde las tablas canónicas de conversaciones y es seguro regenerarlo.

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

Una entrega runnable debe incluir las fuentes Python, tests, documentación, `requirements-web.txt`, `constraints-web-py312.txt`, el código fuente y tests frontend bajo `webui/src` y `webui/tests`, los archivos de configuración/package frontend y los assets construidos bajo `webui/dist`. No debe incluir `webui/node_modules`, `webui/tsconfig.tsbuildinfo`, directorios de caché o bytecode de Python, cachés de coverage/typecheck, `.DS_Store`, archivos AppleDouble `._*`, `__MACOSX`, `Thumbs.db`, `Desktop.ini`, `.gitignore.md`, logs temporales, logs locales de aceptación, `*.log`, `*.ndjson`, `*.jsonl`, `archive/`, `exports/`, ningún `*.zip`, `conversations*.json`, bases de datos reales como `*.db`, `*.sqlite` y `*.sqlite3`, ni sidecars de SQLite como `*.db-journal`, `*.sqlite-wal`, `*.sqlite-shm`, `*.sqlite-journal`, `*.sqlite3-wal`, `*.sqlite3-shm` y `*.sqlite3-journal`. La comprobación de directorio permite el `.git` propio de la raíz objetivo para que un Git clone normal pueda verificarse, pero rechaza `.git` anidados; en un ZIP de entrega cualquier entrada `.git` falla.

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

La base principal almacena conversaciones, mapping nodes, import runs y warnings. Solo los objetos message conservan el objeto JSON raw del mensaje; conversation y mapping-node se normalizan, no se guardan byte por byte. El SHA-256 del ZIP de entrada es opcional y las columnas SHA por entry de `source_files`/`file_index` están reservadas y sin rellenar. La tabla FTS de CLI es `message_fts`. Las tablas auxiliares opcionales para búsqueda Web incluyen `web_message_norm`, `web_title_norm`, `web_message_trigram` y `web_title_trigram`, además de las shadow tables de SQLite FTS5.

El proyecto evita cambiar el schema de la base de datos durante pequeñas correcciones de robustez, salvo que exista una migración planificada y documentada explícitamente. Las bases de datos antiguas a las que les falten columnas nuevas se rechazan con diagnósticos `missing_columns` en `verify` o Web health, sin migrarlas en silencio; en ese caso, haz una copia de seguridad de la base antigua y vuelve a importar el export original en una base nueva.

## Límites conocidos

- Es una herramienta local de archivo, no un servicio de sincronización en la nube.
- La Web UI está pensada para uso local. No la expongas a redes no confiables sin añadir tus propios controles de acceso.
- El parser sigue el formato de exportación de OpenAI / ChatGPT observado hasta ahora. Si cambia el formato de origen, actualiza `inspect` y las pruebas antes de confiar en una nueva ruta de importación.
- Las partes de nombres de archivo exportados se sanean para Windows y sistemas tipo Unix, incluidos nombres reservados de dispositivo como `CON`, `AUX`, `COM1`, `LPT9`, `COM¹` y `LPT²`, además de puntos y espacios finales.
- Los archivos muy grandes pueden tardar en importarse, reconstruir FTS y crear índices Web trigram. Para importaciones grandes, prefiere la ruta `--rebuild-fts`.

## Contratos de seguridad y respuesta

El Web loopback solo acepta `localhost`, `127.0.0.1`, `::1`, el host loopback explícito y hosts configurados. Un bind no loopback exige el hostname/IP LAN real en `CHATGPT_ARCHIVE_ALLOWED_HOSTS`; se rechaza `*`. `CHATGPT_ARCHIVE_TRUSTED_PROXIES` usa un modelo estricto de un solo proxy edge: se ignoran forwarded headers de pares no confiables y el edge directo confiable debe sobrescribir los valores del cliente. Se rechazan Host/Forwarded repetidos, cadenas con comas, sintaxis inválida y conflictos entre `Forwarded` y `X-Forwarded-Host/Proto`. Todas las solicitudes validan Host y las escrituras remotas requieren un `Origin` de mismo origen.

Los fallos de importación usan etapas y códigos estables para preflight, escaneo de fuentes, decodificación JSON, contrato del nivel superior y transacción. El resumen del run fallido y las filas warning persistidas coinciden. Un commit canónico correcto no se describe como “no importado” si fallan después verify, stats o el índice Web opcional. Los fallos de limpieza temporal posteriores al commit son warnings no fatales y no exponen rutas del usuario.

Se rechazan `NaN` / `Infinity` JSON no estándar; los timestamps no finitos en cadenas se guardan como `NULL` con warning. `verify` detecta valores no finitos heredados y API, stats y export mantienen JSON finito. Effective-current informa cycle, missing parent, cross-conversation parent y partial chain. `internal_hidden_count` es el valor oficial; `technical_hidden_count` es un alias idéntico obsoleto. Con `count_total=false`, message search devuelve `total_exact=false` y `total` es solo un límite inferior conocido. Around-node distingue found, visible, pertenencia a effective collection y applied.

“Copiar URL” siempre serializa `match_mode`, `layout`, `show_internal` y el estado compartible de búsqueda/lector. Los valores explícitos de URL tienen prioridad sobre `localStorage`; solo los ausentes usan ajustes locales. Esta versión usa `replaceState` y no restaura el historial paso a paso de búsquedas o selecciones con Atrás/Adelante.

El Release ZIP se escribe en un temporal, verifica el manifest ordenado size/SHA-256 de cada payload, el conjunto exacto de members, assets dist y delivery check, y solo después sustituye atómicamente el destino. Un fallo conserva intacto el release anterior.

El resumen de rollback separa `attempted_*` de `committed_*` en cero. Un run fallido se persiste con una conexión nueva y se informa cualquier fallo secundario. Un fallo de limpieza pre-job conserva el error HTTP principal y añade `cleanup_warning`/`cleanup_error_type` seguros. Los ID de job solo aceptan 32 caracteres hexadecimales en minúscula.

JSON rechaza `NaN`/`Infinity` y desbordamientos como `1e9999`. Un timestamp inválido se guarda como `NULL` con warning que contiene solo campo y tipo. `verify` ejecuta `foreign_key_check`, cuenta por separado nodos y componentes de parent-cycle, y effective-current separa diagnósticos selected-chain y raw-flag.

Una página de message search siempre incluye `total_exact`: es true para DB vacía o resultado determinísticamente vacío, y false para un probe normal con `count_total=false`; conversation page no promete ese campo. Around metadata separa found, pertenencia effective-current, pertenencia requested-path, visible y applied. El fallback raw de texto válido está acotado y es común a reader, búsquedas, highlight, copy y export CLI/Web; raw inválido, sobredimensionado o realmente no textual conserva el placeholder.

Los filtros o exclusiones sin término pueden filtrar conversations, pero message hit, reader highlight y navegación requieren un término positivo de texto. Copiar URL usa un único contexto search/list/selection ya aplicado y no mezcla texto pendiente de debounce. Japonés y español se etiquetan claramente como traducciones parciales. El release valida antes una lista autoritativa de archivos requeridos independiente del collector; si falta source/config/doc, falla sin reemplazar el ZIP anterior.
