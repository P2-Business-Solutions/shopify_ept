"""Carry a repair route without interpreting translated error messages."""
from odoo.exceptions import UserError


class ShopifyRepairBlocked(UserError):
    def __init__(self, message, repair_route='review'):
        super().__init__(message)
        self.repair_route = repair_route
