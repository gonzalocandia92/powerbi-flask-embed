from flask import Flask, render_template
import requests
import json
import logging
from dotenv import load_dotenv
import os

# Cargar variables de entorno desde .env
load_dotenv()

# Configuración del logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# Variables de entorno
TENANT_ID     = os.getenv("TENANT_ID")
CLIENT_ID     = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
USERNAME      = os.getenv("USER")
PASSWORD      = os.getenv("PASS")
WORKSPACE_ID  = os.getenv("WORKSPACE_ID")
REPORT_ID     = os.getenv("REPORT_ID")

# Endpoints
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
SCOPE     = "https://analysis.windows.net/powerbi/api/.default"
API_BASE  = "https://api.powerbi.com/v1.0/myorg"

app = Flask(__name__)

# Obtener access token via ROPC
def get_access_token():
    data = {
        "grant_type": "password",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "username": USERNAME,
        "password": PASSWORD,
        "scope": SCOPE
    }
    try:
        logging.info("Solicitando access token...")
        resp = requests.post(AUTHORITY, data=data)
        resp.raise_for_status()
        token = resp.json().get("access_token")
        logging.info("Access token recibido")
        return token
    except Exception as err:
        logging.error(f"Error obteniendo access token: {err} - {getattr(resp, 'text', '')}")
        raise

# Obtener embedUrl dinámico
def get_report_info(access_token):
    url = f"{API_BASE}/groups/{WORKSPACE_ID}/reports/{REPORT_ID}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        logging.info("Obteniendo información del reporte...")
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        info = resp.json()
        logging.info("Embed URL obtenido: %s", info.get("embedUrl"))
        return info.get("embedUrl")
    except Exception as err:
        logging.error(f"Error obteniendo embedUrl: {err} - {getattr(resp, 'text', '')}")
        raise

@app.route('/')
def index():
    try:
        access_token = get_access_token()
        embed_url    = get_report_info(access_token)
        embed_token  = access_token

        return render_template("report.html",
                               embed_token=embed_token,
                               embed_url=embed_url,
                               report_id=REPORT_ID)
    except Exception as e:
        logging.error(f"Error en index route: {e}")
        return f"<h1>Error cargando reporte: {e}</h1>", 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
