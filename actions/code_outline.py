import ast
from pathlib import Path

def code_outline(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """Muestra la estructura de un archivo Python (funciones, clases, imports)."""
    params = parameters or {}
    path = params.get("path", "")
    name = params.get("name", "")
    
    try:
        target = Path(path) / name if name else Path(path)
        if not target.exists():
            return f"File not found: {target}"
        
        content = target.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(content)
        
        outline = []
        
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                args = ", ".join(arg.arg for arg in node.args.args)
                outline.append(f"def {node.name}({args}) — line {node.lineno}")
            elif isinstance(node, ast.ClassDef):
                outline.append(f"class {node.name} — line {node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    outline.append(f"import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                outline.append(f"from {node.module} import ...")
        
        if not outline:
            return "No functions, classes, or imports found"
        
        return f"Structure of {target.name}:\n" + "\n".join(outline[:50])
    
    except Exception as e:
        return f"Could not parse: {e}"

TOOL = {
    "name": "code_outline",
    "description": "Show the structure of a Python file (functions, classes, imports) without reading the full code",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "path": {
                "type": "STRING",
                "description": "Path to the Python file"
            },
            "name": {
                "type": "STRING",
                "description": "File name"
            }
        },
        "required": ["path"]
    },
    "handler": code_outline,
}