FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    REPORT_PDF_CHROMIUM_NO_SANDBOX=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
COPY requirements/ /app/requirements/
RUN pip install --no-cache-dir -r requirements.txt

# Chromium for the Reporting "Descargar PDF" export (Playwright). Only the headless
# shell build is installed (enough for page.pdf); --with-deps pulls the system libraries
# and fonts it needs. Placed before "COPY . /app" so code changes do not re-download it.
# The container runs as root, hence REPORT_PDF_CHROMIUM_NO_SANDBOX=1 above.
RUN playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

EXPOSE 2052

CMD ["python", "run.py"]
