"""
ai_summary.py
Anthropic Claude API integration for generating order summaries.

generate_order_summary()       — SHORT 6-bullet state summary for order list display
                                  Auto-generated on sync, shown on every order card
generate_comprehensive_summary() — Full analysis for AI tab on demand
"""

import json
import urllib.request
import urllib.error
from typing import Optional

from psycopg2.extras import RealDictCursor
from config import ANTHROPIC_API_KEY
from db_helpers import get_db


def is_configured() -> bool:
    return bool(ANTHROPIC_API_KEY)


def call_anthropic_api(prompt: str, max_tokens: int = 1024) -> str:
    if not ANTHROPIC_API_KEY:
        return "AI Summary not available - API key not configured"

    url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    }

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", ANTHROPIC_API_KEY)
    req.add_header("anthropic-version", "2023-06-01")

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read().decode())
            if result.get('content') and len(result['content']) > 0:
                return result['content'][0].get('text', '')
            return "No summary generated"
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        print(f"Anthropic API Error: {e.code} - {error_body}")
        return f"AI Summary error: {e.code}"
    except Exception as e:
        print(f"Anthropic API Exception: {e}")
        return f"AI Summary error: {str(e)}"


def _get_relative_time(dt) -> str:
    """Convert a datetime to a relative time string."""
    if not dt:
        return ""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        from datetime import timezone as tz
        dt = dt.replace(tzinfo=tz.utc)
    diff = now - dt
    total_hours = diff.total_seconds() / 3600
    if total_hours < 1:
        mins = int(diff.total_seconds() / 60)
        return f"{mins} min ago"
    elif total_hours < 24:
        hrs = int(total_hours)
        return f"{hrs} hr{'s' if hrs != 1 else ''} ago"
    else:
        days = int(total_hours / 24)
        return f"{days} day{'s' if days != 1 else ''} ago"


def generate_order_summary(order_id: str) -> str:
    """
    Generate a SHORT 6-bullet state summary for order card display.

    Format (always exactly 6 bullets):
    • Order age + value
    • Payment status
    • Warehouse / sent-to-warehouse status
    • BOL / shipment status
    • Supplier / tracking communication
    • Next action or escalation

    Auto-generated on sync. Reads notes, events, email snippets.
    """
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            order = cur.fetchone()
            if not order:
                return "Order not found"

            cur.execute("""
                SELECT email_from, email_subject, email_snippet, email_date, snippet_type
                FROM order_email_snippets
                WHERE order_id = %s
                ORDER BY email_date DESC
                LIMIT 10
            """, (order_id,))
            snippets = cur.fetchall()

            cur.execute("""
                SELECT event_type, event_data, created_at
                FROM order_events
                WHERE order_id = %s
                AND event_type NOT IN ('b2bwave_sync', 'auto_sync', 'status_check')
                ORDER BY created_at DESC
                LIMIT 20
            """, (order_id,))
            events = cur.fetchall()

            cur.execute("""
                SELECT warehouse, ship_method, status, tracking, tracking_number,
                       pro_number, weight, created_at, shipped_at, delivered_at,
                       rl_customer_price, li_customer_price, customer_price, ps_quote_price
                FROM order_shipments
                WHERE order_id = %s
            """, (order_id,))
            shipments = cur.fetchall()

    # Build context
    parts = []

    # Order facts
    days_open = order.get('days_open') or 0
    order_total = float(order.get('order_total') or 0)
    parts.append(f"Order #{order_id} | Age: {days_open} day{'s' if days_open != 1 else ''} | Total: ${order_total:.2f}")

    # Payment
    if order.get('payment_received'):
        paid_at = _get_relative_time(order.get('payment_received_at'))
        paid_amt = float(order.get('payment_amount') or order_total)
        parts.append(f"Payment: RECEIVED ${paid_amt:.2f} {paid_at}")
    else:
        # Check if invoice was sent
        invoice_events = [e for e in events if e.get('event_type') == 'email_sent']
        if invoice_events:
            sent_at = _get_relative_time(invoice_events[0].get('created_at'))
            parts.append(f"Payment: NOT received — invoice sent {sent_at}")
        else:
            parts.append("Payment: NOT received — no invoice sent yet")

    # Warehouse
    warehouses = [order.get(f'warehouse_{i}') for i in range(1, 5) if order.get(f'warehouse_{i}')]
    wh_str = ', '.join(warehouses) if warehouses else 'unknown'
    if order.get('sent_to_warehouse'):
        sent_at = _get_relative_time(order.get('sent_to_warehouse_at'))
        parts.append(f"Warehouse ({wh_str}): order sent {sent_at}")
    else:
        parts.append(f"Warehouse ({wh_str}): not yet ordered")

    # BOL / Shipment status
    if order.get('bol_sent'):
        bol_at = _get_relative_time(order.get('bol_sent_at'))
        parts.append(f"BOL: sent {bol_at}")
    elif shipments:
        shipped = [s for s in shipments if s.get('status') == 'shipped']
        if shipped:
            ship_time = _get_relative_time(shipped[0].get('shipped_at'))
            tracking = shipped[0].get('tracking_number') or shipped[0].get('pro_number') or 'pending'
            parts.append(f"Shipment: shipped {ship_time} — tracking {tracking}")
        else:
            parts.append("BOL: not yet sent — shipment pending")
    else:
        parts.append("BOL: not yet sent")

    # Supplier / tracking communication
    supplier_snippets = [s for s in snippets if s.get('snippet_type') in ('supplier', 'tracking', 'warehouse')]
    if supplier_snippets:
        last = supplier_snippets[0]
        time_str = _get_relative_time(last.get('email_date'))
        subject = last.get('email_subject', 'supplier email')
        parts.append(f"Supplier contact: email from {last.get('email_from', 'supplier')} {time_str} — \"{subject[:60]}\"")
    else:
        # Check if we emailed supplier
        supplier_events = [e for e in events if 'supplier' in str(e.get('event_type', '')).lower() or 'warehouse' in str(e.get('event_type', '')).lower()]
        if supplier_events:
            time_str = _get_relative_time(supplier_events[0].get('created_at'))
            parts.append(f"Supplier contact: email sent {time_str} — awaiting response")
        else:
            parts.append("Supplier contact: none yet")

    # Notes (read if present)
    notes_str = ""
    if order.get('notes'):
        notes_str = f"\nInternal notes: {order['notes'][:200]}"

    # Next action
    if not order.get('payment_received'):
        parts.append("Next action: follow up on payment — customer has received invoice")
    elif not order.get('sent_to_warehouse'):
        parts.append("Next action: send order to warehouse once payment confirmed")
    elif not order.get('bol_sent'):
        if supplier_snippets:
            parts.append("Next action: BOL ready to send — warehouse has responded")
        else:
            parts.append("Next action: create and send BOL to warehouse")
    elif not any(s.get('status') == 'shipped' for s in (shipments or [])):
        parts.append("Next action: monitor for tracking/shipping confirmation")
    else:
        parts.append("Next action: monitor delivery — notify customer when delivered")

    # Build the prompt
    context = "\n".join(parts) + notes_str

    prompt = f"""You are generating a 6-bullet status summary for an internal order management dashboard.

PRODUCE EXACTLY 6 BULLETS. Each bullet must be a single sentence starting with "• ".
NO markdown, NO headers, NO bold text. Plain text only.
Use relative time ("2 days ago", "3 hours ago", "this morning").

Required bullet order:
1. Order age and dollar value
2. Payment status (received or pending + when invoice was sent)
3. Warehouse status (ordered or not + which warehouse + when)
4. BOL or shipment status (sent or not + tracking if available)
5. Supplier/warehouse communication (any emails to/from supplier + how long ago)
6. Next action needed or escalation if overdue

Facts to use:
{context}

Write the 6 bullets now:"""

    return call_anthropic_api(prompt, max_tokens=400)


def generate_comprehensive_summary(order_id: str) -> str:
    """Generate detailed comprehensive summary for AI tab on demand."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            order = cur.fetchone()
            if not order:
                return "Order not found"

            cur.execute("""
                SELECT email_from, email_subject, email_snippet, email_date, snippet_type
                FROM order_email_snippets
                WHERE order_id = %s ORDER BY email_date ASC
            """, (order_id,))
            snippets = cur.fetchall()

            cur.execute("""
                SELECT event_type, event_data, created_at
                FROM order_events
                WHERE order_id = %s ORDER BY created_at ASC
            """, (order_id,))
            events = cur.fetchall()

            cur.execute("""
                SELECT warehouse, ship_method, tracking, tracking_number,
                       pro_number, status, weight, created_at
                FROM order_shipments WHERE order_id = %s
            """, (order_id,))
            shipments = cur.fetchall()

    context_parts = []
    context_parts.append(f"ORDER #{order_id}")
    context_parts.append(f"Customer: {order.get('company_name') or order.get('customer_name')}")
    context_parts.append(f"Total: ${order.get('order_total', 0)}")
    context_parts.append(f"Payment Received: {'Yes' if order.get('payment_received') else 'No'}")
    context_parts.append(f"Created: {order.get('created_at')}")
    if order.get('tracking'): context_parts.append(f"Tracking: {order.get('tracking')}")
    if order.get('pro_number'): context_parts.append(f"PRO: {order.get('pro_number')}")
    if order.get('comments'): context_parts.append(f"Customer Comments: {order.get('comments')}")
    if order.get('notes'): context_parts.append(f"Internal Notes: {order.get('notes')}")

    warehouses = [order.get(f'warehouse_{i}') for i in range(1, 5) if order.get(f'warehouse_{i}')]
    if warehouses: context_parts.append(f"Warehouses: {', '.join(warehouses)}")

    if shipments:
        context_parts.append("\n--- SHIPMENTS ---")
        for s in shipments:
            context_parts.append(f"Warehouse: {s.get('warehouse')} | Status: {s.get('status')}")
            if s.get('tracking_number'): context_parts.append(f"  Tracking: {s.get('tracking_number')}")
            if s.get('pro_number'): context_parts.append(f"  PRO: {s.get('pro_number')}")
            if s.get('weight'): context_parts.append(f"  Weight: {s.get('weight')} lbs")

    if snippets:
        context_parts.append("\n--- EMAIL HISTORY ---")
        for s in snippets:
            date_str = s['email_date'].strftime('%m/%d/%y %H:%M') if s.get('email_date') else ''
            context_parts.append(f"[{date_str}] From: {s.get('email_from')} | {s.get('email_subject')}")
            if s.get('email_snippet'):
                context_parts.append(f"{s['email_snippet'][:400]}")

    if events:
        context_parts.append("\n--- EVENT TIMELINE ---")
        important = [e for e in events if e.get('event_type') not in ('b2bwave_sync',)]
        for e in important[-30:]:
            date_str = e['created_at'].strftime('%m/%d/%y %H:%M') if e.get('created_at') else ''
            context_parts.append(f"[{date_str}] {e.get('event_type')}")

    context = "\n".join(context_parts)

    prompt = f"""You are analyzing a cabinet wholesale order. Provide a comprehensive summary.

Include these sections:
1. **Order Overview** - Customer, total, payment status, current stage
2. **Timeline Summary** - Key dates chronologically
3. **Communication History** - Important points from emails
4. **Shipping Status** - What shipped, tracking, delivery status
5. **Issues & Resolutions** - Any problems and how handled
6. **Current Status & Next Steps** - Where things stand and what's needed

Format: clear section headers, bullet points, include specific dates.

ORDER DATA:
{context}"""

    return call_anthropic_api(prompt, max_tokens=2048)


def generate_simple_summary(text: str, max_length: int = 200) -> str:
    if not is_configured():
        return text[:max_length] + "..." if len(text) > max_length else text
    prompt = f"Summarize this in {max_length} characters or less. Be concise:\n\n{text}"
    return call_anthropic_api(prompt, max_tokens=256)
