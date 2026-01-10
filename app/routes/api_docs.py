"""
API Documentation routes.
"""
from flask import Blueprint, render_template, jsonify
from flask_login import login_required

bp = Blueprint('api_docs', __name__, url_prefix='/docs')


@bp.route('/')
@login_required
def index():
    """Display API documentation."""
    return render_template('api_docs/index.html', title='API Documentation')


@bp.route('/openapi.json')
def openapi_spec():
    """Return OpenAPI specification."""
    spec = {
        "openapi": "3.0.0",
        "info": {
            "title": "PowerBI Flask Embed API",
            "description": "API para acceso privado a reportes de Power BI mediante autenticación de empresas",
            "version": "1.0.0"
        },
        "servers": [
            {
                "url": "/",
                "description": "Servidor actual"
            }
        ],
        "paths": {
            "/private/login": {
                "post": {
                    "summary": "Autenticar empresa",
                    "description": "Autentica una empresa y devuelve un token JWT para acceder a los reportes",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["client_id", "client_secret"],
                                    "properties": {
                                        "client_id": {
                                            "type": "string",
                                            "description": "ID del cliente de la empresa"
                                        },
                                        "client_secret": {
                                            "type": "string",
                                            "description": "Secret del cliente de la empresa"
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Autenticación exitosa",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "access_token": {
                                                "type": "string",
                                                "description": "Token JWT de acceso"
                                            },
                                            "token_type": {
                                                "type": "string",
                                                "example": "Bearer"
                                            },
                                            "expires_in": {
                                                "type": "integer",
                                                "description": "Tiempo de expiración del token en segundos"
                                            }
                                        }
                                    }
                                }
                            }
                        },
                        "400": {
                            "description": "Solicitud inválida"
                        },
                        "401": {
                            "description": "Credenciales inválidas"
                        },
                        "403": {
                            "description": "Cliente inactivo"
                        }
                    }
                }
            },
            "/private/reports": {
                "get": {
                    "summary": "Listar reportes de la empresa",
                    "description": "Obtiene la lista de reportes asociados a la empresa autenticada",
                    "security": [
                        {
                            "BearerAuth": []
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Lista de reportes obtenida exitosamente",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "empresa_id": {
                                                "type": "integer"
                                            },
                                            "empresa_nombre": {
                                                "type": "string"
                                            },
                                            "reports": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "config_id": {
                                                            "type": "integer"
                                                        },
                                                        "config_name": {
                                                            "type": "string"
                                                        },
                                                        "report_id": {
                                                            "type": "string"
                                                        },
                                                        "report_name": {
                                                            "type": "string"
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        },
                        "401": {
                            "description": "Token inválido o expirado"
                        }
                    }
                }
            },
            "/private/report-config": {
                "get": {
                    "summary": "Obtener configuración de embed de reporte",
                    "description": "Obtiene la configuración necesaria para embeber un reporte específico",
                    "security": [
                        {
                            "BearerAuth": []
                        }
                    ],
                    "parameters": [
                        {
                            "name": "config_id",
                            "in": "query",
                            "required": True,
                            "schema": {
                                "type": "integer"
                            },
                            "description": "ID de la configuración del reporte"
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Configuración obtenida exitosamente",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "embedUrl": {
                                                "type": "string",
                                                "description": "URL de embed del reporte"
                                            },
                                            "reportId": {
                                                "type": "string",
                                                "description": "ID del reporte de Power BI"
                                            },
                                            "accessToken": {
                                                "type": "string",
                                                "description": "Token de acceso para Power BI"
                                            },
                                            "workspaceId": {
                                                "type": "string",
                                                "description": "ID del workspace de Power BI"
                                            }
                                        }
                                    }
                                }
                            }
                        },
                        "400": {
                            "description": "config_id no proporcionado"
                        },
                        "401": {
                            "description": "Token inválido o expirado"
                        },
                        "403": {
                            "description": "La empresa no tiene acceso a esta configuración"
                        },
                        "404": {
                            "description": "Configuración no encontrada"
                        }
                    }
                }
            }
        },
        "components": {
            "securitySchemes": {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT"
                }
            }
        }
    }
    
    return jsonify(spec)
