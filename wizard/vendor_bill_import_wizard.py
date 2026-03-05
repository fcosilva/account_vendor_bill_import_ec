import base64
import binascii
import io
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
import unicodedata

from odoo import _, fields, models
from odoo.exceptions import UserError, ValidationError

try:
    from PyPDF2 import PdfReader
except ImportError:
    PdfReader = None
    from PyPDF2 import PdfFileReader


class VendorBillImportWizard(models.TransientModel):
    _name = "vendor.bill.import.wizard"
    _description = "Vendor Bill XML Import Wizard"

    file_data = fields.Binary(string="XML File", required=True)
    file_name = fields.Char(string="Filename")

    def action_import(self):
        self.ensure_one()
        file_name = (self.file_name or "").lower()
        is_pdf = file_name.endswith(".pdf")
        is_xml = file_name.endswith(".xml")
        if file_name and not (is_pdf or is_xml):
            raise UserError(_("Only XML or PDF files are supported."))

        file_bytes = self._decode_xml_file()
        if is_pdf:
            bill_data = self._extract_bill_data_from_pdf(file_bytes)
            attachment_mimetype = "application/pdf"
            attachment_name = self.file_name or "supplier_invoice.pdf"
            source_label = "PDF"
        else:
            bill_data = self._extract_bill_data(file_bytes)
            attachment_mimetype = "application/xml"
            attachment_name = self.file_name or "supplier_invoice.xml"
            source_label = "XML"

        try:
            move = self._create_or_update_bill(bill_data)
        except Exception as err:
            if "13 dígitos" in str(err or ""):
                raise UserError(
                    _(
                        "Vendor bill import interceptor: extracted supplier RUC %(vat)s. "
                        "Underlying error: %(error)s",
                        vat=bill_data.get("supplier_vat") or "-",
                        error=str(err),
                    )
                ) from err
            move = self._recover_duplicate_move(err=err, bill_data=bill_data)
            if not move:
                raise
        self._attach_source_file(
            move,
            file_bytes,
            bill_data,
            attachment_name=attachment_name,
            mimetype=attachment_mimetype,
            source_label=source_label,
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("Vendor Bill"),
            "res_model": "account.move",
            "res_id": move.id,
            "view_mode": "form",
            "target": "current",
        }

    def _decode_xml_file(self):
        try:
            return base64.b64decode(self.file_data)
        except (binascii.Error, ValueError) as err:
            raise UserError(_("Invalid file encoding.")) from err

    def _extract_bill_data(self, xml_bytes):
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as err:
            raise UserError(_("Invalid XML file.")) from err

        factura_root, authorization = self._get_factura_root(root)
        info_tributaria = self._child(factura_root, "infoTributaria")
        info_factura = self._child(factura_root, "infoFactura")
        detalles = self._child(factura_root, "detalles")
        if not info_tributaria or not info_factura or not detalles:
            raise UserError(_("XML does not contain a valid SRI invoice structure."))

        supplier_vat = self._normalize_ec_ruc(self._text(info_tributaria, "ruc"))
        if not supplier_vat:
            raise UserError(
                _(
                    "Supplier RUC was not found in the XML or is not a valid 13-digit RUC."
                )
            )

        estab = self._text(info_tributaria, "estab")
        pto_emi = self._text(info_tributaria, "ptoEmi")
        secuencial = self._text(info_tributaria, "secuencial")
        number = "-".join(filter(None, [estab, pto_emi, secuencial]))
        access_key = self._text(info_tributaria, "claveAcceso")
        authorization = self._resolve_authorization_number(
            xml_root=root,
            factura_root=factura_root,
            parsed_authorization=authorization,
            parsed_access_key=access_key,
        )
        if not authorization:
            raise UserError(
                _(
                    "No electronic authorization number was found in the XML. "
                    "Expected one of: numeroAutorizacion, claveAcceso, claveAccesoConsultada."
                )
            )

        invoice_date = self._parse_ec_date(self._text(info_factura, "fechaEmision"))
        if not invoice_date:
            raise UserError(_("Could not determine invoice date from XML."))
        currency = self._resolve_currency(self._text(info_factura, "moneda"))
        sri_payment_id = self._extract_sri_payment_from_xml(info_factura)

        lines = []
        for detail in list(detalles):
            if not self._tag(detail).endswith("detalle"):
                continue
            lines.append(self._extract_line_vals(detail))
        if not lines:
            raise UserError(_("No invoice lines were found in the XML."))

        total_amount = self._float(self._text(info_factura, "importeTotal"))
        company = self.env.company
        company_vat = self._digits(company.vat)
        buyer_vat = self._digits(self._text(info_factura, "identificacionComprador"))
        if company_vat and buyer_vat and company_vat != buyer_vat:
            raise ValidationError(
                _(
                    "XML buyer identification (%(buyer)s) does not match company VAT (%(company)s).",
                    buyer=buyer_vat,
                    company=company_vat,
                )
            )

        return {
            "supplier_vat": supplier_vat,
            "supplier_name": self._text(info_tributaria, "razonSocial"),
            "invoice_date": invoice_date,
            "number": number,
            "authorization": authorization,
            "sri_payment_id": sri_payment_id,
            "currency_id": currency.id if currency else False,
            "line_vals": lines,
            "amount_total_xml": total_amount,
        }

    def _extract_bill_data_from_pdf(self, pdf_bytes):
        metadata = self._extract_pdf_structured_data(pdf_bytes)
        text = self._extract_text_from_pdf(pdf_bytes)
        ride = self._extract_ride_fields_from_pdf(text)
        extracted = self._build_pdf_extraction_json(metadata=metadata, ride=ride, text=text)

        # Supplier RUC must come from explicit R.U.C. label mapping.
        supplier_vat = self._normalize_ec_ruc(
            extracted.get("supplier_vat"), exclude_values=[self.env.company.vat]
        )
        number = extracted.get("invoice_number")
        authorization = extracted.get("authorization")
        missing = []
        if not supplier_vat:
            missing.append(_("supplier RUC"))
        if not number:
            missing.append(_("invoice number"))
        if not authorization:
            missing.append(_("electronic authorization"))
        if missing:
            raise UserError(
                _(
                    "Could not extract required fields from SRI PDF: %(missing)s. "
                    "Please verify the PDF content or import the XML.",
                    missing=", ".join(missing),
                )
            )

        invoice_date = extracted.get("invoice_date")
        if not invoice_date:
            raise UserError(
                _("Could not determine invoice date from PDF.")
            )
        subtotal = extracted.get("subtotal")
        total_amount = extracted.get("total_amount")
        if not total_amount:
            total_amount = self._extract_global_total_amount(text)
        if not total_amount:
            total_amount = subtotal
        if not subtotal:
            subtotal = total_amount
        if not subtotal:
            raise UserError(_("Could not determine invoice totals from PDF."))

        tax_ids = []
        tax_rate = self._compute_tax_rate(subtotal, total_amount)
        if tax_rate > 0:
            tax = self._map_tax(codigo="", codigo_porcentaje="", tarifa=tax_rate)
            if tax:
                tax_ids = [tax.id]

        line_description = extracted.get("line_description") or _("Imported from SRI PDF")
        line_vals = {
            "name": line_description,
            "product_id": self._get_fallback_product(line_description).id,
            "quantity": 1.0,
            "price_unit": subtotal,
            "discount": 0.0,
        }
        if tax_ids:
            line_vals["tax_ids"] = [(6, 0, tax_ids)]

        return {
            "supplier_vat": supplier_vat,
            "supplier_name": self._extract_supplier_name_from_pdf_with_vat(text, supplier_vat)
            or ride.get("supplier_name")
            or metadata.get("supplier_name")
            or self._extract_supplier_name_from_pdf(text)
            or supplier_vat,
            "invoice_date": invoice_date,
            "number": self._normalize_doc_number(number),
            "authorization": authorization,
            "sri_payment_id": extracted.get("sri_payment_id"),
            "currency_id": self.env.company.currency_id.id,
            "line_vals": [line_vals],
            "amount_total_xml": total_amount,
            "debug_payload": {
                "source": "pdf",
                "metadata": metadata,
                "ride": ride,
                "extracted": extracted,
            },
        }

    def _build_pdf_extraction_json(self, metadata, ride, text):
        """Return a normalized extraction payload used to map PDF -> vendor bill fields."""
        payload = {
            "supplier_vat": metadata.get("supplier_vat")
            or ride.get("supplier_vat")
            or self._extract_supplier_vat_from_pdf(text),
            # Prioritize RIDE metadata label "No." (item 2) over visual parsing.
            "invoice_number": metadata.get("invoice_number")
            or ride.get("invoice_number")
            or self._extract_invoice_number_from_pdf(text),
            "authorization": metadata.get("authorization")
            or ride.get("authorization")
            or self._extract_authorization_from_pdf(text),
            "invoice_date": metadata.get("invoice_date")
            or ride.get("invoice_date")
            or self._extract_invoice_date_from_pdf(text)
            or self._extract_authorization_date_from_pdf(text),
            "subtotal": metadata.get("subtotal")
            or ride.get("subtotal")
            or self._extract_amount_from_pdf(text, labels=[r"TOTAL\s+SIN\s+IMPUESTOS", r"SUBTOTAL"]),
            "total_amount": metadata.get("total_amount")
            or ride.get("total_amount")
            or self._extract_amount_from_pdf(
                text, labels=[r"VALOR\s+TOTAL", r"IMPORTE\s+TOTAL", r"\bTOTAL\b"]
            ),
            "line_description": metadata.get("line_description")
            or metadata.get("description")
            or self._extract_line_description_from_compact_ride(text)
            or ride.get("line_description")
            or self._extract_line_description_from_pdf(text),
            "sri_payment_id": metadata.get("sri_payment_id")
            or ride.get("sri_payment_id")
            or self._extract_sri_payment_from_pdf_text(text),
        }
        # Keep a JSON-serializable dict for debug/tracing if needed by callers.
        return json.loads(json.dumps(payload, default=str))

    def _extract_pdf_structured_data(self, pdf_bytes):
        """Extract structured PDF metadata (DocumentInfo, AcroForm, XMP) before text parsing."""
        pairs = []
        metadata = {}
        try:
            if PdfReader:
                reader = PdfReader(io.BytesIO(pdf_bytes))
                info = reader.metadata or {}
                for key, value in dict(info).items():
                    self._append_metadata_pair(pairs, key, value)
                acro = {}
                get_fields = getattr(reader, "get_fields", None)
                if callable(get_fields):
                    acro = get_fields() or {}
                for field_name, field_val in (acro or {}).items():
                    value = field_val
                    if isinstance(field_val, dict):
                        value = (
                            field_val.get("/V")
                            or field_val.get("V")
                            or field_val.get("/DV")
                            or field_val.get("DV")
                            or ""
                        )
                    self._append_metadata_pair(pairs, field_name, value)
                if hasattr(reader, "trailer"):
                    metadata_obj = (
                        reader.trailer.get("/Root", {})
                        .get("/Metadata")
                    )
                    if metadata_obj:
                        try:
                            xmp = metadata_obj.get_object().get_data().decode("utf-8", "ignore")
                        except Exception:
                            xmp = ""
                        if xmp:
                            for key, value in self._extract_xmp_pairs(xmp):
                                self._append_metadata_pair(pairs, key, value)
            else:
                pdf = PdfFileReader(io.BytesIO(pdf_bytes))
                info = pdf.getDocumentInfo() or {}
                for key, value in dict(info).items():
                    self._append_metadata_pair(pairs, key, value)
                fields = pdf.getFields() or {}
                for field_name, field_val in fields.items():
                    value = field_val
                    if isinstance(field_val, dict):
                        value = field_val.get("/V") or field_val.get("V") or ""
                    self._append_metadata_pair(pairs, field_name, value)
                try:
                    root = pdf.trailer["/Root"].getObject()
                    metadata_obj = root.get("/Metadata")
                    if metadata_obj:
                        xmp = metadata_obj.getObject().getData().decode("utf-8", "ignore")
                        for key, value in self._extract_xmp_pairs(xmp):
                            self._append_metadata_pair(pairs, key, value)
                except Exception:
                    pass
        except Exception:
            return metadata

        supplier_vat = self._extract_supplier_ruc_from_metadata_pairs(pairs)
        if supplier_vat:
            metadata["supplier_vat"] = supplier_vat

        number = self._extract_invoice_number_from_metadata_pairs(pairs)
        if number:
            number_candidate = self._extract_invoice_number_from_pdf(number) or number
            metadata["invoice_number"] = self._normalize_doc_number(number_candidate)

        authorization = self._metadata_lookup(
            pairs, key_patterns=[r"AUTORIZACION", r"CLAVE_ACCESO"]
        )
        authorization_digits = self._digits(authorization)
        if authorization_digits:
            metadata["authorization"] = authorization_digits[:49]

        supplier_name = self._metadata_lookup(
            pairs, key_patterns=[r"RAZON_SOCIAL", r"NOMBRES", r"PROVEEDOR", r"EMISOR"]
        )
        if supplier_name:
            metadata["supplier_name"] = re.sub(r"\s+", " ", supplier_name).strip(" -:")

        invoice_date = self._extract_invoice_date_from_metadata_pairs(pairs)
        if invoice_date:
            metadata["invoice_date"] = invoice_date

        subtotal = self._parse_decimal(
            self._metadata_lookup(
                pairs, key_patterns=[r"SUBTOTAL", r"TOTAL_SIN_IMPUESTOS"]
            )
        )
        if subtotal > 0:
            metadata["subtotal"] = subtotal

        total_amount = self._parse_decimal(
            self._metadata_lookup(pairs, key_patterns=[r"VALOR_TOTAL", r"TOTAL"])
        )
        if total_amount > 0:
            metadata["total_amount"] = total_amount

        line_desc = self._extract_line_descriptions_from_metadata_pairs(pairs)
        if not line_desc:
            line_desc = self._metadata_lookup(
                pairs, key_patterns=[r"DESCRIPCION", r"DETALLE", r"CONCEPTO"]
            )
        if line_desc:
            metadata["line_description"] = re.sub(r"\s+", " ", line_desc).strip(" -:")

        sri_payment_id = self._extract_sri_payment_from_metadata_pairs(pairs)
        if sri_payment_id:
            metadata["sri_payment_id"] = sri_payment_id
        return metadata

    def _extract_invoice_date_from_metadata_pairs(self, pairs):
        if not pairs:
            return False

        # Prefer exact invoice date keys in structured metadata.
        prioritized_keys = {
            "FECHA",
            "FECHA_EMISION",
            "FECHA_DE_EMISION",
            "FECHAEMISION",
            "IDENTIFICACION_FECHA",
        }
        for key, value in pairs:
            if key not in prioritized_keys:
                continue
            match = re.search(r"(\d{2}\s*[/-]\s*\d{2}\s*[/-]\s*\d{4})", value or "")
            if not match:
                continue
            try:
                return self._parse_ec_date(re.sub(r"\s+", "", match.group(1)))
            except UserError:
                continue

        # Secondary metadata fallback: any FECHA-like key excluding authorization timestamp keys.
        for key, value in pairs:
            if "FECHA" not in key:
                continue
            if "AUTORIZACION" in key or "HORA" in key:
                continue
            match = re.search(r"(\d{2}\s*[/-]\s*\d{2}\s*[/-]\s*\d{4})", value or "")
            if not match:
                continue
            try:
                return self._parse_ec_date(re.sub(r"\s+", "", match.group(1)))
            except UserError:
                continue
        return False

    def _extract_invoice_number_from_metadata_pairs(self, pairs):
        if not pairs:
            return ""

        # Primary source requested: RIDE metadata label "No." (normalized as NO).
        for key, value in pairs:
            if key not in {"NO", "NRO"}:
                continue
            candidate = self._extract_invoice_number_from_pdf(value or "")
            if candidate:
                return candidate

        # Secondary source: explicit number-like keys in metadata.
        for key, value in pairs:
            if not re.search(r"(NUMERO|NRO|SECUENCIAL|DOCUMENTO|FACTURA)", key, flags=re.IGNORECASE):
                continue
            candidate = self._extract_invoice_number_from_pdf(value or "")
            if candidate:
                return candidate

        return ""

    def _extract_line_descriptions_from_metadata_pairs(self, pairs):
        if not pairs:
            return ""
        candidates = []
        for key, value in pairs:
            if not value:
                continue
            if not re.search(
                r"(DESCRIPCION|DESCRIP|DETALLE|CONCEPTO)", key, flags=re.IGNORECASE
            ):
                continue
            if re.search(r"(ADICIONAL|PRECIO|TOTAL|SUBTOTAL|CANTIDAD|CODIGO)", key, flags=re.IGNORECASE):
                continue
            clean = re.sub(r"\s+", " ", value).strip(" -:")
            if len(clean) < 4:
                continue
            if re.fullmatch(r"[\d\W]+", clean):
                continue
            candidates.append((key, clean))
        if not candidates:
            return ""

        # Stable order for repeated keys with numeric suffixes (e.g., DESCRIPCION_1, DESCRIPCION_2).
        candidates.sort(key=lambda kv: kv[0])
        unique_lines = []
        seen = set()
        for _, text in candidates:
            up = text.upper()
            if up in seen:
                continue
            seen.add(up)
            unique_lines.append(text)
        return "\n".join(unique_lines)

    def _extract_supplier_ruc_from_metadata_pairs(self, pairs):
        """Prefer exact RIDE metadata key for label 'R.U.C.'."""
        if not pairs:
            return ""
        company_vat = self.env.company.vat

        for key, value in pairs:
            if key == "R_U_C":
                ruc = self._normalize_ec_ruc(value, exclude_values=[company_vat])
                if ruc:
                    return ruc

        for key, value in pairs:
            if key in {"RUC", "IDENTIFICACION_PROVEEDOR"}:
                ruc = self._normalize_ec_ruc(value, exclude_values=[company_vat])
                if ruc:
                    return ruc
        return ""

    def _extract_sri_payment_from_metadata_pairs(self, pairs):
        if not pairs:
            return False
        candidates = []
        for key, value in pairs:
            if not value:
                continue
            if "FORMA" in key and "PAGO" in key:
                candidates.append(value)
        return self._resolve_sri_payment_from_values(candidates)

    def _append_metadata_pair(self, pairs, key, value):
        key_text = self._normalize_metadata_key(key)
        value_text = self._metadata_value_to_text(value)
        if key_text and value_text:
            pairs.append((key_text, value_text))

    def _metadata_lookup(self, pairs, key_patterns):
        if not pairs:
            return ""
        for key, value in pairs:
            compact = key.replace("_", "")
            for pattern in key_patterns:
                if re.search(pattern, key, flags=re.IGNORECASE) or re.search(
                    pattern.replace("_", ""), compact, flags=re.IGNORECASE
                ):
                    return value
        return ""

    def _normalize_metadata_key(self, key):
        text = str(key or "").strip()
        if not text:
            return ""
        # Normalize accents so labels like "Descripción" become "DESCRIPCION".
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = text.upper()
        return re.sub(r"[^A-Z0-9]+", "_", text).strip("_")

    def _metadata_value_to_text(self, value):
        if value is None:
            return ""
        text = str(value).strip()
        if not text:
            return ""
        return text[1:] if text.startswith("/") else text

    def _extract_xmp_pairs(self, xmp_text):
        pairs = []
        if not xmp_text:
            return pairs
        for match in re.finditer(
            r"<([A-Za-z0-9:_\-]+)>([^<]{1,400})</\1>", xmp_text, flags=re.IGNORECASE
        ):
            key = match.group(1).split(":")[-1]
            value = match.group(2).strip()
            if value:
                pairs.append((key, value))
        return pairs

    def _get_factura_root(self, root):
        tag = self._tag(root)
        authorization = False
        factura_root = root if tag.endswith("factura") else False

        if tag.endswith("autorizacion"):
            authorization = self._text(root, "numeroAutorizacion")
            raw_comprobante = self._text(root, "comprobante")
            if raw_comprobante:
                try:
                    comprobante_root = ET.fromstring(raw_comprobante)
                except ET.ParseError as err:
                    raise UserError(
                        _("Authorization XML contains invalid embedded comprobante.")
                    ) from err
                factura_root = self._find_factura_node(comprobante_root)
        elif tag.endswith("autorizaciones"):
            auth_nodes = [node for node in root if self._tag(node).endswith("autorizacion")]
            if auth_nodes:
                authorization = self._text(auth_nodes[0], "numeroAutorizacion")
                raw_comprobante = self._text(auth_nodes[0], "comprobante")
                if raw_comprobante:
                    try:
                        comprobante_root = ET.fromstring(raw_comprobante)
                    except ET.ParseError as err:
                        raise UserError(
                            _("Authorization XML contains invalid embedded comprobante.")
                        ) from err
                    factura_root = self._find_factura_node(comprobante_root)

        if not factura_root:
            factura_root = self._find_factura_node(root)
        if not factura_root:
            raise UserError(_("Could not locate the <factura> node in XML."))
        return factura_root, authorization

    def _find_factura_node(self, root):
        if self._tag(root).endswith("factura"):
            return root
        for node in root.iter():
            if self._tag(node).endswith("factura"):
                return node
        return False

    def _extract_line_vals(self, detail):
        code = self._text(detail, "codigoPrincipal") or self._text(detail, "codigoAuxiliar")
        description = self._text(detail, "descripcion") or _("Imported XML line")
        quantity = self._float(self._text(detail, "cantidad"), default=1.0)
        price_unit = self._float(self._text(detail, "precioUnitario"))
        discount_amount = self._float(self._text(detail, "descuento"))

        line_total_wo_tax = self._float(self._text(detail, "precioTotalSinImpuesto"))
        theoretical_total = quantity * price_unit
        discount_pct = 0.0
        if theoretical_total > 0 and discount_amount:
            discount_pct = min(100.0, (discount_amount / theoretical_total) * 100.0)

        tax_ids = []
        taxes_node = self._child(detail, "impuestos")
        if taxes_node is not None:
            for tax_node in taxes_node:
                if not self._tag(tax_node).endswith("impuesto"):
                    continue
                tax = self._map_tax(
                    codigo=self._text(tax_node, "codigo"),
                    codigo_porcentaje=self._text(tax_node, "codigoPorcentaje"),
                    tarifa=self._float(self._text(tax_node, "tarifa")),
                )
                if tax:
                    tax_ids.append(tax.id)

        product = self._find_product(code, description)
        line_vals = {
            "name": description,
            "product_id": product.id,
            "quantity": quantity or 1.0,
            "price_unit": price_unit,
            "discount": discount_pct,
        }
        if tax_ids:
            line_vals["tax_ids"] = [(6, 0, list(dict.fromkeys(tax_ids)))]

        if line_total_wo_tax and not price_unit:
            line_vals["price_unit"] = line_total_wo_tax / max(line_vals["quantity"], 1.0)
        return line_vals

    def _create_or_update_bill(self, bill_data):
        partner = self._find_or_create_partner(
            vat=bill_data["supplier_vat"], name=bill_data["supplier_name"]
        )
        forced_move = self._get_forced_target_move()
        hard_duplicate = self._find_duplicate_candidate(
            number=bill_data["number"],
            supplier_vat=bill_data["supplier_vat"],
            authorization=bill_data["authorization"],
            exclude_move_ids=forced_move.ids if forced_move else None,
        )
        if hard_duplicate:
            hard_duplicate.message_post(
                body=_(
                    "XML import detected an existing supplier document and opened it instead of creating a duplicate."
                )
            )
            return hard_duplicate

        if forced_move:
            move = forced_move
        else:
            move = self._resolve_target_move(
                partner=partner,
                number=bill_data["number"],
                authorization=bill_data["authorization"],
            )
        move_vals = self._prepare_move_vals(partner, bill_data)
        if move and move.state == "draft":
            try:
                move.invoice_line_ids = [(5, 0, 0)]
                move.write(move_vals)
                self._set_latam_document_number(move, bill_data.get("number"))
            except (ValidationError, UserError) as err:
                raise UserError(
                    _(
                        "Bill update failed. Extracted supplier RUC: %(supplier)s. "
                        "Matched partner: %(partner)s (VAT: %(partner_vat)s). "
                        "Original error: %(error)s",
                        supplier=bill_data.get("supplier_vat") or "-",
                        partner=partner.display_name or "-",
                        partner_vat=partner.vat or "-",
                        error=str(err),
                    )
                ) from err
            except Exception as err:
                fallback_move = self._recover_duplicate_move(err=err, bill_data=bill_data)
                if fallback_move:
                    return fallback_move
                raise
        elif move:
            move.message_post(
                body=_(
                    "XML import detected an existing posted bill for this supplier and document number. "
                    "No changes were applied."
                )
            )
        else:
            try:
                move = self.env["account.move"].create(move_vals)
                self._set_latam_document_number(move, bill_data.get("number"))
            except (ValidationError, UserError) as err:
                raise UserError(
                    _(
                        "Bill creation failed. Extracted supplier RUC: %(supplier)s. "
                        "Matched partner: %(partner)s (VAT: %(partner_vat)s). "
                        "Original error: %(error)s",
                        supplier=bill_data.get("supplier_vat") or "-",
                        partner=partner.display_name or "-",
                        partner_vat=partner.vat or "-",
                        error=str(err),
                    )
                ) from err
            except Exception as err:
                fallback_move = self._recover_duplicate_move(err=err, bill_data=bill_data)
                if fallback_move:
                    return fallback_move
                raise

        xml_total = bill_data["amount_total_xml"]
        if xml_total and abs(move.amount_total - xml_total) > 0.05:
            move.message_post(
                body=_(
                    "Warning: XML total (%(xml_total).2f) differs from computed bill total (%(bill_total).2f).",
                    xml_total=xml_total,
                    bill_total=move.amount_total,
                )
            )
        return move

    def _prepare_move_vals(self, partner, bill_data):
        company = self.env.company
        latam_doc = self.env.ref("l10n_ec.ec_dt_01", raise_if_not_found=False)
        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", company.id)], limit=1
        )

        vals = {
            "move_type": "in_invoice",
            "partner_id": partner.id,
            "company_id": company.id,
            "invoice_date": bill_data["invoice_date"],
            "date": bill_data["invoice_date"],
            "invoice_line_ids": [(0, 0, line_vals) for line_vals in bill_data["line_vals"]],
            "ref": bill_data["number"],
        }
        if journal:
            vals["journal_id"] = journal.id
        if bill_data["currency_id"]:
            vals["currency_id"] = bill_data["currency_id"]
        if "l10n_latam_document_type_id" in self.env["account.move"]._fields and latam_doc:
            vals["l10n_latam_document_type_id"] = latam_doc.id
        if (
            bill_data["authorization"]
            and "l10n_ec_electronic_authorization" in self.env["account.move"]._fields
        ):
            vals["l10n_ec_electronic_authorization"] = bill_data["authorization"][:49]
        if (
            bill_data.get("sri_payment_id")
            and "l10n_ec_sri_payment_id" in self.env["account.move"]._fields
        ):
            vals["l10n_ec_sri_payment_id"] = bill_data["sri_payment_id"]
        return vals

    def _set_latam_document_number(self, move, number):
        """Persist document number through `name` because LATAM document number is computed from it."""
        if not move or not number:
            return
        if "l10n_latam_document_type_id" not in move._fields or not move.l10n_latam_document_type_id:
            return
        doc_number = number
        if not move._skip_format_document_number():
            doc_number = move.l10n_latam_document_type_id._format_document_number(number)
        move.name = "%s %s" % (move.l10n_latam_document_type_id.doc_code_prefix, doc_number)

    def _resolve_target_move(self, partner, number, authorization):
        forced_move = self._get_forced_target_move()
        if forced_move:
            return forced_move
        return self._find_duplicate_candidate(
            number=number,
            supplier_vat=partner.vat,
            authorization=authorization,
        )

    def _get_forced_target_move(self):
        forced_move_id = self.env.context.get("import_target_move_id")
        if forced_move_id:
            forced_move = self.env["account.move"].browse(forced_move_id).exists()
            if (
                forced_move
                and forced_move.company_id == self.env.company
                and forced_move.move_type in ("in_invoice", "in_refund")
                and forced_move.state == "draft"
            ):
                return forced_move
        return False

    def _recover_duplicate_move(self, err, bill_data):
        try:
            self.env.cr.rollback()
        except Exception:
            pass
        number = bill_data.get("number")
        supplier_vat = bill_data.get("supplier_vat")
        authorization = bill_data.get("authorization")
        existing = self._find_duplicate_candidate(
            number=number,
            supplier_vat=supplier_vat,
            authorization=authorization,
        )
        if not existing:
            parsed_number = self._extract_number_from_error(err)
            if parsed_number:
                existing = self._find_duplicate_candidate(
                    number=parsed_number,
                    supplier_vat=supplier_vat,
                    authorization=authorization,
                )
        if not existing:
            return False
        existing.message_post(
            body=_(
                "XML import detected a duplicate supplier document. "
                "The existing bill was opened instead."
            )
        )
        return existing

    def _find_existing_by_number(self, number, supplier_vat):
        if not number or "l10n_latam_document_number" not in self.env["account.move"]._fields:
            return False
        vat_digits = self._digits(supplier_vat)
        domain = [
            ("company_id", "=", self.env.company.id),
            ("move_type", "in", ["in_invoice", "in_refund"]),
            ("state", "!=", "cancel"),
        ]
        move_model = self.env["account.move"]
        moves = self.env["account.move"]
        if "l10n_latam_document_number" in move_model._fields:
            moves |= move_model.search(
                domain + [("l10n_latam_document_number", "=", number)], order="id desc", limit=10
            )
        if "ref" in move_model._fields:
            moves |= move_model.search(domain + [("ref", "=", number)], order="id desc", limit=10)
        moves = moves.sorted(key=lambda m: m.id, reverse=True)
        if not moves:
            return False
        if not vat_digits:
            return moves[0]
        for move in moves:
            if self._digits(move.partner_id.vat) == vat_digits:
                return move
        return moves[0]

    def _find_duplicate_candidate(self, number, supplier_vat, authorization, exclude_move_ids=None):
        if not number or not authorization or not supplier_vat:
            return False
        move_model = self.env["account.move"]
        domain = [
            ("company_id", "=", self.env.company.id),
            ("move_type", "in", ["in_invoice", "in_refund"]),
            ("state", "!=", "cancel"),
        ]
        if exclude_move_ids:
            domain.append(("id", "not in", list(exclude_move_ids)))
        vat_digits = self._digits(supplier_vat)
        if vat_digits:
            domain += [
                "|",
                ("partner_id.vat", "ilike", vat_digits),
                ("commercial_partner_id.vat", "ilike", vat_digits),
            ]
        else:
            return False

        number_digits = self._digits(number)
        auth_digits = self._digits(authorization)
        candidates = move_model.search(domain, order="id desc")
        for move in candidates:
            if not self._authorization_matches(move, auth_digits):
                continue
            if not self._matches_number(move, number_digits):
                continue
            return move
        return False

    def _extract_number_from_error(self, error):
        message = str(error or "")
        match = re.search(r"number\s+([0-9][0-9\-\s]{3,})", message, flags=re.IGNORECASE)
        if match:
            return re.sub(r"\s+", "", match.group(1))
        match = re.search(r"(\d{3}-\d{3}-\d{6,9})", message)
        if match:
            return match.group(1)
        digits = self._digits(message)
        if len(digits) >= 15:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:15]}"
        return ""

    def _matches_number(self, move, number_digits):
        if not number_digits:
            return False
        fields_to_check = [
            "ref",
            "name",
            "payment_reference",
            "l10n_latam_document_number",
            "l10n_ec_withhold_number",
        ]
        for field_name in fields_to_check:
            if field_name not in move._fields:
                continue
            if self._digits(getattr(move, field_name, "") or "") == number_digits:
                return True
        return False

    def _authorization_matches(self, move, auth_digits):
        if not auth_digits or "l10n_ec_electronic_authorization" not in move._fields:
            return False
        return self._digits(move.l10n_ec_electronic_authorization or "") == auth_digits

    def _find_or_create_partner(self, vat, name):
        vat_digits = self._normalize_ec_ruc(vat)
        if not vat_digits:
            raise UserError(
                _(
                    "Could not determine a valid 13-digit supplier RUC from the imported file."
                )
            )
        partner = self.env["res.partner"].search(
            [
                ("company_id", "in", [False, self.env.company.id]),
                ("vat", "=", vat_digits),
            ],
            limit=1,
        )
        if partner:
            if self._normalize_ec_ruc(partner.vat) != vat_digits:
                partner = False
            elif partner.vat != vat_digits:
                try:
                    partner.write({"vat": vat_digits})
                except (ValidationError, UserError) as err:
                    raise UserError(
                        _(
                            "Partner VAT update failed. Extracted supplier RUC: %(supplier)s. "
                            "Matched partner: %(partner)s (VAT before update: %(partner_vat)s). "
                            "Original error: %(error)s",
                            supplier=vat_digits,
                            partner=partner.display_name or "-",
                            partner_vat=partner.vat or "-",
                            error=str(err),
                        )
                    ) from err
        if partner:
            return partner

        ident_type = self.env.ref("l10n_ec.ec_ruc", raise_if_not_found=False)
        partner_vals = {
            "name": name or vat_digits,
            "vat": vat_digits,
            "supplier_rank": 1,
            "country_id": self.env.ref("base.ec").id,
        }
        if ident_type and "l10n_latam_identification_type_id" in self.env["res.partner"]._fields:
            partner_vals["l10n_latam_identification_type_id"] = ident_type.id
        try:
            return self.env["res.partner"].create(partner_vals)
        except (ValidationError, UserError) as err:
            raise UserError(
                _(
                    "Supplier creation failed with RUC '%(raw)s' (normalized '%(normalized)s'). "
                    "Original error: %(error)s",
                    raw=vat or "",
                    normalized=vat_digits,
                    error=str(err),
                )
            ) from err

    def _find_product(self, code, description):
        product = False
        product_model = self.env["product.product"]
        if code:
            product = product_model.search(
                [
                    ("company_id", "in", [False, self.env.company.id]),
                    "|",
                    ("default_code", "=", code),
                    ("barcode", "=", code),
                ],
                limit=1,
            )
        if product:
            return product

        return self._get_fallback_product(description)

    def _get_fallback_product(self, description):
        param_value = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("account_vendor_bill_import_ec.default_product_id")
        )
        product = False
        if param_value and param_value.isdigit():
            product = self.env["product.product"].browse(int(param_value)).exists()
        if product:
            return product

        company = self.env.company
        template = self.env["product.template"].sudo().search(
            [
                ("name", "=", "GASTO IMPORTADO XML"),
                ("company_id", "in", [False, company.id]),
            ],
            limit=1,
        )
        if not template:
            template = self.env["product.template"].sudo().create(
                {
                    "name": "GASTO IMPORTADO XML",
                    "type": "service",
                    "purchase_ok": True,
                    "sale_ok": False,
                    "company_id": company.id,
                    "description_purchase": _(
                        "Fallback product for vendor bill imports from XML."
                    ),
                }
            )
        return template.product_variant_id

    def _map_tax(self, codigo, codigo_porcentaje, tarifa):
        tax_model = self.env["account.tax"]
        company = self.env.company
        domain = [("company_id", "=", company.id), ("type_tax_use", "=", "purchase")]

        has_tax_code = "l10n_ec_xml_fe_code" in tax_model._fields
        has_group_code = "l10n_ec_xml_fe_code" in self.env["account.tax.group"]._fields
        if codigo and codigo_porcentaje and has_tax_code and has_group_code:
            tax = tax_model.search(
                domain
                + [
                    ("tax_group_id.l10n_ec_xml_fe_code", "=", codigo),
                    ("l10n_ec_xml_fe_code", "=", codigo_porcentaje),
                ],
                limit=1,
            )
            if tax:
                return tax

        candidates = tax_model.search(
            domain + [("amount_type", "=", "percent"), ("active", "=", True)]
        )
        if not candidates:
            return False

        for tax in candidates:
            if abs(tax.amount - tarifa) < 0.0001:
                return tax
        if abs(tarifa) < 0.0001:
            zero_tax = candidates.filtered(lambda t: abs(t.amount) < 0.0001)[:1]
            return zero_tax or False
        return False

    def _resolve_authorization_number(
        self, xml_root, factura_root, parsed_authorization, parsed_access_key
    ):
        candidates = [
            parsed_authorization,
            parsed_access_key,
            self._text(xml_root, "numeroAutorizacion"),
            self._text(xml_root, "claveAccesoConsultada"),
            self._text(factura_root, "claveAcceso"),
        ]
        for value in candidates:
            digits = self._digits(value)
            if digits:
                return digits
        return ""

    def _resolve_currency(self, currency_text):
        if not currency_text:
            return self.env.company.currency_id
        code = currency_text.strip().upper()
        currency = self.env["res.currency"].search([("name", "=", code)], limit=1)
        if currency:
            return currency
        if code in ("DOLAR", "DOLAR AMERICANO"):
            return self.env.ref("base.USD", raise_if_not_found=False) or self.env.company.currency_id
        return self.env.company.currency_id

    def _attach_source_file(
        self, move, source_bytes, bill_data, attachment_name, mimetype, source_label
    ):
        attachment = self.env["ir.attachment"].create(
            {
                "name": attachment_name,
                "type": "binary",
                "datas": base64.b64encode(source_bytes),
                "res_model": "account.move",
                "res_id": move.id,
                "mimetype": mimetype,
            }
        )
        attachment_ids = [attachment.id]

        debug_payload = bill_data.get("debug_payload")
        if debug_payload:
            debug_name = "%s.debug.json" % (attachment_name.rsplit(".", 1)[0] or "import")
            debug_bytes = json.dumps(
                debug_payload, ensure_ascii=False, indent=2, sort_keys=True, default=str
            ).encode("utf-8")
            debug_attachment = self.env["ir.attachment"].create(
                {
                    "name": debug_name,
                    "type": "binary",
                    "datas": base64.b64encode(debug_bytes),
                    "res_model": "account.move",
                    "res_id": move.id,
                    "mimetype": "application/json",
                }
            )
            attachment_ids.append(debug_attachment.id)
        move.message_post(
            body=_(
                "Vendor bill imported from %(source_type)s.<br/>"
                "Supplier RUC: %(ruc)s<br/>"
                "Document number: %(number)s<br/>"
                "Electronic authorization: %(authorization)s",
                source_type=source_label,
                ruc=bill_data.get("supplier_vat") or "-",
                number=bill_data.get("number") or "-",
                authorization=bill_data.get("authorization") or "-",
            ),
            attachment_ids=attachment_ids,
        )

    def _extract_text_from_pdf(self, pdf_bytes):
        try:
            if PdfReader:
                reader = PdfReader(io.BytesIO(pdf_bytes))
                pages = reader.pages
                extract = lambda p: p.extract_text() or ""
            else:
                reader = PdfFileReader(io.BytesIO(pdf_bytes))
                pages = [reader.getPage(i) for i in range(reader.getNumPages())]
                extract = lambda p: p.extractText() or ""
        except Exception as err:
            raise UserError(_("Invalid PDF file.")) from err
        text_parts = []
        for page in pages:
            text_parts.append(extract(page))
        text = "\n".join(text_parts).replace("\xa0", " ")
        if not text.strip():
            raise UserError(
                _(
                    "The PDF has no readable text. If it is a scanned image, "
                    "please import the XML file."
                )
            )
        return text

    def _extract_ride_fields_from_pdf(self, text):
        """Extract key invoice fields from SRI RIDE by label-value anchors."""
        fields_map = {}

        fields_map["supplier_vat"] = self._extract_supplier_ruc_by_label(text)

        number = self._extract_value_after_label(
            text,
            r"\bN(?:O|º|°)\.?",
            value_pattern=r"(\d{3}\s*-\s*\d{3}\s*-\s*\d{6,9})",
            window=160,
        )
        if number:
            fields_map["invoice_number"] = self._normalize_doc_number(re.sub(r"\s+", "", number))

        auth = self._extract_authorization_by_label(text)
        if auth:
            fields_map["authorization"] = auth

        supplier_name = self._extract_supplier_name_by_label(text)
        if supplier_name:
            fields_map["supplier_name"] = supplier_name

        date_token = self._extract_value_after_label(
            text, r"\bFECHA\b", value_pattern=r"(\d{2}\s*[/-]\s*\d{2}\s*[/-]\s*\d{4})"
        )
        if not date_token:
            date_token = self._extract_value_after_label(
                text,
                r"FECHA\s+DE\s+EMISI[ÓO]N",
                value_pattern=r"(\d{2}\s*[/-]\s*\d{2}\s*[/-]\s*\d{4})",
            )
        if date_token:
            try:
                fields_map["invoice_date"] = self._parse_ec_date(
                    re.sub(r"\s+", "", date_token)
                )
            except UserError:
                pass

        subtotal = self._extract_amount_by_label(
            text, [r"SUBTOTAL\s+SIN\s+IMPUESTOS", r"TOTAL\s+SIN\s+IMPUESTOS", r"SUBTOTAL"]
        )
        if subtotal:
            fields_map["subtotal"] = subtotal

        total_amount = self._extract_amount_by_label(
            text, [r"VALOR\s+TOTAL", r"IMPORTE\s+TOTAL"]
        )
        if total_amount:
            fields_map["total_amount"] = total_amount

        line_description = self._extract_line_description_by_table(text)
        if line_description:
            fields_map["line_description"] = line_description

        sri_payment_id = self._extract_sri_payment_from_pdf_text(text)
        if sri_payment_id:
            fields_map["sri_payment_id"] = sri_payment_id

        return fields_map

    def _extract_value_after_label(self, text, label_pattern, value_pattern, window=220):
        for label in re.finditer(label_pattern, text, flags=re.IGNORECASE):
            area = text[label.end() : label.end() + window]
            match = re.search(value_pattern, area, flags=re.IGNORECASE)
            if match:
                return (match.group(1) or "").strip()
        return ""

    def _extract_authorization_by_label(self, text):
        for label in re.finditer(r"N[ÚU]MERO\s+DE\s+AUTORIZACI[ÓO]N", text, flags=re.IGNORECASE):
            area = text[label.end() : label.end() + 260]
            seq = re.search(r"((?:\d[\s\-]*){35,65})", area)
            if not seq:
                continue
            digits = self._digits(seq.group(1))
            if len(digits) >= 35:
                return digits[:49] if len(digits) >= 49 else digits
        return ""

    def _extract_supplier_ruc_by_label(self, text):
        company_vat = self._digits(self.env.company.vat)
        for label in re.finditer(r"R\.?\s*U\.?\s*C\.?\s*:", text, flags=re.IGNORECASE):
            area = text[label.end() : label.end() + 320]

            # 1) Exact 13-digit token immediately after the R.U.C. label.
            for token in re.finditer(r"(?<!\d)(\d{13})(?!\d)", area):
                candidate = token.group(1)
                if candidate != company_vat and self._is_valid_ec_ruc(candidate):
                    return candidate

            # 2) If broken by separators/spaces, normalize nearby groups.
            for token in re.finditer(r"((?:\d\D*){13})", area):
                candidate = self._digits(token.group(1))
                if len(candidate) == 13 and candidate != company_vat and self._is_valid_ec_ruc(candidate):
                    return candidate

            # 3) Compact capture fallback.
            direct = re.search(r"((?:\d[\s\.\-]*){13,35})", area)
            if direct:
                ruc = self._normalize_ec_ruc(direct.group(1), exclude_values=[company_vat])
                if ruc:
                    return ruc

            # 4) Last resort for concatenated noisy chunks.
            area_digits = self._digits(area)
            for idx in range(0, max(0, len(area_digits) - 12)):
                candidate = area_digits[idx : idx + 13]
                if (
                    candidate
                    and candidate != company_vat
                    and self._is_valid_ec_ruc(candidate)
                ):
                    return candidate
        return ""

    def _extract_supplier_name_by_label(self, text):
        # Common SRI RIDE header:
        # NÚMERO DE AUTORIZACIÓN
        # <49 digits>
        # <SUPPLIER NAME>
        auth_header = re.search(
            r"N[ÚU]MERO\s+DE\s+AUTORIZACI[ÓO]N[\s:\-]*([\s\S]{0,280})",
            text,
            flags=re.IGNORECASE,
        )
        if auth_header:
            block = auth_header.group(1)
            lines = [re.sub(r"\s+", " ", (ln or "").strip()) for ln in block.splitlines()]
            lines = [ln for ln in lines if ln]
            # Skip authorization digits line(s), then take the first clean text line as supplier.
            for line in lines:
                digits = self._digits(line)
                if digits and len(digits) >= 35:
                    continue
                upper = line.upper()
                if any(
                    token in upper
                    for token in ("DIRECCION", "MATRIZ", "SUCURSAL", "OBLIGADO", "AMBIENTE", "EMISION")
                ):
                    continue
                if re.search(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", line) and len(line) >= 5:
                    return line.strip(" -:")

        match = re.search(
            r"\n\s*([A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑ\s]{5,120}?)\s+FECHA\s+Y\s+HORA\s+DE\s+AUTORIZACI[ÓO]N",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return ""
        return re.sub(r"\s+", " ", match.group(1)).strip(" -:")

    def _extract_supplier_name_from_pdf_with_vat(self, text, supplier_vat):
        """Extract supplier legal name from compact RIDE text using supplier RUC as anchor."""
        vat = self._digits(supplier_vat)
        if not vat:
            return ""
        pattern = (
            rf"{re.escape(vat)}\s*"
            r"([A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑ\s]{4,180}?)"
            r"(?=Calle:|Direcci[óo]n|Dirección|Raz[oó]n\s+Social\s*/\s*Nombres|OBLIGADO|AMBIENTE|EMISI[ÓO]N|$)"
        )
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            return ""
        return re.sub(r"\s+", " ", match.group(1)).strip(" -:")

    def _extract_amount_by_label(self, text, label_patterns):
        for pattern in label_patterns:
            for label in re.finditer(pattern, text, flags=re.IGNORECASE):
                area = text[label.start() : label.start() + 140]
                amounts = self._extract_decimal_candidates(area)
                if amounts:
                    return max(amounts)
        return 0.0

    def _extract_line_description_by_table(self, text):
        start = re.search(r"\bDESCRIPCI[ÓO]N\b", text, flags=re.IGNORECASE)
        if not start:
            return ""
        window = text[start.end() : start.end() + 1800]
        end = re.search(
            r"\bSUBTOTAL\b|\bINFORMACI[ÓO]N\s+ADICIONAL\b|\bFORMA\s+DE\s+PAGO\b",
            window,
            flags=re.IGNORECASE,
        )
        block = window[: end.start()] if end else window
        lines = [re.sub(r"\s+", " ", ln).strip(" -:") for ln in block.splitlines()]
        result = []
        for line in lines:
            if not line:
                continue
            upper = line.upper()
            if upper in {"COD.", "AUXILIAR", "CANTIDAD", "DESCRIPCIÓN", "DESCRIPCION"}:
                continue
            if any(
                token in upper
                for token in (
                    "PRECIO UNITARIO",
                    "PRECIO TOTAL",
                    "DETALLE ADICIONAL",
                    "SUBSIDIO",
                    "DESCUENTO",
                    "PRINCIPAL",
                )
            ):
                continue
            cleaned = re.sub(r"^\d{1,6}\s+\d+(?:[.,]\d+)?\s+", "", line)
            cleaned = re.sub(
                r"\s+\d+(?:[.,]\d+)?(?:\s+\d+(?:[.,]\d+)?){2,}$", "", cleaned
            ).strip()
            if not cleaned:
                continue
            if re.match(r"^\d+([.,]\d+)?$", cleaned):
                continue
            if not re.search(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", cleaned):
                continue
            result.append(cleaned)
        return "\n".join(result).strip()

    def _extract_supplier_vat_from_pdf(self, text):
        company_vat = self._digits(self.env.company.vat)
        customer_anchor = re.search(
            r"RAZ[ÓO]N\s+SOCIAL\s*/\s*NOMBRES\s+Y\s+APELLIDOS|IDENTIFICACI[ÓO]N\s+\d{10,13}",
            text,
            flags=re.IGNORECASE,
        )
        pre_customer_text = text[: customer_anchor.start()] if customer_anchor else text

        patterns = [
            r"R\.?\s*U\.?\s*C\.?\s*:\s*([0-9\.\-\s]{13,20})",
            r"\bRUC\b\D{0,10}([0-9\.\-\s]{13,20})",
        ]
        for pattern in patterns:
            match = re.search(pattern, pre_customer_text, flags=re.IGNORECASE)
            if match:
                digits = self._normalize_ec_ruc(match.group(1), exclude_values=[company_vat])
                if digits:
                    return digits

        # Fallback: first 13-digit token in issuer section (before customer section).
        for match in re.finditer(r"(?<!\d)\d{13}(?!\d)", pre_customer_text):
            digits = match.group(0)
            if company_vat and digits == company_vat:
                continue
            return digits
        return ""

    def _extract_invoice_number_from_pdf(self, text):
        doc_pattern = re.compile(r"(?<!\d)(\d{3})\D{0,8}(\d{3})\D{0,8}(\d{6,9})(?!\d)")

        # 0) within FACTURA block, anchored by "No."
        factura_anchor = re.search(
            r"\bFACTURA\b[\s\S]{0,200}",
            text,
            flags=re.IGNORECASE,
        )
        factura_window = factura_anchor.group(0) if factura_anchor else text
        no_anchor = re.search(r"\bN(?:O|º|°)\.?", factura_window, flags=re.IGNORECASE)
        if no_anchor:
            window = factura_window[no_anchor.end() : no_anchor.end() + 120]

            exact = re.search(r"(?<!\d)(\d{3}\s*-\s*\d{3}\s*-\s*\d{6,9})(?!\d)", window)
            if exact:
                return self._normalize_doc_number(re.sub(r"\s+", "", exact.group(1)))

            match = doc_pattern.search(window)
            if match:
                raw = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
                return self._normalize_doc_number(raw)

            contiguous = re.search(r"(?<!\d)(\d{15})(?!\d)", window)
            if contiguous:
                digits = contiguous.group(1)
                return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"

        # 1) strict formatted number anywhere
        exact = re.search(r"(?<!\d)(\d{3}\s*-\s*\d{3}\s*-\s*\d{6,9})(?!\d)", text)
        if exact:
            return self._normalize_doc_number(re.sub(r"\s+", "", exact.group(1)))

        # 2) candidate near comprobante type label
        for anchor in re.finditer(
            r"(FACTURA|NOTA\s+DE\s+CR[ÉE]DITO|NOTA\s+DE\s+D[ÉE]BITO)",
            text,
            flags=re.IGNORECASE,
        ):
            window = text[anchor.start() : anchor.start() + 260]
            match = doc_pattern.search(window)
            if match:
                raw = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
                return self._normalize_doc_number(raw)
        # Avoid global loose fallback because it can confuse RUC-like sequences
        # with document numbers in some RIDE layouts.
        return ""

    def _extract_line_description_from_pdf(self, text):
        start = re.search(r"\bDESCRIPCI[ÓO]N\b", text, flags=re.IGNORECASE)
        if not start:
            return ""
        window = text[start.end() : start.end() + 1500]
        end_markers = [
            r"\bFORMA\s+DE\s+PAGO\b",
            r"\bSUBTOTAL\b",
            r"\bINFORMACI[ÓO]N\s+ADICIONAL\b",
            r"\bVALOR\s+TOTAL\b",
        ]
        end_pos = len(window)
        for marker in end_markers:
            m = re.search(marker, window, flags=re.IGNORECASE)
            if m:
                end_pos = min(end_pos, m.start())
        block = window[:end_pos]
        lines = [re.sub(r"\s+", " ", ln).strip(" -:") for ln in block.splitlines()]
        description_lines = []
        for line in lines:
            if not line:
                continue
            line = re.sub(r"^\d{1,6}\s+\d+(?:[.,]\d+)?\s+", "", line)
            line = re.sub(
                r"\s+\d+(?:[.,]\d+)?(?:\s+\d+(?:[.,]\d+)?){2,}$", "", line
            ).strip()
            if re.match(r"^\d+([.,]\d+)?$", line):
                continue
            if re.match(r"^\d+\s*-\s*", line):
                break
            upper_line = line.upper()
            if upper_line in {
                "DETALLE ADICIONAL",
                "COD.",
                "AUXILIAR",
                "CANTIDAD",
                "DESCRIPCION",
                "DESCRIPCIÓN",
            }:
                continue
            noisy_tokens = (
                "DETALLE",
                "ADICIONAL",
                "SUBSIDIO",
                "DESCUENTO",
                "PRINCIPAL",
                "AUXILIAR",
                "PRECIO",
                "UNITARIO",
                "CANTIDAD",
            )
            if sum(1 for token in noisy_tokens if token in upper_line) >= 2:
                continue
            letters = len(re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", line))
            digits = len(re.findall(r"\d", line))
            if letters < 4 or digits > letters:
                continue
            description_lines.append(line)
        return "\n".join(description_lines).strip()

    def _extract_line_description_from_compact_ride(self, text):
        """
        Handle Jasper-like RIDE extraction where table headers and values are merged
        in one token stream (e.g. 'DescripciónCod.Auxiliar...').
        """
        direct = re.search(
            r"PRECIO\s*UNITARIO[\d\.\,\s]{4,}(.*?)[\d\.\,\s]{4,}INFORMACI[ÓO]N\s+ADICIONAL",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if direct:
            raw = direct.group(1) or ""
            raw = re.sub(r"\s+", " ", raw).strip(" -:")
            raw = re.sub(r"^\d+(?:[.,]\d+)*", "", raw).strip()
            raw = re.sub(r"\d+(?:[.,]\d+)*$", "", raw).strip()
            for connector in ("DE", "DEL", "LA", "EL", "EN", "CON", "POR", "PARA"):
                raw = re.sub(rf"\b({connector})([A-ZÁÉÍÓÚÜÑ])", r"\1 \2", raw)
            raw = re.sub(r"([A-ZÁÉÍÓÚÜÑ]{4,})Y([A-ZÁÉÍÓÚÜÑ]{4,})", r"\1 Y \2", raw)
            raw = re.sub(
                r"([A-ZÁÉÍÓÚÜÑ]{6,})A\s+(LA|EL|LOS|LAS)\b",
                r"\1 A \2",
                raw,
            )
            raw = re.sub(r"\s+", " ", raw).strip(" -:")
            if raw and re.search(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", raw):
                return raw

        start = re.search(r"DESCRIPCI[ÓO]N", text, flags=re.IGNORECASE)
        if not start:
            return ""
        tail = text[start.end() :]
        end = re.search(
            r"INFORMACI[ÓO]N\s+ADICIONAL|FORMA\s+DE\s+PAGO|SUBTOTAL",
            tail,
            flags=re.IGNORECASE,
        )
        block = tail[: end.start()] if end else tail[:1800]
        if not block:
            return ""

        cleaned = block
        cleaned = re.sub(
            r"^Cod\.?\s*Auxiliar\s*Descuento\s*Detalle\s*Adicional\s*Precio\s*sin\s*Subsidio\s*Precio\s*Unitario",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\b\d[\d\.,]{2,}\b", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:")
        if not cleaned:
            return ""
        cleaned = re.sub(r"^\d+(?:[.,]\d+)*", "", cleaned).strip()
        cleaned = re.sub(r"\d+(?:[.,]\d+)*$", "", cleaned).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:")
        if len(cleaned) < 10:
            return ""
        if not re.search(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", cleaned):
            return ""
        return cleaned

    def _extract_authorization_from_pdf(self, text):
        patterns = [
            r"CLAVE\s+DE\s+ACCESO\D*((?:\d[\s\-]*){49,65})",
            r"AUTORIZACI[ÓO]N(?:\s+N[ÚU]MERO)?\D*((?:\d[\s\-]*){35,65})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            digits = self._digits(match.group(1))
            if len(digits) >= 35:
                if len(digits) >= 49:
                    return digits[:49]
                return digits
        for seq in self._digit_sequences(text, min_len=49):
            return seq[:49]
        return ""

    def _extract_invoice_date_from_pdf(self, text):
        date_pattern = r"(\d{2}\s*[/-]\s*\d{2}\s*[/-]\s*\d{4})"
        prioritized = [
            # Common SRI RIDE table header: "Identificación Fecha Guía".
            r"IDENTIFICACI[ÓO]N\s*FECHA\s*GU[ÍI]A[\s\S]{0,220}?" + date_pattern,
            r"IDENTIFICACI[ÓO]N\s+FECHA\D{0,220}" + date_pattern,
            r"FECHA\s+DE\s+EMISI[ÓO]N\D{0,220}" + date_pattern,
            r"\bFECHA\b\D{0,220}" + date_pattern,
        ]
        for pattern in prioritized:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                try:
                    return self._parse_ec_date(re.sub(r"\s+", "", match.group(1)))
                except UserError:
                    pass

        for anchor in (
            r"IDENTIFICACI[ÓO]N\s*FECHA\s*GU[ÍI]A",
            r"FECHA\s+DE\s+EMISI[ÓO]N",
            r"\bFECHA\b",
        ):
            anchor_match = re.search(anchor, text, flags=re.IGNORECASE)
            if not anchor_match:
                continue
            window = text[anchor_match.start() : anchor_match.start() + 320]
            parsed = self._extract_date_from_window(window)
            if parsed:
                return parsed

        match = re.search(
            r"\bFECHA\b\D{0,12}(\d{2}[/-]\d{2}[/-]\d{4})",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return self._parse_ec_date(re.sub(r"\s+", "", match.group(1)))
        match = re.search(
            r"FECHA\s+DE\s+EMISI[ÓO]N\D{0,10}(\d{2}[/-]\d{2}[/-]\d{4})",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return self._parse_ec_date(re.sub(r"\s+", "", match.group(1)))
        for match in re.finditer(date_pattern, text):
            try:
                return self._parse_ec_date(re.sub(r"\s+", "", match.group(1)))
            except UserError:
                continue
        return False

    def _extract_authorization_date_from_pdf(self, text):
        match = re.search(
            r"FECHA\s+Y\s+HORA\s+DE\s+AUTORIZACI[ÓO]N\D{0,24}(\d{2}\s*[/-]\s*\d{2}\s*[/-]\s*\d{4})",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return False
        try:
            return self._parse_ec_date(re.sub(r"\s+", "", match.group(1)))
        except UserError:
            return False

    def _extract_date_from_window(self, text):
        exact = re.search(r"(\d{2}\s*[/-]\s*\d{2}\s*[/-]\s*\d{4})", text)
        if exact:
            try:
                return self._parse_ec_date(re.sub(r"\s+", "", exact.group(1)))
            except UserError:
                pass
        grouped = re.search(
            r"((?:\d\D*){2})\D+((?:\d\D*){2})\D+((?:\d\D*){4})",
            text,
            flags=re.IGNORECASE,
        )
        if grouped:
            day = self._digits(grouped.group(1))[:2]
            month = self._digits(grouped.group(2))[:2]
            year = self._digits(grouped.group(3))[:4]
            if len(day) == 2 and len(month) == 2 and len(year) == 4:
                try:
                    return self._parse_ec_date(f"{day}/{month}/{year}")
                except UserError:
                    return False
        return False

    def _extract_sri_payment_from_pdf_text(self, text):
        candidates = []
        for anchor in re.finditer(r"FORMA\s+DE\s+PAGO", text, flags=re.IGNORECASE):
            window = text[anchor.end() : anchor.end() + 280]
            compact = re.sub(r"\s+", " ", window).strip()
            if compact:
                candidates.append(compact[:120])
            for line in window.splitlines()[:6]:
                cleaned = re.sub(r"\s+", " ", (line or "")).strip(" -:")
                if cleaned:
                    candidates.append(cleaned)
        return self._resolve_sri_payment_from_values(candidates)

    def _extract_amount_from_pdf(self, text, labels):
        for label in labels:
            for match in re.finditer(label, text, flags=re.IGNORECASE):
                window = text[match.start() : match.start() + 280]
                amounts = self._extract_decimal_candidates(window)
                if not amounts:
                    continue
                # In RIDE layouts with wrapped lines, the largest value near the label
                # usually corresponds to the amount displayed for that row.
                return max(amounts)
        return 0.0

    def _extract_global_total_amount(self, text):
        amounts = self._extract_decimal_candidates(text)
        if not amounts:
            return 0.0
        # Fallback for heavily fragmented RIDE text extraction.
        return max(amounts)

    def _extract_decimal_candidates(self, text):
        values = []
        for match in re.finditer(r"\d[\d\.,]{0,18}\d", text):
            token = match.group(0)
            # Keep monetary-like values and ignore long integer identifiers.
            if "." not in token and "," not in token:
                continue
            amount = self._parse_decimal(token)
            if amount > 0 and amount < 100000000:
                values.append(amount)
        return values

    def _parse_decimal(self, value):
        raw = (value or "").strip().replace(" ", "")
        if not raw:
            return 0.0
        if "," in raw and "." in raw:
            if raw.rfind(",") > raw.rfind("."):
                raw = raw.replace(".", "").replace(",", ".")
            else:
                raw = raw.replace(",", "")
        elif "," in raw:
            raw = raw.replace(",", ".")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def _compute_tax_rate(self, subtotal, total):
        if not subtotal or total <= subtotal:
            return 0.0
        diff = total - subtotal
        return round((diff / subtotal) * 100.0, 2)

    def _extract_sri_payment_from_xml(self, info_factura):
        if info_factura is None:
            return False
        candidates = []
        pagos = self._child(info_factura, "pagos")
        if pagos is not None:
            for pago in list(pagos):
                if not self._tag(pago).endswith("pago"):
                    continue
                forma_pago = self._text(pago, "formaPago")
                if forma_pago:
                    candidates.append(forma_pago)
        if not candidates:
            forma_directa = self._text(info_factura, "formaPago")
            if forma_directa:
                candidates.append(forma_directa)
        return self._resolve_sri_payment_from_values(candidates)

    def _resolve_sri_payment_from_values(self, values):
        if not values:
            return False
        if "l10n_ec_sri_payment_id" not in self.env["account.move"]._fields:
            return False
        try:
            payment_model = self.env["l10n_ec.sri.payment"]
        except KeyError:
            return False

        for raw_value in values:
            raw = (raw_value or "").strip()
            if not raw:
                continue
            code_match = re.search(r"(?<!\d)(\d{2})(?!\d)", raw)
            if code_match:
                payment = payment_model.search([("code", "=", code_match.group(1))], limit=1)
                if payment:
                    return payment.id

        for raw_value in values:
            raw = re.sub(r"\s+", " ", (raw_value or "")).strip(" -:")
            if not raw:
                continue
            name_candidate = re.sub(r"^\d{2}\s*[-:]\s*", "", raw).strip()
            if not name_candidate:
                continue
            payment = payment_model.search([("name", "ilike", name_candidate)], limit=1)
            if payment:
                return payment.id
        return False

    def _digit_sequences(self, text, min_len=1):
        for match in re.finditer(r"\d{%d,}" % min_len, text):
            yield match.group(0)

    def _extract_supplier_name_from_pdf(self, text):
        # Common RIDE pattern: issuer name appears at the left of
        # "FECHA Y HORA DE AUTORIZACIÓN" in the header block.
        match = re.search(
            r"\n\s*([A-ZÁÉÍÓÚÜÑ][A-ZÁÉÍÓÚÜÑ\s]{5,120}?)\s+FECHA\s+Y\s+HORA\s+DE",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip(" -:")

        match = re.search(
            r"RAZ[ÓO]N\s+SOCIAL\D{0,10}([^\n\r]{3,120})",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1).strip(" :-")
        # Fallback: first uppercase text line before customer section.
        customer_anchor = re.search(
            r"RAZ[ÓO]N\s+SOCIAL\s*/\s*NOMBRES\s+Y\s+APELLIDOS",
            text,
            flags=re.IGNORECASE,
        )
        scan_text = text[: customer_anchor.start()] if customer_anchor else text[:1200]
        for raw_line in scan_text.splitlines():
            line = re.sub(r"\s+", " ", (raw_line or "").strip())
            if not line:
                continue
            if re.search(r"\d", line):
                continue
            upper_line = line.upper()
            if any(
                token in upper_line
                for token in (
                    "FACTURA",
                    "RUC",
                    "AUTORIZACION",
                    "AMBIENTE",
                    "EMISION",
                    "DIRECCION",
                    "CLAVE DE ACCESO",
                    "OBLIGADO",
                    "CONTRIBUYENTE",
                )
            ):
                continue
            words = [w for w in line.split() if len(w) > 1]
            if len(words) >= 3:
                return line
        return ""

    def _child(self, node, local_name):
        if node is None:
            return None
        for child in list(node):
            if self._tag(child).endswith(local_name):
                return child
        return None

    def _text(self, node, local_name):
        child = self._child(node, local_name)
        return (child.text or "").strip() if child is not None and child.text else ""

    def _tag(self, node):
        return node.tag if hasattr(node, "tag") else ""

    def _parse_ec_date(self, value):
        if not value:
            return False
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        raise UserError(_("Unsupported date format in XML: %s") % value)

    def _digits(self, value):
        return re.sub(r"\D", "", value or "")

    def _normalize_ec_ruc(self, value, exclude_values=None):
        digits = self._digits(value)
        excluded = {self._digits(v) for v in (exclude_values or []) if v}
        valid = lambda d: len(d) == 13 and d not in excluded and self._is_valid_ec_ruc(d)

        if valid(digits):
            return digits

        text = value or ""
        for match in re.finditer(r"(?<!\d)(\d{13})(?!\d)", text):
            candidate = match.group(1)
            if valid(candidate):
                return candidate

        for match in re.finditer(r"((?:\d\D*){13})", text):
            candidate = self._digits(match.group(1))
            if valid(candidate):
                return candidate

        # Last resort for noisy chunks: look for any valid 13-digit slice.
        for idx in range(0, max(0, len(digits) - 12)):
            candidate = digits[idx : idx + 13]
            if valid(candidate):
                return candidate

        return ""

    def _is_valid_ec_ruc(self, value):
        ruc = self._digits(value)
        if len(ruc) != 13:
            return False
        if ruc[:2] == "00":
            return False
        province = int(ruc[:2])
        if province < 1 or (province > 24 and province != 30):
            return False
        third = int(ruc[2])

        def mod10_check(base9):
            coeffs = [2, 1, 2, 1, 2, 1, 2, 1, 2]
            total = 0
            for num, coef in zip(base9, coeffs):
                val = int(num) * coef
                if val >= 10:
                    val -= 9
                total += val
            check = (10 - (total % 10)) % 10
            return check

        def mod11_check(base, coeffs):
            total = sum(int(num) * coef for num, coef in zip(base, coeffs))
            remainder = total % 11
            check = 11 - remainder
            if check == 11:
                check = 0
            elif check == 10:
                check = 1
            return check

        if third in (0, 1, 2, 3, 4, 5):
            if mod10_check(ruc[:9]) != int(ruc[9]):
                return False
        elif third == 6:
            if mod11_check(ruc[:8], [3, 2, 7, 6, 5, 4, 3, 2]) != int(ruc[8]):
                return False
        elif third == 9:
            if mod11_check(ruc[:9], [4, 3, 2, 7, 6, 5, 4, 3, 2]) != int(ruc[9]):
                return False
        else:
            return False

        return ruc[-3:] != "000"

    def _normalize_doc_number(self, number):
        if not number:
            return ""
        number = number.strip()
        parts = number.split("-")
        if len(parts) != 3:
            return number
        estab = parts[0].zfill(3)
        pto_emi = parts[1].zfill(3)
        secuencial = parts[2].zfill(9)
        return f"{estab}-{pto_emi}-{secuencial}"

    def _float(self, value, default=0.0):
        if value in (None, ""):
            return default
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return default
