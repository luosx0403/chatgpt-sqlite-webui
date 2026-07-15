from __future__ import annotations

import ipaddress
import json
import math
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .web_api import WebTrustPolicy, create_api_router
from .web_jobs import ImportJobManager
from .sqlite_errors import sqlite_runtime_error_code


class _UploadBodyTooLarge(Exception):
    pass


def _json_finite(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_finite(item) for item in value]
    return value


class FiniteJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return super().render(_json_finite(content))


def _host_name(value: str) -> str | None:
    if not value or any(char in value for char in "\r\n/\\@"):
        return None
    try:
        return urlsplit("//" + value).hostname
    except ValueError:
        return None


def _normalized_authority(value: str, scheme: str) -> str | None:
    if not value or value != value.strip() or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit("//" + value)
        if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
            return None
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname:
        return None
    hostname = hostname.casefold()
    if port in ({"http": 80, "https": 443}.get(scheme), None):
        port = None
    authority_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{authority_host}:{port}" if port is not None else authority_host


class TrustedAccessMiddleware:
    """Validate Host using a strict single-hop trusted edge-proxy contract.

    A trusted edge must overwrite client-supplied forwarding headers. Repeated
    headers, comma chains, malformed quoted values, or conflicting Forwarded
    and X-Forwarded values are rejected instead of guessing which hop to trust.
    """

    def __init__(self, app, *, policy: WebTrustPolicy) -> None:
        self.app = app
        self.policy = policy
        self._proxy_networks = tuple(ipaddress.ip_network(value, strict=False) for value in policy.trusted_proxies)

    @staticmethod
    def _headers(scope) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for key, value in scope.get("headers", []):
            result.setdefault(key.decode("latin-1").lower(), []).append(value.decode("latin-1"))
        return result

    def _trusted_proxy(self, scope) -> bool:
        client = scope.get("client")
        if not client or not self._proxy_networks:
            return False
        try:
            address = ipaddress.ip_address(client[0])
        except ValueError:
            return False
        return any(address in network for network in self._proxy_networks)

    @staticmethod
    def _split_forwarded_element(value: str) -> list[str] | None:
        parts: list[str] = []
        current: list[str] = []
        quoted = False
        escaped = False
        for char in value:
            if escaped:
                current.append(char)
                escaped = False
            elif char == "\\" and quoted:
                escaped = True
            elif char == '"':
                quoted = not quoted
                current.append(char)
            elif char == "," and not quoted:
                return None
            elif char == ";" and not quoted:
                parts.append("".join(current).strip())
                current = []
            else:
                current.append(char)
        if quoted or escaped:
            return None
        parts.append("".join(current).strip())
        return parts

    @classmethod
    def _forwarded_values(cls, headers: dict[str, list[str]]) -> tuple[str | None, str | None, bool]:
        for name in ("forwarded", "x-forwarded-host", "x-forwarded-proto"):
            if len(headers.get(name, [])) > 1:
                return None, None, False

        forwarded_host: str | None = None
        forwarded_proto: str | None = None
        raw_forwarded = headers.get("forwarded", [])
        if raw_forwarded:
            parts = cls._split_forwarded_element(raw_forwarded[0])
            if parts is None:
                return None, None, False
            seen: set[str] = set()
            for item in parts:
                key, separator, raw_value = item.partition("=")
                key = key.strip().casefold()
                raw_value = raw_value.strip()
                if not separator or not key or key in seen or not raw_value:
                    return None, None, False
                seen.add(key)
                if raw_value.startswith('"'):
                    if len(raw_value) < 2 or not raw_value.endswith('"'):
                        return None, None, False
                    clean = raw_value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                elif '"' in raw_value or any(char.isspace() for char in raw_value):
                    return None, None, False
                else:
                    clean = raw_value
                if key == "host":
                    forwarded_host = clean
                elif key == "proto":
                    forwarded_proto = clean.casefold()

        x_host_values = headers.get("x-forwarded-host", [])
        x_proto_values = headers.get("x-forwarded-proto", [])
        x_host = x_host_values[0].strip() if x_host_values else None
        x_proto = x_proto_values[0].strip().casefold() if x_proto_values else None
        if (x_host and "," in x_host) or (x_proto and "," in x_proto):
            return None, None, False
        if forwarded_host and x_host:
            comparison_scheme = forwarded_proto or x_proto or "http"
            if _normalized_authority(forwarded_host, comparison_scheme) != _normalized_authority(
                x_host, comparison_scheme
            ):
                return None, None, False
        if forwarded_proto and x_proto and forwarded_proto != x_proto:
            return None, None, False
        host = forwarded_host or x_host
        proto = forwarded_proto or x_proto
        if proto is not None and proto not in {"http", "https"}:
            return None, None, False
        return host, proto, True

    @staticmethod
    async def _reject(send, code: str = "host_not_allowed") -> None:
        await UploadIngressMiddleware._error(send, 400, code)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = self._headers(scope)
        host_values = headers.get("host", [])
        if len(host_values) != 1:
            await self._reject(send, "invalid_host_header")
            return
        effective_host = host_values[0]
        effective_scheme = scope.get("scheme", "http")
        trusted_proxy = self._trusted_proxy(scope)
        if trusted_proxy:
            forwarded_host, forwarded_proto, valid_forwarding = self._forwarded_values(headers)
            if not valid_forwarding:
                await self._reject(send, "invalid_forwarded_headers")
                return
            effective_host = forwarded_host or effective_host
            if forwarded_proto:
                effective_scheme = forwarded_proto
        normalized_authority = _normalized_authority(effective_host, effective_scheme)
        hostname = _host_name(normalized_authority or "")
        if hostname is None or hostname.casefold() not in self.policy.allowed_hosts:
            await self._reject(send)
            return
        state = scope.setdefault("state", {})
        state["trusted_host"] = normalized_authority
        state["trusted_hostname"] = hostname.casefold()
        state["trusted_scheme"] = effective_scheme
        state["trusted_proxy"] = trusted_proxy
        await self.app(scope, receive, send)


class UploadIngressMiddleware:
    """Reserve the writer slot and cap upload bytes before multipart parsing."""

    def __init__(self, app, *, manager: ImportJobManager, body_limit: int, trust_policy: WebTrustPolicy) -> None:
        self.app = app
        self.manager = manager
        self.body_limit = body_limit
        self.trust_policy = trust_policy

    @staticmethod
    def _headers(scope) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for key, value in scope.get("headers", []):
            result.setdefault(key.decode("latin-1").lower(), []).append(value.decode("latin-1"))
        return result

    def _origin_allowed(self, headers: dict[str, list[str]], scope) -> tuple[bool, str | None]:
        sec_fetch_site = headers.get("sec-fetch-site", [])
        if sec_fetch_site and sec_fetch_site[0].casefold() == "cross-site":
            return False, "upload_origin_not_allowed"
        origin_values = headers.get("origin", [])
        if not origin_values:
            if self.trust_policy.allow_missing_origin_for_writes:
                return True, None
            return False, "upload_origin_required"
        origin = origin_values[0]
        if (
            origin != origin.strip()
            or not origin
            or "," in origin
            or any(ord(char) < 32 or ord(char) == 127 for char in origin)
        ):
            return False, "upload_origin_not_allowed"
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False, "upload_origin_not_allowed"
        if parsed.path or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
            return False, "upload_origin_not_allowed"
        try:
            origin_hostname = parsed.hostname.casefold() if parsed.hostname else ""
        except ValueError:
            return False, "upload_origin_not_allowed"
        origin_authority = _normalized_authority(parsed.netloc, parsed.scheme)
        state = scope.get("state", {})
        allowed = (
            parsed.scheme in {"http", "https"}
            and origin_hostname in self.trust_policy.allowed_hosts
            and origin_authority == state.get("trusted_host", "")
            and parsed.scheme == state.get("trusted_scheme", scope.get("scheme", "http"))
        )
        return allowed, None if allowed else "upload_origin_not_allowed"

    @staticmethod
    async def _error(send, status: int, code: str) -> None:
        body = json.dumps({"detail": code, "code": code}, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") != "/api/import/upload":
            await self.app(scope, receive, send)
            return
        headers = self._headers(scope)
        duplicate_codes = {
            "origin": "upload_duplicate_origin_header",
            "content-length": "upload_duplicate_content_length",
            "sec-fetch-site": "upload_duplicate_sec_fetch_site",
        }
        for name, code in duplicate_codes.items():
            if len(headers.get(name, [])) > 1:
                await self._error(send, 400, code)
                return
        origin_allowed, origin_error = self._origin_allowed(headers, scope)
        if not origin_allowed:
            await self._error(send, 403, origin_error or "upload_origin_not_allowed")
            return
        length_values = headers.get("content-length", [])
        raw_length = length_values[0] if length_values else None
        if raw_length is None and self.trust_policy.remote:
            await self._error(send, 411, "upload_content_length_required")
            return
        if raw_length is not None:
            if len(raw_length) > 20 or re.fullmatch(r"0|[1-9][0-9]*", raw_length, flags=re.ASCII) is None:
                await self._error(send, 400, "upload_invalid_content_length")
                return
            content_length = int(raw_length)
            if content_length > self.body_limit:
                await self._error(send, 413, "upload_multipart_body_too_large")
                return
        if not self.manager.acquire_pending_upload_slot():
            await self._error(send, 409, "import_job_active")
            return
        state = scope.setdefault("state", {})
        state["upload_slot_reserved"] = True
        state["upload_slot_transferred"] = False
        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.body_limit:
                    raise _UploadBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _UploadBodyTooLarge:
            if not response_started:
                await self._error(send, 413, "upload_multipart_body_too_large")
        finally:
            if not state.get("upload_slot_transferred"):
                self.manager.release_pending_upload_slot()


def create_app(
    db_path: Path | None = None,
    static_dir: Path | None = None,
    allow_fallback: bool = False,
    log_level: str = "warning",
    host: str = "127.0.0.1",
    allowed_hosts: str | None = None,
    trusted_proxies: str | None = None,
) -> FastAPI:
    if db_path is None:
        db_path = Path("archive/chatgpt_archive.db")
    db_path = db_path.resolve()
    from .web_api import _get_upload_policy, _get_web_trust_policy, _remote_access_allowed

    upload_policy = _get_upload_policy(host=host)
    if upload_policy.remote and not _remote_access_allowed():
        raise ValueError(
            "non_loopback_access_requires_opt_in: set CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS=true "
            "only on a trusted network"
        )
    trust_policy = _get_web_trust_policy(
        host=host,
        allowed_hosts=allowed_hosts,
        trusted_proxies=trusted_proxies,
    )
    manager = ImportJobManager(db_path, log_level=log_level)
    app = FastAPI(
        title="ChatGPT Archive Web",
        docs_url=None,
        redoc_url=None,
        default_response_class=FiniteJSONResponse,
    )

    @app.exception_handler(sqlite3.Error)
    async def sqlite_error_response(_request, exc: sqlite3.Error):
        code = sqlite_runtime_error_code(exc)
        status = 503 if code in {"database_locked", "database_readonly", "database_io_error"} else 500
        return FiniteJSONResponse(
            status_code=status,
            content={"detail": {"code": code, "error_type": type(exc).__name__}},
        )

    app.include_router(create_api_router(db_path, manager, upload_policy=upload_policy, trust_policy=trust_policy))
    from .web_api import upload_body_limit

    app.add_middleware(
        UploadIngressMiddleware,
        manager=manager,
        body_limit=upload_body_limit(upload_policy),
        trust_policy=trust_policy,
    )
    app.add_middleware(TrustedAccessMiddleware, policy=trust_policy)

    build_dir = static_dir or Path(__file__).resolve().parent.parent / "webui" / "dist"
    if build_dir.exists() and (build_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=build_dir, html=True), name="webui")
    else:
        if not allow_fallback:
            raise ValueError(
                "React Web UI build is missing. Run: cd webui && npm ci && npm run build. "
                "Use --allow-fallback only for the limited emergency HTML UI."
            )

        @app.get("/", response_class=HTMLResponse)
        def missing_build():
            return """
            <!doctype html>
            <html><head><title>ChatGPT Archive Web</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
            body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f6f4ef;color:#1f2933}
            .shell{display:grid;grid-template-columns:minmax(280px,34vw) 1fr;height:100vh}
            .fallback-warning{background:#7f1d1d;color:white;padding:12px 16px;font-weight:800}
            .fallback-warning code{background:rgba(255,255,255,.18);padding:2px 4px;border-radius:4px}
            aside{border-right:1px solid #ddd;background:#fbfaf7;overflow:auto} main{overflow:auto}
            .search{padding:16px;border-bottom:1px solid #ddd} input{width:100%;padding:10px;border:1px solid #ccc;border-radius:8px}
            .item{display:block;width:100%;text-align:left;border:0;background:transparent;padding:12px;border-bottom:1px solid #eee;cursor:pointer}
            .item:hover,.item.selected{background:#ece8df}.title{font-weight:700}.meta,.snippet{font-size:12px;color:#667085;margin-top:4px}
            header{padding:16px 22px;border-bottom:1px solid #ddd;position:sticky;top:0;background:#f9f7f2}
            .msg{max-width:940px;margin:18px auto;padding:14px 16px;border:1px solid #ddd;border-radius:8px;background:#fff}
            .msg.user{background:#eef6f1}.role{font-size:12px;font-weight:700;color:#667085}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;line-height:1.55}
            mark{background:#ffe68a;border-radius:3px}.row{display:flex;gap:8px;margin-top:8px}select,button,a{padding:7px 10px;border:1px solid #ccc;border-radius:7px;background:#fff;color:inherit;text-decoration:none}
            @media(max-width:820px){.shell{grid-template-columns:1fr}aside{height:42vh;border-right:0;border-bottom:1px solid #ddd}}
            </style></head>
            <body>
              <div class="fallback-warning">Limited minimal fallback UI — not the full React UI. Some features (internal messages, search filters, raw JSON, full reader) are not available here. Build the full UI with <code>cd webui && npm ci && npm run build</code>.</div>
              <div class="shell">
                <aside>
                  <div class="search">
                    <input id="q" maxlength="500" placeholder='Search, "exact phrase", role:user, -exclude'>
                    <div class="row">
                      <select id="sort"><option value="relevance">Relevance</option><option value="newest">Newest</option><option value="oldest">Oldest</option><option value="title">Title</option></select>
                      <select id="path"><option value="current">Current path</option><option value="all">All nodes</option></select>
                    </div>
                    <p class="meta">Fallback UI. Build React UI with <code>cd webui && npm ci && npm run build</code>.</p>
                  </div>
                  <div id="list"></div>
                </aside>
                <main>
                  <header><h2 id="heading">Select a conversation</h2><div id="info" class="meta"></div><div id="actions" class="row"></div></header>
                  <section id="messages"></section>
                </main>
              </div>
              <script>
              const q=document.getElementById('q'), list=document.getElementById('list'), messages=document.getElementById('messages'), heading=document.getElementById('heading'), info=document.getElementById('info'), actions=document.getElementById('actions');
              const sort=document.getElementById('sort'), pathSel=document.getElementById('path'); let selected=null, timer=null;
              const esc=s=>String(s??'').replace(/[&<>"'`]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;','`':'&#96;'}[c]));
              const date=v=>v!==null&&v!==undefined?new Date(v*1000).toLocaleString():'';
              const errorCode=p=>{const d=p&&p.detail; const c=d&&typeof d==='object'?d.code:(typeof d==='string'?d:p&&p.code); return typeof c==='string'&&/^[a-z0-9_:-]+$/.test(c)?c:'request_failed'};
              async function api(url){const r=await fetch(url,{headers:{Accept:'application/json'}}); let data=null; try{data=await r.json()}catch{} if(!r.ok)throw new Error(errorCode(data)); if(!data||typeof data!=='object')throw new Error('invalid_response'); return data;}
              const safeError=e=>({database_migration_required:'Database migration required. Create a verified backup, then run the CLI migrate command.',database_schema_newer:'Database schema is newer than this app.',database_schema_incompatible:'Database schema is incompatible. Run CLI verify.',database_foreign_key_violation:'Database references are damaged. Run CLI verify.',database_locked:'Database is locked; retry after the writer finishes.',database_malformed:'Database is malformed; restore a verified backup.',database_readonly:'Database cannot be opened read-only.',database_io_error:'Database I/O error.'}[e&&e.message]||'The local archive request failed.');
              function showError(target,error){target.textContent=safeError(error); target.className='meta';}
              async function loadList(){try{const p=new URLSearchParams({q:q.value,sort:sort.value,path:pathSel.value,limit:'50'}); const data=await api('/api/conversations?'+p); const items=Array.isArray(data.items)?data.items:[]; list.innerHTML=items.map(x=>`<button class="item ${x.conversation_id===selected?'selected':''}" data-id="${esc(x.conversation_id)}"><span class="title">${esc(x.title||'untitled')}</span><div class="meta">${date(x.update_time??x.create_time)}${x.hit_count?' · '+x.hit_count+' hits':''}</div><div class="snippet">${esc((x.snippets&&x.snippets[0]&&x.snippets[0].snippet)||'')}</div></button>`).join('')}catch(error){showError(list,error)}}
              async function openConv(id){try{selected=id; const d=await api('/api/conversations/'+encodeURIComponent(id)); heading.textContent=d.title||'untitled'; info.textContent=`Created ${date(d.create_time)} · Updated ${date(d.update_time)} · ${d.current_path_nodes??0}/${d.node_count??0} raw current flags; effective path ${d.effective_path||pathSel.value}`; actions.innerHTML=`<a href="/api/conversations/${encodeURIComponent(id)}/export?format=md&path=${pathSel.value}&include_internal=false">Download visible MD</a><a href="/api/conversations/${encodeURIComponent(id)}/export?format=txt&path=${pathSel.value}&include_internal=false">Download visible TXT</a>`; const p=new URLSearchParams({q:q.value,path:pathSel.value,limit:'300',include_internal:'false'}); const page=await api('/api/conversations/'+encodeURIComponent(id)+'/messages?'+p); const items=Array.isArray(page.items)?page.items:[]; messages.innerHTML=items.map(m=>`<article class="msg ${esc(m.role||'message')}"><div class="role">${esc(m.role||'message')} · ${date(m.create_time??m.update_time)}</div><pre>${esc(m.display_text||'[empty]')}</pre></article>`).join(''); await loadList()}catch(error){showError(messages,error)}}
              list.addEventListener('click',e=>{const b=e.target.closest('button[data-id]'); if(b) openConv(b.dataset.id);});
              q.addEventListener('input',()=>{clearTimeout(timer); timer=setTimeout(loadList,220)}); sort.addEventListener('change',loadList); pathSel.addEventListener('change',()=>selected?openConv(selected):loadList());
              window.addEventListener('keydown',e=>{const t=e.target; const typing=t&&(['INPUT','TEXTAREA','SELECT'].includes(t.tagName)||t.isContentEditable); if((!typing&&e.key==='/')||((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k')){e.preventDefault();q.focus();}});
              loadList();
              </script>
            </body></html>
            """
    return app
