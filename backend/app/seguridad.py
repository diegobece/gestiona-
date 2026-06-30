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
    return any(path == p or path.startswith(p + "/") or path == p for p in _PUBLICAS) \
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
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(_pagina_login())

    @app.post("/login", response_class=HTMLResponse)
    def login_post(request: Request, usuario: str = Form(...), password: str = Form(...)):
        usuario = usuario.strip()
        if _credenciales_ok(usuario, password):
            request.session.clear()
            request.session["user"] = usuario
            request.session["t"] = time.time()
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(_pagina_login(error=True), status_code=401)

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # --- Registro autoservicio (con código de invitación) ---------------------
    @app.get("/registro", response_class=HTMLResponse)
    def registro_form(request: Request):
        if not config.REGISTRO_HABILITADO:
            return HTMLResponse(_pagina_registro_off())
        if request.session.get("user"):
            return RedirectResponse("/", status_code=303)
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
        return RedirectResponse("/", status_code=303)

    @app.get("/health")
    def health():
        return {"ok": True}


def _cabeceras_seguridad(resp) -> None:
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )


_ESTILO_AUTH = """
 *{box-sizing:border-box}
 body{font-family:'Segoe UI',system-ui,-apple-system,Arial,sans-serif;background:var(--bg);
  margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;color:var(--text)}
 .box{background:var(--surface-1);border:1px solid var(--border);border-radius:var(--r-xl);
  padding:32px;width:360px;box-shadow:var(--shadow-lg);margin:24px}
 .wm{text-align:center;margin-bottom:4px;line-height:1}
 .wm .g{font-family:Georgia,serif;font-weight:700;color:var(--accent);font-size:28px}
 .wm .m{font-family:'Segoe Script','Brush Script MT',cursive;color:var(--accent);font-size:29px;margin-left:3px;position:relative;top:4px}
 h1{font-size:0.875rem;font-weight:500;text-align:center;color:var(--text-muted);margin:0 0 20px}
 label{display:block;font-size:0.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;
  color:var(--text-muted);margin:14px 0 4px}
 input{width:100%;padding:9px 11px;border:1px solid var(--border);border-radius:var(--r-md);
  font:inherit;font-size:0.9375rem;background:var(--surface-1);color:var(--text);
  transition:border-color 120ms;outline:none}
 input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--ring)}
 button[type=submit]{width:100%;margin-top:20px;background:var(--accent);color:#fff;border:none;
  border-radius:var(--r-md);padding:11px;font:inherit;font-size:0.9375rem;font-weight:600;cursor:pointer;
  transition:background 120ms}
 button[type=submit]:hover{background:var(--accent-hover)}
 button[type=submit]:focus-visible{outline:2px solid var(--ring);outline-offset:2px}
 .err{background:#FEF2F2;border:1px solid #FECACA;border-left:3px solid var(--accent);color:#7F1D1D;
  border-radius:var(--r-md);padding:9px 12px;font-size:0.8125rem;margin-bottom:12px}
 .alt{text-align:center;font-size:0.8125rem;color:var(--text-muted);margin-top:14px}
 .alt a{color:var(--accent);text-decoration:none;font-weight:600}
 .pie{text-align:center;color:var(--text-faint);font-size:0.75rem;margin-top:14px}
 .ayuda{font-size:0.75rem;color:var(--text-faint);margin-top:4px}
 @media(prefers-color-scheme:dark){
  .err{background:color-mix(in srgb,var(--danger) 15%,transparent);
   border-color:color-mix(in srgb,var(--danger) 40%,transparent);color:#FCA5A5}}
"""


_TEMA_JS = (
    '<script>(function(){var t=localStorage.getItem("theme");'
    'if(t)document.documentElement.setAttribute("data-theme",t);'
    'document.addEventListener("DOMContentLoaded",function(){'
    'var isDark=document.documentElement.getAttribute("data-theme")==="dark"'
    '||(!document.documentElement.getAttribute("data-theme")&&window.matchMedia("(prefers-color-scheme:dark)").matches);'
    'var btn=document.getElementById("themeBtn");'
    'if(btn)btn.textContent=isDark?"☀":"☾";});})();<\/script>'
    '<script>function toggleTheme(){'
    'var r=document.documentElement;'
    'var isDark=r.getAttribute("data-theme")==="dark"'
    '||(!r.getAttribute("data-theme")&&window.matchMedia("(prefers-color-scheme:dark)").matches);'
    'var next=isDark?"light":"dark";'
    'r.setAttribute("data-theme",next);localStorage.setItem("theme",next);'
    'document.getElementById("themeBtn").textContent=next==="dark"?"☀":"☾";'
    '}<\/script>'
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
            f'<title>{titulo} · Gestiona más</title>'
            f'<link rel="stylesheet" href="/static/tokens.css"/>'
            f'<style>{_ESTILO_AUTH}</style>'
            f'{_TEMA_JS}'
            f'</head><body>{_TEMA_BTN}')


def _wm() -> str:
    return '<div class="wm"><span class="g">Gestiona</span><span class="m">más</span></div>'


def _pagina_login(error: bool = False) -> str:
    aviso = ('<div class="err">Usuario o contraseña incorrectos</div>' if error else "")
    alta = ('<div class="alt">¿No tienes cuenta? <a href="/registro">Crear cuenta</a></div>'
            if config.REGISTRO_HABILITADO else "")
    return f"""{_cabecera('Acceso')}
 <form class="box" method="post" action="/login">
   {_wm()}
   <h1>Análisis contable — acceso privado</h1>
   {aviso}
   <label for="usuario">Usuario</label>
   <input id="usuario" name="usuario" autocomplete="username" autofocus required/>
   <label for="password">Contraseña</label>
   <input id="password" name="password" type="password" autocomplete="current-password" required/>
   <button type="submit">Entrar</button>
   {alta}
   <div class="pie">Acceso restringido · datos confidenciales</div>
 </form>
</body></html>"""


def _pagina_registro(error: str | None = None) -> str:
    aviso = f'<div class="err">{error}</div>' if error else ""
    return f"""{_cabecera('Crear cuenta')}
 <form class="box" method="post" action="/registro">
   {_wm()}
   <h1>Crear cuenta</h1>
   {aviso}
   <label for="usuario">Usuario</label>
   <input id="usuario" name="usuario" autocomplete="username" autofocus required/>
   <label for="password">Contraseña</label>
   <input id="password" name="password" type="password" autocomplete="new-password" required/>
   <label for="password2">Repite la contraseña</label>
   <input id="password2" name="password2" type="password" autocomplete="new-password" required/>
   <label for="codigo">Código de invitación</label>
   <input id="codigo" name="codigo" required/>
   <div class="ayuda">Te lo facilita el administrador de la plataforma.</div>
   <button type="submit">Crear cuenta y entrar</button>
   <div class="alt">¿Ya tienes cuenta? <a href="/login">Inicia sesión</a></div>
 </form>
</body></html>"""


def _pagina_registro_off() -> str:
    return f"""{_cabecera('Registro')}
 <div class="box">
   {_wm()}
   <h1>Registro no habilitado</h1>
   <p class="ayuda" style="text-align:center;font-size:13px">El alta de cuentas está
   desactivada. Pide acceso al administrador de la plataforma.</p>
   <div class="alt"><a href="/login">Volver al acceso</a></div>
 </div>
</body></html>"""
