# -*- coding: utf-8 -*-

from odoo import api, fields, models, _


class AtelierBoxCorrection(models.Model):
    _name = "ar.atelier.box.correction"
    _inherit = ["ar.atelier.audit.mixin"]
    _audit_fields = ('box_id', 'old_datamatrix', 'new_datamatrix', 'actions_taken', 'user_id', 'correction_date')
    _description = "Correction d'une boxe en anomalie"
    _order = "create_date desc"

    history_snapshot = fields.Json(
        string="Bilan de la correction", copy=False,
        help="Anomalies avant/après et valeurs modifiées au moment de la correction.",
    )

    box_id = fields.Many2one(
        "ar.atelier.box", string="Boxe corrigée",
        required=True, ondelete="cascade", index=True,
    )
    old_datamatrix = fields.Char(
        string="Ancienne étiquette", required=True,
        help="Étiquette avant correction",
    )
    new_datamatrix = fields.Char(
        string="Nouvelle étiquette", required=True,
        help="Étiquette de remplacement, devient l'étiquette principale",
    )
    actions_taken = fields.Text(
        string="Actions effectuées", required=True,
        help="Description des actions effectuées pour corriger l'anomalie",
    )
    user_id = fields.Many2one(
        "res.users", string="Corrigé par",
        default=lambda self: self.env.user, required=True,
    )
    correction_date = fields.Datetime(
        string="Date correction", default=fields.Datetime.now, required=True,
    )
