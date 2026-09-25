import sys
import re

dev_agent_content = open("actions/dev_agent.py", encoding="utf-8").read()

new_get_model = """def _get_model(model_name: str):
    from core.providers import chat
    import json
    import time
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    prov = (cfg.get("specialist_provider") or "gemini").lower()
    mod = (cfg.get("specialist_model") or model_name).strip()

    class _Response:
        def __init__(self, t):
            self.text = t

    class _W:
        def generate_content(self, contents):
            last_exc = None
            for _ in range(3):
                try:
                    text_content = ""
                    if isinstance(contents, list):
                        for part in contents:
                            if isinstance(part, str):
                                text_content += part + "\\n"
                            elif hasattr(part, "text"):
                                text_content += getattr(part, "text", "") + "\\n"
                    else:
                        text_content = str(contents)

                    msgs = [{"role": "user", "content": text_content.strip()}]
                    res = chat(
                        messages=msgs,
                        provider=prov,
                        model=mod,
                        timeout=120
                    )
                    return _Response(res["content"])
                except Exception as exc:
                    last_exc = exc
                    msg = str(exc).lower()
                    if "503" in msg or "429" in msg or "unavailable" in msg or "resource_exhausted" in msg or "timeout" in msg:
                        print(f"[DevAgent] Error temporal ({msg[:80]}...)  reintentando...")
                        time.sleep(2)
                        continue
                    raise
            raise last_exc

    return _W()"""

dev_agent_content = re.sub(
    r'def _get_model\(model_name: str\):.*?return _W\(\)', 
    new_get_model, 
    dev_agent_content, 
    flags=re.DOTALL
)

open("actions/dev_agent.py", "w", encoding="utf-8").write(dev_agent_content)


code_helper_content = open("actions/code_helper.py", encoding="utf-8").read()

new_get_gemini = """def _get_gemini():
    from core.providers import chat
    import json
    import time
    from pathlib import Path
    
    BASE_DIR = Path(__file__).resolve().parent.parent
    API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
    
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    prov = (cfg.get("specialist_provider") or "gemini").lower()
    mod = (cfg.get("specialist_model") or GEMINI_MODEL).strip()

    class _Response:
        def __init__(self, t):
            self.text = t

    class _W:
        def generate_content(self, contents):
            last_exc = None
            for _ in range(3):
                try:
                    text_content = ""
                    if isinstance(contents, list):
                        for part in contents:
                            if isinstance(part, str):
                                text_content += part + "\\n"
                            elif hasattr(part, "text"):
                                text_content += getattr(part, "text", "") + "\\n"
                    else:
                        text_content = str(contents)

                    msgs = [{"role": "user", "content": text_content.strip()}]
                    res = chat(
                        messages=msgs,
                        provider=prov,
                        model=mod,
                        timeout=120
                    )
                    return _Response(res["content"])
                except Exception as exc:
                    last_exc = exc
                    msg = str(exc).lower()
                    if "503" in msg or "429" in msg or "unavailable" in msg or "timeout" in msg:
                        print(f"[CodeHelper] Error temporal ({msg[:80]}...)  reintentando...")
                        time.sleep(2)
                        continue
                    raise
            raise last_exc

    return _W()"""

code_helper_content = re.sub(
    r'def _get_gemini\(\):.*?return _W\(\)', 
    new_get_gemini, 
    code_helper_content, 
    flags=re.DOTALL
)

# And fix screen debug
new_screen = """def _screen_debug_action(description, file_path, player, speak=None) -> str:
    if player:
        player.write_log("[Code] Taking screenshot for analysis...")

    print("[Code] 📸 Capturing screen for debug...")

    screenshot_path = _take_screenshot()
    if not screenshot_path:
        return "Could not take screenshot, sir. Please make sure PyAutoGUI is installed."

    file_content = ""
    if file_path:
        file_content, err = _read_file(file_path)
        if err:
            print(f"[Code] ⚠️ Could not read file: {err}")

    try:
        from core.providers import chat
        import json
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        prov = (cfg.get("specialist_provider") or "gemini").lower()
        mod = (cfg.get("specialist_model") or GEMINI_MODEL).strip()

        image_base64 = _image_to_base64(screenshot_path)
        user_question = description or "What error or problem do you see on the screen? How can it be fixed?"

        context = ""
        if file_content:
            context = f"\\n\\nAdditionally, here is the related file content:\\n```\\n{file_content[:4000]}\\n```"

        analysis_prompt = f\"\"\"You are an expert programmer and debugger analyzing a screenshot.

User's question: {user_question}{context}

Please:
1. Identify any errors, exceptions, or problems visible on the screen
2. Explain what is causing the problem in simple terms
3. Provide a concrete fix or solution
4. If there's code visible, show the corrected version

Be specific and actionable. If you see an error message, quote it exactly.\"\"\"

        msgs = [{"role": "user", "content": [
            {"type": "text", "text": analysis_prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}}
        ]}]

        res = chat(
            messages=msgs,
            provider=prov,
            model=mod,
            timeout=120
        )
        analysis = res["content"].strip()
        
        print(f"[Code] ✓ Screen analysis complete")

        try:
            screenshot_path.unlink()
        except Exception:
            pass

        if file_path and file_content:
            code_match = re.search(r"```[a-zA-Z]*\\n(.*?)```", analysis, re.DOTALL)
            if code_match:
                fixed_code = code_match.group(1).strip()
                save_path  = Path(file_path)
                _save_file(save_path, fixed_code)
                analysis += f"\\n\\n✓ Fixed code has been saved to: {file_path}"
                print(f"[Code] ✓ Fixed code saved: {file_path}")

        return analysis

    except Exception as e:
        try:
            screenshot_path.unlink()
        except Exception:
            pass
        return f"Screen analysis failed: {e}"
"""

code_helper_content = re.sub(
    r'def _screen_debug_action\(description, file_path, player, speak=None\) -> str:.*?(?=\n\ndef code_helper)',
    new_screen,
    code_helper_content,
    flags=re.DOTALL
)

open("actions/code_helper.py", "w", encoding="utf-8").write(code_helper_content)
print("Done routing subagents to central provider!")
