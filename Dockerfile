# --- CSS build ---------------------------------------------------------------
FROM node:22-alpine AS css
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend frontend
COPY templates templates
COPY orders orders
RUN npx tailwindcss -i frontend/app.css -o static/css/app.css --minify

# --- app -------------------------------------------------------------------
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# LibreOffice (headless) + pdftoppm render previews; Carlito/Caladea/Liberation
# are metric-compatible with Calibri/Cambria/Arial so previews match PowerPoint.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-impress-nogui poppler-utils \
        fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation2 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
COPY --from=css /app/static/css/app.css static/css/app.css
RUN DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput \
    && mkdir -p /app/media && chown -R app:app /app/media
USER app
EXPOSE 8000
CMD ["gunicorn", "subra.wsgi", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
