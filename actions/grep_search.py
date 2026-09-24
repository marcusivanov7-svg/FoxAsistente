import re
from pathlib import Path

def grep_search(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """Busca patrones dentro de archivos de código."""
    params = parameters or {}
    pattern = params.get("pattern", "")
    path = params.get("path", ".")
    extension = params.get("extension", "")
    max_results = min(int(params.get("max_results", 20)), 50)
    
    if not pattern:
        return "Error: 'pattern' is required"
    
    try:
        search_path = Path(path).expanduser()
        if not search_path.exists():
            return f"Path not found: {path}"
        
        # Si es un archivo, buscar solo ahí
        if search_path.is_file():
            files = [search_path]
        else:
            # Buscar en todos los archivos del directorio
            files = []
            for ext in [extension] if extension else [".py", ".js", ".ts", ".html", ".css", ".json", ".md"]:
                files.extend(search_path.rglob(f"*{ext}"))
        
        regex = re.compile(pattern, re.IGNORECASE)
        results = []
        
        for file in files[:100]:  # Limitar a 100 archivos
            try:
                content = file.read_text(encoding="utf-8", errors="ignore")
                for i, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        results.append(f"{file.name}:{i}: {line.strip()}")
                        if len(results) >= max_results:
                            break
            except Exception:
                continue
            
            if len(results) >= max_results:
                break
        
        if not results:
            return f"No matches found for '{pattern}'"
        
        return f"Found {len(results)} match(es):\n" + "\n".join(results)
    
    except Exception as e:
        return f"Search error: {e}"

TOOL = {
    "name": "grep_search",
    "description": "Search for text patterns inside code files (like grep)",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "pattern": {
                "type": "STRING",
                "description": "Text or regex pattern to search"
            },
            "path": {
                "type": "STRING",
                "description": "Directory or file to search in (default: current directory)"
            },
            "extension": {
                "type": "STRING",
                "description": "File extension filter (e.g., '.py', '.js')"
            },
            "max_results": {
                "type": "INTEGER",
                "description": "Maximum number of matches (default: 20)"
            }
        },
        "required": ["pattern"]
    },
    "handler": grep_search,
}