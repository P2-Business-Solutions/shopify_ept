import time
import json
import logging
import pytz
import uuid
from odoo import models, fields
from odoo.exceptions import UserError
from psycopg2 import OperationalError
from urllib.error import HTTPError, URLError

from .. import shopify
from .shopify_inventory_utils import (
    INVENTORY_SET_QUANTITIES_MUTATION,
    INVENTORY_LEVELS_QUERY,
    INVENTORY_ACTIVATE_MUTATION,
    chunked,
    inventory_user_error_indexes,
    prepare_inventory_set_input,
    shopify_gid,
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
        start = time.monotonic()
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
            export_stock_queue_line_ids.with_context(
                inventory_export_deadline=start + min(240, max(30, export_stock_queue_process_cron_time - 60)),
            ).process_export_stock_queue_data()
            if time.monotonic() - start > min(240, max(30, export_stock_queue_process_cron_time - 60)):
                return True

    def process_export_stock_queue_data(self):
        """
        Process absolute inventory updates in GraphQL batches of up to 250 rows.
        @author: Nilam Kubavat @Emipro Technologies Pvt.Ltd on date 31-Aug-2022.
        Task Id : 199065
        """
        deadline = self.env.context.get("inventory_export_deadline", time.monotonic() + 240)
        self = self.with_context(inventory_export_deadline=deadline)
        pending_lines = self.filtered(lambda line: line.state in ("draft", "failed"))
        for queue in pending_lines.mapped("export_stock_queue_id"):
            queue_lines = pending_lines.filtered(
                lambda line: line.export_stock_queue_id == queue)
            instance = queue.shopify_instance_id
            instance.connect_in_shopify()
            # Each batch has its own transaction. The shared instance lock also
            # serializes manual processing and queue coalescing with the cron.
            self._cr.commit()
            retry_prepared = False
            for line_batch in chunked(queue_lines):
                if time.monotonic() >= deadline:
                    return True
                self._cr.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", (741913, instance.id))
                if not self._cr.fetchone()[0]:
                    self._cr.rollback()
                    return True
                batch_lines = self.browse([line.id for line in line_batch])
                batch_lines.invalidate_recordset()
                batch_lines = batch_lines.filtered(lambda line: line.state in ("draft", "failed"))
                queue.invalidate_recordset()
                # Release a manually reviewed queue, leaving unprocessed rows
                # available to the next cron if this invocation reaches its budget.
                queue.write({"is_process_queue": False, "is_action_require": False, "queue_process_count": 0})
                if not retry_prepared:
                    queue_lines.invalidate_recordset(["state"])
                    queue_lines.filtered(lambda line: line.state == "failed").write({"state": "draft"})
                    retry_prepared = True
                try:
                    with self.env.cr.savepoint():
                        self._process_inventory_batch(instance, queue, batch_lines)
                except OperationalError:
                    raise
                except Exception as error:
                    self._fail_inventory_lines(instance, batch_lines, str(error))
                self._cr.commit()
        return True

    def _process_inventory_batch(self, instance, queue, batch_lines):
        batch_lines = self._refresh_inventory_lines(instance, batch_lines)
        for location_id in set(batch_lines.mapped("location_id")):
            location_lines = batch_lines.filtered(lambda line: line.location_id == location_id)
            try:
                with self.env.cr.savepoint():
                    self._process_inventory_location(instance, queue, location_lines)
            except OperationalError:
                raise
            except Exception as error:
                self._fail_inventory_lines(instance, location_lines, str(error))

    def _refresh_inventory_lines(self, instance, lines):
        """Recalculate current warehouse stock; never replay an old quantity."""
        valid_lines = self.browse()
        locations = self.env["shopify.location.ept"].search([
            ("instance_id", "=", instance.id), ("legacy", "=", False),
            ("shopify_location_id", "in", lines.mapped("location_id")),
        ])
        for location_id in set(lines.mapped("location_id")):
            location_lines = lines.filtered(lambda line: line.location_id == location_id)
            location = locations.filtered(lambda item: item.shopify_location_id == location_id)
            if len(location) != 1 or not location.export_stock_warehouse_ids:
                self._fail_inventory_lines(instance, location_lines,
                    "The Shopify location is inactive, duplicated, or has no export warehouse mapping. Review Shopify Locations.")
                continue
            products = location_lines.shopify_product_id
            products.invalidate_recordset()
            stock = products.check_stock(instance, products.product_id.ids,
                self.env["product.product"], location.export_stock_warehouse_ids)
            duplicates = self.env["shopify.product.product.ept"].search([
                ("shopify_instance_id", "=", instance.id),
                ("inventory_item_id", "in", products.mapped("inventory_item_id")),
                ("inventory_management", "=", "shopify"),
            ])
            for line in location_lines:
                product = line.shopify_product_id
                try:
                    shopify_gid("InventoryItem", product.inventory_item_id)
                    shopify_gid("ProductVariant", product.variant_id)
                    shopify_gid("Location", line.location_id)
                except ValueError as error:
                    self._fail_inventory_lines(instance, line, str(error))
                    continue
                if (product.shopify_instance_id != instance or line.shopify_instance_id != instance
                        or not product.active or not product.exported_in_shopify
                        or product.inventory_management != "shopify"
                        or not product.inventory_item_id or not product.variant_id
                        or len(duplicates.filtered(lambda item:
                            item.inventory_item_id == product.inventory_item_id)) != 1):
                    self._fail_inventory_lines(instance, line,
                        "Review the product's active Shopify instance, variant and unique inventory item mapping.")
                    continue
                if product.product_id.id not in stock:
                    self._fail_inventory_lines(instance, line, "Odoo did not return a warehouse quantity for this variant.")
                    continue
                quantity = (product.fixed_stock_export_value if product.fixed_stock_export
                            else products.compute_qty_for_export_stock(stock, product, product.product_id))
                line.write({"inventory_item_id": product.inventory_item_id, "quantity": int(quantity)})
                valid_lines |= line
        return valid_lines

    def _read_inventory_location(self, instance, lines):
        location_id = shopify_gid("Location", lines[0].location_id)
        response = self._execute_inventory_graphql(instance, INVENTORY_LEVELS_QUERY, {
            "ids": [shopify_gid("InventoryItem", line.inventory_item_id) for line in lines],
            "locationId": location_id,
        }, "InventoryLevels")
        data = response.get("data") or {}
        location = data.get("location") or {}
        if location.get("id") != location_id or location.get("isActive") is not True:
            raise UserError("Shopify did not confirm the exact active destination location.")
        return {node["id"]: node for node in data.get("nodes") or [] if isinstance(node, dict) and node.get("id")}

    def _process_inventory_location(self, instance, queue, batch_lines):
        deadline = self.env.context.get("inventory_export_deadline")
        if deadline and time.monotonic() >= deadline:
            return
        nodes = self._read_inventory_location(instance, batch_lines)
        ready = self.browse()
        unchanged = self.browse()
        activated = self.browse()
        current_quantities = {}
        for line in batch_lines:
            node = nodes.get(shopify_gid("InventoryItem", line.inventory_item_id), {})
            level = node.get("inventoryLevel") or {}
            quantity = next((row.get("quantity") for row in level.get("quantities") or []
                             if row.get("name") == "available"), None)
            if (node.get("tracked") is not True
                    or "inventoryLevel" not in node
                    or (node.get("variant") or {}).get("id") != shopify_gid("ProductVariant", line.shopify_product_id.variant_id)):
                self._fail_inventory_lines(instance, line,
                    "Shopify did not confirm this tracked variant. Review its variant and inventory item mapping; tracking has not been disabled in Odoo.")
                continue
            if node.get("inventoryLevel") is None:
                deadline = self.env.context.get("inventory_export_deadline")
                if deadline and time.monotonic() >= deadline:
                    continue
                response = self._execute_inventory_graphql(instance, INVENTORY_ACTIVATE_MUTATION, {
                    "inventoryItemId": shopify_gid("InventoryItem", line.inventory_item_id),
                    "locationId": shopify_gid("Location", line.location_id),
                    "available": line.quantity, "idempotencyKey": str(uuid.uuid4()),
                }, "InventoryActivate")
                activation = (response.get("data") or {}).get("inventoryActivate") or {}
                result_level = activation.get("inventoryLevel") or {}
                if (activation.get("userErrors") != []
                        or (result_level.get("item") or {}).get("id") != shopify_gid("InventoryItem", line.inventory_item_id)
                        or (result_level.get("location") or {}).get("id") != shopify_gid("Location", line.location_id)):
                    self._fail_inventory_lines(instance, line,
                        "Shopify could not activate inventory at this mapped location: %s" % activation.get("userErrors"))
                else:
                    activated |= line
                continue
            if ((level.get("location") or {}).get("id") != shopify_gid("Location", line.location_id)
                    or type(quantity) is not int):
                self._fail_inventory_lines(instance, line, "Shopify returned an invalid quantity or a different inventory location.")
                continue
            current_quantities[line.id] = quantity
            if quantity == line.quantity:
                unchanged |= line
            else:
                ready |= line
        unchanged._confirm_inventory_exports()
        if activated:
            self._verify_inventory_exports(instance, activated)
        if not ready:
            return
        self._set_inventory_location(instance, queue, ready, current_quantities)

    def _set_inventory_location(self, instance, queue, batch_lines, current_quantities):
        changes = [{
            "inventory_item_id": line.inventory_item_id,
            "location_id": line.location_id,
            "quantity": line.quantity,
            "change_from_quantity": current_quantities[line.id],
        } for line in batch_lines]
        mutation_input = prepare_inventory_set_input(
            changes,
            "odoo://shopify/inventory-export/%s" % queue.id,
        )
        response = self._execute_inventory_mutation(
            instance, mutation_input, str(uuid.uuid4()))
        if response.get("errors"):
            raise RuntimeError(json.dumps(response["errors"]))

        mutation_result = response.get("data", {}).get("inventorySetQuantities")
        if mutation_result is None:
            raise RuntimeError("Shopify returned no inventorySetQuantities result")
        user_errors = mutation_result.get("userErrors")
        failed_indexes, batch_errors = inventory_user_error_indexes(user_errors)
        if user_errors:
            # A rejected mutation does not prove success for any other row.
            # Isolate indexed failures, then submit the remainder afresh.
            detail = "; ".join(error.get("message", str(error)) for error in user_errors)
            if not batch_errors and failed_indexes and all(0 <= index < len(batch_lines) for index in failed_indexes):
                failed = self.browse([batch_lines[index].id for index in failed_indexes])
                self._fail_inventory_lines(instance, failed, detail)
                remaining = batch_lines - failed
                if remaining:
                    self._process_inventory_location(instance, queue, remaining)
                return
            self._fail_inventory_lines(instance, batch_lines, detail)
            return
        if user_errors != [] or not isinstance(mutation_result.get("inventoryAdjustmentGroup"), dict):
            raise RuntimeError("Shopify did not confirm an inventory adjustment group with an empty error list.")
        self._verify_inventory_exports(instance, batch_lines)

    def _verify_inventory_exports(self, instance, batch_lines):
        verified = self._read_inventory_location(instance, batch_lines)
        successful_lines = self.browse()
        for line in batch_lines:
            node = verified.get(shopify_gid("InventoryItem", line.inventory_item_id), {})
            level = node.get("inventoryLevel") or {}
            quantities = level.get("quantities") or []
            if (node.get("tracked") is True
                    and (node.get("variant") or {}).get("id") == shopify_gid("ProductVariant", line.shopify_product_id.variant_id)
                    and (level.get("location") or {}).get("id") == shopify_gid("Location", line.location_id)
                    and any(row.get("name") == "available" and type(row.get("quantity")) is int
                            and row["quantity"] == line.quantity for row in quantities)):
                successful_lines |= line
            else:
                self._fail_inventory_lines(instance, line,
                    "Shopify read-back did not match the submitted location quantity. Refresh and retry; concurrent stock changes may have occurred.")
        successful_lines._confirm_inventory_exports()

    def _confirm_inventory_exports(self):
        if self:
            self.write({"state": "done", "last_process_date": fields.Datetime.now()})
            self._record_successful_inventory_exports()

    def _execute_inventory_mutation(self, instance, mutation_input, idempotency_key):
        return self._execute_inventory_graphql(instance, INVENTORY_SET_QUANTITIES_MUTATION, {
            "input": mutation_input, "idempotencyKey": idempotency_key,
        }, "InventorySet")

    def _execute_inventory_graphql(self, instance, query, variables, operation_name):
        graphql = shopify.GraphQL()
        # REST uses URL credentials; this GraphQL client sends only headers.
        # Copy them so this store's token never changes shared SDK headers.
        graphql.headers = dict(graphql.headers, **{
            "X-Shopify-Access-Token": instance.shopify_password,
        })
        for attempt in range(3):
            try:
                raw_response = graphql.execute(
                    query, variables=variables, operation_name=operation_name, timeout=20,
                )
            except Exception as error:
                retryable = (getattr(error, "code", None) in (429, 502, 503, 504)
                    or isinstance(error, TimeoutError)
                    or isinstance(error, URLError) and not isinstance(error, HTTPError))
                if retryable and attempt < 2:
                    retry_after = (getattr(error, "headers", None) or {}).get("Retry-After", 2 ** attempt)
                    time.sleep(min(5, max(0, float(retry_after))))
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
            if errors:
                raise RuntimeError(json.dumps(errors))
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
