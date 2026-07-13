from __future__ import annotations

import ipaddress
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .web_api import WebTrustPolicy, create_api_router
from .web_jobs import ImportJobManager


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


class TrustedAccessMiddleware:
    """Validate Host globally and honor forwarded headers only from trusted peers."""

    def __init__(self, app, *, policy: WebTrustPolicy) -> None:
        self.app = app
        self.policy = policy
        self._proxy_networks = tuple(ipaddress.ip_network(value, strict=False) for value in policy.trusted_proxies)

    @staticmethod
    def _headers(scope) -> dict[str, str]:
        return {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}

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
    def _forwarded_values(headers: dict[str, str]) -> tuple[str | None, str | None]:
        host = None
        proto = None
        forwarded = headers.get("forwarded", "").split(",", 1)[0]
        for item in forwarded.split(";"):
            key, separator, value = item.strip().partition("=")
            if not separator:
                continue
            clean = value.strip().strip('"')
            if key.casefold() == "host":
                host = clean
            elif key.casefold() == "proto":
                proto = clean
        host = host or headers.get("x-forwarded-host", "").split(",", 1)[0].strip() or None
        proto = proto or headers.get("x-forwarded-proto", "").split(",", 1)[0].strip() or None
        return host, proto

    @staticmethod
    async def _reject(send) -> None:
        await UploadIngressMiddleware._error(send, 400, "host_not_allowed")

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = self._headers(scope)
        effective_host = headers.get("host", "")
        effective_scheme = scope.get("scheme", "http")
        trusted_proxy = self._trusted_proxy(scope)
        if trusted_proxy:
            forwarded_host, forwarded_proto = self._forwarded_values(headers)
            effective_host = forwarded_host or effective_host
            if forwarded_proto in {"http", "https"}:
                effective_scheme = forwarded_proto
        hostname = _host_name(effective_host)
        if hostname is None or hostname.casefold() not in self.policy.allowed_hosts:
            await self._reject(send)
            return
        state = scope.setdefault("state", {})
        state["trusted_host"] = effective_host.casefold()
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
    def _headers(scope) -> dict[str, str]:
        return {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}

    def _origin_allowed(self, headers: dict[str, str], scope) -> tuple[bool, str | None]:
        if headers.get("sec-fetch-site", "").casefold() == "cross-site":
            return False, "upload_origin_not_allowed"
        origin = headers.get("origin")
        if not origin:
            if self.trust_policy.allow_missing_origin_for_writes:
                return True, None
            return False, "upload_origin_required"
        parsed = urlsplit(origin)
        origin_hostname = parsed.hostname.casefold() if parsed.hostname else ""
        state = scope.get("state", {})
        allowed = (
            parsed.scheme in {"http", "https"}
            and origin_hostname in self.trust_policy.allowed_hosts
            and parsed.netloc.casefold() == state.get("trusted_host", "")
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
        origin_allowed, origin_error = self._origin_allowed(headers, scope)
        if not origin_allowed:
            await self._error(send, 403, origin_error or "upload_origin_not_allowed")
            return
        raw_length = headers.get("content-length")
        if raw_length is None and self.trust_policy.remote:
            await self._error(send, 411, "upload_content_length_required")
            return
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError:
                await self._error(send, 400, "upload_invalid_content_length")
                return
            if content_length < 0:
                await self._error(send, 400, "upload_invalid_content_length")
                return
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
              async function loadList(){const p=new URLSearchParams({q:q.value,sort:sort.value,path:pathSel.value,limit:'50'}); const r=await fetch('/api/conversations?'+p); const data=await r.json(); list.innerHTML=data.items.map(x=>`<button class="item ${x.conversation_id===selected?'selected':''}" data-id="${esc(x.conversation_id)}"><span class="title">${esc(x.title||'untitled')}</span><div class="meta">${date(x.update_time??x.create_time)}${x.hit_count?' · '+x.hit_count+' hits':''}</div><div class="snippet">${esc((x.snippets&&x.snippets[0]&&x.snippets[0].snippet)||'')}</div></button>`).join('');}
              async function openConv(id){selected=id; const d=await (await fetch('/api/conversations/'+encodeURIComponent(id))).json(); heading.textContent=d.title||'untitled'; info.textContent=`Created ${date(d.create_time)} · Updated ${date(d.update_time)} · ${d.current_path_nodes??0}/${d.node_count??0} raw current flags; effective path ${d.effective_path||pathSel.value}`;               actions.innerHTML=`<a href="/api/conversations/${encodeURIComponent(id)}/export?format=md&path=${pathSel.value}&include_internal=false">Download visible MD</a><a href="/api/conversations/${encodeURIComponent(id)}/export?format=txt&path=${pathSel.value}&include_internal=false">Download visible TXT</a>`; const p=new URLSearchParams({q:q.value,path:pathSel.value,limit:'300',include_internal:'false'}); const page=await (await fetch('/api/conversations/'+encodeURIComponent(id)+'/messages?'+p)).json(); messages.innerHTML=page.items.map(m=>`<article class="msg ${esc(m.role||'message')}"><div class="role">${esc(m.role||'message')} · ${date(m.create_time??m.update_time)}</div><pre>${esc(m.content_text||'[empty]')}</pre></article>`).join(''); await loadList();}
              list.addEventListener('click',e=>{const b=e.target.closest('button[data-id]'); if(b) openConv(b.dataset.id);});
              q.addEventListener('input',()=>{clearTimeout(timer); timer=setTimeout(loadList,220)}); sort.addEventListener('change',loadList); pathSel.addEventListener('change',()=>selected?openConv(selected):loadList());
              window.addEventListener('keydown',e=>{const t=e.target; const typing=t&&(['INPUT','TEXTAREA','SELECT'].includes(t.tagName)||t.isContentEditable); if((!typing&&e.key==='/')||((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k')){e.preventDefault();q.focus();}});
              loadList();
              </script>
            </body></html>
            """
    return app
