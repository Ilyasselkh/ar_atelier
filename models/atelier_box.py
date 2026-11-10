# -*- coding: utf-8 -*-

from odoo import api, fields, models, _


# Codes machine -> labels affichables
ANOMALY_LABELS = {
    "warehouse_out_no_tri": "Aucun passage actif en TRI retrouvé pour cette BOX.",
    "warehouse_out_in_stock": "Cette BOX appartient au STOCK ou est déjà sortie du TRI vers STOCK.",
    "warehouse_no_entry": "Cette BOX n’a pas été scannée en entrée pendant la période de l’OF.",
    "warehouse_still_in_tri": "Cette BOX ne doit pas entrer au magasin : elle est encore en TRI (aucune sortie vers STOCK validée).",
    "warehouse_entry_anomaly": "Cette BOX est encore en anomalie au scan d’entrée.",
    "duplicate_other_session": "BOX déjà scannée pendant la période de validité de l’OF",
    "qty_mismatch": "Boxe hors UC (qty ≠ référence)",
    "unknown_ref": "Boxe sans UC (réf inconnue)",
    "inactive_of": "OF non active",
    "unknown_format": "Format étiquette non reconnu",
    "not_in_tri": "Étiquette non passée par le tri",
}


class AtelierBox(models.Model):
    _name = "ar.atelier.box"
    _inherit = ["ar.atelier.audit.mixin"]
    _audit_fields = ('session_id', 'pallet_id', 'section', 'position', 'active', 'datamatrix_raw', 'label_format', 'mat', 'pwg', 'plt', 'po', 'pomat', 'sloc', 'qty', 'wghtg', 'sut', 'hu', 'bsd', 'bat', 'pal', 'btyp', 'bver', 'gtin', 'lot', 'serial', 'variable_qty', 'total_qty', 'production_date', 'expiration_date', 'scan_datetime', 'user_id', 'anomaly_reason_codes')
    _description = "Boxe scannée atelier"
    _order = "scan_datetime desc"
    _rec_name = "datamatrix_raw"

    session_id = fields.Many2one(
        "ar.atelier.session", string="Session",
        required=True, ondelete="cascade", index=True,
    )
    pallet_id = fields.Many2one(
        "ar.atelier.pallet", string="Palette",
        required=True, ondelete="cascade", index=True,
    )
    section = fields.Integer(
        string="Section", required=True, index=True,
        help="1=Stock, 2=Tri, 3=Anomalie",
    )
    position = fields.Integer(string="Position", required=True)
    active = fields.Boolean(
        string="Actif", default=True, index=True,
        help="Décocher = boxe désactivée. Conservée en base pour traçabilité, "
             "mais cachée de l'affichage palette et exclue de la détection doublon.",
    )

    layer = fields.Integer(string="Couche", compute="_compute_3d", store=True)
    grid_row = fields.Integer(string="Rangée", compute="_compute_3d", store=True)
    grid_col = fields.Integer(string="Colonne", compute="_compute_3d", store=True)

    datamatrix_raw = fields.Char(string="Étiquette scannée", required=True, index=True)
    duplicate_box_id = fields.Many2one('ar.atelier.box', string='BOX déjà scannée', ondelete='set null', copy=False)
    duplicate_message = fields.Char(compute='_compute_duplicate_message')

    @api.depends('duplicate_box_id.session_id.name', 'session_id.mode')
    def _compute_duplicate_message(self):
        for box in self:
            box.duplicate_message = (
                (_('Cette BOX est déjà sortie du tri dans la session %s.') if box.session_id.mode == 'post_tri'
                 else _('Cette BOX a déjà été scannée dans la session %s.')) % box.duplicate_box_id.session_id.name
                if box.duplicate_box_id else False)

    label_format = fields.Selection(
        [("custom", "Custom ARaymond"), ("barcode", "Code-barres ARaymond"), ("gs1", "GS1 standard"), ("raw", "Brut")],
        string="Format", default="raw",
    )

    # Champs format custom
    mat = fields.Char(string="Référence (MAT)", index=True)
    pwg = fields.Float(string="Poids unitaire (PWG)")
    plt = fields.Char(string="PLT")
    po = fields.Char(string="N° OF (PO)", index=True)
    pomat = fields.Char(string="POMAT")
    sloc = fields.Char(string="Emplacement (SLOC)")
    qty = fields.Integer(string="Quantité (QTY)")
    wghtg = fields.Float(string="Poids total (WGHTG)")
    sut = fields.Char(string="Unité stockage (SUT)")
    hu = fields.Char(string="Handling Unit (HU)", index=True)
    bsd = fields.Char(string="Date production (BSD)")
    bat = fields.Char(string="Lot (BAT)", index=True)
    pal = fields.Char(string="PAL")
    btyp = fields.Char(string="BTYP")
    bver = fields.Char(string="BVER")

    # Champs format GS1
    gtin = fields.Char(string="GTIN", index=True)
    lot = fields.Char(string="Lot GS1", index=True)
    serial = fields.Char(string="N° série", index=True)
    variable_qty = fields.Integer(string="Qté variable")
    total_qty = fields.Integer(string="Qté totale")
    production_date = fields.Date(string="Date production GS1")
    expiration_date = fields.Date(string="Date expiration")

    # Métadonnées
    scan_datetime = fields.Datetime(
        string="Scanné le", default=fields.Datetime.now, required=True,
    )
    user_id = fields.Many2one(
        "res.users", string="Scanné par", default=lambda self: self.env.user,
    )

    # Anomalies
    anomaly_reason_codes = fields.Char(
        string="Codes anomalie",
        help="Codes machine séparés par virgule (ex: 'qty_mismatch,inactive_of')",
    )
    anomaly_labels = fields.Char(
        string="Anomalies", compute="_compute_anomaly_labels", store=True,
    )

    correction_ids = fields.One2many(
        "ar.atelier.box.correction", "box_id", string="Historique corrections",
    )
    correction_count = fields.Integer(compute="_compute_correction_count", store=True)
    is_anomaly = fields.Boolean(compute="_compute_is_anomaly", store=True)

    @api.depends("section")
    def _compute_is_anomaly(self):
        for b in self:
            b.is_anomaly = b.section == 3

    def write(self, vals):
        sessions = self.session_id
        result = super().write(vals)
        if {'active', 'section', 'session_id'} & vals.keys():
            (sessions | self.session_id)._release_reset_history()
        return result

    def unlink(self):
        sessions = self.session_id
        result = super().unlink()
        sessions.exists()._release_reset_history()
        return result

    @api.depends("correction_ids")
    def _compute_correction_count(self):
        for b in self:
            b.correction_count = len(b.correction_ids)

    @api.depends("anomaly_reason_codes")
    def _compute_anomaly_labels(self):
        for b in self:
            if not b.anomaly_reason_codes:
                b.anomaly_labels = ""
                continue
            codes = [c.strip() for c in b.anomaly_reason_codes.split(",") if c.strip()]
            b.anomaly_labels = " | ".join(
                ANOMALY_LABELS.get(c, c) for c in codes
            )

    @api.depends("position", "pallet_id.cols", "pallet_id.rows")
    def _compute_3d(self):
        for b in self:
            cols = max(b.pallet_id.cols or 4, 1)
            rows = max(b.pallet_id.rows or 2, 1)
            per_layer = cols * rows
            pos = b.position or 0
            b.layer = pos // per_layer
            in_layer = pos % per_layer
            b.grid_row = in_layer // cols
            b.grid_col = in_layer % cols
