# Gestiona más — Plataforma de análisis contable

Detecta **pagos sin factura** y **facturas sin pago** a partir de un Libro Mayor
(Fichas de Mayor) en Excel o PDF. Funciona en local (tus datos no salen del equipo).

## Requisitos

- **Windows** (10/11).
- **Python 3.10 o superior** → https://www.python.org/downloads/
  - ⚠️ Al instalar, marca la casilla **"Add Python to PATH"**.

## Puesta en marcha (3 pasos)

1. **Descomprime** esta carpeta donde quieras (p.ej. el Escritorio).
2. Doble clic en **`INICIAR_PLATAFORMA.bat`**.
   - La primera vez instala las dependencias solo (tarda ~1 minuto).
   - Después arranca el servidor y abre el navegador.
3. Si no se abre solo, entra en: **http://127.0.0.1:8011**

Para volver a abrirla otro día: doble clic otra vez en `INICIAR_PLATAFORMA.bat`
(o abre directamente el enlace si ya está arrancada).

## Tu logo (opcional)

Pon tu logo como `backend/app/static/logo.png` y aparecerá automáticamente en la
cabecera de la web y en los informes PDF. Si no, se usa un wordmark de marca.

## Notas

- **Arranque automático al iniciar sesión**: es opcional y específico de cada equipo.
  Para activarlo, copia `servidor_segundo_plano.vbs` a la carpeta de Inicio de
  Windows (`Win+R` → `shell:startup`) **editando antes la ruta** dentro del .vbs
  para que apunte a donde hayas dejado la carpeta.
- Los **PDF** usan tipografías de Windows; en otros sistemas se usa una fuente básica.
- Es una herramienta de **apoyo a la revisión**: marca lo que puede probar y deja lo
  dudoso para que un humano lo confirme. No sustituye el criterio profesional.
