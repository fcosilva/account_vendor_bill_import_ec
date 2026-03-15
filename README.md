# account_vendor_bill_import_ec

Importacion de comprobantes SRI (XML/PDF) para Odoo 17.

Este modulo permite importar documentos electronicos a facturas en borrador desde el propio formulario de `account.move`.

## Alcance funcional

Soporta importacion en:
- Facturas de proveedor: `in_invoice`, `in_refund`
- Facturas de cliente: `out_invoice`, `out_refund`

Formatos soportados:
- XML SRI (`.xml`)
- RIDE PDF con texto seleccionable (`.pdf`, no escaneado)

## Flujo de uso

1. Abrir una factura en borrador.
2. Clic en `Import XML/PDF`.
3. Subir archivo XML o PDF.
4. Confirmar importacion.

Nota: el wizard se abre desde el boton del formulario (no desde menu independiente).

## Comportamiento por tipo de factura

### Factura de proveedor

- Busca/crea proveedor por RUC.
- Carga fecha, numero de documento, autorizacion, forma de pago SRI, moneda y lineas.
- Valida duplicados por proveedor + numero + autorizacion.
- Si detecta duplicado, abre el comprobante existente.

### Factura de cliente

- Verifica que el emisor del XML/PDF coincida con el RUC de la compania.
- Extrae identificacion del cliente (RUC, cedula o identificacion extranjera).
- Busca cliente por identificacion; si no existe, lo crea.
- Si crea un cliente nuevo, abre popup `Complete Customer Data` para terminar de editarlo.
- No fija secuencia/nombre al importar en borrador, para permitir cambio de diario antes de publicar.

## Configuracion

En `Ajustes > Contabilidad > Invoicing Settings`:
- `Customer Invoice Import Journal`

Reglas del diario configurado:
- Debe pertenecer a la compania activa.
- Debe ser de tipo ventas.
- No debe tener formatos EDI activos.

Si no se configura, el modulo busca automaticamente un diario de ventas sin EDI.

## Extraccion desde PDF

Prioridad de extraccion:
1. Metadata estructurada del PDF (DocumentInfo / AcroForm / XMP)
2. Campos RIDE por etiquetas
3. Fallback por texto (`pdftotext` con `-layout`)

Puntos importantes:
- No usa OCR.
- En facturas de cliente, toma identificacion del bloque de datos del cliente y valida RUC emisor en cabecera.
- La descripcion de linea prioriza extraccion por tabla RIDE y deja parser compacto como ultimo fallback.

## Extraccion desde XML

Admite:
- `<factura>` directo
- `<autorizacion>/<comprobante>` embebido

Campos base:
- `infoTributaria`
- `infoFactura`
- `detalles/detalle`

## Debug y trazabilidad

En importaciones PDF adjunta en chatter:
- Archivo original importado
- `*.debug.json` con `metadata`, `ride` y `extracted`

## Dependencias

- `account`
- `product`
- `l10n_ec`
- `l10n_ec_account_edi`

## Instalacion / actualizacion

```bash
docker-compose run --rm web-dev odoo -d openlab-dev -u account_vendor_bill_import_ec --stop-after-init
docker-compose restart web-dev
```

## Licencia

AGPL-3. Ver archivo `LICENSE`.
