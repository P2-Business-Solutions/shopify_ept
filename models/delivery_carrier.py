# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import models, fields, _
from odoo.exceptions import UserError


class DeliveryCarrier(models.Model):
    """Inherit the model to handle the delivery carrier in the connector"""
    _inherit = "delivery.carrier"

    shopify_code = fields.Char("Shopify Delivery Code")
    shopify_source = fields.Char("Shopify Delivery Source")
    shopify_shipping_mapping_ids = fields.One2many(
        "shopify.shipping.mapping.ept", "carrier_id", string="Shopify Shipping Mappings",
    )
    shopify_tracking_company = fields.Selection([
        ('4PX', '4PX'),
        ('APC', 'APC'),
        ('Amazon Logistics UK', 'Amazon Logistics UK'),
        ('Amazon Logistics US', 'Amazon Logistics US'),
        ('Anjun Logistics', 'Anjun Logistics'),
        ('Australia Post', 'Australia Post'),
        ('Bluedart', 'Bluedart'),
        ('Canada Post', 'Canada Post'),
        ('Canpar', 'Canpar'),
        ('China Post', 'China Post'),
        ('Chukou1', 'Chukou1'),
        ('Correios', 'Correios'),
        ('Couriers Please', 'Couriers Please'),
        ('DHL Express', 'DHL Express'),
        ('DHL eCommerce', 'DHL eCommerce'),
        ('DHL eCommerce Asia', 'DHL eCommerce Asia'),
        ('DPD', 'DPD'),
        ('DPD Local', 'DPD Local'),
        ('DPD UK', 'DPD UK'),
        ('Delhivery', 'Delhivery'),
        ('Eagle', 'Eagle'),
        ('FSC', 'FSC'),
        ('Fastway Australia', 'Fastway Australia'),
        ('FedEx', 'FedEx'),
        ('GLS', 'GLS'),
        ('GLS (US)', 'GLS (US)'),
        ('Globegistics', 'Globegistics'),
        ('Japan Post (EN)', 'Japan Post (EN)'),
        ('Japan Post (JA)', 'Japan Post (JA)'),
        ('La Poste', 'La Poste'),
        ('New Zealand Post', 'New Zealand Post'),
        ('Newgistics', 'Newgistics'),
        ('PostNL', 'PostNL'),
        ('PostNord', 'PostNord'),
        ('Purolator', 'Purolator'),
        ('Royal Mail', 'Royal Mail'),
        ('SF Express', 'SF Express'),
        ('SFC Fulfillment', 'SFC Fulfillment'),
        ('Sagawa (EN)', 'Sagawa (EN)'),
        ('Sagawa (JA)', 'Sagawa (JA)'),
        ('Sendle', 'Sendle'),
        ('Singapore Post', 'Singapore Post'),
        ('StarTrack', 'StarTrack'),
        ('TNT', 'TNT'),
        ('Toll IPEC', 'Toll IPEC'),
        ('UPS', 'UPS'),
        ('USPS', 'USPS'),
        ('Whistl', 'Whistl'),
        ('Yamato (EN)', 'Yamato (EN)'),
        ('Yamato (JA)', 'Yamato (JA)'),
        ('YunExpress', 'YunExpress'),
        ('AGS', 'AGS'),
        ('An Post', 'An Post'),
        ('Asendia USA', 'Asendia USA'),
        ('Bonshaw', 'Bonshaw'),
        ('BPost', 'BPost'),
        ('BPost International', 'BPost International'),
        ('BPost International', 'BPost International'),
        ('CDL Last Mile', 'CDL Last Mile'),
        ('Chronopost', 'Chronopost'),
        ('Colissimo', 'Colissimo'),
        ('Comingle', 'Comingle'),
        ('Coordinadora', 'Coordinadora'),
        ('Correos', 'Correos'),
        ('CTT', 'CTT'),
        ('CTT Express', 'CTT Express'),
        ('Cyprus Post', 'Cyprus Post'),
        ('Delnext', 'Delnext'),
        ('Deutsche Post', 'Deutsche Post'),
        ('DTD Express', 'DTD Express'),
        ('DX', 'DX'),
        ('Estes', 'Estes'),
        ('Evri', 'Evri'),
        ('First Global Logistics', 'First Global Logistics'),
        ('First Line', 'First Line'),
        ('Fulfilla', 'Fulfilla'),
        ('Guangdong Weisuyi Information Technology (WSE)', 'Guangdong Weisuyi Information Technology (WSE)'),
        ('Heppner Internationale Spedition GmbH & Co.', 'Heppner Internationale Spedition GmbH & Co.'),
        ('Iceland Post', 'Iceland Post'),
        ('IDEX', 'IDEX'),
        ('Israel Post', 'Israel Post'),
        ('Lasership', 'Lasership'),
        ('Latvia Post', 'Latvia Post'),
        ('Lietuvos Paštas', 'Lietuvos Paštas'),
        ('Logisters', 'Logisters'),
        ('Lone Star Overnight', 'Lone Star Overnight'),
        ('M3 Logistics', 'M3 Logistics'),
        ('Meteor Space', 'Meteor Space'),
        ('Mondial Relay', 'Mondial Relay'),
        ('NinjaVan', 'NinjaVan'),
        ('North Russia Supply Chain (Shenzhen) Co.', 'North Russia Supply Chain (Shenzhen) Co.'),
        ('OnTrac', 'OnTrac'),
        ('Packeta', 'Packeta'),
        ('Pago Logistics', 'Pago Logistics'),
        ('Ping An Da Tengfei Express', 'Ping An Da Tengfei Express'),
        ('Pitney Bowes', 'Pitney Bowes'),
        ('Portal PostNord', 'Portal PostNord'),
        ('Poste Italiane', 'Poste Italiane'),
        ('PostNord DK', 'PostNord DK'),
        ('PostNord NO', 'PostNord NO'),
        ('PostNord SE', 'PostNord SE'),
        ('Qxpress', 'Qxpress'),
        ('Qyun Express', 'Qyun Express'),
        ('Royal Shipments', 'Royal Shipments'),
        ('SHREE NANDAN COURIER', 'SHREE NANDAN COURIER'),
        ('Southwest Air Cargo', 'Southwest Air Cargo'),
        ('Step Forward Freight', 'Step Forward Freight'),
        ('Swiss Post', 'Swiss Post'),
        ('TForce Final Mile', 'TForce Final Mile'),
        ('Tinghao', 'Tinghao'),
        ('United Delivery Service', 'United Delivery Service'),
        ('Venipak', 'Venipak'),
        ('We Post', 'We Post'),
        ('Wizmo', 'Wizmo'),
        ('WMYC', 'WMYC'),
        ('Xpedigo', 'Xpedigo'),
        ('XPO Logistics', 'XPO Logistics'),
        ('YiFan Express', 'YiFan Express'),
        ('Aramex Australia', 'Aramex Australia'),
        ('TNT Australia', 'TNT Australia'),
        ('Hunter Express', 'Hunter Express'),
        ('Bonds', 'Bonds'),
        ('Allied Express', 'Allied Express'),
        ('Direct Couriers', 'Direct Couriers'),
        ('Northline', 'Northline'),
        ('GO Logistics', 'GO Logistics'),
        ('Österreichische Post', 'Österreichische Post'),
        ('Speedy', 'Speedy'),
        ('Intelcom', 'Intelcom'),
        ('BoxKnight', 'BoxKnight'),
        ('Loomis', 'Loomis'),
        ('WanbExpress', 'WanbExpress'),
        ('Zásilkovna', 'Zásilkovna'),
        ('Deutsche Post (DE)', 'Deutsche Post (DE)'),
        ('Deutsche Post (EN)', 'Deutsche Post (EN)'),
        ('DHL', 'DHL'),
        ('Swiship', 'Swiship'),
        ('Hermes', 'Hermes'),
        ('SEUR', 'SEUR'),
        ('Colissimo', 'Colissimo'),
        ('Mondial Relay', 'Mondial Relay'),
        ('Colis Privé', 'Colis Privé'),
        ('Evri', 'Evri'),
        ('Parcelforce', 'Parcelforce'),
        ('Yodel', 'Yodel'),
        ('DHL Parcel', 'DHL Parcel'),
        ('Tuffnells', 'Tuffnells'),
        ('ACS Courier', 'ACS Courier'),
        ('Fastway', 'Fastway'),
        ('DPD Ireland', 'DPD Ireland'),
        ('DTDC', 'DTDC'),
        ('India Post', 'India Post'),
        ('Gati KWE', 'Gati KWE'),
        ('Professional Couriers', 'Professional Couriers'),
        ('XpressBees', 'XpressBees'),
        ('Ecom Express', 'Ecom Express'),
        ('Ekart', 'Ekart'),
        ('Shadowfax', 'Shadowfax'),
        ('BRT', 'BRT'),
        ('GLS Italy', 'GLS Italy'),
        ('DHL Parcel', 'DHL Parcel'),
        ('Bring', 'Bring'),
        ('Inpost', 'Inpost'),
        ('PTT', 'PTT'),
        ('Yurtiçi Kargo', 'Yurtiçi Kargo'),
        ('Aras Kargo', 'Aras Kargo'),
        ('Sürat Kargo', 'Sürat Kargo'),
        ('Alliance Air Freight', 'Alliance Air Freight'),
        ('Pilot Freight', 'Pilot Freight'),
        ('LSO', 'LSO'),
        ('Old Dominion', 'Old Dominion'),
        ('R+L Carriers', 'R+L Carriers'),
        ('Southwest Air Cargo', 'Southwest Air Cargo'),
        ('Fastway', 'Fastway'),
        ('Skynet', 'Skynet'),
        ('Italy BTR', 'Italy BTR'),
    ], help="shopify_tracking_company selection help:When creating a fulfillment for a supported carrier, send the"
            "tracking_company exactly as written in the list above. If the tracking company doesn't match one of the"
            "supported entries, then the shipping status might not be updated properly during the fulfillment process.")

    def shopify_search_create_delivery_carrier(self, line, instance, order_type=None):
        """Use the order-type delivery method, then configured shipping aliases.

        The historic method name is retained for downstream addon compatibility.
        """
        carrier = self.env["shopify.order.type.mapping.ept"]._get_delivery_carrier(instance, order_type)
        if carrier:
            return carrier
        carrier = self.env["shopify.shipping.mapping.ept"]._find_carrier(line, instance)
        if carrier:
            return carrier
        delivery_source = (line.get("source") or "").strip()
        delivery_code = (line.get("code") or "").strip()
        scope = [("active", "=", True),
                 ("company_id", "in", [instance.shopify_company_id.id, False])]
        # Existing source/code fields remain supported, but imports never learn
        # new values by overwriting a carrier that happens to have the same name.
        if delivery_source and delivery_code:
            carrier = self.search(scope + [("shopify_source", "=", delivery_source),
                                           ("shopify_code", "=", delivery_code)])
            if not carrier:
                carrier = self.search(scope + [("shopify_source", "=", delivery_source),
                                               ("shopify_tracking_company", "=", delivery_code)])
            if carrier:
                return self._shopify_validate_carrier(carrier, instance)
        raise UserError(_(
            "No delivery method is mapped for Shopify store %(store)s: "
            "shipping source=%(shipping_source)r, code=%(code)r, name=%(title)r, order type=%(order_type)r. "
            "Add a mapping under Shopify > Configuration > Shipping Mappings, then retry the order. "
            "Leave Shipping Source blank to share a delivery method across sources.",
            store=instance.name, shipping_source=delivery_source, code=delivery_code,
            title=line.get("title") or "",
            order_type=order_type.display_name if order_type else "",
        ))

    def _shopify_validate_carrier(self, carriers, instance, configuration="Shipping Mappings"):
        if len(carriers) != 1:
            raise UserError(_(
                "Conflicting Shopify delivery methods: %(methods)s. "
                "Configure one delivery method in %(configuration)s for this store.",
                methods=", ".join("%s (ID %s)" % (carrier.name, carrier.id) for carrier in carriers),
                configuration=configuration,
            ))
        if not carriers.active:
            raise UserError(_(
                "The Shopify shipping mapping points to archived delivery method %(method)s. "
                "Select an active delivery method in %(configuration)s before retrying.",
                method=carriers.name, configuration=configuration,
            ))
        if carriers.company_id and carriers.company_id != instance.shopify_company_id:
            raise UserError(_("The mapped delivery method belongs to another company."))
        return carriers

    def search_carrier_for_webhook_fulfillment(
            self, instance, fulfillment_data, preferred_carrier=None, order_type=None):
        """Keep the mapped service when tracking only identifies its shipping company."""
        carrier_company = fulfillment_data.get("tracking_company")
        if preferred_carrier:
            carrier = self._shopify_validate_carrier(preferred_carrier, instance)
            if not carrier_company or carrier.shopify_tracking_company in (False, carrier_company):
                return carrier
        carrier = self.env["shopify.order.type.mapping.ept"]._get_delivery_carrier(instance, order_type)
        if carrier and (not carrier_company or carrier.shopify_tracking_company in (False, carrier_company)):
            return carrier
        scope = [("active", "=", True),
                 ("company_id", "in", [instance.shopify_company_id.id, False])]
        carrier_name = fulfillment_data.get("service")
        if carrier_name:
            carrier = self.env["shopify.shipping.mapping.ept"]._find_carrier(
                {"code": carrier_name, "title": carrier_name}, instance,
            )
            if not carrier:
                carrier = self.search(scope + [("name", "=", carrier_name)])
            if carrier:
                return self._shopify_validate_carrier(carrier, instance)
        if carrier_company:
            carrier = self.search(scope + [("shopify_tracking_company", "=", carrier_company)])
            if carrier:
                return self._shopify_validate_carrier(carrier, instance)
        return self.env["delivery.carrier"]
