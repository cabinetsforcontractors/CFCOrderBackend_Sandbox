"""
config.py
Centralized configuration for CFC Order Backend.
All environment variables and constants in one place.

⚠️ TEST LANE (William 2026-08-02): ALL 10 supplier emails point at
   homesupplyplus@gmail.com — the warehouse actor in the end-to-end test
   (William answers as Bella from that inbox). Customer actor = 4wprince,
   admin = orders@/wpjob1.

   RESTORE BEFORE GO-LIVE — the real supplier addresses:
     LI:              cabinetrydistribution@gmail.com
     DL:              ecomm@dlcabinetry.com
     ROC:             weborders01@roccabinetry.com
     Go Bravura:      vpan@gobravura.com
     Love-Milestone:  lovetoucheskitchen@gmail.com
     Cabinet & Stone: amy@cabinetstonellc.com
     DuraStone:       ranji@durastoneusa.com
     L&C Cabinetry:   lnccabinetryvab@gmail.com
     GHI:             orders@ghicabinets.com
     Linda:           linda@dealercabinetry.com
"""

import os

# =============================================================================
# DATABASE CONFIG
# =============================================================================

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if DATABASE_URL and "sslmode" not in DATABASE_URL:
    DATABASE_URL += ("&" if "?" in DATABASE_URL else "?") + "sslmode=require"

# =============================================================================
# API CONFIGS
# =============================================================================

B2BWAVE_URL = os.environ.get("B2BWAVE_URL", "").strip().rstrip('/')
B2BWAVE_USERNAME = os.environ.get("B2BWAVE_USERNAME", "").strip()
B2BWAVE_API_KEY = os.environ.get("B2BWAVE_API_KEY", "").strip()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
SHIPPO_API_KEY = os.environ.get("SHIPPO_API_KEY", "").strip()
SQUARE_ACCESS_TOKEN = os.environ.get("SQUARE_ACCESS_TOKEN", "").strip()
SQUARE_ENVIRONMENT = os.environ.get("SQUARE_ENVIRONMENT", "sandbox").strip()
CHECKOUT_BASE_URL = os.environ.get("CHECKOUT_BASE_URL", "").strip()
CHECKOUT_SECRET = os.environ.get("CHECKOUT_SECRET", "default-secret-change-me")
GMAIL_SEND_ENABLED = os.environ.get("GMAIL_SEND_ENABLED", "false").lower() == "true"
RL_QUOTE_SANDBOX_URL = os.environ.get("RL_QUOTE_SANDBOX_URL", "https://rl-quote-sandbox.onrender.com").strip()

# =============================================================================
# AUTO-SYNC CONFIG
# William 2026-08-02: orders poll every 2 MINUTES (720 calls/day — .net
# orders feel instant). The heavy companions (Gmail, Square, AI refresh)
# keep the old 7.5-minute rhythm — see sync_service. A 1 AM ET deep catch
# sweeps a 14-day window nightly as the belt-and-suspenders net.
# =============================================================================

AUTO_SYNC_INTERVAL_MINUTES = 2
AUTO_SYNC_COMPANION_MINUTES = 7.5
AUTO_SYNC_DAYS_BACK = 7

# =============================================================================
# SUPPLIER INFO
# ⚠️ TEST LANE (William 2026-08-02): every 'email' below = the warehouse
#   actor homesupplyplus@gmail.com. The real address to restore before
#   go-live rides each line as a comment (full roster in the file header).
# =============================================================================

SUPPLIER_INFO = {
    'LI': {
        'name': 'Cabinetry Distribution',
        'address': '561 Keuka Rd, Interlachen FL 32148',
        'contact': 'Li Yang (615) 410-6775',
        'email': 'homesupplyplus@gmail.com'  # RESTORE: cabinetrydistribution@gmail.com
    },
    'DL': {
        'name': 'DL Cabinetry',
        'address': '8145 Baymeadows Way W, Jacksonville FL 32256',
        'contact': 'Lily Chen (904) 723-1061',
        'email': 'homesupplyplus@gmail.com'  # RESTORE: ecomm@dlcabinetry.com
    },
    'ROC': {
        'name': 'ROC Cabinetry',
        'address': '505 Best Friend Court Suite 580, Norcross GA 30071',
        'contact': 'Franklin Velasquez (770) 847-8222',
        'email': 'homesupplyplus@gmail.com'  # RESTORE: weborders01@roccabinetry.com
    },
    'Go Bravura': {
        'name': 'Go Bravura',
        'address': '14200 Hollister Street Suite 200, Houston TX 77066',
        'contact': 'Vincent Pan (832) 756-2768',
        'email': 'homesupplyplus@gmail.com'  # RESTORE: vpan@gobravura.com
    },
    'Love-Milestone': {
        'name': 'Love-Milestone',
        'address': '10963 Florida Crown Dr STE 100, Orlando FL 32824',
        # William 2026-07-30: the contact is Bella now, no longer Ireen
        'contact': 'Bella',
        'email': 'homesupplyplus@gmail.com'  # RESTORE: lovetoucheskitchen@gmail.com
    },
    'Cabinet & Stone': {
        'name': 'Cabinet & Stone',
        'address': '1760 Stebbins Dr, Houston TX 77043',
        'contact': 'Amy Cao (281) 833-0980',
        'email': 'homesupplyplus@gmail.com'  # RESTORE: amy@cabinetstonellc.com
    },
    'DuraStone': {
        'name': 'DuraStone',
        'address': '9815 North Fwy, Houston TX 77037',
        'contact': 'Ranjith Venugopalan / Rachel Guo (832) 228-7866',
        'email': 'homesupplyplus@gmail.com'  # RESTORE: ranji@durastoneusa.com
    },
    'L&C Cabinetry': {
        'name': 'L&C Cabinetry',
        'address': '2028 Virginia Beach Blvd, Virginia Beach VA 23454',
        'contact': 'Rey Allison (757) 917-5619',
        'email': 'homesupplyplus@gmail.com'  # RESTORE: lnccabinetryvab@gmail.com
    },
    'GHI': {
        'name': 'GHI Cabinets',
        'address': '1807 48th Ave E Unit 110, Palmetto FL 34221',
        'contact': 'Kathryn Belfiore (941) 479-8070',
        # William 2026-07-28: orders go to the orders box going forward
        'email': 'homesupplyplus@gmail.com'  # RESTORE: orders@ghicabinets.com
    },
    'Linda': {
        'name': 'Dealer Cabinetry',
        'address': '202 West Georgia Ave, Bremen GA 30110',
        'contact': 'Linda Yang (678) 821-3505',
        'email': 'homesupplyplus@gmail.com'  # RESTORE: linda@dealercabinetry.com
    }
}

# =============================================================================
# WAREHOUSE CONFIG
# =============================================================================

WAREHOUSE_ZIPS = {
    'LI': '32148',
    'DL': '32256',
    'ROC': '30071',
    'GHI': '34221',
    'Go Bravura': '77066',
    'Love-Milestone': '32824',
    'Cabinet & Stone': '77043',
    'Cabinet & Stone CA': '90660',
    'DuraStone': '77037',
    'L&C Cabinetry': '23454',
    'Linda': '30110',
    'Cabinetry Distribution': '32148',
    'DL Cabinetry': '32256',
    'ROC Cabinetry': '30071',
    'GHI Cabinets': '34221',
    'Dealer Cabinetry': '30110',
}

OVERSIZED_KEYWORDS = ['OVEN', 'PANTRY', '96"', '96*', 'X96', '96X', '96H', '96 H']

# =============================================================================
# COMMERCIAL ADDRESS OVERRIDES (William 2026-07-30)
# Addresses that ALWAYS quote commercial no matter what Smarty or a carrier
# classifier says. 561 Keuka Rd = LI's warehouse dock in Interlachen — the
# 5731/5676 lesson: it LOOKS residential to classifiers, it is a commercial
# warehouse. Match = case-insensitive substring against the destination
# street line.
# =============================================================================

COMMERCIAL_ADDRESS_OVERRIDES = [
    "561 keuka",
]


def is_commercial_override(street: str) -> bool:
    """True when the destination street is a known commercial address that
    must never quote residential (William 2026-07-30)."""
    s = (street or "").lower()
    return any(tok in s for tok in COMMERCIAL_ADDRESS_OVERRIDES)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def pays_by_check(order) -> bool:
    """Accounts invoiced WITHOUT a Square payment link — they pay by check
    (William 2026-07-29: Nationwide). NO_PAYMENT_LINK_CUSTOMERS env =
    comma-separated substrings matched case-insensitively against the
    order's company and customer names; the Nationwide default stands
    until overridden."""
    tokens = [t.strip().lower() for t in os.environ.get(
        "NO_PAYMENT_LINK_CUSTOMERS", "nationwide custom homes").split(",")
        if t.strip()]
    hay = " ".join([str((order or {}).get("company_name") or ""),
                    str((order or {}).get("customer_name") or "")]).lower()
    return any(t in hay for t in tokens)


def is_b2bwave_configured():
    return bool(B2BWAVE_URL and B2BWAVE_USERNAME and B2BWAVE_API_KEY)

def is_anthropic_configured():
    return bool(ANTHROPIC_API_KEY)

def is_shippo_configured():
    return bool(SHIPPO_API_KEY)

def is_square_configured():
    return bool(SQUARE_ACCESS_TOKEN)

def is_rl_quote_sandbox_configured():
    return bool(RL_QUOTE_SANDBOX_URL)
