"""Freeze the complete privileged card worker into one source snapshot."""
from pathlib import Path
import ast
import base64
import hashlib


def media_source(root: Path) -> tuple[bytes, dict]:
    sources = {name: (root / name).read_bytes() for name in ('card_saves.py', 'media.py')}
    local_names = {path.stem for path in root.glob('*.py')}
    frozen_names = {Path(name).stem for name in sources}
    for name, data in sources.items():
        for node in ast.walk(ast.parse(data, filename=name)):
            imported = ([alias.name.split('.')[0] for alias in node.names] if isinstance(node, ast.Import)
                        else [node.module.split('.')[0]] if isinstance(node, ast.ImportFrom) and node.module else [])
            if set(imported) & (local_names - frozen_names):
                raise ValueError('The media worker imports a local module that is not frozen: ' + name)
    encoded = {name: base64.b64encode(data).decode('ascii') for name, data in sources.items()}
    bundle = (
        "import base64, sys, types\n"
        "_saves = types.ModuleType('card_saves')\n"
        "_saves.__file__ = 'CartShelfCardSaves'\n"
        "sys.modules['card_saves'] = _saves\n"
        "exec(compile(base64.b64decode('" + encoded['card_saves.py'] + "'), 'CartShelfCardSaves', 'exec'), _saves.__dict__)\n"
        "_main = types.ModuleType('__main__')\n"
        "_main.__file__ = 'CartShelfMedia'\n"
        "sys.modules['__main__'] = _main\n"
        "exec(compile(base64.b64decode('" + encoded['media.py'] + "'), 'CartShelfMedia', 'exec'), _main.__dict__)\n"
    ).encode('utf-8')
    return bundle, {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
