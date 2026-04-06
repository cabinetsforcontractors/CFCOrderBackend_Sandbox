"""
config.py
Centralized configuration for CFC Order Backend.
All environment variables and constants in one place.
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
# =============================================================================

AUTO_SYNC_INTERVAL_MINUTES = 7.5
AUTO_SYNC_DAYS_BACK = 7

# =============================================================================
# SUPPLIER INFO
# =============================================================================

SUPPLIER_INFO = {
    'LI': {
        'name': 'Cabinetry Distribution',
        'address': '561 Keuka Rd, Interlachen FL 32148',
        'contact': 'Li Yang (615) 410-6775',
        'email': 'cabinetrydistribution@gmail.com'
    },
    'DL': {
        'name': 'DL Cabinetry',
        'address': '8145 Baymeadows Way W, Jacksonville FL 32256',
        'contact': 'Lily Chen (904) 723-1061',
        'email': 'ecomm@dlcabinetry.com'
    },
    'ROC': {
        'name': 'ROC Cabinetry',
        'address': '505 Best Friend Court Suite 580, Norcross GA 30071',
        'contact': 'Franklin Velasquez (770) 847-8222',
        'email': 'weborders01@roccabinetry.com'
    },
    'Go Bravura': {
        'name': 'Go Bravura',
        'address': '14200 Hollister Street Suite 200, Houston TX 77066',
        'contact': 'Vincent Pan (832) 756-2768',
        'email': 'vpan@gobravura.com'
    },
    'Love-Milestone': {
        'name': 'Love-Milestone',
        'address': '10963 Florida Crown Dr STE 100, Orlando FL 32824',
        'contact': 'Ireen',
        'email': 'lovetoucheskitchen@gmail.com'
    },
    'Cabinet & Stone': {
        'name': 'Cabinet & Stone',
        'address': '1760 Stebbins Dr, Houston TX 77043',
        'contact': 'Amy Cao (281) 833-0980',
        'email': 'amy@cabinetstonellc.com'
    },
    'DuraStone': {
        'name': 'DuraStone',
        'address': '9815 North Fwy, Houston TX 77037',
        'contact': 'Ranjith Venugopalan / Rachel Guo (832) 228-7866',
        'email': 'ranji@durastoneusa.com'
    },
    'L&C Cabinetry': {
        'name': 'L&C Cabinetry',
        'address': '2028 Virginia Beach Blvd, Virginia Beach VA 23454',
        'contact': 'Rey Allison (757) 917-5619',
        'email': 'lnccabinetryvab@gmail.com'
    },
    'GHI': {
        'name': 'GHI Cabinets',
        'address': '1807 48th Ave E Unit 110, Palmetto FL 34221',
        'contact': 'Kathryn Belfiore (941) 479-8070',
        'email': 'kbelfiore@ghicabinets.com'
    },
    'Linda': {
        'name': 'Dealer Cabinetry',
        'address': '202 West Georgia Ave, Bremen GA 30110',
        'contact': 'Linda Yang (678) 821-3505',
        'email': 'linda@dealercabinetry.com'
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
    'Cabinet & Stone CA': '90723',
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
# HELPER FUNCTIONS
# =============================================================================

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
