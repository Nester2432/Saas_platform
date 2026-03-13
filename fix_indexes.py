import os

def replace_in_file(filepath, replacements):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    for old, new in replacements.items():
        content = content.replace(old, new)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

replace_in_file('modules/inventario/models.py', {
    'idx_movimiento_empresa_producto_fecha': 'idx_mov_emp_prod_fecha',
    'idx_movimiento_empresa_referencia': 'idx_mov_emp_ref',
    'idx_movimiento_empresa_producto_created': 'idx_mov_emp_prod_created'
})

replace_in_file('modules/ventas/models.py', {
    'idx_linea_venta_empresa_producto': 'idx_linvta_emp_prod',
    'idx_devolucion_linea_devolucion': 'idx_devlin_dev',
    'idx_devolucion_linea_linea_venta': 'idx_devlin_lin_vta'
})

print("Done")
