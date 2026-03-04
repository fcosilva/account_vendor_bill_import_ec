# account_vendor_bill_import_ec

Importación de facturas de proveedor para Ecuador (SRI) en Odoo 17.

Permite cargar facturas de proveedor desde:
- XML SRI (`.xml`)
- RIDE PDF oficial con texto seleccionable (`.pdf`, no escaneado)

## Objetivo

Evitar digitación manual de facturas de compra y reducir errores de captura.

## Dependencias

Módulos requeridos:
- `account`
- `product`
- `l10n_ec`
- `l10n_ec_account_edi`

## Instalación / actualización

```bash
docker-compose run --rm web-dev odoo -d openlab-dev -u account_vendor_bill_import_ec --stop-after-init
docker-compose restart web-dev
```

## Dónde usarlo

En una factura de proveedor en borrador:
1. Ir a `Contabilidad > Proveedores > Facturas`.
2. Abrir una factura borrador (`Factura de proveedor` o `Nota de crédito de proveedor`).
3. Clic en botón `Import XML/PDF`.
4. Subir archivo y clic en `Import`.

Nota:
- Se mantiene solo esta vía (botón en formulario). No se usa menú separado.

## Qué campos llena

Campos principales en `account.move`:
- `Proveedor` (por RUC, creando partner si no existe)
- `Fecha de factura`
- `Número de Documento`
- `Referencia de factura`
- `Autorización electrónica`
- `Forma de pago (SRI)` (si se detecta)
- Líneas de factura (descripción, subtotal, impuestos según lo inferido)

## Regla de duplicados

La importación valida duplicado por combinación:
- RUC del proveedor
- Número de documento
- Número de autorización

Si ya existe, abre la factura existente en lugar de crear otra.

## Enfoque de extracción PDF

Prioridad de extracción:
1. Metadata estructurada del PDF (DocumentInfo / AcroForm / XMP)
2. Campos RIDE por etiquetas
3. Fallback por texto del PDF

Importante:
- Para RUC se prioriza la etiqueta `R.U.C.`.
- Para número de documento se prioriza `No.` del RIDE.
- No usa OCR.

## Enfoque de extracción XML

Se espera XML SRI de factura en esquema offline, incluyendo:
- `<factura>` directo, o
- `<autorizacion>/<comprobante>` embebido

Nodos principales usados:
- `infoTributaria`
- `infoFactura`
- `detalles/detalle`

Mapeo principal:
- `infoTributaria/ruc` -> RUC proveedor
- `infoTributaria/estab + ptoEmi + secuencial` -> número de documento
- `numeroAutorizacion` / `claveAcceso` -> autorización electrónica
- `infoFactura/fechaEmision` -> fecha de factura
- `pagos/pago/formaPago` -> forma de pago SRI
- `detalles/detalle` -> líneas de factura

Validaciones XML:
- Estructura mínima SRI válida.
- RUC proveedor válido (13 dígitos).
- Fecha de emisión interpretable.
- Al menos una línea de detalle.
- Coincidencia de identificación comprador vs VAT de la compañía cuando aplica.

## Debug de importación (PDF)

En cada importación PDF se adjuntan en el chatter de la factura:
- PDF original importado
- Archivo `*.debug.json` con:
  - `metadata`
  - `ride`
  - `extracted`

Esto permite auditar qué valor se tomó para cada campo.

## Errores comunes

- `Could not extract required fields from SRI PDF...`
  - El PDF no contiene metadata/campos esperados o no es un RIDE oficial usable.

- `The PDF has no readable text...`
  - El archivo es imagen/escaneo sin capa de texto.

- `A bill already exists for this supplier...`
  - El comprobante ya fue registrado.

- `Si su tipo de identificación es RUC, debe tener 13 dígitos`
  - El RUC extraído no es válido o está incompleto.

## Alcance actual

- Enfocado en facturas de proveedor SRI.
- El parser está optimizado para RIDE oficial; formatos PDF no estándar pueden requerir ajustes.
