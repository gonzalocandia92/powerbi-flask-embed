FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System libraries for WeasyPrint (Reporting "Descargar PDF" export): Pango/HarfBuzz for text
# shaping and font subsetting. Fonts are installed explicitly so PDFs never depend on the host:
# Inter is the reports' body font, Liberation Serif backs the "Times New Roman" fallback of the
# display font, and DejaVu covers the symbols used by the reports (trend arrows, bullets).
# Placed before the pip layer so Python dependency changes do not reinstall them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
        fonts-inter fonts-liberation2 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY deploy/fontconfig/99-report-fonts.conf /etc/fonts/conf.d/99-report-fonts.conf
COPY requirements.txt /app/requirements.txt
COPY requirements/ /app/requirements/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 2052

CMD ["python", "run.py"]
