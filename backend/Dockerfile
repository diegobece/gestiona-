# Imagen de producción de Gestiona más (FastAPI).
# Construir desde la carpeta backend/:   docker build -t gestiona-mas .
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

# Escucha solo en local del contenedor; Cloudflare Tunnel se conecta por aquí.
EXPOSE 8011
ENV GESTIONA_ENV=production

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8011", "--log-level", "warning"]
