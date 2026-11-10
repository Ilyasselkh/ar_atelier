from odoo import fields, models, _
from odoo.exceptions import AccessError


class AtelierReset(models.TransientModel):
    _name = 'ar.atelier.reset'
    _description = 'Réinitialisation des sessions Atelier'

    result = fields.Text(string='Résultat', readonly=True)

    def _check_reset_access(self):
        if not self.env.su and not self.env.user.has_group('ar_atelier.group_atelier_reset'):
            raise AccessError(_("Vous n'avez pas accès à la réinitialisation des sessions."))

    def action_reset(self):
        self.ensure_one()
        self._check_reset_access()
        # Fixed global scope: this permission grants this operation only.
        context = {key: self.env.context[key] for key in ('lang', 'tz', 'allowed_company_ids') if key in self.env.context}
        Session = self.env['ar.atelier.session'].with_context(context).sudo()
        self.env.cr.execute('SELECT pg_advisory_xact_lock(191924, 1)')
        # Scanning uses the same row lock so a new anomaly cannot slip between
        # the anomaly check and the state transition.
        self.env.cr.execute("SELECT id FROM ar_atelier_session WHERE state IN ('draft', 'in_progress') ORDER BY id FOR UPDATE")
        sessions = Session.browse([row[0] for row in self.env.cr.fetchall()])
        sessions.invalidate_recordset(['state'])
        sessions = sessions.filtered(lambda s: s.state in ('draft', 'in_progress'))
        reset_names, blocked = [], []
        now = fields.Datetime.now()
        for session in sessions:
            if self.env['ar.atelier.box'].sudo().search_count([
                ('session_id', '=', session.id), ('active', '=', True), ('section', '=', 3),
            ]):
                session.write({'reset_pending': True})
                blocked.append(_("Merci de traiter les anomalies de la session %s et de relancer la réinitialisation.") % session.name)
                continue
            session.write({'state': 'reset', 'date_end': now, 'reset_pending': False})
            session.message_post(body=_("Session terminée par réinitialisation. Les BOX et l'historique sont conservés."), subtype_xmlid='mail.mt_note')
            reset_names.append(session.name)
        self.env['ar.atelier.box'].flush_model()
        self.env.cr.execute('SELECT COALESCE(MAX(id), 0) FROM ar_atelier_box')
        self.env['ir.config_parameter'].sudo().set_param('ar_atelier.reset_box_cutoff', str(self.env.cr.fetchone()[0]))
        lines = [_('%s session(s) réinitialisée(s).') % len(reset_names)]
        if reset_names:
            lines.append(', '.join(reset_names))
        lines.extend(blocked)
        if not sessions:
            lines.append(_('Aucune session non clôturée à réinitialiser.'))
        self.result = '\n\n'.join(lines)
        return {
            'type': 'ir.actions.act_window', 'name': _('Réinitialisation'),
            'res_model': self._name, 'res_id': self.id,
            'views': [(False, 'form')], 'target': 'new',
        }

    def action_history(self):
        self.ensure_one()
        self._check_reset_access()
        return {
            'type': 'ir.actions.act_window', 'name': _('Sessions réinitialisées'),
            'res_model': 'ar.atelier.session', 'views': [(False, 'list'), (False, 'form')],
            'domain': [('state', '=', 'reset')],
            'context': {'create': False, 'edit': False, 'delete': False},
            'target': 'current',
        }
