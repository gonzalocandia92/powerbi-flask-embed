FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System libraries for WeasyPrint (Reporting "Descargar PDF" export): Pango/HarfBuzz for
# text shaping and font subsetting, plus DejaVu so symbols used by the reports
# (arrows, trend glyphs) always have a font. Placed before the pip layer so Python
# dependency changes do not reinstall them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
COPY requirements/ /app/requirements/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 2052

CMD ["python", "run.py"]
