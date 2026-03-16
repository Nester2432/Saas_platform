
---

## 📁 Carpeta: `modules/clientes/`
**Descripción de propósito:** Gestiona la agenda de clientes de cada organización, perfiles detallados, notas de contacto e historial de actividad.
**Problemas o mejoras encontradas:** 
- Almacenamiento redundante de historial de clientes y uso excesivo de metadatos acoplados en los serializers. Recientemente se optimizó el query N+1, pero `models.py` tiene 287 líneas e incluye lógica que podría delegarse a servicios.
**Recomendaciones de arquitectura:** Separar la lógica de negocio de las vistas a una capa de servicios formal (eg. `ClienteService`). 

### 📄 Archivo: `modules/clientes/models.py`
- **Qué hace:** Define `Cliente`, `EtiquetaCliente`, `NotaCliente` e `HistorialCliente`.
- **Problemas detectados:** `HistorialCliente` parece cruzar responsabilidades con `modules/auditlog`.
- **Riesgos o bugs potenciales:** Si ambos sistemas loggean lo mismo, la base de datos crecerá innecesariamente.
- **Refactor sugerido:** Usar `AuditLog` para el historial de modificaciones del cliente, y dejar `HistorialCliente` estrictamente para "acciones de negocio" explícitas.
- **Nivel de calidad (1–10):** 7

### 📄 Archivo: `modules/clientes/api/views.py`
- **Qué hace:** Expone endpoints CRUD para Clientes con soporte de filtros y métricas.
- **Problemas detectados:** Las vistas (`ClienteViewSet`) son masivas y manejan lógica transaccional compleja usando `perform_create` y `perform_update`.
- **Riesgos o bugs potenciales:** Lógica difícil de probar unitariamente.
- **Refactor sugerido:** Mover las transacciones a un `clientes_service.py`. Las vistas solo deberían validar HTTP, instanciar servicios y retornar responses.
- **Nivel de calidad (1–10):** 7

---

## 📁 Carpeta: `modules/inventario/`
**Descripción de propósito:** Sistema completo de gestión de stock, movimientos, mermas, transferencias y control de proveedores.
**Problemas o mejoras encontradas:** 
- Es uno de los módulos más densos y complejos del sistema (`models.py` = 904 líneas, `movimientos.py` = 704 líneas).
- Existen excepciones repetitivas e importaciones en múltiples lugares del servicio.
**Recomendaciones de arquitectura:** Agrupar en subdominios: `catalogo`, `movimientos`, `proveedores`. 

### 📄 Archivo: `modules/inventario/models.py`
- **Qué hace:** Modela `Producto`, `Categoria`, `MovimientoStock`, entre 15 entidades. Mantiene invariantes de stock en vivo.
- **Problemas detectados:** `models.py` es un monolito gigante (casi 1000 líneas).
- **Riesgos o bugs potenciales:** Alto riesgo de conflictos de merge en equipos grandes y pérdida de cohesión.
- **Refactor sugerido:** Dividir en un paquete `modules/inventario/models/` con archivos como `producto.py`, `movimiento.py`, y enlazarlos en el `__init__.py`.
- **Nivel de calidad (1–10):** 6

### 📄 Archivo: `modules/inventario/services/movimientos.py`
- **Qué hace:** Ejecuta lógica de entrada, salida y ajustes de inventario de forma segura (Race Condition protection).
- **Problemas detectados:** Enorme cantidad de código defensivo (700 líneas), con transacciones largas que bloquean filas (`select_for_update`).
- **Riesgos o bugs potenciales:** Transacciones largas y pesadas (`select_for_update`) pueden causar lockings/deadlocks en BBDD si hay picos de uso en el tenant.
- **Refactor sugerido:** Utilizar Event Sourcing y proyecciones asíncronas para el stock calculable (o actualizar de forma diferida) si la concurrencia es alta.
- **Nivel de calidad (1–10):** 7

---

## 📁 Carpeta: `modules/turnos/`
**Descripción de propósito:** Motor de reservas y agendamiento para profesionales y servicios.
**Problemas o mejoras encontradas:** 
- `disponibilidad.py` y `turnos.py` son complejos porque el cálculo de overlap de fechas en código python puede ser inestable si no se utilizan adecuadamente rangos en Postgres.
**Recomendaciones de arquitectura:** Si la BBDD es PostgreSQL, sugeriría migrar el chequeo de solapamientos a exclusiones CONSTRAINT usando tipos `TSRANGE` nativos.

### 📄 Archivo: `modules/turnos/services/disponibilidad.py`
- **Qué hace:** Calcula ventanas de tiempo libre basándose en horarios de profesionales, excepciones y reservas previas.
- **Problemas detectados:** Operaciones pesadas en memoria/Python iterando sobre slots (`_check_horario_disponible`).
- **Riesgos o bugs potenciales:** Escalabilidad pobre si el profesional tiene un rango de horario masivo u obtiene muchas reglas de recurrencia en un solo fetch.
- **Refactor sugerido:** Delegar el join a consultas SQL espaciales/temporales a nivel de Base de Datos.
- **Nivel de calidad (1–10):** 7

### 📄 Archivo: `modules/turnos/api/permissions.py`
- **Qué hace:** Reglas muy granulares para roles dentro del módulo (PuedeVerTurnos, PuedeCrearTurnos).
- **Problemas detectados:** Las reglas están hardcodeadas (casi 350 líneas) basadas en strings en vez de una verdadera ACL dinámica o base de datos de claims en `EmpresaRol`.
- **Nivel de calidad (1–10):** 6
