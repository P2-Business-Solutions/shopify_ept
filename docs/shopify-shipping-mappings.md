# Shared Shopify delivery methods

Version 18.0.3.50 adds order-type delivery choices to the existing **Order Type
Mapping** settings. Version 18.0.3.49 introduced **Shopify → Configuration → Shipping Mappings**. Multiple
shipping options can point to one Odoo delivery method. The same mappings are
also visible on the delivery method's **Shopify Carrier Info** tab.

## Bajío setup after upgrading the module

Create this mapping before processing new orders:

| Setting | Value |
| --- | --- |
| Instance | Bajio |
| Delivery Method | UPS Ground (the existing integrated method, ID 6) |
| Match Using | Shipping Name |
| Shipping Code / Name | Free Shipping |
| Shipping Source | Leave blank |

This rule accepts the observed **Free Shipping** name from both web and
Collective orders, including lines with a different or missing code/source.
It does not guess Collective's historical shipping-line source. Shipping
charges still come from Shopify: a $0 shipping line remains $0 even though the
selected Odoo method is UPS Ground.

## Warranty and Repair: reuse Order Type Mapping

Open **Shopify → Configuration → Instances → Bajio → Order Type Mapping** and
set the optional **Delivery Method** on the existing tag mappings:

| Existing Shopify tag | Odoo order type | Delivery Method |
| --- | --- | --- |
| Warranty | Warranty | Select the desired Warranty delivery method |
| Repair | Repair | Select the desired Repair delivery method |
| Other orders / instance default | Existing order type | General shipping mapping applies when no type method is set |

These are configuration examples; use the existing tags and order types in the
instance rather than creating duplicate classification rules. The Shopify team
can keep its existing **Free Shipping** profile for both Warranty and Repair.

On a new import, the connector resolves the order type using the existing tag
mapping and its sequence. It then uses the delivery method configured for that
type on the same instance. The method overrides shipping-option mappings for
that type; the shipping amount still comes from Shopify. No new shipping
profiles or renamed checkout rates are required.

All tags mapped to the same order type share one delivery method. A method may
be set on one or several of those rows; setting conflicting methods is rejected.
Leave all the rows for that type blank to use the general shipping mappings.
An archived or wrong-company method requires correction instead of falling
through to the retail method.

For an existing Odoo order, manual refresh respects its saved Order Type,
including a deliberate manual change, instead of reclassifying it from Shopify
tags. Changing the type alone does not rewrite shipping: use **Update Order**
to apply the current mapping to the order and its open deliveries.

Review the old `shopify / Free Shipping` fields on FedEx Warranty and UPS Ground
Warranty. They duplicate UPS Ground's old mapping. The new explicit mapping
takes precedence over those fields; clear obsolete fields once any intended
exceptions have been expressed as explicit mappings. Do not remove the UPS
credentials or service configuration.

## Matching and failures

Mappings are scoped to one Shopify instance and its company. A blank Shipping
Source accepts any shipping-line source; it does not filter the order's sales
channel. Source and code matching are exact and ignore surrounding spaces.
Shipping-name matching also ignores letter case.

The existing Order Type Mapping's Delivery Method is considered first. When no
method is configured for the resolved type, explicit shipping-option rules are
considered in this order:

1. Shipping code with an exact source.
2. Shipping code from any source.
3. Shipping name with an exact source.
4. Shipping name from any source.

One option can have only one mapping per instance, including archived mappings.
Edit or reactivate an existing rule instead of adding a duplicate. Different
sources/codes/names may all select the same carrier. A source-specific rule can
override an unrestricted rule of the same type.

If no explicit rule matches, the carrier's existing Shopify source/code fields
are supported for compatibility. Exact code matches take precedence over the
older tracking-company fallback. Multiple matching legacy delivery methods are
reported as a conflict instead of selecting the first one by sequence.

Imports no longer overwrite the carrier's source/code fields, match arbitrary
carrier names, or create generic delivery methods. An unmapped option fails
before a sales order is created. Its queue payload is retained and its log
contains the shipping source, code, and name needed to configure a mapping.
Configure the mapping and retry the queue. An explicit rule targeting an
archived or wrong-company carrier also fails instead of selecting another
service. Archived rules are ignored.

Shopify and Inventory managers can manage mappings. Other internal users can
read mappings within their allowed companies. Order-type delivery methods are
managed through the existing Shopify instance settings and keep their existing
edit permissions.

## Existing orders and fulfillment

Adding a mapping does not rewrite historical orders or shipments. For an
existing order that needs correction, review it and use **Update Order** from
Shopify. Manual refresh resolves all shipping options before making changes,
updates the order and open outbound deliveries, and aligns an uninvoiced
delivery product even when the shipping charge is still zero. Invoiced delivery
products and completed/cancelled deliveries are preserved.

Fulfillment updates keep the order's selected service when its tracking company
matches (for example UPS Ground when tracking says UPS). Without an existing
selection, ambiguous carrier-company matches require review. Fulfillment
updates never create delivery methods.

At investigation time, #144623 had a waiting delivery using archived method 74;
review and refresh that order after configuring the mapping. #144690 and #144726
had completed deliveries; their existing shipment history is not changed by
this upgrade.

## Validation

Standalone behavior tests:

```sh
python3 -m unittest discover -s tests -p 'test_shopify_shipping_mapping.py' -v
```

Database tests are in `tests/test_shopify_shipping_mapping_orm.py` and run under
Odoo with the module's post-install tests. They cover uniqueness, company/access
rules, archived targets, real shipping-line creation, and repeatable free
shipping refresh. Run these in staging before the production module upgrade.

Validated October 16, 2026 in a disposable local Odoo 18.0-20260908 Community /
PostgreSQL 16 database. The module's new model, access rules and views loaded on
installation and upgrade. The final 18.0.3.50 upgrade run passed all **60 database tests**
selected from order types, shipping mappings, POS fulfillment and POS order imports,
with no failures or errors. All **158 standalone tests** passed, including 36 shipping
behavior tests. Coverage includes the existing tag sequence, shared Warranty/Repair
shipping names, saved order types on refresh, consistent methods across tags,
company isolation and operational read-only access. Manifest data paths, XML, CSV, Python compilation and whitespace
checks passed. No production records, mappings or shipments were changed.
