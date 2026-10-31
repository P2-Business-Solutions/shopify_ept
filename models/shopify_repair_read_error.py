from odoo.exceptions import UserError


class RepairEvidenceBlocked(UserError):
    """Preserve native UI messages while supplying a stable safe RPC code."""
    def __init__(self, code, message):
        super().__init__(message)
        self.repair_code = code
