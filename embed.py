# embed.py
import os
import requests
from dotenv import load_dotenv

load_dotenv()

TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
USER = os.getenv("USER")
PASS = os.getenv("PASS")
WORKSPACE_ID = os.getenv("WORKSPACE_ID")
REPORT_ID = os.getenv("REPORT_ID")

def get_embed_for_config(cfg=None):
    """
    Obtiene el embed token y la URL del reporte para Power BI usando ROPC.
    Basado en la implementación original que funcionaba en app.py.
    """
    # 1. Obtener token de Azure AD
    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "grant_type": "password",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "https://analysis.windows.net/powerbi/api/.default",
        "username": USER,
        "password": PASS
    }

    r = requests.post(token_url, data=data)
    r.raise_for_status()
    access_token = r.json().get("access_token")

    # 2. Obtener información del reporte desde Power BI REST API
    report_url = f"https://api.powerbi.com/v1.0/myorg/groups/{WORKSPACE_ID}/reports/{REPORT_ID}"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(report_url, headers=headers)
    resp.raise_for_status()
    report_info = resp.json()

    # 3. Retornar la configuración para el template HTML
    return {
        "embed_token": access_token,  # tu implementación original usaba el mismo token
        "embed_url": report_info["embedUrl"],
        "report_id": REPORT_ID
    }
