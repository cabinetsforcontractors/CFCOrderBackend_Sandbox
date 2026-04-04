"""
ai_summary.py
Anthropic Claude API integration for generating order summaries.
"""

import json
import urllib.request
import urllib.error
from typing import Optional

from psycopg2.extras import RealDictCursor
from config import ANTHROPIC_API_KEY
from db_helpers import get_db


def is_configured() -> bool:
    """Check if Anthropic API is configured"""
    return bool(ANTHROPIC_API_KEY)


def call_anthropic_api(prompt: str, max_tokens: int = 1024) -> str:
    """Call Anthropic Claude API to generate summary"""
    if not ANTHROPIC_API_KEY:
        return "AI Summary not available - API key not configured"

    url = "https://api.anthropic.com/v1/messages"

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "messages": [
            {"role": "user", "content": prompt}
        ]
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


def generate_order_summary(order_id: str) -> str:
    """
    Generate a 6-bullet state summary for order card display.

    Each bullet represents the current state of one dimension:
    1. Order age and value
    2. Payment status
    3. Warehouse / sent-to-warehouse status
    4. BOL / shipment / tracking status
    5. Supplier or warehouse communication
    6. Next action or escalation

    Also reads internal notes and email snippets from suppliers.
    """

    # Gather all order data
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            order = cur.fetchone()

            if not order:
                return "Order not found"

            # Email snippets (supplier emails, warehouse responses)
            cur.execute("""
                SELECT email_from, email_subject, email_snippet, email_date, snippet_type
                FROM order_email_snippets
                WHERE order_id = %s
                ORDER BY email_date DESC
                LIMIT 20
            """, (order_id,))
            snippets = cur.fetchall()

            # Recent non-sync events
            cur.execute("""
                SELECT event_type, event_data, created_at
                FROM order_events
                WHERE order_id = %s
                AND event_type NOT IN ('b2bwave_sync', 'auto_sync', 'status_check')
                ORDER BY created_at DESC
                LIMIT 20
            """, (order_id,))
            events = cur.fetchall()

            # Shipments
            cur.execute("""
                SELECT warehouse, ship_method, status, tracking, tracking_number,
                       pro_number, weight, created_at, shipped_at, delivered_at,
                       bol_sent, bol_sent_at
                FROM order_shipments
                WHERE order_id = %s
                ORDER BY created_at ASC
            """, (order_id,))
            shipments = cur.fetchall()

    # Build context
    context_parts = []

    context_parts.append(f"ORDER #{order_id}")
    context_parts.append(f"Customer: {order.get('company_name') or order.get('customer_name')}")
    context_parts.append(f"Order Total: ${order.get('order_total', 0)}")
    context_parts.append(f"Days Open: {order.get('days_open', 0)}")
    context_parts.append(f"Payment Received: {'Yes' if order.get('payment_received') else 'No'}")
    context_parts.append(f"Payment Received At: {order.get('payment_received_at') or 'N/A'}")
    context_parts.append(f"Invoice Sent: {'Yes' if order.get('payment_link_sent') else 'No'}")
    context_parts.append(f"Invoice Sent At: {order.get('payment_link_sent_at') or 'N/A'}")
    context_parts.append(f"Sent to Warehouse: {'Yes' if order.get('sent_to_warehouse') else 'No'}")
    context_parts.append(f"Sent to Warehouse At: {order.get('sent_to_warehouse_at') or 'N/A'}")
    context_parts.append(f"BOL Sent: {'Yes' if order.get('bol_sent') else 'No'}")
    context_parts.append(f"BOL Sent At: {order.get('bol_sent_at') or 'N/A'}")

    if order.get('tracking'):
        context_parts.append(f"Tracking: {order.get('tracking')}")
    if order.get('pro_number'):
        context_parts.append(f"PRO Number: {order.get('pro_number')}")
    if order.get('comments'):
        context_parts.append(f"Customer Comments: {order.get('comments')}")
    if order.get('notes'):
        context_parts.append(f"Internal Notes: {order.get('notes')}")

    warehouses = [order.get(f'warehouse_{i}') for i in range(1, 5) if order.get(f'warehouse_{i}')]
    if warehouses:
        context_parts.append(f"Warehouses: {', '.join(warehouses)}")

    if shipments:
        context_parts.append("\nSHIPMENTS:")
        for s in shipments:
            context_parts.append(f"  Warehouse: {s.get('warehouse')} | Status: {s.get('status')} | Method: {s.get('ship_method') or 'Not set'}")
            if s.get('tracking_number'): context_parts.append(f"  Tracking: {s.get('tracking_number')}")
            if s.get('pro_number'): context_parts.append(f"  PRO: {s.get('pro_number')}")
            if s.get('shipped_at'): context_parts.append(f"  Shipped At: {s.get('shipped_at')}")
            if s.get('delivered_at'): context_parts.append(f"  Delivered At: {s.get('delivered_at')}")
            if s.get('bol_sent'): context_parts.append(f"  BOL Sent At: {s.get('bol_sent_at')}")

    if snippets:
        context_parts.append("\nEMAIL COMMUNICATIONS:")
        for s in snippets:
            date_str = s['email_date'].strftime('%m/%d %H:%M') if s.get('email_date') else ''
            context_parts.append(f"  [{date_str}] From: {s.get('email_from', 'Unknown')} | Subject: {s.get('email_subject', '')}")
            if s.get('email_snippet'):
                context_parts.append(f"  {s['email_snippet'][:300]}")

    if events:
        context_parts.append("\nORDER EVENTS (most recent first):")
        for e in events:
            date_str = e['created_at'].strftime('%m/%d %H:%M') if e.get('created_at') else ''
            context_parts.append(f"  [{date_str}] {e.get('event_type')}")

    context = "\n".join(context_parts)

    prompt = f"""You are generating a 6-bullet state summary for an internal cabinet order dashboard.

PRODUCE EXACTLY 6 BULLETS. Each bullet starts with "• " and is ONE sentence.
NO markdown, NO headers, NO bold. Plain text only.
Use relative time ("2 days ago", "3 hours ago", "this morning", "just now").
Today's date/time context is available from the timestamps in the data.

Required bullet order — always all 6, even if the answer is "not yet":
1. Order age and dollar value (e.g. "• Order is 2 days old — $4,250 due")
2. Payment status (e.g. "• Payment received 3 hours ago" or "• Payment not received — invoice sent this morning")
3. Warehouse order status (e.g. "• Order sent to LI warehouse 1 day ago" or "• Not yet sent to warehouse — awaiting payment")
4. BOL and shipment status (e.g. "• BOL sent Apr 4, tracking PRO 12345" or "• No BOL yet — shipment pending")
5. Supplier/warehouse communication (e.g. "• Email from LI 2 hours ago confirming order received" or "• No supplier contact yet")
6. Next action (e.g. "• Escalate: no tracking after 2 days — call warehouse" or "• Wait for payment then send to warehouse")

Use the internal notes and email communications if present — they contain important context.

ORDER DATA:
{context}"""

    return call_anthropic_api(prompt, max_tokens=400)


def generate_comprehensive_summary(order_id: str) -> str:
    """Generate detailed comprehensive summary for order popup - full history analysis"""

    # Gather all order data
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get order details
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            order = cur.fetchone()

            if not order:
                return "Order not found"

            # Get ALL email snippets (more than card summary)
            cur.execute("""
                SELECT email_from, email_subject, email_snippet, email_date, snippet_type
                FROM order_email_snippets
                WHERE order_id = %s
                ORDER BY email_date ASC
            """, (order_id,))
            snippets = cur.fetchall()

            # Get ALL events
            cur.execute("""
                SELECT event_type, event_data, created_at
                FROM order_events
                WHERE order_id = %s
                ORDER BY created_at ASC
            """, (order_id,))
            events = cur.fetchall()

            # Get shipments
            cur.execute("""
                SELECT warehouse, ship_method, tracking, pro_number,
                       status, weight, ship_method, created_at
                FROM order_shipments
                WHERE order_id = %s
                ORDER BY created_at ASC
            """, (order_id,))
            shipments = cur.fetchall()

    # Build comprehensive context for AI
    context_parts = []

    # Order info
    context_parts.append(f"ORDER #{order_id}")
    context_parts.append(f"Customer: {order.get('company_name') or order.get('customer_name')}")
    context_parts.append(f"Status: {order.get('status', 'Unknown')}")
    context_parts.append(f"Order Total: ${order.get('order_total', 0)}")
    context_parts.append(f"Payment Received: {'Yes' if order.get('payment_received') else 'No'}")
    context_parts.append(f"Created: {order.get('created_at')}")

    if order.get('tracking'):
        context_parts.append(f"Tracking: {order.get('tracking')}")
    if order.get('pro_number'):
        context_parts.append(f"PRO Number: {order.get('pro_number')}")
    if order.get('comments'):
        context_parts.append(f"Customer Comments: {order.get('comments')}")
    if order.get('notes'):
        context_parts.append(f"Internal Notes: {order.get('notes')}")

    # Warehouses
    warehouses = [order.get(f'warehouse_{i}') for i in range(1, 5) if order.get(f'warehouse_{i}')]
    if warehouses:
        context_parts.append(f"Warehouses: {', '.join(warehouses)}")

    # Shipments
    if shipments:
        context_parts.append("\n--- SHIPMENTS ---")
        for s in shipments:
            context_parts.append(f"Warehouse: {s.get('warehouse')} | Carrier: {s.get('carrier')} | Status: {s.get('status')}")
            if s.get('tracking'):
                context_parts.append(f"  Tracking: {s.get('tracking')}")
            if s.get('pro_number'):
                context_parts.append(f"  PRO: {s.get('pro_number')}")
            if s.get('weight'):
                context_parts.append(f"  Weight: {s.get('weight')} lbs | Cost: ${s.get('ship_method', 0)}")

    # ALL Email communications (chronological for full history)
    if snippets:
        context_parts.append("\n--- EMAIL HISTORY (oldest to newest) ---")
        for s in snippets:
            date_str = s['email_date'].strftime('%m/%d/%y %H:%M') if s.get('email_date') else ''
            context_parts.append(f"[{date_str}] From: {s.get('email_from', 'Unknown')}")
            context_parts.append(f"Subject: {s.get('email_subject', '')}")
            if s.get('email_snippet'):
                # Include more of the snippet for comprehensive view
                context_parts.append(f"{s['email_snippet'][:500]}")
            context_parts.append("")

    # ALL Events (chronological)
    if events:
        context_parts.append("\n--- EVENT TIMELINE ---")
        for e in events:
            date_str = e['created_at'].strftime('%m/%d/%y %H:%M') if e.get('created_at') else ''
            event_data = e.get('event_data', '')
            if isinstance(event_data, dict):
                event_data = json.dumps(event_data)
            context_parts.append(f"[{date_str}] {e.get('event_type')}: {str(event_data)[:200]}")

    context = "\n".join(context_parts)

    # Comprehensive prompt
    prompt = f"""You are analyzing a cabinet order for a wholesale business. Provide a COMPREHENSIVE summary that helps staff understand the full history of this order.

Include these sections:
1. **Order Overview** - Customer, total, payment status, current stage
2. **Timeline Summary** - Key dates and what happened chronologically  
3. **Communication History** - Important points from emails (customer requests, issues, confirmations)
4. **Shipping Status** - What shipped from where, tracking info, delivery status
5. **Issues & Resolutions** - Any problems that came up and how they were handled
6. **Current Status & Next Steps** - Where things stand now and what needs to happen next

Format rules:
- Use clear section headers
- Use bullet points within sections
- Include specific dates when relevant
- Highlight any unusual requests or issues
- Be thorough but organized
- If information is missing for a section, skip that section

ORDER DATA:
{context}"""

    return call_anthropic_api(prompt, max_tokens=2048)


def generate_simple_summary(text: str, max_length: int = 200) -> str:
    """Generate a simple summary of any text"""
    if not is_configured():
        return text[:max_length] + "..." if len(text) > max_length else text

    prompt = f"""Summarize this in {max_length} characters or less. Be concise:

{text}"""

    return call_anthropic_api(prompt, max_tokens=256)
