# Modèles prévus (voir SPECIFICATION.md, §5) :
#   notify_type           registre des types de notification (libellé du bouton, cible de l'URL)
#   notify_teams_message  file d'attente des envois
#   res_users             troisième valeur de notification_type (« teams »), identifiant Teams
#   mail_notification     valeur « teams » de la trace d'envoi
#   mail_thread           routage vers Teams et _notify_thread_by_teams
#   res_config_settings   transport, URL, secret, reprises
