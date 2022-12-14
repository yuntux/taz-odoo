from odoo import models, fields, api
from odoo.exceptions import AccessDenied, UserError, ValidationError
from odoo import _
import logging
_logger = logging.getLogger(__name__)


class HrEmployeeBase(models.AbstractModel):
    _inherit = "hr.employee.base"

    current_leave_state = fields.Selection(selection_add=[('canceled', 'Annulée')])

class staffingLeave(models.Model):
    _inherit = 'hr.leave'

    request_date_to_period = fields.Selection([
        ('am', 'Morning'), ('pm', 'Afternoon')],
        string="Date Period End", default='pm')

    #TODO : contrôler que si date_from = date_to on ne peut pas avoir request_date_from_period=pm ET request_date_to_period==am

    state = fields.Selection(selection_add=[('canceled', 'Annulée')])




    #override to deal with uom in days and request_date_to_period
    #odoo/addons/project_timesheet_holidays/models/hr_holidays.py 
    def _timesheet_create_lines(self):
        vals_list = []
        for leave in self:
            if not leave.employee_id:
                continue
            work_hours_data = leave.employee_id.list_work_time_per_day(
                leave.date_from,
                leave.date_to)

            encoding_uom_id = self.env.company.timesheet_encode_uom_id
            if encoding_uom_id == self.env.ref("uom.product_uom_hour"):
                for index, (day_date, work_hours_count) in enumerate(work_hours_data):
                    vals_list.append(leave._timesheet_prepare_line_values(index, work_hours_data, day_date, work_hours_count))
            if encoding_uom_id == self.env.ref("uom.product_uom_day"):
                for index, (day_date, work_hours_count) in enumerate(work_hours_data):
                    work_days_count = 1.0
                    if index == 0 and leave.request_date_from_period == "pm":
                        work_days_count += -0.5
                    if index == len(work_hours_data)-1 and leave.request_date_to_period == "am":
                        work_days_count += -0.5
                    vals_list.append(leave._timesheet_prepare_line_values(index, work_hours_data, day_date, work_days_count))
            else : 
                raise ValidationError(_("Company timesheet encoding uom should be either Hours or Days."))

        return self.env['account.analytic.line'].sudo().create(vals_list)



    #mieux gérer les contrôles de conflit : pas possible d'avoir 2 demis jours 
    """
    @api.constrains('date_from', 'date_to', 'employee_id')
    def _check_date(self):
        if self.env.context.get('leave_skip_date_check', False):
            return

        all_employees = self.employee_id | self.employee_ids
        all_leaves = self.search([
            ('date_from', '<', max(self.mapped('date_to'))),
            ('date_to', '>', min(self.mapped('date_from'))),
            ('employee_id', 'in', all_employees.ids),
            ('id', 'not in', self.ids),
            ('state', 'not in', ['cancel', 'refuse']),
        ])
        for holiday in self:
            domain = [
                ('date_from', '<', holiday.date_to),
                ('date_to', '>', holiday.date_from),
                ('id', '!=', holiday.id),
                ('state', 'not in', ['cancel', 'refuse']),
            ]

            employee_ids = (holiday.employee_id | holiday.employee_ids).ids
            search_domain = domain + [('employee_id', 'in', employee_ids)]

            # Begin change for dealing with 2 half-days holidays on the same day : on for morning, other for afertnoon
            conflicting_holidays_tmp = all_leaves.filtered_domain(search_domain)
            conflicting_holidays = []
            for conflicting_holiday in conflicting_holidays_tmp:
                if holiday.date_to == conflicting_holiday.date_to and holiday.request_date_to_period == 'am':
                    continue
                conflicting_holidays.append(conflicting_holiday)
            # end change


            if conflicting_holidays:
                conflicting_holidays_list = []
                # Do not display the name of the employee if the conflicting holidays have an employee_id.user_id equivalent to the user id
                holidays_only_have_uid = bool(holiday.employee_id)
                holiday_states = dict(conflicting_holidays.fields_get(allfields=['state'])['state']['selection'])
                for conflicting_holiday in conflicting_holidays:
                    conflicting_holiday_data = {}
                    conflicting_holiday_data['employee_name'] = conflicting_holiday.employee_id.name
                    conflicting_holiday_data['date_from'] = format_date(self.env, min(conflicting_holiday.mapped('date_from')))
                    conflicting_holiday_data['date_to'] = format_date(self.env, min(conflicting_holiday.mapped('date_to')))
                    conflicting_holiday_data['state'] = holiday_states[conflicting_holiday.state]
                    if conflicting_holiday.employee_id.user_id.id != self.env.uid:
                        holidays_only_have_uid = False
                    if conflicting_holiday_data not in conflicting_holidays_list:
                        conflicting_holidays_list.append(conflicting_holiday_data)
                if not conflicting_holidays_list:
                    return
                conflicting_holidays_strings = []
                if holidays_only_have_uid:
                    for conflicting_holiday_data in conflicting_holidays_list:
                        conflicting_holidays_string = _('From %(date_from)s To %(date_to)s - %(state)s',
                                                        date_from=conflicting_holiday_data['date_from'],
                                                        date_to=conflicting_holiday_data['date_to'],
                                                        state=conflicting_holiday_data['state'])
                        conflicting_holidays_strings.append(conflicting_holidays_string)
                    raise ValidationError(_('You can not set two time off that overlap on the same day.\nExisting time off:\n%s') %
                                          ('\n'.join(conflicting_holidays_strings)))
                for conflicting_holiday_data in conflicting_holidays_list:
                    conflicting_holidays_string = _('%(employee_name)s - From %(date_from)s To %(date_to)s - %(state)s',
                                                    employee_name=conflicting_holiday_data['employee_name'],
                                                    date_from=conflicting_holiday_data['date_from'],
                                                    date_to=conflicting_holiday_data['date_to'],
                                                    state=conflicting_holiday_data['state'])
                    conflicting_holidays_strings.append(conflicting_holidays_string)
                conflicting_employees = set(employee_ids) - set(conflicting_holidays.employee_id.ids)
                # Only one employee has a conflicting holiday
                if len(conflicting_employees) == len(employee_ids) - 1:
                    raise ValidationError(_('You can not set two time off that overlap on the same day for the same employee.\nExisting time off:\n%s') %
                                          ('\n'.join(conflicting_holidays_strings)))
                raise ValidationError(_('You can not set two time off that overlap on the same day for the same employees.\nExisting time off:\n%s') %
                                      ('\n'.join(conflicting_holidays_strings)))

    """
