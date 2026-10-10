"""Validate user-entered Shopify payout identities before requesting any data."""
import re


def parse_payout_ids(value):
    identities = []
    for token in re.split(r'[\s,;]+', (value or '').strip()):
        if not token:
            continue
        match = re.fullmatch(r'(?:gid://shopify/ShopifyPaymentsPayout/)?([0-9]+)', token)
        if not match or int(match[1]) == 0:
            raise ValueError('Enter Shopify payout IDs, separated by commas or new lines.')
        identity = str(int(match[1]))
        if identity not in identities:
            identities.append(identity)
    if not identities:
        raise ValueError('Enter at least one Shopify payout ID.')
    return identities
