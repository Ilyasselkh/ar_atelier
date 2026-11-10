from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError


class AtelierClosureSettings(models.TransientModel):
    _name = 'ar.atelier.closure.settings'
    _description = 'Durée de clôture Atelier'

    minutes = fields.Integer(
        string='Durée d’inactivité (minutes)', required=True,
        default=lambda self: self.env['ar.atelier.session']._get_idle_timeout_minutes(),
    )

    @api.constrains('minutes')
    def _check_minutes(self):
        for settings in self:
            if settings.minutes <= 0:
                raise ValidationError(_('La durée doit être un nombre entier de minutes supérieur à zéro.'))

    def action_save(self):
        self.ensure_one()
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Seuls les administrateurs Odoo peuvent modifier la durée de clôture.'))
        self._check_minutes()
        self.env['ir.config_parameter'].sudo().set_param('ar_atelier.idle_timeout_minutes', self.minutes)
        return {'type': 'ir.actions.act_window_close'}
