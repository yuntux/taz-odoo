from dateutil.relativedelta import relativedelta

from odoo import models, fields, api


class HrSkillLevel(models.Model):
    _inherit = 'hr.skill.level'
    description = fields.Text("Description")


class HrSkill(models.Model):
    _inherit = 'hr.skill'
    description = fields.Text("Description")
    is_certification = fields.Boolean("Certification")
    validity_duration_in_months = fields.Integer("Durée de validité (mois)")


class HrEmployeeSkill(models.Model):
    _inherit = 'hr.employee.skill'
    skill_is_certification = fields.Boolean(related='skill_id.is_certification')
    obtained_date = fields.Date("Date d'obtention")
    expiry_date = fields.Date("Date d'expiration", compute='_compute_expiry_date', store=True)

    @api.depends('obtained_date', 'skill_id.is_certification', 'skill_id.validity_duration_in_months')
    def _compute_expiry_date(self):
        for rec in self:
            if not rec.skill_id.is_certification or not rec.obtained_date:
                rec.expiry_date = False
            else:
                rec.expiry_date = rec.obtained_date + relativedelta(months=rec.skill_id.validity_duration_in_months)
