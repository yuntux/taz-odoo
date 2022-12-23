import requests
import zlib
import os
import json
import datetime
from lxml import etree, html

from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError
from odoo import _
          
import logging
_logger = logging.getLogger(__name__)
          
##################################################################
##########                SET PARAMETERS                ##########
##################################################################

proto = "https://"
host = "tasmane.fitnetmanager.com"
api_root = "/FitnetManager/rest/"

cache_mode = True
cache_folder = '/tmp/'

##################################################################
##########                 REST CLIENT                  ##########
##################################################################
class ClientRestFitnetManager:
    def __init__(self,proto, host, api_root, login_password):
        self.proto = proto
        self.host = host
        self.api_root = api_root
        self.login_password = login_password
        url_appel_api = proto+host+api_root
        self.url_appel_api = url_appel_api
        _logger.info("ClientRestFitnetManager : " + self.url_appel_api)

    def get_api(self, target_action, read_cache=True):
        if cache_mode :
            path = os.path.join(cache_folder, target_action.replace('/','_'))
            if read_cache :
                if os.path.exists(path):
                    with open(path, 'r', encoding='utf-8') as cf:
                        return json.loads(cf.read())

        headers = {
            'Accept-Encoding': 'gzip, deflate, br',
            'Authorization' : "Basic "+self.login_password,
            'Accept': 'application/json',
            'Host': self.host,
            #'Connection': 'Keep-Alive',
            'User-Agent': '007',
        }
        _logger.info("Calling "+ self.url_appel_api+target_action)
        response = requests.get(self.url_appel_api+target_action, headers=headers)
        response_code = response.status_code
        _logger.info("HTTP return code :" + str(response_code))
        #res = response.content.decode('utf-8')
        #j = json.load(res)
        res = response.json()
        if cache_mode :
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(res, f, indent=4)
        return res


class fitnetPartner(models.Model):
    _inherit = "res.partner"
    _sql_constraints = [
        ('fitnet_id_uniq', 'UNIQUE (fitnet_id)',  "Impossible d'enregistrer deux objets avec le même Fitnet ID.")
    ]
    fitnet_id = fields.Char("Fitnet ID")

class fitnetProjectStage(models.Model):
    _inherit = "project.project.stage"
    _sql_constraints = [
        ('fitnet_id_uniq', 'UNIQUE (fitnet_id)',  "Impossible d'enregistrer deux objets avec le même Fitnet ID.")
    ]
    fitnet_id = fields.Char("Fitnet ID")

class fitnetEmployee(models.Model):
    _inherit = "hr.employee"
    _sql_constraints = [
        ('fitnet_id_uniq', 'UNIQUE (fitnet_id)',  "Impossible d'enregistrer deux objets avec le même Fitnet ID.")
    ]
    fitnet_id = fields.Char("Fitnet ID")

class fitnetAnalyticLine(models.Model):
    _inherit = 'account.analytic.line'
    _sql_constraints = [
        ('fitnet_id_uniq', 'UNIQUE (fitnet_id)',  "Impossible d'enregistrer deux objets avec le même Fitnet ID.")
    ]
    fitnet_id = fields.Char("Fitnet ID")

class fitnetNeed(models.Model):
    _inherit = "staffing.need"
    _sql_constraints = [
        ('fitnet_id_uniq', 'UNIQUE (fitnet_id)',  "Impossible d'enregistrer deux objets avec le même Fitnet ID.")
    ]
    fitnet_id = fields.Char("Fitnet ID")

class fitnetLeave(models.Model):
    _inherit = "hr.leave"
    _sql_constraints = [
        ('fitnet_id__uniq', 'UNIQUE (fitnet_id)',  "Impossible d'enregistrer deux objects avec le même Fitnet ID.")
    ]
    fitnet_id = fields.Char("Fitnet ID")

class fitnetLeaveType(models.Model):
    _inherit = "hr.leave.type"
    _sql_constraints = [
        ('fitnet_id__uniq', 'UNIQUE (fitnet_id)',  "Impossible d'enregistrer deux objects avec le même Fitnet ID.")
    ]
    fitnet_id = fields.Char("Fitnet ID")

class fitnetProject(models.Model):
    _inherit = "project.project"
    _sql_constraints = [
        ('fitnet_id__uniq', 'UNIQUE (fitnet_id)',  "Impossible d'enregistrer deux objects avec le même Fitnet ID.")
    ]
    fitnet_id = fields.Char("Fitnet ID")

    def synchAllFitnet(self):
        login_password = self.env['ir.config_parameter'].sudo().get_param("fitnet_login_password") 
        client = ClientRestFitnetManager(proto, host, api_root, login_password)

        self.sync_employees(client)
        self.sync_customers(client)

        #TODO           self.sync_prospect(client)
        #TODO           self.sync_project(client)

        self.sync_contracts(client)

        #self.sync_assignments(client)
        #self.sync_forecastedActivities(client)
        #self.sync_timesheets(client)
        
        #self.sync_holidays(client)

        #TODO           self.sync_assignmentsoffContract(client)
        #TODO           self.sync_offContractActivities(client)


    def sync_holidays(self, client):
        _logger.info('---- sync_holydays')
        odoo_model_name = 'hr.leave'
        for year in range(2020, 2024):
            for month in range(1,13):
                _logger.info('Get leaves for %s/%s' % (str(month), str(year)))
                fitnet_objects = client.get_api('leaves/getLeavesWithRepartition/1/%s/%s' % (month, year))
                #_logger.info(len(fitnet_objects))
                _logger.info(fitnet_objects)
                if fitnet_objects is list : #this id False when there is no leaves for a month
                    fitnet_leave_contents = []
                    for obj in fitnet_objects:
                        for leaveType in obj['leaveTypes']:
                            leaveType['master_fitnet_leave_id'] = obj['leaveId']
                            leaveType['designation'] = obj['designation']
                            leaveType['employeeId'] = obj['employeeId']
                            leaveType['status'] = obj['status']
                            if leaveType['startMidday'] == True:
                                leaveType['request_date_from_period'] = 'pm'
                            else :
                                leaveType['request_date_from_period'] = 'am'
                            if leaveType['endMidday'] == True:
                                leaveType['request_date_to_period'] = 'am'
                            else :
                                leaveType['request_date_to_period'] = 'pm'
                            fitnet_leave_contents.append(leaveType)

                    mapping_fields = {
                        'designation' : {'odoo_field' : 'name'},
                        'employeeId' : {'odoo_field' : 'employee_id'},
                        'typeId' : {'odoo_field' : 'holiday_status_id'},
                        'beginDate' : {'odoo_field' : 'date_from'},
                        'endDate' : {'odoo_field' : 'date_to'},
                        'request_date_from_period' : {'odoo_field' : 'request_date_from_period', 'selection_mapping' : {'am' : 'am', 'pm':'pm'}},
                        'request_date_to_period' : {'odoo_field' : 'request_date_to_period', 'selection_mapping' : {'am' : 'am', 'pm':'pm'}},
                        'status' : {'odoo_field' : 'state', 'selection_mapping' : {"Demande accordée" : 'validate'}},
                        }
                    #self.create_overide_by_fitnet_values(odoo_model_name, fitnet_leave_contents, mapping_fields, 'id')



    def sync_timesheets(self, client):
        _logger.info('---- sync_timesheets')
        #fitnet_objects = client.get_api("activities/getActivitiesOnContract/1/01-01-2018/01-01-2050") #=> endpoint unitile : il ne comporte pas l'assignementID
        fitnet_objects = client.get_api("timesheet/timesheetAssignment?companyId=1&startDate=01-01-2018&endDate=01-06-2032")
        fitnet_filtered = []
        for obj in fitnet_objects:
            if obj['amount'] == 0.0 : 
                continue
            if obj['activityType'] != 1:
                #Activity type: 1:Contracted activity, 2:Off-Contract activity, 3:Training 
                #TODO : réintégrer les types 2 quand self.sync_assignmentsoffContract sera implémentée
                continue

            #TODO : comment exclure les pointages des semaines non validées, pour éviter de cumuler pointages non validés et forecast sur une même semaine
            obj['category'] = 'project_employee_validated'
            obj['fitnet_id'] = 'timesheet_' + str(obj['timesheetAssignmentID'])
            fitnet_filtered.append(obj)

        _logger.info(len(fitnet_objects))
        _logger.info(len(fitnet_filtered))

        odoo_model_name = 'account.analytic.line'

        mapping_fields = {
            'assignmentID' : {'odoo_field' : 'staffing_need_id'},
            'assignmentDate' : {'odoo_field' : 'date'},
            'amount' : {'odoo_field' : 'unit_amount'},
            'category' : {'odoo_field' : 'category', 'selection_mapping' : {'project_employee_validated' : 'project_employee_validated'}},
            }
        self.create_overide_by_fitnet_values(odoo_model_name, fitnet_filtered, mapping_fields, 'fitnet_id')

    def sync_forecastedActivities(self, client):
        _logger.info('---- sync_forecastedActivities')
        fitnet_objects = client.get_api("forecastedActivities") #TODO : pour éviter les risques d'incohérence utiliser la même ressourcque que sync_timesheets et moduler le filtre
        for obj in fitnet_objects:
            obj['category'] = 'project_forecast'
            obj['fitnet_id'] = 'forecastedActivityAssigment_' + str(obj['forecastedActivityAssigmentId'])
        odoo_model_name = 'account.analytic.line'
        mapping_fields = {
            'assigmentId' : {'odoo_field' : 'staffing_need_id'},
            'date' : {'odoo_field' : 'date'},
            'forecastedAmount' : {'odoo_field' : 'unit_amount'},
            'category' : {'odoo_field' : 'category', 'selection_mapping' : {'project_forecast' : 'project_forecast'}},
            }
        self.create_overide_by_fitnet_values(odoo_model_name, fitnet_objects, mapping_fields, 'fitnet_id')

    def sync_assignments(self, client):
        _logger.info('---- sync_assignments')
        fitnet_objects = client.get_api("assignments?companyId=1&startDate=01-01-2018&endDate=31-12-2040")
        for obj in fitnet_objects:
            obj['status'] = 'done'

        mapping_fields = {
            'assignmentStartDate' : {'odoo_field' : 'begin_date'},
            'assignmentEndDate' : {'odoo_field' : 'end_date'},
            'contractID' : {'odoo_field' : 'project_id'}, 
            'employeeID' : {'odoo_field' : 'staffed_employee_id'}, 
            'status' : {'odoo_field' : 'state', 'selection_mapping' : {'done' : 'done'}},
            }
        odoo_model_name = 'staffing.need'
        self.create_overide_by_fitnet_values(odoo_model_name, fitnet_objects, mapping_fields, 'assignmentOnContractID')

    def sync_customers(self, client):
        _logger.info('---- sync_customers')
        customers = client.get_api("customers/1")
        for customer in customers:
            odoo_customer = self.env['res.partner'].search([('fitnet_id', '=', customer['clientId']), ('is_company', '=', True)])
            if len(odoo_customer) > 1 :
                #_logger.info("Plus d'un res.partner pour cet id client fitnet")
                continue
            if len(odoo_customer) == 0:
                odoo_customer = self.env['res.partner'].search([('ref', '=ilike', customer['clientCode']), ('is_company', '=', True), ('fitnet_id', '=', False)])
                if len(odoo_customer) > 1 :
                    #_logger.info("Plus d'un res.partner pour cet ref %s" % customer['clientCode'])
                    continue
                if len(odoo_customer) == 0:
                    odoo_customer = self.env['res.partner'].search([('name', '=ilike', customer['clientCode']), ('is_company', '=', True), ('fitnet_id', '=', False)])
                    if len(odoo_customer) > 1 :
                        #_logger.info("Plus d'un res.partner pour ce nom %s" % customer['clientCode'])
                        continue
                    if len(odoo_customer) == 0:
                        _logger.info("Aucun res.partner Odoo pour FitnetID=%s / Fitnet name=%s" % (customer['clientId'], customer['name']))
                        continue
                #get FitnetID
                odoo_customer.fitnet_id = customer['clientId']
                _logger.info("Intégration de l'ID Fitnet pour le res.partner : Odoo ID=%s / Odoo name=%s / FitnetID=%s / Fitnet name=%s" % (odoo_customer.id, odoo_customer.name, customer['clientId'], customer['name']))

            #TODO : importer les autres champs de fitnet
               
    def sync_employees(self, client):
        _logger.info('--- synch_employees')
        employees = client.get_api("employees/1")
        _logger.info('nb employees ' + str(len(employees)))

        #Intégrer l'id Fitnet aux hr.employee si on peut le trouver via l'email du hr.employee ou bien d'un res_users (et dans ce cas création du hr.employee à la volée)
        for employee in employees:
            odoo_employee = self.env['hr.employee'].search([('fitnet_id','=',employee['employee_id'])])
            if len(odoo_employee) > 1 :
                _logger.info("Plus d'un hr.employee a cet id fitnet %s" % str(employee['employee_id']))
                continue
            if len(odoo_employee) == 0 :
                _logger.info(employee['name'])
                _logger.info(employee['email'])
                if not employee['email']:
                    _logger.info("Pas d'email sur Fitnet")
                    continue
                #intégrer l'ID Fitnet au hr.employee
                odoo_employee = self.env['hr.employee'].search([('work_email','=',employee['email']), ('fitnet_id', '=', False)])
                if len(odoo_employee) > 1 :
                    _logger.info("Erreur : plusieurs hr.employee on ce work_email : %s" % employee['email'])
                    continue
                if len(odoo_employee) == 0:
                    #créer l'employé Odoo s'il existe un user Odoo qui porte le même identifiant
                    odoo_user = self.env['res.users'].search([('login','=',employee['email']), ('employee_id','=',False)])
                    if len(odoo_user) == 1:
                        odoo_user.action_create_employee()
                        _logger.info("Création de l'employée depuis l'utilsiateur avec le login=%s" % odoo_user.login)
                        odoo_employee = odoo_user.employee_id
                    else :
                        _logger.info("Aucun hr.employee ni res.users Odoo pour FitnetID=%s / Fitnet email=%s" % (employee['employee_id'],employee['email']))
                        continue
                odoo_employee.fitnet_id = employee['employee_id']
                _logger.info("Intégration de l'ID Fitnet pour le hr.employee :  Odoo ID=%s / Odoo name=%s / FitnetID=%s / Fitnet name=%s" % (odoo_employee.id, odoo_employee.name, employee['employee_id'], employee['name']))
        
        #Pour les employee qui existent sur Fitnet mais pas sur Odoo (exemple : anciens tasmaniens) : on crée le hr.employee mais sans res_user associé
            #TODO : créer le res_user associé si la date d'entrée fitnet > date du jour et que date de sortie non défini ou > date du jour
        # ... puis mettre à jour les valeurs des employés Odoo
        mapping_fields = {
            'name' : {'odoo_field' : 'name'},
            'surname' : {'odoo_field' : 'first_name'},
            'email' : {'odoo_field' : 'work_email'},
            'gender' : {'odoo_field' : 'gender', 'selection_mapping' : {'Male' : 'male', 'Female' : 'female'}},
            #registration_id
            #hiringDate => attribut du hr.contract
            #leavingDate => attribut du hr.contract
            #foreignEmployee
            #address
            #zone_23_key_P_1-S_1 #Champ onDemande pour l'email personnel
            #zone_23_key_P_268-S_1 #Champ onDemande pour le mobile personnel
            }
        odoo_model_name = 'hr.employee'
        self.create_overide_by_fitnet_values(odoo_model_name, employees, mapping_fields, 'employee_id')




    def sync_contracts(self, client):
        _logger.info('---- sync_contracts')
        mapping_fields = {
            'title' : {'odoo_field' : 'name'},
            'customerId' : {'odoo_field' : 'partner_id'},
            'beginDate' : {'odoo_field' : 'date_start'},
            'endDate' : {'odoo_field' : 'date'},
            'contractNumber' : {'odoo_field' : 'number'},
            'contractAmount' : {'odoo_field' : 'order_amount'},
            'is_purchase_order_received' : {'odoo_field' : 'is_purchase_order_received'},
            'contractCategoryId' : {
                'odoo_field' : 'outsourcing', 
                'selection_mapping':
                    {
                        '0' : False,
                        '1' : 'no-outsourcing', #Sans Sous-Traitance
                        '2' : 'direct-paiement-outsourcing', #Sous-Traitance paiement direct
                        '3' : 'outsourcing', #Sous-Traitance paiement Tasmane
                        '4' : 'direct-paiement-outsourcing-company', #Sous-Traitance paiement direct + Tasmane
                        '5' : 'co-sourcing', #Avec Cotraitance
                    },
                },
            'remark' : {'odoo_field' : 'remark'},
        #    'description' : {'odoo_field' : 'description'},
            'orderNumber' : {'odoo_field' : 'purchase_order_number'},
            'billedAmount' : {'odoo_field' : 'billed_amount'},
            'payedAmount' : {'odoo_field' : 'payed_amount'},
            'status' : {'odoo_field' : 'stage_id'},
            'project_director_employee_id' : {'odoo_field' : 'project_director_employee_id'},
            'commercialStatusID' : {
                'odoo_field' : 'probability', 
                'selection_mapping':
                    { 
                        '0' : False,
                        '2' : '30',
                        '3' : '70',
                        '5' : '100',
                        '9' : '0',
                    },
                },
            }
        odoo_model_name = 'project.project'
        fitnet_objects = client.get_api("contracts/1")

        for obj in fitnet_objects:
            # Transco de la liste déroulante Bon de commmande reçu en un booléen sur Odoo
            if self.get_proprieteOnDemand_by_id(obj, "zone_13_key_P_1-S_1")  == "Reçu":
                obj['is_purchase_order_received'] = True
            else:
                obj['is_purchase_order_received'] = False
        

            # Recherche du res.user Odoo qui correspond au DM de la mission
            comList = obj['affectedCommercialsList']
            fitnet_employee_id = None
            if len(comList) == 1 :
                fitnet_employee_id = comList[0]['employeeId'] 
            else :
                if len(comList) > 1 :
                    for commercial in comList:
                        if commercial['fullName'] == obj['contractCreator']:
                            fitnet_employee_id = commercial['employeeId']
                    if fitnet_employee_id == None :
                        fitnet_employee_id = comList[0]['employeeId']
            obj['project_director_employee_id'] = fitnet_employee_id

        self.create_overide_by_fitnet_values(odoo_model_name, fitnet_objects, mapping_fields, 'contractId')


    def get_proprieteOnDemand_by_id(self, fitnet_object, prop_id):
        res = None
        for prop in fitnet_object['proprieteOnDemand']:
            if prop['id'] == prop_id:
                res = prop['value']
        return res

    def create_overide_by_fitnet_values(self, odoo_model_name, fitnet_objects, mapping_fields, fitnet_id_fieldname) :
        _logger.info('--- create_overide_by_fitnet_values')

        count_last_sql_commit = 0
        for fitnet_object in fitnet_objects: 
            count_last_sql_commit += 1 
            if count_last_sql_commit % 1000 == 0:
                _logger.info('######## SQL COMMIT')
                self.env.cr.commit()
            #### chercher l'objet et le créer s'il n'existe pas
            fitnet_id = fitnet_object[fitnet_id_fieldname]
            odoo_objects = self.env[odoo_model_name].search([('fitnet_id', '=', fitnet_id)])
            odoo_object = False
            if len(odoo_objects) > 1:
                continue
            if len(odoo_objects) == 1 :
                odoo_object = odoo_objects[0]
                res = self.prepare_update_from_fitnet_values(odoo_model_name, fitnet_object, mapping_fields, odoo_object)
                if len(res) > 0:
                    _logger.info("Mise à jour de l'objet %s ID= %s avec les valeurs de Fitnet %s" % (odoo_model_name, str(odoo_object.id), str(res)))
                    odoo_object.write(res)
            if len(odoo_objects) == 0 :
                dic = self.prepare_update_from_fitnet_values(odoo_model_name, fitnet_object, mapping_fields)
                dic['fitnet_id'] = fitnet_id
                odoo_object = self.env[odoo_model_name].create(dic)
                _logger.info("Create Odoo instance of %s object for fitnet %s=%s with values %s" % (odoo_model_name, fitnet_id_fieldname, fitnet_id, str(dic)))
            #if not c:
            #    continue
        _logger.info('######## FINAL SQL COMMIT')
        self.env.cr.commit()


    def prepare_update_from_fitnet_values(self, odoo_model_name, fitnet_object, mapping_fields, odoo_object=False) :
            #_logger.info('--- prepare_update_from_fitnet_values')
            #### mise à jour depuis Fitnet
            models = self.env['ir.model'].search([('model','=',odoo_model_name)])
            if len(models) != 1:
                _logger.info("Objet non trouvé %s." % odoo_model_name)
                return False
            model = models[0]

            res = {}
            for fitnet_field_name, odoo_dic in mapping_fields.items():
                #_logger.info('fitnet_field_name %s' % fitnet_field_name)
                odoo_field_name = odoo_dic['odoo_field']
                odoo_field = self.env['ir.model.fields'].search([('model_id', '=', model.id), ('name', '=', odoo_field_name)])[0]
                odoo_value = None

                if fitnet_field_name in fitnet_object.keys():
                    fitnet_value = fitnet_object[fitnet_field_name]
                else : 
                    onDemand = self.get_proprieteOnDemand_by_id(fitnet_object, fitnet_field_name)
                    if onDemand is not None:
                        fitnet_value = onDemand
                    else :
                        _logger("Champ inexistant dans l'objet dans l'objet Fitnet %s" % fitnet_field_name)

                if odoo_field.ttype in ["char", "html", "text", "date", "float", "integer", "boolean", "selection"]  :
                    if fitnet_value == None:
                        fitnet_value = False
                    odoo_value = fitnet_value

                    if odoo_field.ttype in ["date"]  :
                        odoo_value = datetime.datetime.strptime(fitnet_value, '%d/%m/%Y').date()

                    if odoo_field.ttype in ["float"] :
                        odoo_value = float(fitnet_value)

                    if odoo_field.ttype in ["integer"] :
                        odoo_value = int(fitnet_value)

                    if odoo_field.ttype in ["boolean"] :
                        odoo_value = bool(fitnet_value)

                    if odoo_field.ttype in ["selection"] :
                        odoo_value = odoo_dic['selection_mapping'][str(fitnet_value)]

                    if odoo_field.ttype in ["html"] :
                        if fitnet_value and len(fitnet_value.strip())>0: 
                            #TODO : cette conversion ne donne pas le bon encodage => les commentaires avect des accent sont toujours raffraichis, même si Odoo a déjà la bonne valeur
                            html_fitnet = html.tostring(html.fromstring(fitnet_value)).decode('utf-8')
                            #_logger.info(html_fitnet)
                            odoo_value = html_fitnet

                            #html_fitnet5 = html.tostring(html.fromstring(fitnet_value.encode('utf-8'))).decode('utf-8')
                            #_logger.info(html_fitnet5)
                            #html_fitnet4 = html.tostring(html.fromstring(fitnet_value.encode('utf-8')), encoding='utf-8').decode('utf-8')
                            #_logger.info(html_fitnet4)
                            #html_fitnet3 = html.tostring(html.fromstring(fitnet_value, parser=html.HTMLParser(encoding='utf-8'))).decode('utf-8')
                            #_logger.info(html_fitnet3)
                            #html_fitnet2 = html.tostring(html.fromstring(fitnet_value))
                            #_logger.info(html_fitnet2)

                            #html_odoo =  html.tostring(odoo_object[odoo_field_name])
                            #if html_fitnet == html_odoo:
                            #    odoo_value = odoo_object[odoo_field_name]

                    if odoo_object :
                        if odoo_object[odoo_field_name] != odoo_value:
                            #_logger.info(odoo_object[odoo_field_name])
                            #_logger.info(odoo_value)
                            res[odoo_field_name] = odoo_value
                    else :
                            res[odoo_field_name] = odoo_value


                if odoo_field.ttype == "many2one" :
                    if fitnet_value == None : #le champ manu2one était valorisé sur Fitnet, mais a été remis à blanc sur Fitnet
                        if odoo_object :
                            if odoo_object[odoo_field_name] :
                                res[odoo_field_name] = False
                                _logger.info('AAAAA '+str(fitnet_value)+"  "+str(type(odoo_object[odoo_field_name])))
                        else:
                            res[odoo_field_name] = False
                        continue

                    target_objects = self.env[odoo_field.relation].search([('fitnet_id','=',fitnet_value)])
                    if len(target_objects) > 1 :
                        _logger.info("Plusieurs objets Odoo %s ont le fitnet_id %s" % (odoo_field.relation, fitnet_value))
                        continue
                    if len(target_objects) == 1 :
                        target_object = target_objects[0]
                        odoo_value = target_object.id
                        if odoo_object :
                            if odoo_object[odoo_field_name] != target_object:
                                res[odoo_field_name] = odoo_value
                        else :
                            res[odoo_field_name] = odoo_value
                    if len(target_objects) == 0 :
                        _logger.info("Erreur : aucun objet %s n'a de fitnet_id valorisé à %s" % (odoo_field.relation, fitnet_value))
                        continue
                #écraser la valeur Odoo par la valeur Fitnet si elles sont différentes
                if odoo_value is None:
                    _logger.info("Type non géré pour le champ Fitnet %s = %s" % (fitnet_field_name, fitnet_value))
                    continue

            return res
