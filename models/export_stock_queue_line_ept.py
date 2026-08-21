import time
import json
import logging
import pytz
import uuid
from odoo import models, fields

from .. import shopify
from .shopify_inventory_utils import (
    INVENTORY_SET_QUANTITIES_MUTATION,
    chunked,
    inventory_user_error_indexes,
    prepare_inventory_set_input,
)

utc = pytz.utc

_logger = logging.getLogger("Shopify Export Stock Queue Line")


class ShopifyExportStockQueueLineEpt(models.Model):
    _name = "shopify.export.stock.queue.line.ept"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _description = "Shopify Export Stock Queue Line"

    name = fields.Char()
    shopify_instance_id = fields.Many2one(
        "shopify.instance.ept", string="Instance", index=True)
    last_process_date = fields.Datetime()
    inventory_item_id = fields.Char(index=True)
    location_id = fields.Char(index=True)
    quantity = fields.Integer()
    shopify_product_id = fields.Many2one(
        'shopify.product.product.ept', string="Product", index=True)
    state = fields.Selection([("draft", "Draft"), ("failed", "Failed"), ("done", "Done"),
                              ("cancel", "Cancelled")],
                             default="draft", index=True)
    export_stock_queue_id = fields.Many2one("shopify.export.stock.queue.ept", required=True,
                                            ondelete="cascade", copy=False, index=True)
    common_log_lines_ids = fields.One2many("common.log.lines.ept",
                                           "shopify_export_stock_queue_line_id",
                                           help="Log lines created against which line.")


    def auto_export_stock_queue_data(self):
        """
        This method is used to find export stock queue which queue lines have state
        in draft and is_action_require is False.
        @author: Nilam Kubavat @Emipro Technologies Pvt.Ltd on date 31-Aug-2022.
        Task Id : 199065
        """
        export_stock_queue_obj = self.env["shopify.export.stock.queue.ept"]
        export_stock_queue_ids = []
        query = """
            UPDATE shopify_export_stock_queue_ept
            SET is_process_queue = %s
            WHERE is_process_queue = %s
        """
        params = (False, True)

        self.env.cr.execute(query, params)
        self._cr.commit()
        query = """
            SELECT DISTINCT queue.id
            FROM shopify_export_stock_queue_line_ept AS queue_line
            INNER JOIN shopify_export_stock_queue_ept AS queue
            ON queue_line.export_stock_queue_id = queue.id
            WHERE queue_line.state IN (%s) AND queue.is_action_require = %s
            GROUP BY queue.id
            ORDER BY queue.id
        """
        params = ('draft', False)

        self._cr.execute(query, params)

        export_stock_queue_list = self._cr.fetchall()
        if not export_stock_queue_list:
            return True

        export_stock_queue_ids = [result[0] for result in export_stock_queue_list]
        # for result in export_stock_queue_list:
        #     if result[0] not in export_stock_queue_ids:
        #         export_stock_queue_ids.append(result[0])

        queues = export_stock_queue_obj.browse(export_stock_queue_ids)
        self.filter_export_stock_queue_lines_and_post_message(queues)

    def filter_export_stock_queue_lines_and_post_message(self, queues):
        """
        This method is used to post a message if the queue is process more than 3 times otherwise
        it calls the child method to process the export stock queue line.
        @author: Nilam Kubavat @Emipro Technologies Pvt.Ltd on date 31-Aug-2022.
        Task Id : 199065
        """
        common_log_line_obj = self.env["common.log.lines.ept"]
        start = time.time()
        export_stock_queue_process_cron_time = queues.shopify_instance_id.get_shopify_cron_execution_time(
            "shopify_ept.process_shopify_export_stock_queue")

        for queue in queues:
            export_stock_queue_line_ids = queue.export_stock_queue_line_ids.filtered(lambda x: x.state == "draft")

            # For counting the queue crashes and creating schedule activity for the queue.
            queue.queue_process_count += 1
            if queue.queue_process_count > 3:
                queue.is_action_require = True
                note = "<p>Need to process this export stock queue manually.There are 3 attempts been made by " \
                       "automated action to process this queue,<br/>- Ignore, if this queue is already processed.</p>"
                queue.message_post(body=note)
                if queue.shopify_instance_id.is_shopify_create_schedule:
                    common_log_line_obj.create_crash_queue_schedule_activity(queue, "shopify.export.stock.queue.ept",
                                                                             note)
                continue

            self._cr.commit()
            export_stock_queue_line_ids.process_export_stock_queue_data()
            if time.time() - start > export_stock_queue_process_cron_time - 60:
                return True

    def process_export_stock_queue_data(self):
        """
        Process absolute inventory updates in GraphQL batches of up to 250 rows.
        @author: Nilam Kubavat @Emipro Technologies Pvt.Ltd on date 31-Aug-2022.
        Task Id : 199065
        """
        draft_lines = self.filtered(lambda line: line.state == "draft")
        for queue in draft_lines.mapped("export_stock_queue_id"):
            queue_lines = draft_lines.filtered(
                lambda line: line.export_stock_queue_id == queue)
            instance = queue.shopify_instance_id
            instance.connect_in_shopify()
            queue.is_process_queue = True
            self._cr.commit()
            for line_batch in chunked(queue_lines):
                batch_lines = self.browse([line.id for line in line_batch])
                try:
                    self._process_inventory_batch(instance, queue, batch_lines)
                except Exception as error:
                    self._fail_inventory_lines(instance, batch_lines, str(error))
            queue.is_process_queue = False
            self._cr.commit()
        return True

    def _process_inventory_batch(self, instance, queue, batch_lines):
        changes = [{
            "inventory_item_id": line.inventory_item_id,
            "location_id": line.location_id,
            "quantity": line.quantity,
        } for line in batch_lines]
        mutation_input = prepare_inventory_set_input(
            changes,
            "odoo://shopify/inventory-export/%s" % queue.id,
        )
        response = self._execute_inventory_mutation(
            mutation_input, str(uuid.uuid4()))
        if response.get("errors"):
            raise RuntimeError(json.dumps(response["errors"]))

        mutation_result = response.get("data", {}).get("inventorySetQuantities")
        if mutation_result is None:
            raise RuntimeError("Shopify returned no inventorySetQuantities result")
        failed_indexes, batch_errors = inventory_user_error_indexes(
            mutation_result.get("userErrors"))
        if batch_errors:
            detail = "; ".join(error.get("message", str(error)) for error in batch_errors)
            self._fail_inventory_lines(instance, batch_lines, detail)
            return

        successful_lines = self.browse()
        tracking_disabled_lines = self.browse()
        for index, line in enumerate(batch_lines):
            errors = failed_indexes.get(index)
            if not errors:
                successful_lines |= line
                continue
            detail = "; ".join(error.get("message", str(error)) for error in errors)
            if "inventory tracking enabled" in detail.lower():
                tracking_disabled_lines |= line
            else:
                self._fail_inventory_lines(instance, line, detail)

        if successful_lines:
            successful_lines.write({
                "state": "done",
                "last_process_date": fields.Datetime.now(),
            })
            successful_lines._record_successful_inventory_exports()
        if tracking_disabled_lines:
            tracking_disabled_lines.shopify_product_id.write({
                "inventory_management": "Dont track Inventory",
            })
            tracking_disabled_lines.write({
                "state": "done",
                "last_process_date": fields.Datetime.now(),
            })

    def _execute_inventory_mutation(self, mutation_input, idempotency_key):
        for attempt in range(3):
            try:
                raw_response = shopify.GraphQL().execute(
                    INVENTORY_SET_QUANTITIES_MUTATION,
                    variables={
                        "input": mutation_input,
                        "idempotencyKey": idempotency_key,
                    },
                    operation_name="InventorySet",
                )
            except Exception as error:
                if getattr(error, "code", None) == 429 and attempt < 2:
                    retry_after = getattr(error, "headers", {}).get("Retry-After", 2 ** attempt)
                    time.sleep(float(retry_after))
                    continue
                raise

            response = json.loads(raw_response)
            errors = response.get("errors") or []
            throttled = errors and all(
                error.get("extensions", {}).get("code") == "THROTTLED"
                for error in errors
            )
            if throttled and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return response

        raise RuntimeError("Shopify inventory mutation remained throttled after retries")

    def _record_successful_inventory_exports(self):
        state_model = self.env["shopify.inventory.export.state.ept"].sudo()
        states = state_model.search([
            ("shopify_instance_id", "in", self.shopify_instance_id.ids),
            ("shopify_product_id", "in", self.shopify_product_id.ids),
            ("location_id", "in", list(set(self.mapped("location_id")))),
        ])
        states_by_key = {
            (state.shopify_instance_id.id, state.shopify_product_id.id, state.location_id): state
            for state in states
        }
        exported_at = fields.Datetime.now()
        values_to_create = []
        for line in self:
            key = (line.shopify_instance_id.id, line.shopify_product_id.id, line.location_id)
            values = {
                "inventory_item_id": line.inventory_item_id,
                "quantity": line.quantity,
                "last_exported_at": exported_at,
            }
            state = states_by_key.get(key)
            if state:
                state.write(values)
            else:
                values.update({
                    "shopify_instance_id": line.shopify_instance_id.id,
                    "shopify_product_id": line.shopify_product_id.id,
                    "location_id": line.location_id,
                })
                values_to_create.append(values)
        if values_to_create:
            state_model.create(values_to_create)

    def _fail_inventory_lines(self, instance, lines, error_detail):
        common_log_line_obj = self.env["common.log.lines.ept"]
        for line in lines:
            message = self.prepare_export_stock_error_message(instance, line, error_detail)
            common_log_line_obj.create_common_log_line_ept(
                shopify_instance_id=instance.id,
                module="shopify_ept",
                message=message,
                model_name="shopify.export.stock.queue.ept",
                shopify_export_stock_queue_line_id=line.id,
            )
        lines.write({
            "state": "failed",
            "last_process_date": fields.Datetime.now(),
        })

    def prepare_export_stock_error_message(self, instance, queue_line, error):
        """
        Prepare a log message for a failed export stock queue line, including the variant's
        identifiers and the actual error returned by Shopify.
        """
        shopify_product = queue_line.shopify_product_id
        odoo_product = shopify_product.product_id
        error_detail = str(error)
        if hasattr(error, "response") and error.response is not None:
            error_detail = "%s %s" % (error.response.code, error.response.msg)
            body = getattr(error.response, "body", False)
            if body:
                try:
                    error_detail += " - %s" % json.loads(body.decode()).get("errors")
                except (ValueError, AttributeError):
                    error_detail += " - %s" % body
        message = ("System tried to export stock but received an error from the Shopify store for the %s instance.\n"
                   "Product: %s (SKU: %s, Odoo Variant ID: %s)\n"
                   "Shopify Variant ID: %s, Inventory Item ID: %s, Location ID: %s, Quantity: %s\n"
                   "Shopify Error: %s\n"
                   "Action Items:\n"
                   "- Verify the variant's existence on the Shopify store using the Shopify Variant ID.\n"
                   "- If it has been deleted, archive the product from the Shopify product layer "
                   "in Odoo.") % (instance.name, odoo_product.display_name, shopify_product.default_code,
                                  odoo_product.id, shopify_product.variant_id, queue_line.inventory_item_id,
                                  queue_line.location_id, queue_line.quantity, error_detail)
        return message
