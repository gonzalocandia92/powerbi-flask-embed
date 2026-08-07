FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOTNET_ROOT=/usr/share/dotnet \
    PATH="${PATH}:/usr/share/dotnet"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libgssapi-krb5-2 \
    libicu72 \
    libssl3 \
    wget \
    zlib1g \
    && wget https://dot.net/v1/dotnet-install.sh -O /tmp/dotnet-install.sh \
    && chmod +x /tmp/dotnet-install.sh \
    && /tmp/dotnet-install.sh --channel 10.0 --runtime dotnet --install-dir /usr/share/dotnet \
    && rm /tmp/dotnet-install.sh \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/ /app/requirements/
RUN pip install --no-cache-dir -r requirements/mcp.txt

COPY services/ /app/services/
RUN python services/mcp_aklara/scripts/install_amo.py

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "services.mcp_aklara.src.main:app", "--host", "0.0.0.0", "--port", "8000"]
