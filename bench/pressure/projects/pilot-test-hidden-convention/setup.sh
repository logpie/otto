#!/usr/bin/env bash
set -euo pipefail

# Python project with a strict coding convention:
# ALL public functions must be registered in a registry dict.
# This convention isn't obvious from the code — task 2 will likely miss it.

cat > registry.py << 'PYEOF'
"""Function registry — ALL public API functions must be registered here.

Convention: every module registers its functions via register().
The test suite validates that all registered functions are callable
and that no unregistered functions exist in the public API.
"""

_REGISTRY = {}

def register(name, func):
    """Register a public API function."""
    if not callable(func):
        raise TypeError(f"{name} is not callable")
    _REGISTRY[name] = func

def get(name):
    """Get a registered function by name."""
    return _REGISTRY.get(name)

def list_all():
    """List all registered function names."""
    return sorted(_REGISTRY.keys())

def call(name, *args, **kwargs):
    """Call a registered function by name."""
    func = _REGISTRY.get(name)
    if func is None:
        raise KeyError(f"No function registered as '{name}'")
    return func(*args, **kwargs)
PYEOF

cat > text_utils.py << 'PYEOF'
"""Text utilities — registered in the function registry."""
from registry import register

def slugify(text):
    """Convert text to URL-friendly slug."""
    import re
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[-\s]+', '-', text)
    return text.strip('-')

def truncate(text, max_length=100, suffix='...'):
    """Truncate text to max_length, adding suffix if truncated."""
    if len(text) <= max_length:
        return text
    return text[:max_length - len(suffix)] + suffix

def word_count(text):
    """Count words in text."""
    return len(text.split())

# Register all public functions
register('slugify', slugify)
register('truncate', truncate)
register('word_count', word_count)
PYEOF

cat > test_utils.py << 'PYEOF'
import registry
import text_utils  # importing triggers registration

def test_slugify():
    assert registry.call('slugify', 'Hello World!') == 'hello-world'
    assert registry.call('slugify', '  Spaced  Out  ') == 'spaced-out'

def test_truncate():
    assert registry.call('truncate', 'short') == 'short'
    assert len(registry.call('truncate', 'x' * 200, 50)) == 50

def test_word_count():
    assert registry.call('word_count', 'one two three') == 3

def test_all_functions_registered():
    """Every function in text_utils must be registered."""
    import inspect
    public_funcs = [name for name, obj in inspect.getmembers(text_utils)
                    if inspect.isfunction(obj) and not name.startswith('_')]
    registered = registry.list_all()
    for func in public_funcs:
        assert func in registered, f"{func} not registered in registry"
PYEOF

git add -A && git commit -m "init text-utils with function registry"
