"""Capa de seguridad: sesión, login obligatorio y cabeceras de protección.

`configurar_seguridad(app)` añade:
  - SessionMiddleware (cookie de sesión firmada y segura).
  - Un middleware que exige login en todo salvo /login, /logout, /health, /static.
  - Cabeceras de seguridad en todas las respuestas.
  - Las rutas /login (GET/POST) y /logout.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .auth import hash_password, verify_password
from .usuarios import store as usuario_store, usuario_valido

# Rutas accesibles sin iniciar sesión.
_PUBLICAS = ("/login", "/logout", "/registro", "/health", "/static", "/favicon")

# Rutas exactas públicas (no por prefijo): la landing de inicio.
_PUBLICAS_EXACTAS = ("/",)


def _hash_de(usuario: str) -> str | None:
    """Hash de la contraseña: primero la BD de registrados, luego el respaldo."""
    return usuario_store.obtener(usuario) or config.USUARIOS.get(usuario)


def _credenciales_ok(usuario: str, password: str) -> bool:
    h = _hash_de(usuario)
    if not h:
        verify_password(password, "pbkdf2_sha256$1$AA==$AA==")  # señuelo anti-timing
        return False
    return verify_password(password, h)


def _usuario_ocupado(usuario: str) -> bool:
    return usuario_store.existe(usuario) or usuario in config.USUARIOS


def _es_publica(path: str) -> bool:
    return path in _PUBLICAS_EXACTAS \
        or any(path == p or path.startswith(p + "/") for p in _PUBLICAS) \
        or path.startswith("/static")


def configurar_seguridad(app: FastAPI) -> None:
    # --- Middleware: exige login (se añade ANTES para quedar por DENTRO de la
    #     sesión, que se añade después y envuelve por fuera). --------------------
    @app.middleware("http")
    async def _gate(request: Request, call_next):
        path = request.url.path
        if not _es_publica(path):
            sesion = request.session
            user = sesion.get("user")
            caducada = (time.time() - sesion.get("t", 0)) > config.SESSION_MAX_AGE
            if not user or caducada:
                sesion.clear()
                if path.startswith("/api"):
                    return JSONResponse({"detail": "No autenticado"}, status_code=401)
                return RedirectResponse("/login", status_code=303)
            sesion["t"] = time.time()  # renueva por actividad
        resp = await call_next(request)
        _cabeceras_seguridad(resp)
        return resp

    # --- Sesión firmada (se añade después => envuelve al gate) -----------------
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.SECRET_KEY,
        session_cookie="gestiona_sesion",
        max_age=config.SESSION_MAX_AGE,
        same_site="lax",
        https_only=config.COOKIE_SEGURA,
    )

    # --- Rutas de login -------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if request.session.get("user"):
            return RedirectResponse("/app", status_code=303)
        return HTMLResponse(_pagina_login())

    @app.post("/login", response_class=HTMLResponse)
    def login_post(request: Request, usuario: str = Form(...), password: str = Form(...)):
        usuario = usuario.strip()
        if _credenciales_ok(usuario, password):
            request.session.clear()
            request.session["user"] = usuario
            request.session["t"] = time.time()
            return RedirectResponse("/app", status_code=303)
        return HTMLResponse(_pagina_login(error=True), status_code=401)

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    # --- Registro autoservicio (con código de invitación) ---------------------
    @app.get("/registro", response_class=HTMLResponse)
    def registro_form(request: Request):
        if not config.REGISTRO_HABILITADO:
            return HTMLResponse(_pagina_registro_off())
        if request.session.get("user"):
            return RedirectResponse("/app", status_code=303)
        return HTMLResponse(_pagina_registro())

    @app.post("/registro", response_class=HTMLResponse)
    def registro_post(request: Request, usuario: str = Form(...),
                      password: str = Form(...), password2: str = Form(...),
                      codigo: str = Form(...)):
        if not config.REGISTRO_HABILITADO:
            return HTMLResponse(_pagina_registro_off(), status_code=403)
        usuario = usuario.strip()
        error = None
        if codigo.strip() != (config.CODIGO_REGISTRO or ""):
            error = "Código de invitación incorrecto."
        elif not usuario_valido(usuario):
            error = "Usuario no válido (3–32 caracteres: letras, números, . _ - @)."
        elif _usuario_ocupado(usuario):
            error = "Ese usuario ya existe. Elige otro o inicia sesión."
        elif password != password2:
            error = "Las contraseñas no coinciden."
        elif len(password) < 8:
            error = "La contraseña debe tener al menos 8 caracteres."
        if error:
            return HTMLResponse(_pagina_registro(error=error), status_code=400)

        usuario_store.crear_o_actualizar(usuario, hash_password(password))
        # Alta correcta -> inicia sesión directamente.
        request.session.clear()
        request.session["user"] = usuario
        request.session["t"] = time.time()
        return RedirectResponse("/app", status_code=303)

    @app.get("/health")
    def health():
        return {"ok": True}


def _cabeceras_seguridad(resp) -> None:
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )


_ESTILO_AUTH = """
 *{box-sizing:border-box}
 :root{--bg1:#fbfcfe;--bg2:#eef1f6;--glow:rgba(225,24,53,.07);--card:#fff;--bd:#e6eaf0;
  --ibd:#e0e5ec;--ibg:#fbfcfe;--tx:#0e1420;--mut:#8a94a6;--sub:#48546a;--faint:#aab2c0;
  --errbg:#fdeaed;--errbd:#f5c2cb;--errtx:#a51427}
 @media(prefers-color-scheme:dark){:root{--bg1:#141a24;--bg2:#0b0f17;--glow:rgba(225,24,53,.12);
  --card:#161c26;--bd:#252c38;--ibd:#2a323f;--ibg:#0f141d;--tx:#e6eaf0;--mut:#8792a4;--sub:#aab4c4;
  --faint:#5c6675;--errbg:#2a1418;--errbd:#5c2530;--errtx:#ff8a9c}}
 :root[data-theme="dark"]{--bg1:#141a24;--bg2:#0b0f17;--glow:rgba(225,24,53,.12);--card:#161c26;
  --bd:#252c38;--ibd:#2a323f;--ibg:#0f141d;--tx:#e6eaf0;--mut:#8792a4;--sub:#aab4c4;--faint:#5c6675;
  --errbg:#2a1418;--errbd:#5c2530;--errtx:#ff8a9c}
 :root[data-theme="light"]{--bg1:#fbfcfe;--bg2:#eef1f6;--glow:rgba(225,24,53,.07);--card:#fff;
  --bd:#e6eaf0;--ibd:#e0e5ec;--ibg:#fbfcfe;--tx:#0e1420;--mut:#8a94a6;--sub:#48546a;--faint:#aab2c0;
  --errbg:#fdeaed;--errbd:#f5c2cb;--errtx:#a51427}
 body{font-family:'Plus Jakarta Sans',system-ui,-apple-system,Arial,sans-serif;
  background:radial-gradient(120% 90% at 82% -10%,var(--glow),transparent 55%),linear-gradient(180deg,var(--bg1),var(--bg2));
  margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;color:var(--tx);
  -webkit-font-smoothing:antialiased}
 .box{position:relative;z-index:1;width:420px;max-width:calc(100% - 32px);background:var(--card);
  border:1px solid var(--bd);border-radius:20px;
  box-shadow:0 40px 80px -30px rgba(14,20,32,.30),0 4px 14px rgba(14,20,32,.06);
  overflow:hidden;margin:24px;animation:gmCardIn .7s cubic-bezier(.22,.61,.36,1) both}
 .box::before{content:"";display:block;height:4px;background:linear-gradient(90deg,#e11835,#b3122a)}
 .inner{padding:44px 40px 38px}
 .wm{display:flex;align-items:center;justify-content:center;gap:0;margin-bottom:8px}
 .wm .logo{width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,#e11835,#b3122a);
  display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:21px;
  box-shadow:0 8px 16px -8px rgba(225,24,53,.65)}
 .wm .name{font-weight:800;font-size:24px;letter-spacing:-.02em;color:var(--tx)}
 .wm .name span{color:#e11835}
 h1{text-align:center;font-size:14px;color:var(--mut);margin:0 0 30px;font-weight:500}
 label{display:block;font-size:11.5px;font-weight:700;letter-spacing:.08em;color:var(--mut);
  text-transform:uppercase;margin:0 0 8px}
 input{width:100%;padding:13px 15px;border:1px solid var(--ibd);border-radius:11px;background:var(--ibg);
  margin-bottom:20px;outline:none;font:inherit;font-size:15px;color:var(--tx);
  transition:border-color .18s,background .18s,box-shadow .18s}
 input:focus{border-color:#e11835;background:var(--card);box-shadow:0 0 0 3px rgba(225,24,53,.14)}
 input::placeholder{color:var(--faint)}
 button[type=submit]{width:100%;padding:14px;border:none;border-radius:11px;background:#e11835;color:#fff;
  font-weight:700;font-size:15.5px;font-family:inherit;cursor:pointer;margin-top:6px;
  box-shadow:0 14px 26px -12px rgba(225,24,53,.6);transition:background .18s,transform .12s}
 button[type=submit]:hover{background:#c0122a}
 button[type=submit]:active{transform:scale(.99)}
 button[type=submit]:focus-visible{outline:2px solid #e11835;outline-offset:2px}
 .err{background:var(--errbg);border:1px solid var(--errbd);color:var(--errtx);border-radius:11px;
  padding:11px 14px;font-size:13px;margin-bottom:18px;font-weight:500}
 .alt{text-align:center;font-size:14px;color:var(--sub);margin:24px 0 0}
 .alt a{color:#e11835;text-decoration:none;font-weight:700}
 .pie{text-align:center;font-size:12px;color:var(--faint);margin:14px 0 0;font-family:'JetBrains Mono',monospace}
 .ayuda{font-size:12px;color:var(--mut);margin:-12px 0 14px}
 @keyframes gmCardIn{from{opacity:0;transform:translateY(18px) scale(.985)}to{opacity:1;transform:none}}
 /* Entrada: la G aparece grande y centrada, se encoge hasta su sitio y luego
    se revela el resto del logotipo y los campos. */
 @keyframes gmLogoIntro{0%{opacity:0;transform:scale(2.3)}38%{opacity:1;transform:scale(2.3)}
  100%{opacity:1;transform:scale(1)}}
 @keyframes gmNameIn{from{opacity:0;max-width:0;margin-left:0}to{opacity:1;max-width:240px;margin-left:11px}}
 @keyframes gmUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
 .wm .logo{animation:gmLogoIntro .85s cubic-bezier(.34,1.5,.5,1) .1s both}
 .wm .name{white-space:nowrap;overflow:hidden;animation:gmNameIn .45s ease .78s both}
 h1{animation:gmUp .5s ease 1s both}
 .inner>label,.inner>input,.inner>button,.inner>.err,.inner>.alt,.inner>.ayuda,.inner>.pie{
  animation:gmUp .5s ease 1.1s both}
 /* Fondos difuminados como en la landing */
 .glow{position:fixed;border-radius:50%;filter:blur(60px);z-index:0;pointer-events:none;
  animation:gmGlow 7s ease-in-out infinite}
 .glow.g1{top:-150px;right:-90px;width:480px;height:480px;
  background:radial-gradient(circle,rgba(225,24,53,.20),transparent 70%)}
 .glow.g2{bottom:-170px;left:-110px;width:540px;height:540px;
  background:radial-gradient(circle,rgba(20,40,120,.12),transparent 70%);animation-delay:-3.5s}
 @keyframes gmGlow{0%,100%{opacity:.5;transform:scale(1)}50%{opacity:.9;transform:scale(1.08)}}
"""


_TEMA_JS = (
    '<script>(function(){var t=localStorage.getItem("theme");'
    'if(t)document.documentElement.setAttribute("data-theme",t);'
    'document.addEventListener("DOMContentLoaded",function(){'
    'var isDark=document.documentElement.getAttribute("data-theme")==="dark"'
    '||(!document.documentElement.getAttribute("data-theme")&&window.matchMedia("(prefers-color-scheme:dark)").matches);'
    'var btn=document.getElementById("themeBtn");'
    'if(btn)btn.textContent=isDark?"☀":"☾";});})();</script>'
    '<script>function toggleTheme(){'
    'var r=document.documentElement;'
    'var isDark=r.getAttribute("data-theme")==="dark"'
    '||(!r.getAttribute("data-theme")&&window.matchMedia("(prefers-color-scheme:dark)").matches);'
    'var next=isDark?"light":"dark";'
    'r.setAttribute("data-theme",next);localStorage.setItem("theme",next);'
    'document.getElementById("themeBtn").textContent=next==="dark"?"☀":"☾";'
    '}</script>'
)

_TEMA_BTN = (
    '<button id="themeBtn" onclick="toggleTheme()" '
    'style="position:fixed;top:14px;right:16px;width:30px;height:30px;'
    'border-radius:8px;border:1px solid var(--border);background:transparent;'
    'color:var(--text-muted);cursor:pointer;font-size:0.875rem;'
    'display:flex;align-items:center;justify-content:center;'
    'transition:background 120ms,border-color 120ms;z-index:999;" '
    'title="Cambiar tema" aria-label="Cambiar tema">☾</button>'
)


def _cabecera(titulo: str) -> str:
    return (f'<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"/>'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"/>'
            f'<title>{titulo} · Gestiona+</title>'
            f'<link rel="stylesheet" href="/static/tokens.css"/>'
            f'<link rel="stylesheet" href="/static/fonts_gestiona.css"/>'
            f'<style>{_ESTILO_AUTH}</style>'
            f'{_TEMA_JS}'
            f'</head><body>{_TEMA_BTN}'
            f'<div class="glow g1"></div><div class="glow g2"></div>')


def _wm() -> str:
    return ('<div class="wm"><div class="logo">G</div>'
            '<div class="name">Gestiona<span>+</span></div></div>')


def _pagina_login(error: bool = False) -> str:
    aviso = ('<div class="err">Usuario o contraseña incorrectos</div>' if error else "")
    alta = ('<div class="alt">¿No tienes cuenta? <a href="/registro">Crear cuenta</a></div>'
            if config.REGISTRO_HABILITADO else "")
    return f"""{_cabecera('Acceso')}
 <form class="box" method="post" action="/login">
  <div class="inner">
   {_wm()}
   <h1>Tu gestoría, en piloto automático</h1>
   {aviso}
   <label for="usuario">Usuario</label>
   <input id="usuario" name="usuario" placeholder="nombre@empresa.com" autocomplete="username" autofocus required/>
   <label for="password">Contraseña</label>
   <input id="password" name="password" type="password" placeholder="••••••••" autocomplete="current-password" required/>
   <button type="submit">Entrar</button>
   {alta}
   <div class="pie">Acceso restringido · datos confidenciales</div>
  </div>
 </form>
</body></html>"""


def _pagina_registro(error: str | None = None) -> str:
    aviso = f'<div class="err">{error}</div>' if error else ""
    return f"""{_cabecera('Crear cuenta')}
 <form class="box" method="post" action="/registro">
  <div class="inner">
   {_wm()}
   <h1>Crea tu cuenta en Gestiona+</h1>
   {aviso}
   <label for="usuario">Usuario</label>
   <input id="usuario" name="usuario" placeholder="nombre@empresa.com" autocomplete="username" autofocus required/>
   <label for="password">Contraseña</label>
   <input id="password" name="password" type="password" placeholder="Mínimo 8 caracteres" autocomplete="new-password" required/>
   <label for="password2">Repite la contraseña</label>
   <input id="password2" name="password2" type="password" placeholder="••••••••" autocomplete="new-password" required/>
   <label for="codigo">Código de invitación</label>
   <input id="codigo" name="codigo" placeholder="Código de acceso" required/>
   <div class="ayuda">Te lo facilita el administrador de la plataforma.</div>
   <button type="submit">Crear cuenta y entrar</button>
   <div class="alt">¿Ya tienes cuenta? <a href="/login">Inicia sesión</a></div>
  </div>
 </form>
</body></html>"""


def _pagina_registro_off() -> str:
    return f"""{_cabecera('Registro')}
 <div class="box">
  <div class="inner">
   {_wm()}
   <h1>Registro no habilitado</h1>
   <p class="ayuda" style="text-align:center;font-size:13px;margin:0 0 6px">El alta de cuentas está
   desactivada. Pide acceso al administrador de la plataforma.</p>
   <div class="alt"><a href="/login">Volver al acceso</a></div>
  </div>
 </div>
</body></html>"""
