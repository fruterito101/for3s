# Renderizador de SPAs para For3s. Parte de la imagen oficial de Playwright
# (trae Chromium + todas las libs del sistema, sorteando Ubuntu 26.04 del host)
# y le añade Playwright-Python + nuestro script. El bot le pasa una URL por
# stdin/arg y recibe el texto renderizado por stdout (JSON).
FROM mcr.microsoft.com/playwright:v1.60.0-noble
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --break-system-packages --no-cache-dir playwright==1.60.0
COPY render.py /render.py
COPY render_http.py /render_http.py
EXPOSE 8080
# v1.1 BUG-9b: corre como SERVICIO HTTP hermano (no docker run por disparo).
# El bot le pide GET http://render:8080/?url=...
CMD ["python3", "/render_http.py"]
