# Copyright (C) 2020 - Iván Todorovich <ivan.todorovich@gmail.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

from openupgradelib import openupgrade
from psycopg2 import sql


def migrate_mass_editing(env):
    """Migrates mass.editing to ir.actions.server"""
    # Create new context actions
    env.cr.execute(
        sql.SQL(
            """
        SELECT sa.id FROM ir_act_server sa
        INNER JOIN mass_editing me
        ON sa.{} = me.id
        AND sa.state = 'mass_edit'
        AND me.ref_ir_act_window_id IS NOT NULL
        """
        ).format(sql.Identifier(openupgrade.get_legacy_name("mass_editing_id")))
    )
    server_action_ids = [r[0] for r in env.cr.fetchall()]
    if server_action_ids:
        env["ir.actions.server"].browse(server_action_ids).create_action()
    # Remove previous context actions
    env["ir.actions.act_window"].search(
        [("res_model", "=", "mass.editing.wizard")]
    ).unlink()
    # Unset the 'inherit_id' value of the 'view_mass_editing_wizard_form'
    # that still target a view from 'mass_operation_abstract'.
    # When 'mass_operation_abstract' is uninstalled + the database cleanup
    # (with 'database_cleanup'), the wizard of 'mass_editing' gets purged
    # in cascade because of this link.
    env.ref("mass_editing.view_mass_editing_wizard_form").inherit_id = False


@openupgrade.migrate()
def migrate(env, version):
    if openupgrade.table_exists(env.cr, "mass_editing"):
        migrate_mass_editing(env)
