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
- SQLite con soporte JSON. FTS5 es opcional; si no está disponible o falta `message_fts`, se conserva la búsqueda segura por escaneo.
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

El archivo de constraints para Python 3.12 fija todas las versiones resueltas de las dependencias Web, pero no es un hash lock multiplataforma. Un flujo de publicación futuro debería generar y verificar hashes específicos para la matriz Python/SO admitida; hasta entonces, instala solo desde un índice de paquetes de confianza y conserva el lockfile de npm y las auditorías como controles independientes del frontend.

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

Las exportaciones CLI y Web usan por defecto la ruta actual efectiva y solo mensajes visibles. Usa explícitamente `--path all` y/o `--include-internal` para incluir ramas o mensajes internos; el manifest registra ambas opciones. La CLI lee nodos en lotes acotados, mientras que la descarga Web y `Copiar conversación de la ruta actual` usan streams de texto dedicados y acotados en el servidor. El texto canónico completo y el texto legacy/raw recuperable no dependen del presupuesto de respuesta del reader.

Reconstruye los índices opcionales de búsqueda Web:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

`web-index` normaliza mensajes y títulos por etapas explícitas y crea trigram cuando está disponible. Cada build usa nombres staging impredecibles y un lease persistente con owner token; un segundo build se rechaza con `web_index_build_in_progress` y la limpieza stale también valida la propiedad exacta. Todas las etapas usan keyset acotado, presupuestos separados para input, normalización, derivados y FTS bind, y reportan picos reales. El writer lock se libera entre lotes. Solo la publicación final usa una transacción `BEGIN IMMEDIATE` corta para volver a comprobar canonical generations, propiedad de objetos y metadata antes de publicar atómicamente. Hasta entonces los readers ven el índice anterior. Un cambio de generation, interrupción, error de disco o cancelación conserva el índice anterior y limpia solo los objetos de ese lease. `POST /api/import/jobs/{job_id}/web-index/cancel` solo se aplica a la etapa index del import job.

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

Para entradas de directorio, las plataformas POSIX usan `dir_fd` y `O_NOFOLLOW` componente por componente cuando están disponibles. El fallback portable rechaza componentes symlink/reparse y verifica containment inmediatamente antes de abrir por ruta, pero la biblioteca estándar de Python no puede eliminar todas las carreras de reemplazo local en cada plataforma. No importes un directorio extraído que un usuario local no confiable o un proceso concurrente pueda modificar; usa el ZIP original de solo lectura para ese modelo de amenaza.

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

La búsqueda global de current path obtiene primero candidatos de conversación independientes del path mediante índices normalizados de mensajes/títulos y predicados seguros de source/date/role; después materializa effective-current solo para esos candidatos. Las consultas solo de exclusión que no se pueden reducir usan un fallback explícito de base completa. La navegación de hits del reader carga una sola página compacta al inicio y añade páginas al acercarse al límite cargado. El SQL de búsqueda y Web-index usa una forma portable que evita flattening, sin exigir `AS MATERIALIZED`, y resuelve cada candidato legacy raw como máximo una vez por etapa lógica.

Las acciones de copiar y exportar del lector siguen el contrato visible del lector. `Copiar conversación de la ruta actual` usa el stream dedicado de texto completo para la ruta actual del reader y respeta la opción Show internal messages, pero ignora los filtros de búsqueda actuales. No acumula páginas del reader en el navegador. `Copiar visibles` copia solo los mensajes visibles ya cargados. Los enlaces de descarga usan la misma ruta actual y la misma opción Show internal. El acceso raw por mensaje es una vista previa raw ampliada con límite; las respuestas truncadas deben renderizar `raw_text` como texto plano de vista previa y la UI solo muestra esa capped preview.

Cuando el reader salta a un hit con `around_node_id`, usa la misma colección paginada que el reader: visible-only rows si Show internal está desactivado, la node collection completa si Show internal está activado y la effective all-node collection para conversaciones dañadas sin current-path node.

La Web UI puede usarse de dos formas. Si la base de datos ya existe, pásala de forma explícita o usa la ruta por defecto. Si no existe, arranca la Web UI igualmente y usa el panel de importación para subir un ZIP de ChatGPT. Las importaciones subidas se serializan para que solo haya un writer SQLite en el proceso.

Tras una importación Web correcta, el backend ejecuta el mismo import pipeline que la CLI y luego ejecuta `verify`, `stats` y `web-index`. El ZIP subido es una copia temporal del lado del servidor y se limpia de forma independiente del archivo original en tu disco.

Los fallos preflight y los trabajos terminales pueden devolver varios `cleanup_warnings`. La React UI muestra cada warning code seguro y cada `path_kind` con texto localizado, y conserva el fallback obsoleto `cleanup_warning`. No muestra rutas temporales, nombres de archivo, mensajes del SO ni detalles de clases de error; cada polling reemplaza el snapshot del mismo trabajo en vez de añadir avisos duplicados.

Si no puede servirse la app React preconstruida, el fallback HTML es una interfaz de emergencia deliberadamente limitada, no un sustituto del reader completo. Tiene menos controles y las descargas excluyen nodos internal salvo petición explícita. Reconstruye `webui/dist` para la UI completa.

## Límites de seguridad de subida Web

Las subidas Web aplican límites de seguridad a nivel de aplicación antes de iniciar el job de importación. Estos se controlan mediante variables de entorno y son independientes del `import` de CLI (que no usa estos límites).

La subida Web reserva un pending slot antes de leer el archivo para que una subida grande no compita con otro writer. Cualquier error posterior a esa reserva, incluida una falla al crear la ruta temporal de subida, debe liberar el slot y limpiar el directorio temporal del servidor; un import job iniciado correctamente toma posesión del slot y de la copia temporal.

Un kill del proceso, OOM o fallo del host puede omitir la limpieza normal y dejar un directorio antiguo `chatgpt-archive-upload-*` en el directorio temporal del sistema operativo. Esta versión no los elimina automáticamente porque un error de ownership/age podría borrar datos ajenos. Con el servidor detenido, un administrador solo puede borrar individualmente un directorio antiguo tras comprobar el prefijo exacto, la propiedad de la cuenta actual, su antigüedad, la ausencia de symlinks/reparse points y que ningún job lo use; nunca debe borrar el ZIP exportado por el usuario ni usar un wildcard sin verificar.

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

**Política de enlace remoto.** Un bind no loopback requiere `CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS=true`, `CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS=true` o `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local` como opt-in. Los valores remote-safe son 128 MiB ZIP, 256 MiB por JSON member, 512 MiB total sin comprimir, ratio 200.0, 200 JSON members y 10,000 ZIP members; el ratio loopback/local es 1000.0. `ALLOW_REMOTE_UPLOADS` solo relaja límites explícitos y los demás siguen remote-safe. `REMOTE_UPLOAD_PROFILE=local` restaura todos los límites no definidos a sus valores locales grandes y solo debe usarse en una LAN de confianza.

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

`message_fts_rebuildable` es una capacidad runtime real, no una constante: si falta FTS pero FTS5 está disponible informa `missing` y rebuildable true; sin módulo FTS5 informa `capability_unavailable` y false; una tabla dañada informa `damaged`, y la reconstrucción depende del mismo probe runtime acotado. Los demás errores SQLite se propagan al clasificador estructurado de base de datos.

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

La base canónica usa `PRAGMA user_version` (versión actual 5). La versión 3 añadió `NOT NULL` a identidades TEXT; la versión 4 añadió revisiones duraderas address/graph por campos; la versión 5 añade revisión display duradera por fila y estado de compatibilidad para que readers nuevos no reutilicen cursores o decisiones stale. Migration instala filas y triggers managed en la misma transacción bloqueada e invalida índices opcionales cuando procede. Las rutas readonly no ejecutan DDL de migration; una base compatible antigua devuelve `database_migration_required`. Verifica un backup externo antes de migrar.

Health y `verify` distinguen entre `message_fts` opcional ausente y dañado. El daño informa `optional_message_fts_error` con una indicación `--rebuild-fts`; los fallos generales malformed, locked, readonly, I/O y SQL runtime no se ocultan como capacidad ausente y usan `database_malformed`, `database_locked`, `database_readonly`, `database_io_error` o `database_runtime_failure`.

## Contratos Round 10 de recursos, propiedad y recuperación

Los objetos managed FTS, Web-index opcionales, staging, metadata, generation y shadow solo se modifican con DDL destructivo después de validar exactamente tipo, tabla, SQL y fingerprint de propiedad. Las colisiones devuelven `core_fts_name_collision`, `optional_index_name_collision` o `staging_name_collision`; un objeto de usuario no es desechable por parecerse de nombre. El Web index opcional usa formato 5, nombres staging impredecibles por build y un lease persistente con owner token. Un segundo build devuelve `web_index_build_in_progress`; la recuperación stale valida owner, identidad de base, schema, generations, formato y nombres. Input, normalización, derivados y FTS bind tienen presupuestos separados y contadores reales current/peak; la clasificación completa en stream impide ocultar un placeholder tras un prefijo mayor de 256 caracteres.

Los cursores de texto largo se vinculan a una revisión durable guardada directamente en la fila message objetivo. Triggers managed de insert/update incrementan la revisión en toda escritura que afecte al texto mostrado, incluso si un writer SQLite externo no actualiza `content_hash`; las filas no relacionadas no invalidan el cursor. Una base anterior a version 5 queda bajo el gate migration-required y la migración writer explícita rellena sus revisiones. El cursor también vincula un digest estable de identidad, impidiendo que reutilizar un rowid reactive un cursor antiguo.

La búsqueda exacta lee BLOB canonical incrementalmente en chunks solapados de 64 KiB y guarda resultados verificados en un artefacto TEMP local a la conexión, reutilizado para count/page/snippet/span/anchor. Los límites normales por fila son 32 MiB de caracteres y 32 MiB UTF-8; el opt-in local confiable admite 100 MiB de caracteres. El fallback raw-only tiene límites separados de 1 MiB/800.000 caracteres y cada solicitud presupuestos agregados y de candidatos independientes. Una solicitud acotada devuelve hits confirmados y diagnóstico partial/pending; si puede continuar el scan, devuelve un continuation firmado ligado a query, identidad DB y generations. `count_total=false` no afirma un total exacto. Un hit tardío incluye un byte anchor UTF-8 ligado a revisión para que el reader haga seek directo sin reproducir megabytes de páginas.

Cada conversation element nuevo tiene límites independientes de 32 MiB UTF-8, 32 MiB decodificados, 1.000.000 escalares léxicos y 5.000 mapping nodes; el sanitizer legacy/API tiene aparte un máximo de 250.000 escalares. `conversation_node_limit_exceeded` omite el elemento sin conservar contenido. El límite de 100.000 nodes del reader/effective-current/export existe solo para bases legacy o externas compatibles; no promete importar 100.000 nodes. Todos los shards ZIP seleccionados comparten una sesión de lectura y el descubrimiento de directorio es incremental. `parent` vacío es compatibilidad legacy root/missing-parent. Readiness valida longitud y Unicode inseguro en todos los campos address/graph; revisiones duraderas por campo invalidan caches sin consultar `PRAGMA data_version` en lecturas normales.

El bulk import del proyecto sustituye temporalmente triggers generation exactos y propios dentro de la misma transacción con write lock, incrementa una vez cada dominio dirty y restaura/valida los triggers. Rollback o crash restaura DDL/data; writers externos conservan triggers por statement. Los scopes finite effective-current se comparan exactamente en batches SQLite TEMP acotados. La exportación completa guarda plan y nodes en SQLite temporal y usa keyset streaming, sin un grafo Python de todo el archivo.

Con `--delete-input-on-success`, la ruta original del usuario existe hasta que termina el canonical commit. Después se escribe y fsync un journal durable ligado a identidad antes del rename. Una interrupción deja un token recuperable explícitamente con `python chatgpt_archive.py recover-delete-input --directory <dir> --token <token>` sin sobrescribir replacements. Secure delete se rechaza en Windows y sin operaciones descriptor-relative no-follow. Las constraints Web fijan versiones resueltas, pero no son un hash lock multiplataforma; usa un índice de paquetes confiable.

## Límites conocidos

- Es una herramienta local de archivo, no un servicio de sincronización en la nube.
- La Web UI está pensada para uso local. No la expongas a redes no confiables sin añadir tus propios controles de acceso.
- El parser sigue el formato de exportación de OpenAI / ChatGPT observado hasta ahora. Si cambia el formato de origen, actualiza `inspect` y las pruebas antes de confiar en una nueva ruta de importación.
- Las partes de nombres de archivo exportados se sanean para Windows y sistemas tipo Unix, incluidos nombres reservados de dispositivo como `CON`, `AUX`, `COM1`, `LPT9`, `COM¹` y `LPT²`, además de puntos y espacios finales.
- Los archivos muy grandes pueden tardar en importarse, reconstruir FTS y crear índices Web trigram. Para importaciones grandes, prefiere la ruta `--rebuild-fts`.

## Contratos de seguridad y respuesta

La entrada de carga acepta un solo valor para `Origin`, `Content-Length` y `Sec-Fetch-Site`. Origin debe ser un único origen HTTP(S) sin credenciales, ruta, consulta, fragmento, controles ni cadenas separadas por comas; Content-Length debe ser un entero decimal ASCII no negativo en forma canónica. Los encabezados duplicados o mal formados se rechazan antes de analizar multipart, y una configuración de ratio inválida o no finita vuelve al valor seguro y finito del perfil.

El Web loopback solo acepta `localhost`, `127.0.0.1`, `::1`, el host loopback explícito y hosts configurados. Un bind no loopback exige el hostname/IP LAN real en `CHATGPT_ARCHIVE_ALLOWED_HOSTS` (o `--allowed-hosts`); se rechaza `*`. `CHATGPT_ARCHIVE_TRUSTED_PROXIES` (o `--trusted-proxies`) usa un modelo estricto de un solo proxy edge: se ignoran forwarded headers de pares no confiables y el edge directo confiable debe sobrescribir los valores del cliente. Se rechazan Host/Forwarded repetidos, cadenas con comas, sintaxis inválida y conflictos entre `Forwarded` y `X-Forwarded-Host/Proto`. Todas las solicitudes validan Host y las escrituras remotas requieren un `Origin` de mismo origen. Solo el perfil loopback confiable permite clientes sin Origin; las subidas siempre rechazan `Sec-Fetch-Site: cross-site`.

Los fallos incluyen una etapa source-read y códigos estables `upload_preflight_failed`, `input_source_open_failed`, `input_source_not_regular_file`, `source_read_failed`, `source_changed_during_read`, `invalid_conversation_encoding` y `json_integer_too_large`. La limpieza usa `cleanup_warnings` estructurado; `cleanup_warning` conserva solo el primer elemento por compatibilidad.

El JSON independiente, directorios y ZIP usan el mismo framer de una pasada y una sola transacción; cada elemento se escanea y decodifica una vez, con límites de 32 MiB para UTF-8 y caracteres decodificados, 256 niveles y 1.000.000 escalares léxicos. El sanitizer iterativo de legacy raw limita por separado a 250.000 escalares, el recorrido a 100.000 nodos, el raw preview a 80.000 bytes y el payload API saneado completo a 4 MiB. Todas las entradas del directorio central ZIP y del directorio cuentan para el máximo de 100.000 members. Se acepta solo un BOM inicial y se rechazan las demás codificaciones inválidas. Los ID canónicos nuevos se limitan a 512 caracteres; `/api/by-id/*` admite legacy ID hasta 16 Ki caracteres y un ID antiguo mayor deja readiness en `database_data_incompatible`.

La identidad se valida con stat/hash/read ligados al descriptor. `--delete-input-on-success` usa staging rename atómico y una barrera final; una carrera de nombre ocupado que no puede recuperarse genera `delete_input_recovery_required`. Migration solo acepta predecesores conocidos con definición exacta y rechaza antes de DDL cualquier objeto que ocupe un nombre managed con tipo, destino o definición incorrectos mediante `database_managed_object_name_collision`.

Los JSON no estándar `NaN`/`Infinity`, incluidos números estándar desbordados como `1e9999`, se rechazan; los timestamps inválidos se guardan como `NULL` con un warning sin contenido. La API de mensajes devuelve un único `display_text` limitado por el presupuesto del reader y usa metadata de truncamiento/total-exactness para indicar si puede recuperarse completo, sin duplicar `content_text`/`render_text`. Las lecturas CLI/Web ordinarias y `/api/health` por defecto usan un schema gate acotado y no ejecutan `foreign_key_check`; `verify` y `/api/health?deep=true` realizan la comprobación exacta completa con campos de freshness. Cada lectura lógica CLI/Web de varias sentencias inicia un único snapshot SQLite antes de los probes de schema/capability y lo libera al terminar o fallar el stream. Se conservan las semánticas effective-current, pagination y around-node.

“Copiar URL” siempre serializa `match_mode`, `layout`, `show_internal` y el estado compartible de búsqueda/lector. Los valores explícitos de URL tienen prioridad sobre `localStorage`; solo los ausentes usan ajustes locales. Esta versión usa `replaceState` y no restaura el historial paso a paso de búsquedas o selecciones con Atrás/Adelante.

El Release ZIP usa metadata fija para bytes reproducibles, verifica el manifest SHA-256 y los members, y solo después sustituye atómicamente el destino. Un fallo conserva intacto el release anterior.

El resumen de rollback separa `attempted_*` de `committed_*` en cero. Un run fallido se persiste con una conexión nueva y se informa cualquier fallo secundario. Un fallo de limpieza pre-job conserva el error HTTP principal y añade `cleanup_warning`/`cleanup_error_type` seguros. Los ID de job solo aceptan 32 caracteres hexadecimales en minúscula.

JSON rechaza `NaN`/`Infinity` y desbordamientos como `1e9999`. Un timestamp inválido se guarda como `NULL` con warning que contiene solo campo y tipo. Las lecturas CLI/Web ordinarias y `/api/health` por defecto usan una puerta de schema acotada y no ejecutan `foreign_key_check`; `verify` y `/api/health?deep=true` procesan en stream la comprobación completa. El total es exacto, la sample en memoria está acotada y los campos de modo, tiempo de terminación, generation y stale declaran el contrato; el coste de CPU/VM crece con la base. Los contadores effective-current mantienen sus unidades documentadas.

El texto largo usa un cursor opaco ligado a la revisión y lecturas incrementales BLOB de SQLite; el offset numérico de compatibilidad se limita a 1.048.576 caracteres y después se exige el cursor. Raw preview usa una consulta BLOB acotada segura para NUL e informa tamaño en bytes y si es exacto. NUL visibles y surrogates aislados se representan siempre como U+FFFD, mientras el raw JSON sigue escapado. Los escalares de resultados tienen presupuestos explícitos y metadata de truncamiento/longitud; los ID no se truncan.

La exportación CLI/Web rechaza antes de materializar más de 100.000 nodos, 32 MiB de entrada canonical/raw en un nodo o 128 MiB por conversación; la salida en stream se limita a 256 MiB. Effective-current usa el mismo límite online de 100.000 nodos y 128 MiB de entrada de ID de grafo por conversación. CLI escribe un temporal en el directorio destino mientras calcula hash y compara el archivo anterior en stream, y solo reemplaza atómicamente contenido distinto. La copia del navegador usa `ReadableStream` y, antes de tocar el portapapeles, aborta sobre 16 MiB UTF-8 u 8 Mi caracteres y recomienda Descargar; nunca copia texto parcial.

La exportación completa escanea conversations una vez hacia un plan SQLite temporal en el directorio de salida, asigna nombres seguros en disco y transmite hashes y manifests JSONL/CSV. Rechaza más de 1.000.000 conversations, 1 GiB de metadata del plan o 2 GiB por manifest. Effective-current global limita 100.000 conversations, 1.000.000 nodes, 512 MiB de grafo y 1 GiB temporal estimado, con batches de 20.000 filas/nodes y 64 MiB.

Una página de message search siempre incluye `total_exact`: es true para DB vacía o resultado determinísticamente vacío, y false para un probe normal con `count_total=false`; conversation page no promete ese campo. Around metadata separa found, pertenencia effective-current, pertenencia requested-path, visible y applied. El fallback raw de texto válido está acotado y es común a reader, búsquedas, highlight, copy y export CLI/Web; raw inválido, sobredimensionado o realmente no textual conserva el placeholder.

Los filtros o exclusiones sin término pueden filtrar conversations, pero message hit, reader highlight y navegación requieren un término positivo de texto. Copiar URL usa un único contexto search/list/selection ya aplicado y no mezcla texto pendiente de debounce. Japonés y español se etiquetan claramente como traducciones parciales. El release valida antes una lista autoritativa de archivos requeridos independiente del collector; si falta source/config/doc, falla sin reemplazar el ZIP anterior.

La validación devuelve como máximo 16 elementos seguros con solo `location`, `field` y `code` público estable. La verificación exacta de candidatos, snippets tardíos, enrichment y respuesta serializada están acotados. Agotar el presupuesto en candidatos posteriores no descarta hits confirmados: la respuesta marca partial/pending y ofrece continuation ligado cuando puede reanudar. Un candidato que supera el límite duro por fila queda pending, no se convierte en false-exact miss. El índice Web mide bytes leídos, normalizados y enlazados a FTS. El release calcula hash, escribe y verifica por chunks.

Python `zipfile` y el pipeline de importación admiten estructuras ZIP64, con una regresión de member ZIP64 forzado pequeño; la aceptación habitual no genera un ZIP físico superior a 4 GiB. Siguen aplicándose todos los límites de members, bytes, ratio de compresión, disco y CPU.

Una exportación CLI/Web larga mantiene deliberadamente un único SQLite read snapshot hasta terminar, fallar o desconectarse el cliente. En modo WAL, un reader largo puede retrasar el checkpoint y permitir que el WAL crezca con writers concurrentes; duración, CPU/VM, WAL y disco temporal siguen siendo proporcionales a los datos elegidos. No se debe romper la consistencia del snapshot para adelantar el checkpoint.

`npm run build` usa `webui/scripts/build.mjs`, hace typecheck, construye en un staging directory hermano, valida todos los assets citados por el `index.html` staged, publica primero los assets y reemplaza atómicamente `dist/index.html` al final. Su self-test de fallo inyectado verifica que un build fallido conserva utilizables el entry point y los assets anteriores.

Los candidatos se verifican exactamente mediante BLOB incremental con límites predeterminados independientes de 32 Mi caracteres y 32 MiB UTF-8. Las pruebas locales de confianza pueden usar `CHATGPT_ARCHIVE_SEARCH_EXACT_VERIFY_CHARS` hasta 100 Mi caracteres; ese opt-in explícito también permite la capacidad UTF-8 válida correspondiente. Al agotar el presupuesto de candidatos se devuelven partial/pending y un continuation firmado cuando está disponible; un legacy que supera el límite duro por fila queda pending, nunca false-exact. El cursor largo liga revisión e identidad de la fila objetivo y el anchor de búsqueda guarda directamente el byte offset UTF-8.
