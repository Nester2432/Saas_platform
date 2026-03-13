import os

filepath = 'modules/clientes/tests/test_clientes_api.py'
with open(filepath, 'r', encoding='utf-8') as f:
    lines = f.read().splitlines()

# The file repeats itself starting from line 964.
# We keep lines 0 to 963 (which is exactly 964 lines).
content = '\n'.join(lines[:964]) + '\n'

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(content)
print("Truncated file successfully.")
