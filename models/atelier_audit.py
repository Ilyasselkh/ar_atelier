from functools import wraps

from markupsafe import Markup
from odoo import api, models, _
from odoo.exceptions import AccessError
from odoo.tools import format_date, format_datetime


def scan_operation(method):
    """Elevate only fixed scan operations after checking actor and session ownership.

    No context flag supplied by an RPC client can grant this capability.
    sudo preserves uid, so the audit still identifies the actual operator.
    """
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if not self.env.su:
            user = self.env.user
            if not (user.has_group('ar_atelier.group_atelier_user') or user.has_group('base.group_system')):
                raise AccessError(_("Accès au scan Atelier non autorisé."))
            self.check_access('read')
            if not user.has_group('ar_atelier.group_atelier_manager') and any(s.user_id != user for s in self):
                raise AccessError(_("Vous pouvez scanner uniquement dans vos propres sessions."))
        # Never elevate client-supplied defaults or mail/audit control flags.
        context = {key: self.env.context[key] for key in ('lang', 'tz', 'allowed_company_ids')
                   if key in self.env.context}
        return method(self.with_context(context).sudo(), *args, **kwargs)
    return wrapped


class AtelierAuditMixin(models.AbstractModel):
    _name = 'ar.atelier.audit.mixin'
    _description = 'Traçabilité des opérations Atelier'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _audit_fields = ()

    def _audit_parents(self):
        self.ensure_one()
        mapping = {'ar.atelier.pallet': ('session_id',), 'ar.atelier.box': ('session_id', 'pallet_id'),
                   'ar.atelier.box.correction': ('box_id',), 'ar.atelier.tri.ref': ('list_id',),
                   'ar.atelier.of.ref': ('list_id',)}
        return [self[field] for field in mapping.get(self._name, ()) if self[field]]

    def _audit_snapshot(self, names=None):
        self.ensure_one()
        result = {}
        for name in self._audit_fields:
            if names is not None and name not in names:
                continue
            field = self._fields[name]
            value = self[name]
            if field.type == 'many2one':
                value = {'id': value.id, 'nom': value.display_name} if value else False
            elif field.type == 'binary':
                value = 'Fichier fourni' if value else False
            result[name] = value
        return result

    def _audit_value_label(self, name, value, metadata):
        field = self._fields[name]
        if field.type == 'boolean':
            return _("Oui") if value else _("Non")
        if value is False or value is None or value == '':
            return _("Non renseigné")
        if field.type == 'many2one':
            return value['nom']
        if field.type == 'selection':
            return dict(metadata.get('selection', [])).get(value, value)
        if field.type == 'date':
            return format_date(self.env, value)
        if field.type == 'datetime':
            return format_datetime(self.env, value)
        if field.type == 'float':
            return f'{value:g}'
        return str(value)

    def _audit_body(self, operation, details, title=None):
        self.ensure_one()
        labels = {'create': 'Création', 'write': 'Modification', 'unlink': 'Suppression'}
        metadata = self.fields_get(list(details), attributes=['string', 'selection'])
        lines = []
        for name, value in details.items():
            label = metadata[name]['string']
            if operation == 'write':
                before = self._audit_value_label(name, value['avant'], metadata[name])
                after = self._audit_value_label(name, value['après'], metadata[name])
                lines.append(Markup('<li><strong>%s :</strong> %s → %s</li>') % (label, before, after))
            else:
                if self._fields[name].type != 'boolean' and (value is False or value is None or value == ''):
                    continue
                lines.append(Markup('<li><strong>%s :</strong> %s</li>') % (
                    label, self._audit_value_label(name, value, metadata[name])))
        heading = title or _('%s — %s') % (labels[operation], self.display_name)
        return Markup('<p><strong>%s</strong></p><ul>%s</ul>') % (heading, Markup('').join(lines))

    def _audit_event(self, operation, details):
        for record in self:
            body = record._audit_body(operation, details)
            targets = [record]
            targets.extend(record._audit_parents())
            for target in targets:
                target.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')

    @api.model_create_multi
    def create(self, vals_list):
        records = super(AtelierAuditMixin, self.with_context(mail_create_nolog=True)).create(vals_list)
        records = records.with_env(self.env)
        for record in records:
            record._audit_event('create', record._audit_snapshot())
        return records

    def write(self, vals):
        self.check_access('write')
        if (self._name == 'ar.atelier.box' and 'active' in vals and not vals['active']
                and not self.env.su and not self.env.user.has_group('base.group_system')):
            raise AccessError(_("Seuls les administrateurs Odoo peuvent désactiver une boxe."))
        before = {record.id: record._audit_snapshot(vals) for record in self}
        # Keep the former parent in the history when a line is moved.
        previous_parents = {r.id: r._audit_parents() for r in self}
        result = super().write(vals)
        for record in self:
            after = record._audit_snapshot(vals)
            changes = {name: {'avant': value, 'après': after[name]} for name, value in before[record.id].items()
                       if value != after[name] or self._fields[name].type == 'binary'}
            if changes:
                record._audit_event('write', changes)
                for old_parent in previous_parents[record.id]:
                    if old_parent not in record._audit_parents():
                        body = record._audit_body('write', changes, title=_("Ligne déplacée : %s") % record.display_name)
                        old_parent.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')
        return result

    def _audit_deletion(self):
        # Delete children through the ORM: SQL cascades skip both audit hooks
        # and mail.thread cleanup (messages, followers and activities).
        children = {'ar.atelier.session': ('box_ids', 'pallet_ids'), 'ar.atelier.pallet': ('box_ids',),
                    'ar.atelier.box': ('correction_ids',), 'ar.atelier.tri.list': ('ref_ids',),
                    'ar.atelier.of.list': ('of_ids',)}
        for record in self.with_context(active_test=False):
            record._audit_event('unlink', record._audit_snapshot())
            for field in children.get(record._name, ()):
                record[field].unlink()

    def unlink(self):
        if not self.env.su and not (self.env.user.has_group('ar_atelier.group_atelier_delete')
                                   or self.env.user.has_group('base.group_system')):
            raise AccessError(_("Seuls les administrateurs Odoo ou les utilisateurs disposant de l’accès Atelier — Suppression peuvent supprimer des données Atelier."))
        self.check_access('unlink')
        self._audit_deletion()
        return super().unlink()
