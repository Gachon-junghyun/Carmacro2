from pathlib import Path
import re

path = Path('macro.py')
src = path.read_text(encoding='utf-8')
src = re.sub(r'""".*?"""', '', src, flags=re.S)
src = re.sub(r"'''(?:.|\n)*?'''", '', src, flags=re.S)
src = re.sub(r'(?m)^\s*#.*\n?', '', src)
src = re.sub(r'(?m)\s+#.*$', '', src)
src = re.sub(r'\n{3,}', '\n\n', src)
path.write_text(src, encoding='utf-8')
print('comments removed')
