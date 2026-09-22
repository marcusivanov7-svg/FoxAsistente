"""
FOX Agent Plugin Template.

Copiar este archivo, renombrarlo (sin underscore al inicio), completar
AGENT_PLUGIN y run(). Fox lo descubre automáticamente en plugins/agent/.
"""

AGENT_PLUGIN = {
    "name": "my_agent_tool",         # snake_case, único
    "description": (
        "Descripción que el LLM usa para decidir cuándo llamar esta herramienta. "
        "Sé explícito sobre los casos de uso."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "input": {"type": "STRING", "description": "El argumento principal"},
        },
        "required": ["input"],
    },
}


def run(parameters: dict) -> str:
    """
    parameters: dict con los argumentos declarados en AGENT_PLUGIN['parameters'].
    Devolvé siempre un string — es lo que el agente recibe como resultado.
    Nunca lances excepciones; catchealas y devolvé un mensaje de error.
    """
    input_val = parameters.get("input", "")
    try:
        return f"Resultado para: {input_val}"
    except Exception as e:
        return f"Error en my_agent_tool: {e}"
