from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .web_api import create_api_router
from .web_jobs import ImportJobManager


def create_app(
    db_path: Path | None = None,
    static_dir: Path | None = None,
    allow_fallback: bool = False,
    log_level: str = "warning",
) -> FastAPI:
    if db_path is None:
        db_path = Path("archive/chatgpt_archive.db")
    db_path = db_path.resolve()
    app = FastAPI(title="ChatGPT Archive Web", docs_url=None, redoc_url=None)
    app.include_router(create_api_router(db_path, ImportJobManager(db_path, log_level=log_level)))

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
              <div class="fallback-warning">Limited fallback UI, not the full React UI. Build the full UI with <code>cd webui && npm ci && npm run build</code>.</div>
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
              const date=v=>v?new Date(v*1000).toLocaleString():'';
              async function loadList(){const p=new URLSearchParams({q:q.value,sort:sort.value,path:pathSel.value,limit:'50'}); const r=await fetch('/api/conversations?'+p); const data=await r.json(); list.innerHTML=data.items.map(x=>`<button class="item ${x.conversation_id===selected?'selected':''}" data-id="${esc(x.conversation_id)}"><span class="title">${esc(x.title||'untitled')}</span><div class="meta">${date(x.update_time||x.create_time)}${x.hit_count?' · '+x.hit_count+' hits':''}</div><div class="snippet">${esc((x.snippets&&x.snippets[0]&&x.snippets[0].snippet)||'')}</div></button>`).join('');}
              async function openConv(id){selected=id; const d=await (await fetch('/api/conversations/'+encodeURIComponent(id))).json(); heading.textContent=d.title||'untitled'; info.textContent=`Created ${date(d.create_time)} · Updated ${date(d.update_time)} · ${d.current_path_nodes||0}/${d.node_count||0} current path nodes`; actions.innerHTML=`<a href="/api/conversations/${encodeURIComponent(id)}/export?format=md&path=${pathSel.value}">Download MD</a><a href="/api/conversations/${encodeURIComponent(id)}/export?format=txt&path=${pathSel.value}">Download TXT</a>`; const p=new URLSearchParams({q:q.value,path:pathSel.value,limit:'300'}); const page=await (await fetch('/api/conversations/'+encodeURIComponent(id)+'/messages?'+p)).json(); messages.innerHTML=page.items.map(m=>`<article class="msg ${esc(m.role||'message')}"><div class="role">${esc(m.role||'message')} · ${date(m.create_time||m.update_time)}</div><pre>${esc(m.content_text||'[empty]')}</pre></article>`).join(''); await loadList();}
              list.addEventListener('click',e=>{const b=e.target.closest('button[data-id]'); if(b) openConv(b.dataset.id);});
              q.addEventListener('input',()=>{clearTimeout(timer); timer=setTimeout(loadList,220)}); sort.addEventListener('change',loadList); pathSel.addEventListener('change',()=>selected?openConv(selected):loadList());
              window.addEventListener('keydown',e=>{const t=e.target; const typing=t&&(['INPUT','TEXTAREA','SELECT'].includes(t.tagName)||t.isContentEditable); if((!typing&&e.key==='/')||((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k')){e.preventDefault();q.focus();}});
              loadList();
              </script>
            </body></html>
            """
    return app
