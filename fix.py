import re
content = open('actions/code_helper.py', encoding='utf-8').read()
old_s = 'f"\\n\\nAdditionally, here is the related file content:\\n```\\n{file_content[:4000]}\\n```"'
content = re.sub(r'f"\s*Additionally, here is the related file content:\s*```\s*\{file_content\[:4000\]\}\s*```"', old_s, content, flags=re.DOTALL)
open('actions/code_helper.py', 'w', encoding='utf-8').write(content)
