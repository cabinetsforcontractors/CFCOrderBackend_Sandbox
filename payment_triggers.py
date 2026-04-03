"""
payment_triggers.py
Automated triggers that fire when a Square payment is received.

Trigger 2: Auto-create BOL for each LTL warehouse shipment
Trigger 4: Send payment_confirmation email to customer

Entry point: run_payment_triggers(order_id, order_data, payment_amount)
Called by square_sync.run_square_sync() after marking an order as paid.
"""

from typing import Dict, List


def send_payment_confirmation(order_id: str, order_data: dict, payment_amount: float) -> Dict:
    """
    Trigger 4: Send payment confirmation email to customer.
    """
    try:
        from email_sender import send_order_email

        customer_email = order_data.get('email', '')
        if not customer_email:
            print(f"[PAYMENT TRIGGER] No customer email for order {order_id} — skipping confirmation email")
            return {'success': False, 'error': 'No customer email on order'}

        order_data['payment_amount'] = payment_amount

        result = send_order_email(
            order_id=order_id,
            template_id='payment_confirmation',
            to_email=customer_email,
            order_data=order_data,
            triggered_by='square_sync'
        )
        print(f"[PAYMENT TRIGGER] Confirmation email order {order_id}: success={result.get('success')}")
        return result

    except Exception as e:
        print(f"[PAYMENT TRIGGER] Confirmation email failed for order {order_id}: {e}")
        return {'success': False, 'error': str(e)}


def auto_create_bols(order_id: str) -> List[Dict]:
    """
    Trigger 2: Auto-create BOLs for all LTL warehouse shipments on an order.

    Only fires for shipments with shipping_method == 'ltl'.
    Small package (Shippo) shipments do not need a BOL — logged as skipped.

    Returns list of results per warehouse.
    """
    results = []

    try:
        from checkout import fetch_b2bwave_order, calculate_order_shipping, WAREHOUSES
        from rl_carriers import create_bol, is_configured

        if not is_configured():
            print(f"[PAYMENT TRIGGER] R+L API not configured — skipping auto-BOL for order {order_id}")
            return [{'success': False, 'error': 'R+L Carriers API not configured'}]

        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            print(f"[PAYMENT TRIGGER] Order {order_id} not found in B2BWave — skipping auto-BOL")
            return [{'success': False, 'error': f'Order {order_id} not found in B2BWave'}]

        shipping = order_data.get('shipping_address', {})
        dest_address = {
            'address': shipping.get('address', ''),
            'city': shipping.get('city', ''),
            'state': shipping.get('state', ''),
            'zip': shipping.get('zip', ''),
            'country': shipping.get('country', 'US'),
        }

        shipping_calc = calculate_order_shipping(order_data, dest_address)

        company_name = order_data.get('company_name') or order_data.get('customer_name', 'Customer')

        for shipment in shipping_calc.get('shipments', []):
            warehouse_code = shipment.get('warehouse')
            shipping_method = shipment.get('shipping_method')

            # Only BOL for LTL — small package ships via Shippo, no BOL needed
            if shipping_method != 'ltl':
                print(f"[PAYMENT TRIGGER] Order {order_id} / {warehouse_code}: {shipping_method} — no BOL needed")
                results.append({
                    'warehouse': warehouse_code,
                    'shipping_method': shipping_method,
                    'bol_created': False,
                    'reason': 'small_package_no_bol_needed'
                })
                continue

            warehouse = WAREHOUSES.get(warehouse_code)
            if not warehouse:
                results.append({
                    'warehouse': warehouse_code,
                    'bol_created': False,
                    'error': f'Unknown warehouse: {warehouse_code}'
                })
                continue

            weight = shipment.get('weight', 100)
            items = shipment.get('items', [])
            pieces = len(items) if items else 1

            item_descriptions = [
                f"{item.get('quantity', 1)}x {item.get('name', item.get('sku', 'Cabinet'))}"
                for item in items[:3]
            ]
            description = '; '.join(item_descriptions)
            if len(items) > 3:
                description += f" +{len(items) - 3} more items"
            if len(description) > 100:
                description = f"RTA Cabinets - {len(items)} items"

            quote_number = ''
            quote_data = shipment.get('quote', {})
            if isinstance(quote_data.get('quote'), dict):
                quote_number = quote_data['quote'].get('quote_number', '')

            try:
                bol_result = create_bol(
                    shipper_name=warehouse.get('name'),
                    shipper_address=warehouse.get('address', ''),
                    shipper_city=warehouse.get('city'),
                    shipper_state=warehouse.get('state'),
                    shipper_zip=warehouse.get('zip'),
                    shipper_phone=warehouse.get('phone', ''),
                    consignee_name=company_name,
                    consignee_address=shipping.get('address', ''),
                    consignee_address2=shipping.get('address2', ''),
                    consignee_city=shipping.get('city', ''),
                    consignee_state=shipping.get('state', ''),
                    consignee_zip=shipping.get('zip', ''),
                    consignee_phone=order_data.get('customer_phone', ''),
                    consignee_email=order_data.get('customer_email', ''),
                    weight_lbs=int(weight),
                    pieces=pieces,
                    description=description,
                    freight_class='85',
                    po_number=order_id,
                    quote_number=quote_number,
                    special_instructions=f'Auto-created on payment — Order #{order_id}',
                )

                pro_number = bol_result.get('pro_number', '')
                print(f"[PAYMENT TRIGGER] BOL created order {order_id} / {warehouse_code}: PRO {pro_number}")

                results.append({
                    'warehouse': warehouse_code,
                    'bol_created': True,
                    'pro_number': pro_number,
                    'bol_result': bol_result
                })

            except Exception as e:
                print(f"[PAYMENT TRIGGER] BOL failed order {order_id} / {warehouse_code}: {e}")
                results.append({
                    'warehouse': warehouse_code,
                    'bol_created': False,
                    'error': str(e)
                })

    except Exception as e:
        print(f"[PAYMENT TRIGGER] auto_create_bols error order {order_id}: {e}")
        results.append({'success': False, 'error': str(e)})

    return results


def run_payment_triggers(order_id: str, order_data: dict, payment_amount: float) -> Dict:
    """
    Entry point — run all payment triggers for an order.
    Called by square_sync.run_square_sync() after marking an order as paid.

    Returns dict with results for each trigger.
    """
    print(f"[PAYMENT TRIGGER] Running triggers for order {order_id}, payment ${payment_amount:.2f}")

    results = {
        'order_id': order_id,
        'payment_amount': payment_amount,
        'email_confirmation': None,
        'bols': []
    }

    # Trigger 4: Payment confirmation email
    results['email_confirmation'] = send_payment_confirmation(order_id, order_data, payment_amount)

    # Trigger 2: Auto-create BOLs for LTL shipments
    results['bols'] = auto_create_bols(order_id)

    return results
