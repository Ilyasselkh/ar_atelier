# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class AtelierPallet(models.Model):
    _name = "ar.atelier.pallet"
    _inherit = ["ar.atelier.audit.mixin"]
    _audit_fields = ('session_id', 'number', 'capacity', 'rows', 'cols', 'layers')
    _description = "Palette atelier"
    _order = "session_id desc, number"
    _rec_name = "display_name"

    session_id = fields.Many2one(
        "ar.atelier.session",
        string="Session",
        required=True,
        ondelete="cascade",
        index=True,
    )
    number = fields.Integer(
        string="Numéro palette",
        required=True,
        index=True,
        help="1, 2 ou 3",
    )

    # Configuration de l'empilement (40 boxes max = 5 couches x 8 boxes en 4x2)
    capacity = fields.Integer(string="Capacité max", default=40)
    rows = fields.Integer(string="Rangées", default=2)
    cols = fields.Integer(string="Colonnes", default=4)
    layers = fields.Integer(string="Couches", default=5)

    box_ids = fields.One2many(
        "ar.atelier.box",
        "pallet_id",
        string="Boxes",
    )
    box_count = fields.Integer(
        string="Nb boxes",
        compute="_compute_box_count",
        store=True,
    )
    is_full = fields.Boolean(
        string="Pleine",
        compute="_compute_box_count",
        store=True,
    )

    display_name = fields.Char(
        string="Libellé",
        compute="_compute_display_name",
        store=True,
    )

    _sql_constraints = [
        (
            "unique_session_number",
            "unique(session_id, number)",
            "Une seule palette par numéro et par session.",
        ),
        (
            "number_in_range",
            "CHECK(number IN (1, 2, 3))",
            "Le numéro de palette doit être 1, 2 ou 3.",
        ),
    ]

    @api.depends("box_ids", "box_ids.active", "capacity")
    def _compute_box_count(self):
        for p in self:
            # On ne compte que les boxes actives (les désactivées sont conservées en BDD pour traçabilité)
            count = len(p.box_ids.filtered(lambda b: b.active))
            p.box_count = count
            p.is_full = count >= p.capacity if p.capacity else False

    @api.depends("session_id.name", "number")
    def _compute_display_name(self):
        for p in self:
            p.display_name = "%s — Palette %s" % (
                p.session_id.name or "?",
                p.number or "?",
            )
