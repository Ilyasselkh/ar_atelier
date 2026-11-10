# -*- coding: utf-8 -*-
import base64
import io
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


# ============================================================================
# Référentiel UC
# ============================================================================
class AtelierUcRef(models.Model):
    _name = "ar.atelier.uc.ref"
    _inherit = ["ar.atelier.audit.mixin"]
    _audit_fields = ('name', 'description', 'qty_per_carton', 'active')
    _description = "Référence UC : MAT + nb pièces par carton"
    _order = "name"
    _rec_name = "name"

    name = fields.Char(string="Référence (MAT)", required=True, index=True)
    description = fields.Char(string="Description")
    qty_per_carton = fields.Integer(
        string="Quantité par carton",
        required=True, default=0,
        help="Quantité attendue dans un carton de cette référence.",
    )
    active = fields.Boolean(string="Actif", default=True)

    _sql_constraints = [
        ("name_uniq", "unique(name)", "Cette référence existe déjà dans le référentiel UC."),
    ]


# ============================================================================
# Helpers Excel
# ============================================================================
def _read_xlsx_rows(file_b64):
    """Lit un fichier xlsx encodé en base64 et retourne une liste de dicts {col: value}."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise UserError(_("Module Python 'openpyxl' non installé. Contactez l'administrateur."))
    try:
        data = base64.b64decode(file_b64)
        wb = load_workbook(io.BytesIO(data), data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    except Exception as e:
        raise UserError(_("Fichier Excel illisible : %s") % e)
    if not rows:
        raise UserError(_("Fichier Excel vide."))
    headers = [str(h).strip().lower() if h is not None else "" for h in rows[0]]
    result = []
    for row in rows[1:]:
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue
        result.append({headers[i]: row[i] for i in range(len(headers)) if i < len(row)})
    return result


def _build_xlsx_template(headers, sample_rows, sheet_name="Template"):
    """Construit un xlsx en mémoire avec les en-têtes et exemples. Retourne base64."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        raise UserError(_("Module Python 'openpyxl' non installé."))
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="714B67", end_color="714B67", fill_type="solid")
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = header_fill
    for row_idx, row in enumerate(sample_rows, start=2):
        for col_idx, val in enumerate(row, start=1):
            ws.cell(row=row_idx, column=col_idx, value=val)
    for col_idx in range(1, len(headers) + 1):
        ws.column_dimensions[chr(64 + col_idx)].width = 22
    buf = io.BytesIO()
    wb.save(buf)
    return base64.b64encode(buf.getvalue())


# ============================================================================
# Listes hebdomadaires de TRI
# ============================================================================
class AtelierTriList(models.Model):
    _name = "ar.atelier.tri.list"
    _inherit = ["ar.atelier.audit.mixin"]
    _audit_fields = ('name', 'valid_from', 'valid_to', 'active', 'import_file', 'import_filename')
    _description = "Liste hebdomadaire des références à trier"
    _order = "valid_from desc"
    _rec_name = "name"

    name = fields.Char(string="Nom de la liste", required=True,
                       help="Auto-généré: TRI_SEMXX_YYYY selon valid_from")
    valid_from = fields.Date(string="Valide du", required=True, default=fields.Date.context_today)
    valid_to = fields.Date(string="Valide jusqu'au", required=True)
    active = fields.Boolean(string="Actif", default=True)
    ref_ids = fields.One2many("ar.atelier.tri.ref", "list_id", string="Références à trier")
    ref_count = fields.Integer(compute="_compute_ref_count", store=True)

    # Import Excel
    import_file = fields.Binary(string="Fichier Excel à importer", attachment=False)
    import_filename = fields.Char(string="Nom du fichier")

    @api.depends("ref_ids")
    def _compute_ref_count(self):
        for r in self:
            r.ref_count = len(r.ref_ids)

    @api.onchange("valid_from")
    def _onchange_valid_from_name(self):
        if self.valid_from:
            iso_year, iso_week, _ = self.valid_from.isocalendar()
            auto_name = "TRI_SEM%02d_%d" % (iso_week, iso_year)
            # Toujours regénérer pour reflet de la semaine
            if not self.name or self.name.startswith("TRI_SEM"):
                self.name = auto_name

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") and vals.get("valid_from"):
                from datetime import date
                d = vals["valid_from"]
                if isinstance(d, str):
                    d = fields.Date.from_string(d)
                iso_year, iso_week, _ = d.isocalendar()
                vals["name"] = "TRI_SEM%02d_%d" % (iso_week, iso_year)
        return super().create(vals_list)

    @api.constrains("valid_from", "valid_to")
    def _check_dates(self):
        for r in self:
            if r.valid_to < r.valid_from:
                raise ValidationError(_("La date de fin doit être >= date de début."))

    @api.model
    def get_active_list(self, target_date=None):
        d = target_date or fields.Date.context_today(self)
        return self.search([
            ("active", "=", True),
            ("valid_from", "<=", d),
            ("valid_to", ">=", d),
        ], order="valid_from desc", limit=1)

    def action_import_excel(self):
        """Importe les références depuis le fichier Excel uploadé.
        Colonnes attendues : 'reference' (ou 'mat'), 'description', 'tri_percent' (ou 'pourcentage')
        """
        self.ensure_one()
        self.check_access("write")
        if not self.import_file:
            raise UserError(_("Aucun fichier sélectionné. Uploade un fichier Excel."))
        rows = _read_xlsx_rows(self.import_file)
        if not rows:
            raise UserError(_("Fichier Excel vide ou en-têtes manquantes."))

        # Reset des refs existantes (remplacement complet)
        if self.ref_ids:
            self.ref_ids.unlink()

        created = 0
        Ref = self.env["ar.atelier.tri.ref"]
        for row in rows:
            ref_val = (row.get("reference") or row.get("mat") or row.get("référence") or "")
            if isinstance(ref_val, (int, float)):
                ref_val = str(int(ref_val)) if float(ref_val).is_integer() else str(ref_val)
            ref_val = str(ref_val).strip()
            if not ref_val:
                continue
            desc = row.get("description") or ""
            pct = row.get("tri_percent") or row.get("pourcentage") or row.get("percent") or 0.0
            try: pct = float(pct)
            except (TypeError, ValueError): pct = 0.0
            Ref.create({
                "list_id": self.id,
                "name": ref_val,
                "description": str(desc).strip() if desc else False,
                "tri_percent": pct,
            })
            created += 1
        # Reset du fichier
        self.import_file = False
        self.import_filename = False
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Import terminé"),
                "message": _("%s références importées dans %s") % (created, self.name),
                "type": "success",
                "sticky": False,
            },
        }

    def action_download_template(self):
        """Télécharge un template Excel pour les listes TRI."""
        self.ensure_one()
        headers = ["reference", "description", "tri_percent"]
        sample = [
            ["505075566", "Exemple référence A", 25.5],
            ["505075567", "Exemple référence B", 50.0],
            ["505075568", "", 75.0],
        ]
        data = _build_xlsx_template(headers, sample, sheet_name="Liste TRI")
        attachment = self.env["ir.attachment"].create({
            "name": "template_liste_tri.xlsx",
            "type": "binary",
            "datas": data,
            "res_model": self._name,
            "res_id": self.id,
            "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % attachment.id,
            "target": "self",
        }


class AtelierTriRef(models.Model):
    _name = "ar.atelier.tri.ref"
    _inherit = ["ar.atelier.audit.mixin"]
    _audit_fields = ('list_id', 'name', 'description', 'tri_percent')
    _description = "Référence dans une liste de TRI"
    _order = "list_id desc, name"
    _rec_name = "name"

    list_id = fields.Many2one(
        "ar.atelier.tri.list", string="Liste TRI",
        required=True, ondelete="cascade", index=True,
    )
    name = fields.Char(string="Référence (MAT)", required=True, index=True)
    description = fields.Char(string="Description")
    tri_percent = fields.Float(
        string="% Tri",
        help="Pourcentage de tri (informatif uniquement).",
    )

    _sql_constraints = [
        ("name_list_uniq", "unique(list_id, name)",
         "Cette référence existe déjà dans cette liste."),
    ]


# ============================================================================
# Listes hebdomadaires d'OF
# ============================================================================
class AtelierOfList(models.Model):
    _name = "ar.atelier.of.list"
    _inherit = ["ar.atelier.audit.mixin"]
    _audit_fields = ('name', 'valid_from', 'valid_to', 'active', 'import_file', 'import_filename')
    _description = "Liste hebdomadaire des Ordres de Fabrication actifs"
    _order = "valid_from desc"
    _rec_name = "name"

    name = fields.Char(string="Nom de la liste", required=True,
                       help="Auto-généré: OF_SEMXX_YYYY selon valid_from")
    valid_from = fields.Date(string="Valide du", required=True, default=fields.Date.context_today)
    valid_to = fields.Date(string="Valide jusqu'au", required=True)
    active = fields.Boolean(string="Actif", default=True)
    of_ids = fields.One2many("ar.atelier.of.ref", "list_id", string="Ordres de fabrication actifs")
    of_count = fields.Integer(compute="_compute_of_count", store=True)

    import_file = fields.Binary(string="Fichier Excel à importer", attachment=False)
    import_filename = fields.Char(string="Nom du fichier")

    @api.depends("of_ids")
    def _compute_of_count(self):
        for r in self:
            r.of_count = len(r.of_ids)

    @api.onchange("valid_from")
    def _onchange_valid_from_name(self):
        if self.valid_from:
            iso_year, iso_week, _ = self.valid_from.isocalendar()
            auto_name = "OF_SEM%02d_%d" % (iso_week, iso_year)
            if not self.name or self.name.startswith("OF_SEM"):
                self.name = auto_name

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") and vals.get("valid_from"):
                d = vals["valid_from"]
                if isinstance(d, str):
                    d = fields.Date.from_string(d)
                iso_year, iso_week, _ = d.isocalendar()
                vals["name"] = "OF_SEM%02d_%d" % (iso_week, iso_year)
        return super().create(vals_list)

    @api.constrains("valid_from", "valid_to")
    def _check_dates(self):
        for r in self:
            if r.valid_to < r.valid_from:
                raise ValidationError(_("La date de fin doit être >= date de début."))

    @api.model
    def get_active_list(self, target_date=None):
        d = target_date or fields.Date.context_today(self)
        return self.search([
            ("active", "=", True),
            ("valid_from", "<=", d),
            ("valid_to", ">=", d),
        ], order="valid_from desc", limit=1)

    def action_import_excel(self):
        """Importe les OF depuis le fichier Excel.
        Colonnes attendues : 'of' (ou 'po'), 'description'
        """
        self.ensure_one()
        self.check_access("write")
        if not self.import_file:
            raise UserError(_("Aucun fichier sélectionné."))
        rows = _read_xlsx_rows(self.import_file)
        if not rows:
            raise UserError(_("Fichier Excel vide ou en-têtes manquantes."))
        if self.of_ids:
            self.of_ids.unlink()
        created = 0
        Ref = self.env["ar.atelier.of.ref"]
        for row in rows:
            of_val = (row.get("of") or row.get("po") or row.get("ordre") or "")
            if isinstance(of_val, (int, float)):
                of_val = str(int(of_val)) if float(of_val).is_integer() else str(of_val)
            of_val = str(of_val).strip()
            if not of_val:
                continue
            desc = row.get("description") or ""
            Ref.create({
                "list_id": self.id,
                "name": of_val,
                "description": str(desc).strip() if desc else False,
            })
            created += 1
        self.import_file = False
        self.import_filename = False
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Import terminé"),
                "message": _("%s OF importés dans %s") % (created, self.name),
                "type": "success",
                "sticky": False,
            },
        }

    def action_download_template(self):
        self.ensure_one()
        headers = ["of", "description"]
        sample = [
            ["14785885", "Exemple OF A"],
            ["14785886", "Exemple OF B"],
            ["14785887", ""],
        ]
        data = _build_xlsx_template(headers, sample, sheet_name="Liste OF")
        attachment = self.env["ir.attachment"].create({
            "name": "template_liste_of.xlsx",
            "type": "binary",
            "datas": data,
            "res_model": self._name,
            "res_id": self.id,
            "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % attachment.id,
            "target": "self",
        }


class AtelierOfRef(models.Model):
    _name = "ar.atelier.of.ref"
    _inherit = ["ar.atelier.audit.mixin"]
    _audit_fields = ('list_id', 'name', 'description')
    _description = "Ordre de Fabrication actif dans une liste"
    _order = "list_id desc, name"
    _rec_name = "name"

    list_id = fields.Many2one(
        "ar.atelier.of.list", string="Liste OF",
        required=True, ondelete="cascade", index=True,
    )
    name = fields.Char(string="N° OF (PO)", required=True, index=True)
    description = fields.Char(string="Description")

    _sql_constraints = [
        ("name_list_uniq", "unique(list_id, name)",
         "Cet OF existe déjà dans cette liste."),
    ]
