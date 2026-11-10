# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from .atelier_audit import scan_operation

from .gs1_parser import parse_label
from .atelier_box import ANOMALY_LABELS
from datetime import datetime, time, timedelta
import pytz
from markupsafe import Markup


class AtelierSession(models.Model):
    _name = "ar.atelier.session"
    _description = "Session de scan atelier"
    _inherit = ["ar.atelier.audit.mixin"]
    _audit_fields = ('name', 'user_id', 'operator_code', 'operator_name', 'date_start', 'date_end', 'state', 'mode', 'last_scan', 'reset_pending')
    _order = "create_date desc"
    _rec_name = "name"

    name = fields.Char(string="Référence", required=True, copy=False, readonly=True,
                       index=True, default=lambda self: _("Nouveau"))
    user_id = fields.Many2one("res.users", string="Utilisateur Odoo",
                              default=lambda self: self.env.user, required=True, tracking=True)
    operator_code = fields.Char(string="Matricule opérateur", index=True, tracking=True)
    operator_name = fields.Char(string="Nom opérateur", tracking=True)
    warehouse_out_reason = fields.Text(string='Motif', tracking=True)
    reset_pending = fields.Boolean(string='Réinitialisation bloquée par des anomalies', readonly=True, copy=False, index=True)

    @api.constrains('state', 'mode', 'warehouse_out_reason')
    def _check_warehouse_out_reason(self):
        for session in self:
            if (session.mode == 'warehouse_out' and session.state == 'done'
                    and not (session.warehouse_out_reason or '').strip()):
                raise ValidationError(_('Le motif est obligatoire pour une session Sortie Magasin LOG clôturée.'))

    @scan_operation
    def save_warehouse_out_reason(self, reason):
        self.ensure_one()
        if self.mode != 'warehouse_out' or self.state != 'in_progress':
            raise UserError(_("Le motif peut être saisi dans une session Sortie Magasin LOG en cours."))
        self.write({'warehouse_out_reason': (reason or '').strip()})
        self._touch_activity()
        return True
    date_start = fields.Datetime(string="Début", default=fields.Datetime.now,
                                 required=True, tracking=True)
    date_end = fields.Datetime(string="Clôture", tracking=True)

    state = fields.Selection(
        [("draft", "Brouillon"), ("in_progress", "En cours"),
         ("done", "Clôturée"), ("cancelled", "Annulée"), ("reset", "Réinitialisée")],
        default="draft", required=True, tracking=True,
    )
    mode = fields.Selection(
        [("initial", "Scan initial (Stock/Tri/Anomalie)"),
         ("post_tri", "Sortie de tri (Stock/Anomalie)"),
         ("warehouse", "Scan Magasin (Stock/Anomalie)"),
         ("warehouse_out", "Sortie Magasin (Tri/Anomalie)")],
        default="initial", required=True, tracking=True, index=True,
        help="initial = scan d'entrée, route vers Stock/Tri/Anomalie. "
             "post_tri = scan après tri, route vers Stock/Anomalie uniquement.",
    )

    pallet_ids = fields.One2many("ar.atelier.pallet", "session_id", string="Palettes")
    box_ids = fields.One2many("ar.atelier.box", "session_id", string="Boxes scannées")

    total_boxes = fields.Integer(compute="_compute_counters", store=True)
    boxes_p1 = fields.Integer(compute="_compute_counters", store=True)
    boxes_p2 = fields.Integer(compute="_compute_counters", store=True)
    boxes_p3 = fields.Integer(compute="_compute_counters", store=True)
    last_scan = fields.Char(string="Dernier scan", copy=False)
    last_activity_at = fields.Datetime(string='Dernière activité', default=fields.Datetime.now, copy=False, index=True)

    def _touch_activity(self):
        for session in self.filtered(lambda s: s.state == 'in_progress'):
            session.sudo().write({'last_activity_at': fields.Datetime.now()})

    @scan_operation
    def record_activity(self):
        self.ensure_one()
        self._touch_activity()
        return True

    @api.model
    def _get_idle_timeout_minutes(self):
        value = self.env['ir.config_parameter'].sudo().get_param('ar_atelier.idle_timeout_minutes', '5')
        try:
            minutes = int(value)
        except (ValueError, TypeError):
            return 5
        return minutes if 0 < minutes <= 2147483647 else 5

    def _close_if_idle(self):
        minutes = self._get_idle_timeout_minutes()
        cutoff = fields.Datetime.now() - timedelta(minutes=minutes)
        for session in self:
            # Lock against scans and activity updates before checking the deadline.
            self.env.cr.execute('SELECT id FROM ar_atelier_session WHERE id = %s FOR UPDATE', [session.id])
            session.invalidate_recordset(['state', 'last_activity_at', 'operator_code'])
            if (session.state == 'in_progress' and session.last_activity_at
                    and (session.operator_code or '').strip()
                    and session.last_activity_at <= cutoff and not session._closure_blocker()):
                session.action_done()
                session.message_post(body=_('Clôture automatique après %s minutes sans activité, sans BOX active en anomalie.') % minutes,
                                     subtype_xmlid='mail.mt_note')

    @api.model
    def _cron_close_idle_sessions(self):
        self.search([('state', '=', 'in_progress'),
                     ('operator_code', '!=', False),
                     ('last_activity_at', '<=', fields.Datetime.now() - timedelta(minutes=self._get_idle_timeout_minutes()))])._close_if_idle()

    @scan_operation
    def check_inactivity(self):
        self.ensure_one()
        self._close_if_idle()
        return {'state': self.state, 'idle_timeout_minutes': self._get_idle_timeout_minutes()}

    @api.depends("box_ids", "box_ids.section", "box_ids.active")
    def _compute_counters(self):
        for s in self:
            actives = s.box_ids.filtered(lambda b: b.active)
            s.total_boxes = len(actives)
            s.boxes_p1 = len(actives.filtered(lambda b: b.section == 1))
            s.boxes_p2 = len(actives.filtered(lambda b: b.section == 2))
            s.boxes_p3 = len(actives.filtered(lambda b: b.section == 3))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", _("Nouveau")) == _("Nouveau"):
                vals["name"] = self.env["ir.sequence"].next_by_code("ar.atelier.session") or _("Nouveau")
        sessions = super().create(vals_list)
        for s in sessions:
            s._ensure_pallets()
        return sessions

    def _ensure_pallets(self):
        self.ensure_one()
        Pallet = self.env["ar.atelier.pallet"]
        existing = {p.number: p for p in self.pallet_ids}
        # En mode post_tri : seulement palettes 1 (stock) et 3 (anomalie)
        # En mode initial : palettes 1, 2, 3
        numbers = {'initial': (1, 2, 3), 'warehouse_out': (2, 3)}.get(self.mode, (1, 3))
        for n in numbers:
            if n not in existing:
                Pallet.create({"session_id": self.id, "number": n})

    @scan_operation
    def action_resume_scan(self):
        self.ensure_one()
        if not self.exists() or self.state != 'in_progress':
            raise UserError(_("Démarrez la session avant de reprendre le scan."))
        self._touch_activity()
        tags = {
            'initial': 'ar_atelier_scan_app',
            'post_tri': 'ar_atelier_scan_app_post_tri',
            'warehouse': 'ar_atelier_scan_app_warehouse',
            'warehouse_out': 'ar_atelier_scan_app_warehouse_out',
        }
        return {
            'type': 'ir.actions.client',
            'name': _('Scan — %s') % self.name,
            'tag': tags[self.mode],
            'params': {'session_id': self.id, 'mode': self.mode},
        }

    def action_start(self):
        for s in self:
            if s.state == "draft":
                s._ensure_pallets()
                s.write({"state": "in_progress", "date_start": fields.Datetime.now(),
                         "last_activity_at": fields.Datetime.now(), "date_end": False})

    def action_done(self):
        self._check_closure_allowed()
        for s in self:
            s.write({"state": "done", "date_end": fields.Datetime.now()})

    def _closure_blocker(self):
        self.ensure_one()
        count = self.env['ar.atelier.box'].search_count([
            ('session_id', '=', self.id), ('section', '=', 3), ('active', '=', True),
        ])
        if count:
            return {
                'success': False, 'closure_blocked': True, 'anomaly_count': count,
                'message': _("Merci de corriger les anomalies avant de clôturer la session."),
            }
        if self.mode == 'warehouse_out' and not (self.warehouse_out_reason or '').strip():
            return {'success': False, 'closure_blocked': True, 'reason_required': True,
                    'message': _('Renseignez le motif avant de clôturer la session Sortie Magasin LOG.')}
        return None

    def _check_closure_allowed(self):
        for session in self:
            blocker = session._closure_blocker()
            if blocker:
                raise UserError(blocker['message'])

    def write(self, vals):
        if 'state' in vals and any(s.state == 'reset' and vals['state'] != 'reset' for s in self):
            raise UserError(_("Une session réinitialisée ne peut pas être rouverte ou changer d'état."))
        if vals.get('state') == 'reset':
            if not self.env.su and not self.env.user.has_group('ar_atelier.group_atelier_reset'):
                raise AccessError(_("Vous n'avez pas accès à la réinitialisation des sessions."))
            for session in self:
                if session.state not in ('draft', 'in_progress'):
                    raise UserError(_("Seules les sessions non clôturées peuvent être réinitialisées."))
                if self.env['ar.atelier.box'].search_count([
                    ('session_id', '=', session.id), ('active', '=', True), ('section', '=', 3),
                ]):
                    raise UserError(_("Merci de traiter les anomalies de la session %s et de relancer la réinitialisation.") % session.name)
        if vals.get('state') == 'done':
            self.check_access('write')
            self._check_closure_allowed()
        return super().write(vals)

    def action_cancel(self):
        for s in self:
            s.write({"state": "cancelled", "date_end": fields.Datetime.now()})

    def action_reset_to_draft(self):
        for s in self:
            s.write({"state": "draft"})

    @api.model
    @scan_operation
    def get_or_create_active(self, mode="initial", raw_code=None):
        if mode not in ("initial", "post_tri", "warehouse", "warehouse_out"):
            raise ValidationError(_("Mode de scan invalide."))
        session = self.search([
            ("user_id", "=", self.env.user.id),
            ("state", "=", "in_progress"),
            ("mode", "=", mode),
            ("operator_code", "!=", False),
            ("operator_name", "!=", False),
        ], order="create_date desc", limit=1)
        if not session:
            if raw_code is None:
                return False
            parts = raw_code.strip().split(';', 2)
            if len(parts) != 3 or parts[0].upper() != 'START' or not parts[1].strip() or not parts[2].strip():
                raise UserError(_("Scannez le matricule au format START;matricule;nom pour ouvrir une session."))
            session = self.create({'mode': mode, 'operator_code': parts[1].strip(),
                                   'operator_name': parts[2].strip()})
            session.action_start()
        return session.id

    @scan_operation
    def action_close_and_new(self):
        self.ensure_one()
        blocker = self._closure_blocker()
        if blocker:
            return blocker
        self.action_done()
        return False

    # --- Codes spéciaux START / END ---

    def _handle_end_scan(self):
        self.ensure_one()
        blocker = self._closure_blocker()
        if blocker:
            return blocker
        # Vide = aucune boxe active (les désactivées ne comptent pas pour fermer une session)
        was_empty = not self.box_ids.filtered(lambda b: b.active)
        if self.state == "in_progress":
            self.action_done()
        if was_empty:
            return {
                "success": True, "action": "session_closed_empty",
                "message": _("Session %s clôturée (vide).") % self.name,
            }
        return {
            "success": True, "action": "session_closed_empty",
            "message": _("Session %s clôturée. Scannez votre matricule pour commencer.") % self.name,
        }

    def _handle_start_scan(self, raw_code):
        self.ensure_one()
        parts = raw_code.split(";")
        if len(parts) < 3:
            return {"success": False, "message": _("Format START attendu : START;matricule;nom")}
        matricule = (parts[1] or "").strip()
        nom = ";".join(parts[2:]).strip()
        if not matricule or not nom:
            return {"success": False, "message": _("Matricule ou nom manquant.")}

        # Identify an existing waiting session in place, including sessions
        # containing boxes scanned before identification became mandatory.
        if (not (self.operator_code or '').strip() or not (self.operator_name or '').strip()
                or not self.box_ids.filtered(lambda b: b.active)):
            self.write({"operator_code": matricule, "operator_name": nom})
            if self.state == "draft":
                self.action_start()
            return {
                "success": True, "action": "session_updated",
                "operator_code": matricule, "operator_name": nom,
                "message": _("Opérateur identifié : %s (%s)") % (nom, matricule),
            }
        blocker = self._closure_blocker()
        if blocker:
            return blocker
        self.action_done()
        new_session = self.create({"operator_code": matricule, "operator_name": nom, "mode": self.mode})
        new_session.action_start()
        return {
            "success": True, "action": "session_changed",
            "new_session_id": new_session.id,
            "operator_code": matricule, "operator_name": nom,
            "message": _("Nouvelle session pour %s (%s)") % (nom, matricule),
        }

    # --- Analyse / routing ---

    @api.model
    def _scan_history_domain(self):
        # IDs define an exact boundary, including scans in the same second.
        cutoff = int(self.env['ir.config_parameter'].sudo().get_param('ar_atelier.reset_box_cutoff', '0'))
        return [
            ('session_id.state', '!=', 'reset'),
            '|', ('id', '>', cutoff), ('session_id.reset_pending', '=', True),
        ]

    def _release_reset_history(self):
        for session in self.sudo().filtered('reset_pending'):
            if not self.env['ar.atelier.box'].sudo().search_count([
                ('session_id', '=', session.id), ('active', '=', True), ('section', '=', 3),
            ]):
                session.write({'reset_pending': False})

    def _period_boxes(self, parsed):
        """Use the active OF list's inclusive local dates, converted to UTC."""
        of_list = self.env['ar.atelier.of.list'].get_active_list()
        if not of_list or not of_list.of_ids.filtered(lambda r: r.name == parsed.get('po')):
            return self.env['ar.atelier.box']
        tz = pytz.timezone(self.env.context.get('tz') or self.env.user.tz or 'UTC')
        start = tz.localize(datetime.combine(of_list.valid_from, time.min)).astimezone(pytz.UTC).replace(tzinfo=None)
        end = tz.localize(datetime.combine(of_list.valid_to + timedelta(days=1), time.min)).astimezone(pytz.UTC).replace(tzinfo=None)
        return self.env['ar.atelier.box'].search([
            ('datamatrix_raw', '=', parsed['raw']), ('active', '=', True),
            ('scan_datetime', '>=', start), ('scan_datetime', '<', end),
            ('id', '!=', parsed.get('_exclude_box_id', 0)),
        ] + self._scan_history_domain(), order='scan_datetime, id')

    def _analyze_scan(self, parsed):
        """
        Analyse le contenu parsé pour déterminer :
        - les codes d'anomalie (liste)
        - la section finale (1=stock, 2=tri, 3=anomalie)

        En mode INITIAL : contrôles UC + OF + doublon. Routing stock/tri/anomalie.
        En mode POST_TRI : contrôles UC + déjà passé par tri. Routing stock/anomalie uniquement.

        Retourne : (section, [anomaly_codes])
        """
        self.ensure_one()
        reasons = []
        if not parsed:
            return 3, ["unknown_format"]

        if self.mode == 'warehouse_out':
            history = self.env['ar.atelier.box'].search([
                ('datamatrix_raw', '=', parsed['raw']), ('active', '=', True),
                ('id', '!=', parsed.get('_exclude_box_id', 0)),
                ('session_id.mode', 'in', ['initial', 'post_tri', 'warehouse']),
                ('section', 'in', [1, 2]),
            ] + self._scan_history_domain(), order='scan_datetime desc, id desc')
            latest = history[:1]
            if not latest:
                return 3, ['warehouse_out_no_tri']
            if latest.section == 1:
                return 3, ['warehouse_out_in_stock']
            return 2, []

        mat = (parsed.get("mat") or "").strip()
        qty = parsed.get("qty") or 0
        po = (parsed.get("po") or "").strip()
        raw = parsed.get("raw") or ""
        period_boxes = self._period_boxes(parsed)
        candidates = period_boxes.filtered(
            lambda b: b.session_id.mode == self.mode and
            (self.mode == 'initial' or b.section == 1))
        duplicate = candidates[:1]
        parsed['_duplicate_box_id'] = duplicate.id if duplicate else False
        if duplicate:
            reasons.append('duplicate_other_session')

        if self.mode == 'warehouse':
            uc = self.env['ar.atelier.uc.ref'].search([('name', '=', mat), ('active', '=', True)], limit=1)
            if not mat or not uc:
                reasons.append('unknown_ref')
            elif not uc.qty_per_carton or not qty or qty != uc.qty_per_carton:
                reasons.append('qty_mismatch')
            of_list = self.env['ar.atelier.of.list'].get_active_list()
            if not of_list or not of_list.of_ids.filtered(lambda r: r.name == po):
                reasons.append('inactive_of')
            entries = period_boxes.filtered(lambda b: b.session_id.mode == 'initial')
            if not entries:
                reasons.append('warehouse_no_entry')
            elif not entries.filtered(lambda b: b.section == 1):
                tri_entries = entries.filtered(lambda b: b.section == 2)
                exits = period_boxes.filtered(lambda b: b.session_id.mode == 'post_tri')
                if tri_entries:
                    if not exits.filtered(lambda b: b.section == 1 and b.scan_datetime >= tri_entries[0].scan_datetime):
                        reasons.append('warehouse_still_in_tri')
                        for box in exits.filtered(lambda b: b.section == 3):
                            reasons.extend(filter(None, (box.anomaly_reason_codes or '').split(',')))
                else:
                    reasons.append('warehouse_entry_anomaly')
                    for box in entries:
                        reasons.extend(filter(None, (box.anomaly_reason_codes or '').split(',')))
            reasons = list(dict.fromkeys(reasons))
            return (3, reasons) if reasons else (1, [])

        # ============ MODE POST_TRI : contrôles allégés ============
        if self.mode == "post_tri":
            # 1. UC : référence et qty/carton (idem mode initial)
            UcRef = self.env["ar.atelier.uc.ref"]
            uc = UcRef.search([("name", "=", mat), ("active", "=", True)], limit=1) if mat else UcRef
            if not mat or not uc:
                reasons.append("unknown_ref")
            elif not uc.qty_per_carton or not qty or qty != uc.qty_per_carton:
                reasons.append("qty_mismatch")

            # 2. Vérification que l'étiquette est bien passée par le tri en mode initial
            #    (boxe active section=2 dans une session mode=initial)
            if raw:
                tri_box = period_boxes.filtered(lambda b: b.section == 2 and b.session_id.mode == 'initial')[:1]
                if not tri_box:
                    reasons.append("not_in_tri")

            if reasons:
                return 3, reasons  # palette anomalie
            return 1, []  # palette stock

        # ============ MODE INITIAL : contrôles complets ============

        # 1. UC : référence et qty/carton (STRICT)
        # Anomalie si :
        #   - mat absent
        #   - mat pas dans le référentiel UC actif
        #   - qty_per_carton de la ref = 0 ou non défini
        #   - qty étiquette = 0 ou non défini
        #   - qty étiquette != qty_per_carton de la ref
        UcRef = self.env["ar.atelier.uc.ref"]
        uc = UcRef.search([("name", "=", mat), ("active", "=", True)], limit=1) if mat else UcRef
        if not mat or not uc:
            reasons.append("unknown_ref")
        elif not uc.qty_per_carton or not qty or qty != uc.qty_per_carton:
            reasons.append("qty_mismatch")

        # 2. OF : STRICT — toujours valider contre une liste active
        # Anomalie si :
        #   - po absent ou = "0"
        #   - aucune liste OF active cette semaine
        #   - po pas présent dans la liste OF active
        OfList = self.env["ar.atelier.of.list"]
        active_of_list = OfList.get_active_list()
        if not po or po == "0":
            reasons.append("inactive_of")
        elif not active_of_list:
            reasons.append("inactive_of")
        elif not active_of_list.of_ids.filtered(lambda r: r.name == po):
            reasons.append("inactive_of")

        # Détermination de la section
        if reasons:
            return 3, reasons  # palette anomalie

        # Pas d'anomalie -> stock ou tri selon liste TRI active
        TriList = self.env["ar.atelier.tri.list"]
        active_tri_list = TriList.get_active_list()
        if active_tri_list and active_tri_list.ref_ids.filtered(lambda r: r.name == mat):
            return 2, []  # tri
        return 1, []  # stock

    # --- Scan ---

    @scan_operation
    def action_scan(self, raw_code):
        self.ensure_one()
        # Reset takes the exclusive version of this lock before selecting its
        # boundary. Concurrent scans can run together, but cannot cross a reset.
        self.env.cr.execute('SELECT pg_advisory_xact_lock_shared(191924, 1)')
        self.env.cr.execute('SELECT id FROM ar_atelier_session WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset(['state'])
        if self.state == 'reset':
            return {'success': False, 'message': _("Session réinitialisée. Scannez votre matricule pour ouvrir une nouvelle session.")}
        if not raw_code:
            return {"success": False, "message": _("Code vide")}

        code = raw_code.strip()
        self._touch_activity()
        upper = code.upper()
        if upper == "END;SCAN":
            return self._handle_end_scan()
        if upper.startswith("START;"):
            return self._handle_start_scan(code)

        if not (self.operator_code or '').strip() or not (self.operator_name or '').strip():
            return {
                'success': False, 'identification_required': True,
                'message': _("Identifiez-vous pour commencer le scan."),
            }

        status = self.get_referential_status(mode=self.mode)
        if status["missing"]:
            missing = status["missing"]
            labels = " et ".join([", ".join(missing[:-1]), missing[-1]]) if len(missing) > 1 else missing[0]
            return {
                "success": False,
                "referential_blocked": True,
                "message": _("Merci d’alimenter %s pour autoriser le scan.") % labels,
            }

        if self.state == "draft":
            self.action_start()
        if self.state in ("done", "cancelled"):
            return {"success": False, "message": _("Session clôturée.")}

        # Serialize scans of the same label, including different sessions.
        self.env.cr.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [code])

        # Reconnaissance automatique QR et code-barres ARaymond
        parsed = parse_label(code)
        if not parsed:
            # Format non reconnu -> rejet immédiat, pas de création de boxe
            return {
                "success": False,
                "message": _("Format étiquette non reconnu. Formats attendus : QR {MAT:...;QTY:...;PO:...;HU:...} ou code-barres Réf. UC (9) + Lot (10) + Quantité (7) + OF (8) + HU terminé par +."),
            }
        label_format = parsed["_format"]

        # Analyse + routing (toutes les anomalies métier restent traitées normalement)
        section, reasons = self._analyze_scan(parsed)

        # Cible palette
        pallet = self.pallet_ids.filtered(lambda p: p.number == section)
        if not pallet:
            self._ensure_pallets()
            pallet = self.pallet_ids.filtered(lambda p: p.number == section)

        active_boxes_in_pallet = pallet.box_ids.filtered(lambda b: b.active)
        if pallet.is_full:
            return {
                "success": False,
                "message": _("Palette %s pleine (%s/%s) — arrêt de scan.") % (
                    section, len(active_boxes_in_pallet), pallet.capacity,
                ),
            }

        # Position en BDD = nb actives (l'ordre chronologique sans les trous des désactivées)
        position = len(active_boxes_in_pallet)
        vals = {
            "session_id": self.id,
            "pallet_id": pallet.id,
            "datamatrix_raw": code,
            "section": section,
            "position": position,
            "label_format": label_format,
            "duplicate_box_id": parsed.get('_duplicate_box_id'),
            "anomaly_reason_codes": ",".join(reasons) if reasons else False,
        }

        # Champs métier communs aux deux formats
        vals.update({
            "mat": parsed.get("mat"),
            "pwg": parsed.get("pwg") or 0.0,
            "plt": parsed.get("plt"),
            "po": parsed.get("po"),
            "pomat": parsed.get("pomat"),
            "sloc": parsed.get("sloc"),
            "qty": parsed.get("qty") or 0,
            "wghtg": parsed.get("wghtg") or 0.0,
            "sut": parsed.get("sut"),
            "hu": parsed.get("hu"),
            "bsd": parsed.get("bsd"),
            "bat": parsed.get("bat"),
            "pal": parsed.get("pal"),
            "btyp": parsed.get("btyp"),
            "bver": parsed.get("bver"),
        })

        scanned_box = self.env["ar.atelier.box"].create(vals)

        section_name = {1: "Stock", 2: "Tri", 3: "Anomalie"}.get(section, "?")
        msg = _("Boxe routée vers %s (%s/%s)") % (section_name, len(pallet.box_ids), pallet.capacity)
        if scanned_box.duplicate_box_id:
            msg += ' — ' + scanned_box.duplicate_message
        if reasons:
            labels = " | ".join(ANOMALY_LABELS.get(r, r) for r in reasons)
            msg += " — " + labels
        return {
            "success": True, "section": section, "section_name": section_name,
            "position": position,
            "pallet_count": len(pallet.box_ids.filtered(lambda b: b.active)),
            "pallet_capacity": pallet.capacity,
            "anomaly_reasons": reasons,
            "message": msg,
        }

    # --- Suppression boxe ---

    @api.model
    def delete_box(self, box_id):
        """Désactive une boxe anomalie (soft delete).
        La boxe reste en BDD pour la traçabilité mais :
        - Disparait de l'affichage palette
        - Exclue de la détection de doublon (autre session ou même)
        - Ne compte plus dans les compteurs Stock/Tri/Anomalie
        """
        if not self.env.su and not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Seuls les administrateurs Odoo peuvent supprimer ou désactiver une boxe."))
        box = self.env["ar.atelier.box"].browse(box_id)
        box.check_access('read')
        if not box.exists():
            return {"success": False, "message": _("Boxe introuvable.")}
        if not box.is_anomaly:
            return {"success": False, "message": _("Seules les boxes en anomalie peuvent être désactivées.")}
        if not box.active:
            return {"success": False, "message": _("Boxe déjà désactivée.")}
        box.sudo().write({"active": False})
        box.session_id._touch_activity()
        return {
            "success": True,
            "message": _("Boxe désactivée (conservée en base pour traçabilité)."),
        }

    @scan_operation
    def cancel_duplicate_box(self, box_id, actions_taken=None):
        self.ensure_one()
        actions = (actions_taken or '').strip()
        if not actions:
            raise UserError(_("Description des actions obligatoire."))
        box = self.env['ar.atelier.box'].browse(box_id).exists()
        if not box or box.session_id != self or not box.active:
            raise UserError(_("Seule une BOX active de cette session peut être annulée."))
        reasons = ' | '.join(filter(None, [box.anomaly_labels, box.duplicate_message]))
        body = Markup('<p>BOX annulée : %s. Anomalies : %s</p><p>Actions effectuées : %s</p>') % (box.datamatrix_raw, reasons, actions)
        box.write({'active': False})
        self._touch_activity()
        self.message_post(body=body, subtype_xmlid='mail.mt_note')
        box.message_post(body=body, subtype_xmlid='mail.mt_note')
        return {'success': True, 'message': _('BOX annulée ; historique conservé dans le chatter de la session.')}

    # --- Détail boxe ---

    @api.model
    def get_box_detail(self, box_id):
        box = self.env["ar.atelier.box"].browse(box_id)
        if not box.exists():
            return {"success": False, "message": _("Boxe introuvable.")}
        corrections = [{
            "id": c.id, "old": c.old_datamatrix, "new": c.new_datamatrix,
            "actions": c.actions_taken, "user": c.user_id.name,
            "date": fields.Datetime.to_string(c.correction_date),
            "summary": c.history_snapshot or False,
        } for c in box.correction_ids.sorted("correction_date")]
        anomaly_codes = [c.strip() for c in (box.anomaly_reason_codes or "").split(",") if c.strip()]
        return {
            "success": True, "id": box.id,
            "datamatrix": box.datamatrix_raw, "format": box.label_format,
            "section": box.section,
            "section_name": {1: "Stock", 2: "Tri", 3: "Anomalie"}.get(box.section, "?"),
            "is_anomaly": box.is_anomaly,
            "can_cancel_duplicate": bool(box.active),
            "duplicate_message": box.duplicate_message or '',
            "position": box.position, "layer": box.layer,
            "scan_datetime": fields.Datetime.to_string(box.scan_datetime),
            "user": box.user_id.name,
            "anomaly_codes": anomaly_codes,
            "anomaly_labels": [ANOMALY_LABELS.get(c, c) for c in anomaly_codes],
            "fields": {
                "mat": box.mat or "", "pwg": box.pwg, "plt": box.plt or "",
                "po": box.po or "", "pomat": box.pomat or "", "sloc": box.sloc or "",
                "qty": box.qty, "wghtg": box.wghtg, "sut": box.sut or "",
                "hu": box.hu or "", "bsd": box.bsd or "", "bat": box.bat or "",
                "pal": box.pal or "", "btyp": box.btyp or "", "bver": box.bver or "",
                "gtin": box.gtin or "", "lot": box.lot or "", "serial": box.serial or "",
            },
            "corrections": corrections,
        }

    @api.model
    def correct_box(self, box_id, new_datamatrix, actions_taken):
        if not self.env.su and not self.env.user.has_group("ar_atelier.group_atelier_manager"):
            raise AccessError(_("Seuls les Managers Atelier peuvent modifier les boxes."))
        box = self.env["ar.atelier.box"].browse(box_id)
        if not box.exists():
            return {"success": False, "message": _("Boxe introuvable.")}
        new_code = (new_datamatrix or "").strip()
        if not new_code:
            return {"success": False, "message": _("Nouvelle étiquette vide.")}
        actions = (actions_taken or "").strip()
        if not actions:
            return {"success": False, "message": _("Description des actions obligatoire.")}

        dup = box.session_id.box_ids.filtered(
            lambda b: b.datamatrix_raw == new_code and b.id != box.id and b.active
        )
        if dup:
            return {"success": False, "message": _("Cette nouvelle étiquette existe déjà dans la session.")}

        old_code = box.datamatrix_raw
        old_reasons = [code.strip() for code in (box.anomaly_reason_codes or '').split(',') if code.strip()]
        old_section = box.section

        # Re-parser et re-router
        parsed = parse_label(new_code)
        if parsed:
            parsed['_exclude_box_id'] = box.id
        new_vals = {"datamatrix_raw": new_code}

        if parsed:
            new_vals["label_format"] = parsed["_format"]
            for key in ("mat", "plt", "po", "pomat", "sloc", "sut", "hu",
                        "bsd", "bat", "pal", "btyp", "bver"):
                new_vals[key] = parsed.get(key) or False
            new_vals["pwg"] = parsed.get("pwg") or 0.0
            new_vals["qty"] = parsed.get("qty") or 0
            new_vals["wghtg"] = parsed.get("wghtg") or 0.0
            new_section, reasons = box.session_id._analyze_scan(parsed)
            new_vals['duplicate_box_id'] = parsed.get('_duplicate_box_id')
        else:
            new_vals['duplicate_box_id'] = False
            new_vals["label_format"] = "raw"
            new_section, reasons = 3, ["unknown_format"]

        new_vals["anomaly_reason_codes"] = ",".join(reasons) if reasons else False

        if new_section != box.section:
            new_pallet = box.session_id.pallet_ids.filtered(lambda p: p.number == new_section)
            if new_pallet and not new_pallet.is_full:
                new_vals["section"] = new_section
                new_vals["pallet_id"] = new_pallet.id
                new_vals["position"] = len(new_pallet.box_ids)

        changed_fields = []
        for key, value in new_vals.items():
            if key in ('datamatrix_raw', 'label_format', 'anomaly_reason_codes', 'section', 'pallet_id', 'position', 'duplicate_box_id'):
                continue
            if box[key] != value:
                changed_fields.append({
                    'field': key, 'label': box._fields[key].string,
                    'before': box[key] if box[key] is not False else '',
                    'after': value if value is not False else '',
                })
        labels = lambda codes: [ANOMALY_LABELS.get(code, code) for code in codes]
        section_names = {1: 'Stock', 2: 'Tri', 3: 'Anomalie'}
        final_section = new_vals.get('section', old_section)
        summary = {
            'before': labels(old_reasons),
            'after': labels(reasons),
            'resolved': labels([code for code in old_reasons if code not in reasons]),
            'introduced': labels([code for code in reasons if code not in old_reasons]),
            'changes': changed_fields,
            'same_label': old_code == new_code,
            'section_before': section_names.get(old_section, '?'),
            'section_after': section_names.get(final_section, '?'),
            'transfer_pending': final_section != new_section,
        }
        box.write(new_vals)
        box.session_id._touch_activity()
        self.env["ar.atelier.box.correction"].create({
            "box_id": box.id,
            "old_datamatrix": old_code,
            "new_datamatrix": new_code,
            "actions_taken": actions,
            "history_snapshot": summary,
        })
        return {
            "success": True, "box_id": box.id,
            "new_section": new_vals.get("section", box.section),
            "message": _("Boxe corrigée et étiquette mise à jour."),
        }

    # --- État + métadonnées des référentiels pour le frontend ---

    @api.model
    def get_referential_status(self, mode=None):
        """Retourne dates de MAJ et validité des référentiels (pour bandeau frontend)."""
        UcRef = self.env["ar.atelier.uc.ref"]
        TriList = self.env["ar.atelier.tri.list"]
        OfList = self.env["ar.atelier.of.list"]

        last_uc = UcRef.search([], order="write_date desc", limit=1)
        active_tri = TriList.get_active_list()
        active_of = OfList.get_active_list()
        uc_count = UcRef.search_count([("active", "=", True)])
        missing = []
        if not uc_count:
            missing.append("UC")
        if not active_tri or not active_tri.ref_count:
            missing.append("TRI")
        if not active_of or not active_of.of_count:
            missing.append("OF")

        return {
            "missing": [] if mode == 'warehouse_out' else missing,
            "can_manage": self.env.user.has_group("ar_atelier.group_atelier_manager"),
            "can_configure_closure": self.env.user.has_group('base.group_system'),
            "can_reset_sessions": self.env.user.has_group('ar_atelier.group_atelier_reset'),
            "can_view_referentials": self.env.user.has_group("ar_atelier.group_atelier_display_referentials"),
            "can_delete": self.env.user.has_group("base.group_system"),
            "uc": {
                "last_update": fields.Datetime.to_string(last_uc.write_date) if last_uc else "",
                "count": uc_count,
            },
            "tri": {
                "active_list": active_tri.name if active_tri else "",
                "valid_from": fields.Date.to_string(active_tri.valid_from) if active_tri else "",
                "valid_to": fields.Date.to_string(active_tri.valid_to) if active_tri else "",
                "ref_count": active_tri.ref_count if active_tri else 0,
                "last_update": fields.Datetime.to_string(active_tri.write_date) if active_tri else "",
            },
            "of": {
                "active_list": active_of.name if active_of else "",
                "valid_from": fields.Date.to_string(active_of.valid_from) if active_of else "",
                "valid_to": fields.Date.to_string(active_of.valid_to) if active_of else "",
                "of_count": active_of.of_count if active_of else 0,
                "last_update": fields.Datetime.to_string(active_of.write_date) if active_of else "",
            },
        }

    def get_pallet_state(self):
        self.ensure_one()
        result = {
            "session_id": self.id,
            "session_name": self.name,
            "session_state": self.state,
            "operator_code": self.operator_code or "",
            "operator_name": self.operator_name or "",
            "warehouse_out_reason": self.warehouse_out_reason or "",
            "pallets": [],
        }
        for n in (1, 2, 3):
            pallet = self.pallet_ids.filtered(lambda p: p.number == n)
            if not pallet:
                continue
            # Filtrer les actives + renuméroter display_position pour combler les trous
            active_boxes = pallet.box_ids.filtered(lambda b: b.active).sorted("position")
            result["pallets"].append({
                "number": n,
                "capacity": pallet.capacity,
                "rows": pallet.rows,
                "cols": pallet.cols,
                "layers": pallet.layers,
                "boxes": [
                    {
                        "id": b.id,
                        "position": idx,  # display position (renumérotée 0..N)
                        "real_position": b.position,  # position chrono BDD (pour audit)
                        "datamatrix": b.datamatrix_raw,
                        "mat": b.mat or "",
                        "qty": b.qty or 0,
                        "bat": b.bat or "",
                        "hu": b.hu or "",
                        "is_anomaly": b.is_anomaly,
                        "anomaly_labels": ' | '.join(filter(None, [b.anomaly_labels, b.duplicate_message])),
                        "correction_count": b.correction_count,
                    }
                    for idx, b in enumerate(active_boxes)
                ],
            })
        return result
