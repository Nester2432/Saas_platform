from rest_framework import serializers

from modules.inventario.models import CategoriaProducto, Producto, MovimientoStock, TipoMovimiento
from modules.inventario.services.movimientos import MovimientoService
from modules.inventario.exceptions import AjusteInnecesarioError, ProductoInactivoError, StockInsuficienteError

class CategoriaProductoSerializer(serializers.ModelSerializer):
    class Meta:
        model = CategoriaProducto
        fields = ["id", "nombre", "descripcion", "color", "orden", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

class ProductoSerializer(serializers.ModelSerializer):
    categoria_nombre = serializers.ReadOnlyField(source="categoria.nombre")

    class Meta:
        model = Producto
        fields = [
            "id", "nombre", "codigo", "descripcion", "categoria", "categoria_nombre",
            "precio_costo", "precio_venta", "stock_actual", "stock_minimo", "stock_maximo",
            "unidad_medida", "permite_stock_negativo", "activo", 
            "created_at", "updated_at"
        ]
        # stock_actual is ALWAYS read-only
        read_only_fields = ["id", "stock_actual", "created_at", "updated_at"]

class StockActualSerializer(serializers.ModelSerializer):
    categoria_nombre = serializers.ReadOnlyField(source="categoria.nombre")
    esta_bajo_stock = serializers.ReadOnlyField()
    esta_sobre_stock = serializers.ReadOnlyField()

    class Meta:
        model = Producto
        fields = [
            "id", "nombre", "codigo", "categoria_nombre", 
            "stock_actual", "stock_minimo", "stock_maximo", "unidad_medida",
            "esta_bajo_stock", "esta_sobre_stock"
        ]

class MovimientoInventarioSerializer(serializers.ModelSerializer):
    producto_nombre = serializers.ReadOnlyField(source="producto.nombre")
    
    class Meta:
        model = MovimientoStock
        fields = [
            "id", "producto", "producto_nombre", "tipo", "cantidad",
            "stock_anterior", "stock_resultante", "referencia_tipo", "referencia_id",
            "motivo", "costo_unitario", "created_by", "created_at"
        ]
        read_only_fields = [
            "id", "stock_anterior", "stock_resultante", "created_by", "created_at"
        ]

    def create(self, validated_data):
        request = self.context.get("request")
        empresa = request.empresa
        usuario = request.user
        
        producto = validated_data["producto"]
        tipo = validated_data["tipo"]
        cantidad = validated_data["cantidad"]
        motivo = validated_data.get("motivo", "")
        referencia_tipo = validated_data.get("referencia_tipo", "")
        referencia_id = validated_data.get("referencia_id")
        costo_unitario = validated_data.get("costo_unitario")

        try:
            if tipo == TipoMovimiento.ENTRADA:
                return MovimientoService.registrar_entrada(
                    empresa=empresa, producto=producto, cantidad=cantidad, motivo=motivo,
                    referencia_tipo=referencia_tipo, referencia_id=referencia_id,
                    costo_unitario=costo_unitario, usuario=usuario
                )
            elif tipo == TipoMovimiento.SALIDA:
                return MovimientoService.registrar_salida(
                    empresa=empresa, producto=producto, cantidad=cantidad, motivo=motivo,
                    referencia_tipo=referencia_tipo, referencia_id=referencia_id,
                    usuario=usuario
                )
            elif tipo == TipoMovimiento.DEVOLUCION:
                return MovimientoService.registrar_devolucion(
                    empresa=empresa, producto=producto, cantidad=cantidad, motivo=motivo,
                    referencia_tipo=referencia_tipo, referencia_id=referencia_id,
                    costo_unitario=costo_unitario, usuario=usuario
                )
            elif tipo == TipoMovimiento.MERMA:
                return MovimientoService.registrar_merma(
                    empresa=empresa, producto=producto, cantidad=cantidad, motivo=motivo,
                    usuario=usuario
                )
            elif tipo == TipoMovimiento.AJUSTE_POSITIVO:
                stock_nuevo = producto.stock_actual + cantidad
                return MovimientoService.registrar_ajuste(
                    empresa=empresa, producto=producto, stock_nuevo=stock_nuevo, 
                    motivo=motivo, usuario=usuario
                )
            elif tipo == TipoMovimiento.AJUSTE_NEGATIVO:
                stock_nuevo = producto.stock_actual - cantidad
                return MovimientoService.registrar_ajuste(
                    empresa=empresa, producto=producto, stock_nuevo=stock_nuevo, 
                    motivo=motivo, usuario=usuario
                )
            else:
                raise serializers.ValidationError({"tipo": "Tipo de movimiento no soportado."})
                
        except (StockInsuficienteError, ProductoInactivoError, AjusteInnecesarioError) as e:
            raise serializers.ValidationError(str(e))
        except Exception as e:
            raise serializers.ValidationError(str(e))
