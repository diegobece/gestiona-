# Despliegue en Cloudflare (dominio + HTTPS + login)

Esta guía publica la plataforma en tu **dominio de Cloudflare**, con **HTTPS** y
**login**, sin exponer la IP del servidor ni abrir puertos.

## Arquitectura

```
Navegador  →  Cloudflare (tu dominio · HTTPS · Access/login)  →  túnel cifrado  →  tu app (FastAPI, 127.0.0.1:8011)
```

Cloudflare no ejecuta Python: la app corre en una **máquina** (un VPS de ~5 €/mes
como Hetzner/DigitalOcean, o un mini‑PC/servidor siempre encendido) y **Cloudflare
Tunnel** la publica en tu dominio. Hay **dos capas de login**:
1. **Cloudflare Access** (recomendado): muro de acceso por email/Google antes de
   llegar a la app.
2. **Login propio de la app** (ya incluido): segunda capa, por si se accede directo.

---

## 1. Preparar la app (en el servidor)

```bash
git clone / copia la carpeta del proyecto
cd "gestiona project/backend"
python -m pip install -r requirements.txt
cd ..
cp .env.example .env             # y edítalo:
#   GESTIONA_ENV=production
#   GESTIONA_SECRET_KEY=<clave larga: python -c "import secrets;print(secrets.token_hex(32))">
#   GESTIONA_CODIGO_REGISTRO=<código de invitación que repartirás>

# Opción A (recomendada): cada persona se registra en /registro con el código.
# Opción B: tú creas las cuentas a mano:
python crear_usuario.py          # crea un usuario y contraseña (usuarios.db)
```

## 2. Arrancar la app (solo en local: 127.0.0.1)

```bash
# Carga el .env y arranca escuchando SOLO en local (Cloudflare se conecta por el túnel).
cd backend
uvicorn app.api.main:app --host 127.0.0.1 --port 8011
```

En un servidor Linux, déjala como **servicio** (systemd) para que arranque sola.
Alternativa: **Docker** (ver `Dockerfile`):
```bash
docker build -t gestiona-mas ./backend
docker run -d --restart unless-stopped -p 127.0.0.1:8011:8011 \
  -e GESTIONA_ENV=production -e GESTIONA_SECRET_KEY=... \
  -v $PWD/users.json:/app/../users.json gestiona-mas
```

## 3. Publicar con Cloudflare Tunnel

1. Ten tu **dominio** añadido en Cloudflare (Cloudflare te lo proporciona/gestiona).
2. Instala `cloudflared` en el servidor: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
3. Autentica y crea el túnel:
   ```bash
   cloudflared tunnel login
   cloudflared tunnel create gestiona
   cloudflared tunnel route dns gestiona app.TUDOMINIO.com
   ```
4. Configura `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: gestiona
   credentials-file: /root/.cloudflared/<ID>.json
   ingress:
     - hostname: app.TUDOMINIO.com
       service: http://127.0.0.1:8011
     - service: http_status:404
   ```
5. Arranca el túnel (y déjalo como servicio):
   ```bash
   cloudflared tunnel run gestiona
   # o:  cloudflared service install
   ```
   Ya tienes **https://app.TUDOMINIO.com** con HTTPS automático.

## 4. Añadir el muro de acceso (Cloudflare Access — Zero Trust)

1. En el panel de Cloudflare → **Zero Trust** → **Access** → **Applications** → *Add
   an application* → **Self‑hosted**.
2. Dominio: `app.TUDOMINIO.com`.
3. **Policy**: *Allow* solo a los emails autorizados (o tu dominio de correo). Método
   de login: **One‑time PIN (email)** o Google/Microsoft.
4. Guarda. Ahora, antes de ver la app, Cloudflare pide identidad. Es gratis hasta 50
   usuarios.

(Con Access activo, el login propio de la app queda como segunda barrera.)

---

## Protección de datos (lo técnico ya está; lo legal es tuyo)

Incluido en la app:
- **Login obligatorio** (contraseñas con hash PBKDF2; nunca en claro).
- **Sesiones firmadas** con cookie `HttpOnly`, `SameSite=Lax` y `Secure` en producción.
- **HTTPS** de extremo a extremo (Cloudflare) y **sin puertos abiertos** (túnel).
- **Cabeceras de seguridad** (CSP, X‑Frame‑Options, nosniff…).
- Los archivos subidos se procesan y se **borran** (no se almacenan en disco).
- Documentación de API deshabilitada en producción.

Pendiente por tu parte (organizativo/legal, RGPD):
- Tratas datos fiscales de terceros: revisa si necesitas **contrato de encargado de
  tratamiento** con tus clientes y registra las medidas de seguridad.
- Política de **copias de seguridad** de `users.json` y `overrides.db`.
- Da de baja a usuarios cuando ya no deban acceder (`users.json` + Access).

## Mantenimiento

- Crear/actualizar usuarios: `python crear_usuario.py`.
- Cerrar sesión: botón **Salir** en la cabecera.
- Cambiar duración de sesión: `GESTIONA_SESSION_HORAS` en `.env`.
