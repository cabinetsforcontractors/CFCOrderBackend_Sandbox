"""
gmail_sync.py
Gmail email scanning for CFC Order Workflow
Detects: Payment links sent, Payments received (Square), RL Quotes, Tracking numbers

Phase 3B Enhancement:
  - Tracks last_customer_email_at per order for lifecycle engine
  - Detects "cancel" keyword in customer emails
  - Tags system-generated emails so they don't reset lifecycle clock
"""

import os
import re
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# Gmail API Config - loaded from environment
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "").strip()
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "").strip()
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "").strip()

# Email sender patterns
SQUARE_PAYMENT_SENDER = "noreply@messaging.squareup.com"
RL_CARRIERS_SENDER = "rlloads@rlcarriers.com"

# System-generated email subjects (do NOT reset lifecycle clock)
SYSTEM_EMAIL_SUBJECTS = [
    "order hasn't been paid",
    "order marked inactive",
    "order will be deleted",
    "order will be canceled",
    "cancellation confirmation",
]

# Cache access token
_access_token = None
_token_expires = None

def gmail_configured():
    """Check if Gmail credentials are configured"""
    return bool(GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN)

def get_gmail_access_token():
    """Get a fresh access token using the refresh token"""
    global _access_token, _token_expires
    
    # Return cached token if still valid
    if _access_token and _token_expires and datetime.now(timezone.utc) < _token_expires:
        return _access_token
    
    if not gmail_configured():
        print("[GMAIL] Not configured")
        return None
    
    try:
        token_data = urllib.parse.urlencode({
            'client_id': GMAIL_CLIENT_ID,
            'client_secret': GMAIL_CLIENT_SECRET,
            'refresh_token': GMAIL_REFRESH_TOKEN,
            'grant_type': 'refresh_token'
        }).encode()
        
        req = urllib.request.Request('https://oauth2.googleapis.com/token', data=token_data)
        
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            _access_token = data.get('access_token')
            # Token typically valid for 1 hour, we'll refresh at 50 min
            _token_expires = datetime.now(timezone.utc) + timedelta(minutes=50)
            return _access_token
            
    except Exception as e:
        print(f"[GMAIL] Token refresh error: {e}")
        return None

def gmail_api_request(endpoint, params=None):
    """Make authenticated request to Gmail API"""
    token = get_gmail_access_token()
    if not token:
        return None
    
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        print(f"[GMAIL] API error {e.code}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        print(f"[GMAIL] Request error: {e}")
        return None

def search_emails(query, max_results=50):
    """Search Gmail for messages matching query"""
    data = gmail_api_request("messages", {"q": query, "maxResults": max_results})
    if not data:
        return []
    return data.get("messages", [])

def get_email_content(message_id):
    """Get email details including subject, from, body"""
    data = gmail_api_request(f"messages/{message_id}", {"format": "full"})
    if not data:
        return None
    
    headers = {h['name'].lower(): h['value'] for h in data.get('payload', {}).get('headers', [])}
    
    # Extract body
    body = ""
    payload = data.get('payload', {})
    
    if 'body' in payload and payload['body'].get('data'):
        import base64
        body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')
    elif 'parts' in payload:
        for part in payload['parts']:
            if part.get('mimeType') == 'text/plain' and part.get('body', {}).get('data'):
                import base64
                body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
                break
    
    return {
        'id': message_id,
        'subject': headers.get('subject', ''),
        'from': headers.get('from', ''),
        'to': headers.get('to', ''),
        'date': headers.get('date', ''),
        'body': body
    }

def extract_order_id(text):
    """Extract order ID from text (4-5 digit number)"""
    # Look for patterns like "order 5307" or "#5307" or "Order #5307"
    match = re.search(r'(?:order\s*#?\s*|#)(\d{4,5})\b', text, re.IGNORECASE)
    if match:
        return match.group(1)
    
    # Try standalone 4-5 digit numbers (less reliable)
    match = re.search(r'\b(\d{4,5})\b', text)
    if match:
        return match.group(1)
    
    return None

def extract_payment_amount(text):
    """Extract dollar amount from text"""
    match = re.search(r'\$([\d,]+\.?\d*)', text)
    if match:
        return float(match.group(1).replace(',', ''))
    return None

def extract_customer_name(text):
    """Extract customer name from Square payment email"""
    # Pattern: "$X payment received from Name"
    match = re.search(r'payment received from\s+([^\n]+)', text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None

# Import needed for base64 in get_email_content
import urllib.parse


# =============================================================================
# LIFECYCLE HELPERS (Phase 3B)
# =============================================================================

def is_system_generated_email(subject: str) -> bool:
    """
    Check if an email subject indicates a system-generated lifecycle email.
    System emails do NOT reset the lifecycle clock.
    """
    if not subject:
        return False
    subject_lower = subject.lower()
    for pattern in SYSTEM_EMAIL_SUBJECTS:
        if pattern in subject_lower:
            return True
    return False


def is_customer_email(email_from: str, email_to: str) -> str:
    """
    Determine if this is a customer-related email (to or from customer).
    Returns 'from_customer', 'to_customer', or 'internal'.
    
    CFC emails: cabinetsforcontractors, william, 4wprince
    """
    cfc_patterns = ['cabinetsforcontractors', 'william', '4wprince', 'team@cabinetcloudai']
    
    from_lower = (email_from or '').lower()
    to_lower = (email_to or '').lower()
    
    from_is_cfc = any(p in from_lower for p in cfc_patterns)
    to_is_cfc = any(p in to_lower for p in cfc_patterns)
    
    if not from_is_cfc and to_is_cfc:
        return 'from_customer'
    elif from_is_cfc and not to_is_cfc:
        return 'to_customer'
    else:
        return 'internal'


def update_last_customer_email(conn, order_id: str, email_date_str: str = None):
    """
    Update last_customer_email_at for an order.
    Called on every customer-related email detected during sync.
    
    This drives the lifecycle engine's day counter.
    """
    with conn.cursor() as cur:
        if email_date_str:
            try:
                # Try parsing the email date
                email_dt = datetime.fromisoformat(email_date_str.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                email_dt = datetime.now(timezone.utc)
        else:
            email_dt = datetime.now(timezone.utc)
        
        # Only update if this email is MORE RECENT than the current value
        cur.execute("""
            UPDATE orders 
            SET last_customer_email_at = GREATEST(
                COALESCE(last_customer_email_at, '1970-01-01'::timestamptz),
                %s
            ),
            updated_at = NOW()
            WHERE order_id = %s
        """, (email_dt, order_id))
        
        conn.commit()


def check_cancel_keyword(conn, order_id: str, email_body: str, email_subject: str):
    """
    Check if a customer email contains a cancel keyword.
    If detected, triggers lifecycle cancellation.
    
    Returns True if cancel was detected and processed.
    """
    from lifecycle_engine import detect_cancel_keyword, cancel_order
    
    text = f"{email_subject} {email_body}"
    if detect_cancel_keyword(text):
        print(f"[GMAIL] Cancel keyword detected for order {order_id}")
        result = cancel_order(order_id, reason="customer_request")
        
        # Log the detection event
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'cancel_keyword_detected', %s, 'gmail_sync')
            """, (order_id, json.dumps({
                'subject': email_subject[:100],
                'body_snippet': email_body[:200]
            })))
            conn.commit()
        
        return True
    return False


# =============================================================================
# MAIN SYNC FUNCTION
# =============================================================================

def run_gmail_sync(db_conn, hours_back=2):
    """
    Main email sync function - scans Gmail and updates orders.
    
    Phase 3B: Now also tracks last_customer_email_at for lifecycle engine
    and detects cancel keywords in customer emails.
    
    Returns dict with counts of what was processed.
    """
    if not gmail_configured():
        print("[GMAIL] Not configured, skipping email sync")
        return {"status": "skipped", "reason": "not_configured"}
    
    print(f"[GMAIL] Starting email sync (last {hours_back} hours)")
    
    results = {
        "payment_links": 0,
        "payments_received": 0,
        "rl_quotes": 0,
        "tracking_numbers": 0,
        "lifecycle_updates": 0,
        "cancel_detections": 0,
        "errors": []
    }
    
    time_filter = f"newer_than:{hours_back}h"
    
    # 1. Payment Links Sent (sent emails with square.link)
    try:
        messages = search_emails(f"{time_filter} in:sent square.link")
        print(f"[GMAIL] Found {len(messages)} sent emails with square.link")
        
        for msg in messages:
            try:
                email = get_email_content(msg['id'])
                if not email:
                    continue
                
                # Only process if we sent it
                if 'cabinetsforcontractors' not in email['from'].lower() and 'william' not in email['from'].lower():
                    continue
                
                if 'square.link' not in email['body'].lower():
                    continue
                
                order_id = extract_order_id(email['subject'] + ' ' + email['body'])
                if order_id:
                    update_order_payment_link_sent(db_conn, order_id, email)
                    results["payment_links"] += 1
                    
                    # Phase 3B: Update lifecycle (sent TO customer, so it's customer-related)
                    if not is_system_generated_email(email['subject']):
                        update_last_customer_email(db_conn, order_id, email.get('date'))
                        results["lifecycle_updates"] += 1
                    
            except Exception as e:
                results["errors"].append(f"Payment link error: {e}")
                
    except Exception as e:
        results["errors"].append(f"Payment link search error: {e}")
    
    # 2. Payments Received (Square notifications)
    try:
        messages = search_emails(f'{time_filter} from:{SQUARE_PAYMENT_SENDER} subject:"payment received"')
        print(f"[GMAIL] Found {len(messages)} Square payment emails")
        
        for msg in messages:
            try:
                email = get_email_content(msg['id'])
                if not email:
                    continue
                
                amount = extract_payment_amount(email['subject'])
                customer_name = extract_customer_name(email['subject'])
                
                if amount and customer_name:
                    matched = match_payment_to_order(db_conn, amount, customer_name, email)
                    if matched:
                        results["payments_received"] += 1
                        
            except Exception as e:
                results["errors"].append(f"Payment received error: {e}")
                
    except Exception as e:
        results["errors"].append(f"Payment search error: {e}")
    
    # 3. RL Quote Numbers
    try:
        messages = search_emails(f'{time_filter} ("RL Quote" OR "quote number" OR from:{RL_CARRIERS_SENDER})')
        print(f"[GMAIL] Found {len(messages)} potential RL quote emails")
        
        for msg in messages:
            try:
                email = get_email_content(msg['id'])
                if not email:
                    continue
                
                # Look for quote number pattern
                quote_match = re.search(r'(?:RL\s+)?Quote\s*(?:No|#)?[:\s]*(\d{6,10})', 
                                       email['body'], re.IGNORECASE)
                if quote_match:
                    quote_no = quote_match.group(1)
                    order_id = extract_order_id(email['subject'] + ' ' + email['body'])
                    if order_id:
                        update_order_rl_quote(db_conn, order_id, quote_no, email)
                        results["rl_quotes"] += 1
                        
            except Exception as e:
                results["errors"].append(f"RL quote error: {e}")
                
    except Exception as e:
        results["errors"].append(f"RL quote search error: {e}")
    
    # 4. Tracking Numbers / PRO Numbers
    try:
        messages = search_emails(f'{time_filter} (PRO OR tracking OR "has shipped")')
        print(f"[GMAIL] Found {len(messages)} potential tracking emails")
        
        for msg in messages:
            try:
                email = get_email_content(msg['id'])
                if not email:
                    continue
                
                text = email['subject'] + ' ' + email['body']
                
                # PRO number pattern
                pro_match = re.search(r'PRO\s*(?:#|Number)?[:\s]*([A-Z]{0,2}\d{8,10}(?:-\d)?)', 
                                     text, re.IGNORECASE)
                if pro_match:
                    pro_no = pro_match.group(1).upper()
                    order_id = extract_order_id(text)
                    if order_id:
                        update_order_tracking(db_conn, order_id, pro_no, 'PRO', email)
                        results["tracking_numbers"] += 1
                        continue
                
                # UPS tracking (1Z...)
                ups_match = re.search(r'\b(1Z[A-Z0-9]{16})\b', text)
                if ups_match:
                    order_id = extract_order_id(text)
                    if order_id:
                        update_order_tracking(db_conn, order_id, ups_match.group(1), 'UPS', email)
                        results["tracking_numbers"] += 1
                        
            except Exception as e:
                results["errors"].append(f"Tracking error: {e}")
                
    except Exception as e:
        results["errors"].append(f"Tracking search error: {e}")
    
    # 5. Phase 3B: Scan for customer emails (lifecycle tracking + cancel detection)
    try:
        messages = search_emails(f'{time_filter} (in:inbox OR in:sent) (order OR cabinet OR shipping)')
        print(f"[GMAIL] Found {len(messages)} potential customer emails for lifecycle tracking")
        
        for msg in messages:
            try:
                email = get_email_content(msg['id'])
                if not email:
                    continue
                
                # Skip system-generated emails
                if is_system_generated_email(email.get('subject', '')):
                    continue
                
                # Determine if customer-related
                direction = is_customer_email(email.get('from', ''), email.get('to', ''))
                if direction == 'internal':
                    continue
                
                # Try to find order ID
                text = email['subject'] + ' ' + email['body']
                order_id = extract_order_id(text)
                if not order_id:
                    continue
                
                # Update lifecycle timestamp
                update_last_customer_email(db_conn, order_id, email.get('date'))
                results["lifecycle_updates"] += 1
                
                # Check for cancel keyword (only in customer-sent emails)
                if direction == 'from_customer':
                    try:
                        canceled = check_cancel_keyword(
                            db_conn, order_id, 
                            email.get('body', ''), 
                            email.get('subject', '')
                        )
                        if canceled:
                            results["cancel_detections"] += 1
                    except Exception as e:
                        # Don't let cancel detection failure break the sync
                        results["errors"].append(f"Cancel detection error for {order_id}: {e}")
                
            except Exception as e:
                results["errors"].append(f"Lifecycle tracking error: {e}")
                
    except Exception as e:
        results["errors"].append(f"Lifecycle scan error: {e}")
    
    print(f"[GMAIL] Sync complete: {results}")
    return results


# =============================================================================
# DATABASE UPDATE FUNCTIONS
# =============================================================================

def update_order_payment_link_sent(conn, order_id, email):
    """Mark order as payment link sent"""
    from psycopg2.extras import RealDictCursor
    
    with conn.cursor() as cur:
        # Check if already marked
        cur.execute("SELECT payment_link_sent FROM orders WHERE order_id = %s", (order_id,))
        row = cur.fetchone()
        if not row:
            print(f"[GMAIL] Order {order_id} not found")
            return False
        if row[0]:  # Already marked
            return False
        
        cur.execute("""
            UPDATE orders SET 
                payment_link_sent = TRUE,
                payment_link_sent_at = NOW(),
                updated_at = NOW()
            WHERE order_id = %s
        """, (order_id,))
        
        # Log event
        cur.execute("""
            INSERT INTO order_events (order_id, event_type, event_data, source)
            VALUES (%s, 'payment_link_sent', %s, 'gmail_sync')
        """, (order_id, json.dumps({'subject': email['subject'][:100]})))
        
        conn.commit()
        print(f"[GMAIL] Order {order_id}: payment link sent")
        return True

def match_payment_to_order(conn, amount, customer_name, email):
    """Try to match a Square payment to an order"""
    from psycopg2.extras import RealDictCursor
    
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Try to find matching order by amount and customer name
        cur.execute("""
            SELECT order_id, customer_name, company_name, order_total, payment_received
            FROM orders 
            WHERE payment_received = FALSE
            AND (
                LOWER(customer_name) LIKE LOWER(%s)
                OR LOWER(company_name) LIKE LOWER(%s)
            )
            ORDER BY order_date DESC
            LIMIT 5
        """, (f'%{customer_name.split()[0]}%', f'%{customer_name.split()[0]}%'))
        
        candidates = cur.fetchall()
        
        for order in candidates:
            order_total = float(order['order_total'] or 0)
            # Payment might include shipping, so check if amount >= order total
            if amount >= order_total * 0.95:  # Allow 5% variance
                # Found match
                cur.execute("""
                    UPDATE orders SET 
                        payment_received = TRUE,
                        payment_received_at = NOW(),
                        payment_amount = %s,
                        updated_at = NOW()
                    WHERE order_id = %s
                """, (amount, order['order_id']))
                
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, 'payment_received', %s, 'gmail_sync')
                """, (order['order_id'], json.dumps({
                    'amount': amount, 
                    'customer': customer_name,
                    'subject': email['subject'][:100]
                })))
                
                # Phase 3B: Payment is customer activity — update lifecycle
                update_last_customer_email(conn, order['order_id'])
                
                conn.commit()
                print(f"[GMAIL] Order {order['order_id']}: payment ${amount} received from {customer_name}")
                return True
        
        print(f"[GMAIL] No match for payment ${amount} from {customer_name}")
        return False

def update_order_rl_quote(conn, order_id, quote_no, email):
    """Update order with RL quote number"""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE orders SET 
                rl_quote_no = %s,
                updated_at = NOW()
            WHERE order_id = %s
        """, (quote_no, order_id))
        
        cur.execute("""
            INSERT INTO order_events (order_id, event_type, event_data, source)
            VALUES (%s, 'rl_quote_captured', %s, 'gmail_sync')
        """, (order_id, json.dumps({'quote_no': quote_no})))
        
        conn.commit()
        print(f"[GMAIL] Order {order_id}: RL quote {quote_no}")
        return True

def update_order_tracking(conn, order_id, tracking_no, carrier, email):
    """Update order with tracking number"""
    with conn.cursor() as cur:
        tracking_text = f"{carrier} {tracking_no}" if carrier != 'PRO' else f"R+L PRO {tracking_no}"
        
        cur.execute("""
            UPDATE orders SET 
                tracking = %s,
                pro_number = CASE WHEN %s = 'PRO' THEN %s ELSE pro_number END,
                updated_at = NOW()
            WHERE order_id = %s
        """, (tracking_text, carrier, tracking_no, order_id))
        
        cur.execute("""
            INSERT INTO order_events (order_id, event_type, event_data, source)
            VALUES (%s, 'tracking_captured', %s, 'gmail_sync')
        """, (order_id, json.dumps({'tracking': tracking_no, 'carrier': carrier})))
        
        conn.commit()
        print(f"[GMAIL] Order {order_id}: {carrier} tracking {tracking_no}")
        return True
