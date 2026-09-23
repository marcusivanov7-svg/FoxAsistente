# -*- coding: utf-8 -*-
"""verification_layer.py - Capa de verificación de resultados.

Revisa los resultados devueltos por las herramientas antes de que el 
LLM los procese. Si detecta errores críticos, salidas vacías o fallos, 
inyecta consejos de auto-corrección para guiar al modelo.
"""

def verify_tool_result(tool_name: str, args: dict, result: str) -> str:
    """
    Analiza el resultado de una herramienta y añade advertencias
    de verificación si algo parece haber fallado.
    """
    result_lower = result.lower()
    
    # 1. Verificación de resultado vacío
    if not result or result.isspace():
        return "[VERIFICATION FAILED] La herramienta no devolvió ningún resultado (cadena vacía). Intenta verificar los parámetros o usar una herramienta diferente."

    # 2. Detección de errores y excepciones
    error_keywords = ["traceback (most recent call last)", "exception:", "error:", "file not found", "access denied"]
    if any(keyword in result_lower for keyword in error_keywords):
        return (
            f"{result}\n\n"
            f"[VERIFICATION WARNING] La ejecución de '{tool_name}' falló o devolvió un error. "
            "Por favor, lee el error cuidadosamente. Corrige los parámetros o intenta un método alternativo."
        )

    # 3. Verificación específica por herramienta
    if tool_name == "web_search":
        if "no results" in result_lower or "no se encontraron" in result_lower:
            return f"{result}\n\n[VERIFICATION TIP] No se encontró información. Intenta ampliar los términos de búsqueda o usar otra consulta."
            
    elif tool_name == "python_repl" or tool_name == "execute_code":
        if "syntaxerror" in result_lower or "nameerror" in result_lower:
            return f"{result}\n\n[VERIFICATION WARNING] El código falló. Revisa la sintaxis, asegúrate de haber importado las librerías necesarias e inténtalo de nuevo."

    # Si todo parece bien, se devuelve el resultado original
    return result
