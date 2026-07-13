# Flujo Agéntico de Power BI Chat

Este diagrama describe el flujo actual de orquestación en `app/services/agent_core.py`.

```mermaid
sequenceDiagram
    autonumber
    actor Usuario
    participant Route as Flask Route (/chat)
    participant Service as Chatbot Service
    participant DB_App as Base de Datos (SQL)
    participant Agent as Agent Core (Orquestador)
    participant MicroAgent as Micro-Agente (Rewriter)
    participant Voyage as Voyage AI (Embedder)
    participant Claude as Agente Principal (Claude)
    participant PBITools as Power BI Tools
    participant PBI as Power BI REST API

    Usuario->>Route: POST /chat (pregunta, slug)
    Route->>Service: procesar_interaccion_completa()
    
    rect rgb(245, 245, 245)
        Note over Service, DB_App: Preparación y Persistencia Inicial
        Service->>DB_App: Resolver Reporte & Dataset por slug
        DB_App-->>Service: DatasetID, Credenciales
        Service->>DB_App: Crear/Cargar Sesión & Guardar Pregunta Usuario
        Service->>DB_App: Cargar Historial de Mensajes
        DB_App-->>Service: Mensajes previos
    end

    Service->>Agent: run_chat_turn(pregunta, historial, dataset_id)

    rect rgb(240, 248, 255)
        Note over Agent, MicroAgent: Fase 1: RAG Agéntico (Schema Pre-fetch)
        Agent->>MicroAgent: Optimizar palabras clave (Haiku)
        MicroAgent-->>Agent: "sales_order, Date, ventas"
        Agent->>Voyage: embed("palabras clave")
        Voyage-->>Agent: Vector
        Agent->>DB_App: Buscar Tablas/Medidas similares (Cosine Distance)
        DB_App-->>Agent: Fragmentos de Esquema JSON
    end

    rect rgb(255, 240, 245)
        Note over Agent, Claude: Fase 2: Orquestación (Prompting)
        Agent->>Claude: System Prompt + Schema + Historial + Tools
    end

    rect rgb(240, 255, 240)
        Note over Claude, PBI: Fase 3: Ciclo de Razonamiento y Herramientas
        loop Tool Rounds (Máx 6)
            Claude->>Agent: call_tool("execute_dax_query", DAX)
            Agent->>PBITools: execute_dax_query_local(DAX)
            PBITools->>PBI: POST /executeQueries
            
            alt Éxito o Error de Power BI
                PBI-->>PBITools: JSON Data / Error API
            end
            
            PBITools-->>Agent: JSON String
            Agent-->>Claude: tool_result
            
            opt Si falta contexto o hay error persistente
                Claude->>Agent: call_tool("get_schema_context", question)
                Agent->>MicroAgent: Re-escribir términos
                Agent->>Voyage: Nuevo embedding
                Agent->>DB_App: Recuperar esquema extra
                DB_App-->>Agent: Extra Context
                Agent-->>Claude: tool_result
            end
        end
    end

    Agent-->>Service: Resultado (Answer, DAX, Tokens, Metadatos)

    rect rgb(255, 250, 240)
        Note over Service, DB_App: Persistencia Final
        Service->>DB_App: Guardar Respuesta Asistente (Tokens, Latencia, DAX)
        DB_App-->>Service: OK
    end

    Service-->>Route: JSON Response
    Route-->>Usuario: Respuesta final formateada
```

## Detalles Técnicos
- **Micro-Agente:** Utiliza `claude-haiku-4-5-20251001` para optimizar los términos de búsqueda.
- **Voyage AI:** Modelo `voyage-4` para embeddings de alta precisión en el dominio semántico.
- **Agente Principal:** Orquestador basado en Claude que maneja la lógica de negocio y generación de DAX.
- **Autocorrección:** Si la API de Power BI devuelve un error, el Agente Principal recibe el mensaje de error y utiliza sus reglas de sintaxis DAX para intentar una corrección en la siguiente ronda.
