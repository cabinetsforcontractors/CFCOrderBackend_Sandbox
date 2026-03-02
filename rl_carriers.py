"""
rl_carriers.py
Direct R+L Carriers API integration for LTL freight quotes.
API Docs: https://api.rlc.com/swagger/ui/index#/RateQuote
"""

import json
import urllib.request
import urllib.error
import os
from typing import Dict, List, Optional
from datetime import datetime, timedelta

# R+L Carriers API configuration
RL_API_BASE_URL = "https://api.rlc.com"


def _get_api_key() -> str:
    """Get API key from environment (read at request time)"""
    return os.environ.get("RL_CARRIERS_API_KEY", "")


class RLCarriersError(Exception):
    """Custom exception for R+L Carriers API errors"""
    def __init__(self, message: str, errors: List[Dict] = None):
        self.message = message
        self.errors = errors or []
        super().__init__(message)


def is_configured() -> bool:
    """Check if R+L Carriers API is configured"""
    return bool(_get_api_key())


def _make_request(endpoint: str, method: str = "GET", data: dict = None) -> dict:
    """Make authenticated request to R+L Carriers API"""
    api_key = _get_api_key()
    if not api_key:
        raise RLCarriersError("R+L Carriers API key not configured")
    
    url = f"{RL_API_BASE_URL}/{endpoint}"
    
    req = urllib.request.Request(url, method=method)
    req.add_header("apiKey", api_key)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    
    if data:
        req.data = json.dumps(data).encode('utf-8')
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
            
            # Check for API errors (Code 200 = success, 0 also okay)
            response_code = result.get("Code", 200)
            errors = result.get("Errors", [])
            
            if response_code not in [0, 200] or errors:
                error_msg = "; ".join([e.get("ErrorMessage", "Unknown error") for e in errors]) if errors else f"API error (code {response_code})"
                raise RLCarriersError(error_msg, errors)
            
            return result
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        raise RLCarriersError(f"HTTP {e.code}: {error_body}")
    except urllib.error.URLError as e:
        raise RLCarriersError(f"Connection error: {str(e)}")


def get_rate_quote(
    origin_zip: str,
    origin_city: str,
    origin_state: str,
    dest_zip: str,
    dest_city: str,
    dest_state: str,
    weight_lbs: int,
    freight_class: str = "85",
    pieces: int = 1,
    length: float = None,
    width: float = None,
    height: float = None,
    additional_services: List[str] = None,
    pickup_date: str = None
) -> Dict:
    """
    Get LTL freight rate quote from R+L Carriers.
    
    Args:
        origin_zip: Origin ZIP code
        origin_city: Origin city name
        origin_state: Origin state (2-letter)
        dest_zip: Destination ZIP code
        dest_city: Destination city name
        dest_state: Destination state (2-letter)
        weight_lbs: Total weight in pounds
        freight_class: NMFC freight class (default "85" for RTA cabinets)
        pieces: Number of pieces/pallets
        length: Length in inches (optional)
        width: Width in inches (optional)
        height: Height in inches (optional)
        additional_services: List of accessorial codes (optional)
        pickup_date: Pickup date YYYY-MM-DD (optional, defaults to tomorrow)
    
    Returns:
        Dict with quote details including price and quote number
    """
    # Default pickup date to tomorrow if not specified (R+L wants MM/dd/yyyy format)
    if not pickup_date:
        pickup_date = (datetime.now() + timedelta(days=1)).strftime("%m/%d/%Y")
    
    # Build request payload
    payload = {
        "RateQuote": {
            "Origin": {
                "City": origin_city,
                "StateOrProvince": origin_state,
                "ZipOrPostalCode": origin_zip,
                "CountryCode": "USA"
            },
            "Destination": {
                "City": dest_city,
                "StateOrProvince": dest_state,
                "ZipOrPostalCode": dest_zip,
                "CountryCode": "USA"
            },
            "Items": [
                {
                    "Weight": int(weight_lbs),
                    "Class": freight_class
                }
            ],
            "PickupDate": pickup_date
        }
    }
    
    # Add dimensions if provided
    if length and width and height:
        payload["RateQuote"]["Items"][0]["Length"] = float(length)
        payload["RateQuote"]["Items"][0]["Width"] = float(width)
        payload["RateQuote"]["Items"][0]["Height"] = float(height)
    
    # Add additional services if provided
    if additional_services:
        payload["RateQuote"]["AdditionalServices"] = additional_services
    
    # Make API request
    result = _make_request("RateQuote", method="POST", data=payload)
    
    # Parse response
    rate_quote = result.get("RateQuote", {})
    service_levels = rate_quote.get("ServiceLevels", [])
    
    # Find standard service level (or first available)
    standard_quote = None
    for level in service_levels:
        if level.get("Code") == "STD" or level.get("Name") == "Standard":
            standard_quote = level
            break
    
    if not standard_quote and service_levels:
        standard_quote = service_levels[0]
    
    if not standard_quote:
        raise RLCarriersError("No rate quotes returned")
    
    # Extract pricing
    charge = standard_quote.get("Charge", "0")
    net_charge = standard_quote.get("NetCharge", charge)
    
    # Clean up price strings (remove $ and commas)
    def parse_price(price_str):
        if not price_str:
            return 0.0
        return float(str(price_str).replace("$", "").replace(",", ""))
    
    return {
        "quote_number": standard_quote.get("QuoteNumber"),
        "service_name": standard_quote.get("Name", "Standard"),
        "service_code": standard_quote.get("Code", "STD"),
        "service_days": standard_quote.get("ServiceDays", 0),
        "gross_charge": parse_price(charge),
        "net_charge": parse_price(net_charge),
        "customer_discounts": rate_quote.get("CustomerDiscounts", ""),
        "pickup_date": rate_quote.get("PickupDate"),
        "is_direct": rate_quote.get("IsDirect", False),
        "origin": rate_quote.get("Origin", {}),
        "destination": rate_quote.get("Destination", {}),
        "all_service_levels": service_levels,
        "charges": rate_quote.get("Charges", []),
        "messages": result.get("Messages", [])
    }


def get_simple_quote(
    origin_zip: str,
    dest_zip: str,
    weight_lbs: int,
    freight_class: str = "85"
) -> Dict:
    """
    Simplified rate quote - only requires ZIP codes and weight.
    City/state are looked up automatically by R+L.
    
    Args:
        origin_zip: Origin ZIP code
        dest_zip: Destination ZIP code
        weight_lbs: Total weight in pounds
        freight_class: NMFC freight class (default "85" for RTA cabinets)
    
    Returns:
        Dict with quote details
    """
    # R+L API requires city/state, but we can use placeholder values
    # and let R+L correct them based on ZIP
    # Using generic placeholders that R+L will override
    
    payload = {
        "RateQuote": {
            "Origin": {
                "ZipOrPostalCode": origin_zip,
                "CountryCode": "USA"
            },
            "Destination": {
                "ZipOrPostalCode": dest_zip,
                "CountryCode": "USA"
            },
            "Items": [
                {
                    "Weight": int(weight_lbs),
                    "Class": freight_class
                }
            ],
            "PickupDate": (datetime.now() + timedelta(days=1)).strftime("%m/%d/%Y")
        }
    }
    
    result = _make_request("RateQuote", method="POST", data=payload)
    
    rate_quote = result.get("RateQuote", {})
    service_levels = rate_quote.get("ServiceLevels", [])
    
    # Get standard service
    standard_quote = None
    for level in service_levels:
        if level.get("Code") == "STD" or level.get("Name") == "Standard":
            standard_quote = level
            break
    
    if not standard_quote and service_levels:
        standard_quote = service_levels[0]
    
    if not standard_quote:
        raise RLCarriersError("No rate quotes returned")
    
    def parse_price(price_str):
        if not price_str:
            return 0.0
        return float(str(price_str).replace("$", "").replace(",", ""))
    
    net_charge = parse_price(standard_quote.get("NetCharge", standard_quote.get("Charge", "0")))
    
    return {
        "quote_number": standard_quote.get("QuoteNumber"),
        "net_charge": net_charge,
        "service_days": standard_quote.get("ServiceDays", 0),
        "carrier": "R+L Carriers",
        "service": standard_quote.get("Name", "Standard LTL")
    }


def get_pallet_types() -> List[Dict]:
    """Get available pallet types from R+L"""
    result = _make_request("RateQuote/GetPalletTypes", method="GET")
    return result.get("PalletTypes", [])


def track_shipment(pro_number: str) -> Dict:
    """
    Track a shipment by PRO number.
    
    Args:
        pro_number: R+L PRO number
    
    Returns:
        Dict with tracking information
    """
    result = _make_request(f"ShipmentTracing?request.traceNumbers={pro_number}&request.traceType=PRO", method="GET")
    
    shipments = result.get("Shipments", [])
    if not shipments:
        raise RLCarriersError(f"No shipment found for PRO {pro_number}")
    
    return shipments[0]


# =============================================================================
# BILL OF LADING (BOL)
# =============================================================================

def create_bol(
    # Shipper info
    shipper_name: str,
    shipper_address: str,
    shipper_city: str,
    shipper_state: str,
    shipper_zip: str,
    shipper_phone: str,
    # Consignee info
    consignee_name: str,
    consignee_address: str,
    consignee_city: str,
    consignee_state: str,
    consignee_zip: str,
    consignee_phone: str,
    # Shipment details
    weight_lbs: int,
    pieces: int = 1,
    description: str = "RTA Cabinets",
    freight_class: str = "85",
    # Optional
    shipper_address2: str = "",
    consignee_address2: str = "",
    consignee_email: str = "",
    po_number: str = "",
    quote_number: str = "",
    special_instructions: str = "",
    bol_date: str = None,
    # Pickup request options
    include_pickup: bool = False,
    pickup_date: str = None,
    pickup_ready_time: str = "09:00",
    pickup_close_time: str = "17:00"
) -> Dict:
    """
    Create a Bill of Lading with R+L Carriers.
    
    Returns:
        Dict with PRO number and pickup request ID (if requested)
    """
    if not bol_date:
        bol_date = datetime.now().strftime("%m/%d/%Y")
    
    payload = {
        "BillOfLading": {
            "BOLDate": bol_date,
            "Shipper": {
                "CompanyName": shipper_name,
                "AddressLine1": shipper_address,
                "AddressLine2": shipper_address2,
                "City": shipper_city,
                "StateOrProvince": shipper_state,
                "ZipOrPostalCode": shipper_zip,
                "CountryCode": "USA",
                "PhoneNumber": shipper_phone
            },
            "Consignee": {
                "CompanyName": consignee_name,
                "AddressLine1": consignee_address,
                "AddressLine2": consignee_address2,
                "City": consignee_city,
                "StateOrProvince": consignee_state,
                "ZipOrPostalCode": consignee_zip,
                "CountryCode": "USA",
                "PhoneNumber": consignee_phone,
                "EmailAddress": consignee_email
            },
            "Items": [
                {
                    "Pieces": pieces,
                    "PackageType": "PLT",  # Pallet
                    "Description": description,
                    "Class": freight_class,
                    "Weight": int(weight_lbs)
                }
            ],
            "FreightChargePaymentMethod": "Prepaid",
            "ServiceLevel": "Standard"
        }
    }
    
    # Add reference numbers if provided
    ref_numbers = {}
    if po_number:
        ref_numbers["PONumber"] = po_number
    if quote_number:
        ref_numbers["RateQuoteNumber"] = quote_number
    if ref_numbers:
        payload["BillOfLading"]["ReferenceNumbers"] = ref_numbers
    
    # Add special instructions
    if special_instructions:
        payload["BillOfLading"]["SpecialInstructions"] = special_instructions
    
    # Add pickup request if requested
    if include_pickup:
        if not pickup_date:
            pickup_date = (datetime.now() + timedelta(days=1)).strftime("%m/%d/%Y")
        
        payload["PickupRequest"] = {
            "PickupInformation": {
                "PickupDate": pickup_date,
                "ReadyTime": pickup_ready_time,
                "CloseTime": pickup_close_time
            },
            "SendEmailConfirmation": True
        }
    
    result = _make_request("BillOfLading", method="POST", data=payload)
    
    return {
        "pro_number": result.get("ProNumber"),
        "pickup_request_id": result.get("PickupRequestNumber"),
        "messages": result.get("Messages", [])
    }


def get_bol(pro_number: str) -> Dict:
    """Get BOL details by PRO number"""
    result = _make_request(f"BillOfLading?request.proNumber={pro_number}", method="GET")
    return result.get("BillOfLading", {})


def print_bol_pdf(pro_number: str) -> str:
    """
    Get BOL as PDF (base64 encoded).
    
    Returns:
        Base64 encoded PDF document
    """
    result = _make_request(f"BillOfLading/PrintBOL?request.proNumber={pro_number}", method="GET")
    return result.get("BolDocument", "")


def print_shipping_labels(pro_number: str, num_labels: int = 4, style: int = 1) -> str:
    """
    Get shipping labels as PDF (base64 encoded).
    
    Args:
        pro_number: R+L PRO number
        num_labels: Number of labels (1-100)
        style: Label style (1-13)
    
    Returns:
        Base64 encoded PDF document
    """
    result = _make_request(
        f"BillOfLading/PrintShippingLabels?request.proNumber={pro_number}&request.style={style}&request.numberOfLabels={num_labels}",
        method="GET"
    )
    return result.get("ShippingLabelsFile", "")


# =============================================================================
# PICKUP REQUESTS
# =============================================================================

def create_pickup_for_pro(
    pro_number: str,
    pickup_date: str = None,
    ready_time: str = "09:00 AM",
    close_time: str = "05:00 PM",
    contact_name: str = "",
    contact_phone: str = "",
    contact_email: str = "",
    additional_instructions: str = "",
    send_email_confirmation: bool = True
) -> Dict:
    """
    Schedule pickup for an existing BOL/PRO number.
    This is the simpler method when you already have a PRO.
    
    Args:
        pro_number: R+L PRO number from BOL
        pickup_date: Date in MM/dd/yyyy format (optional, defaults to tomorrow)
        ready_time: Ready time in HH:MM AM/PM format (e.g., "09:00 AM")
        close_time: Close time in HH:MM AM/PM format (e.g., "05:00 PM")
    
    Returns:
        Dict with pickup request ID
    """
    if not pickup_date:
        pickup_date = (datetime.now() + timedelta(days=1)).strftime("%m/%d/%Y")
    
    payload = {
        "ProNumber": pro_number,
        "PickupInformation": {
            "PickupDate": pickup_date,
            "ReadyTime": ready_time,
            "CloseTime": close_time,
            "AdditionalInstructions": additional_instructions
        },
        "SendEmailConfirmation": send_email_confirmation
    }
    
    # Add contact if provided
    if contact_name or contact_phone or contact_email:
        payload["Contact"] = {
            "Name": contact_name,
            "PhoneNumber": contact_phone,
            "EmailAddress": contact_email
        }
    
    # Use FromBOL endpoint for PRO-based pickup
    result = _make_request("PickupRequest/FromBOL", method="POST", data=payload)
    
    return {
        "pickup_request_id": result.get("PickupRequestId"),
        "messages": result.get("Messages", [])
    }


def create_pickup_request(
    # Shipper info
    shipper_name: str,
    shipper_address: str,
    shipper_city: str,
    shipper_state: str,
    shipper_zip: str,
    shipper_phone: str,
    # Destination info
    dest_city: str,
    dest_state: str,
    dest_zip: str,
    # Shipment details
    weight_lbs: int,
    pieces: int = 1,
    # Pickup schedule
    pickup_date: str = None,
    ready_time: str = "09:00",
    close_time: str = "17:00",
    # Optional
    shipper_address2: str = "",
    contact_name: str = "",
    contact_email: str = "",
    additional_instructions: str = "",
    send_email_confirmation: bool = True
) -> Dict:
    """
    Create a pickup request with R+L Carriers.
    
    Returns:
        Dict with pickup request ID
    """
    if not pickup_date:
        pickup_date = (datetime.now() + timedelta(days=1)).strftime("%m/%d/%Y")
    
    payload = {
        "Pickup": {
            "Shipper": {
                "CompanyName": shipper_name,
                "AddressLine1": shipper_address,
                "AddressLine2": shipper_address2,
                "City": shipper_city,
                "StateOrProvince": shipper_state,
                "ZipOrPostalCode": shipper_zip,
                "CountryCode": "USA",
                "PhoneNumber": shipper_phone
            },
            "Contact": {
                "Name": contact_name or shipper_name,
                "PhoneNumber": shipper_phone,
                "EmailAddress": contact_email
            },
            "Destinations": [
                {
                    "City": dest_city,
                    "StateOrProvince": dest_state,
                    "ZipOrPostalCode": dest_zip,
                    "CountryCode": "USA",
                    "Weight": int(weight_lbs),
                    "Pieces": pieces,
                    "PackageType": "PLT"
                }
            ],
            "PickupDate": pickup_date,
            "ReadyTime": ready_time,
            "CloseTime": close_time,
            "AdditionalInstructions": additional_instructions
        },
        "SendEmailConfirmation": send_email_confirmation
    }
    
    result = _make_request("PickupRequest", method="POST", data=payload)
    
    return {
        "pickup_request_id": result.get("PickupRequestId"),
        "messages": result.get("Messages", [])
    }


def get_pickup_request(pickup_request_id: int) -> Dict:
    """Get pickup request details by ID"""
    result = _make_request(f"PickupRequest?request.pickupRequestId={pickup_request_id}", method="GET")
    return {
        "pickup": result.get("Pickup", {}),
        "pickup_request_id": result.get("PickupRequestId")
    }


def get_pickup_by_pro(pro_number: str) -> Dict:
    """Get pickup request details by PRO number"""
    result = _make_request(f"PickupRequest?request.proNumber={pro_number}", method="GET")
    return {
        "pickup": result.get("Pickup", {}),
        "pickup_request_id": result.get("PickupRequestId")
    }


def cancel_pickup_request(pickup_request_id: int, reason: str = "Order cancelled") -> Dict:
    """Cancel a pickup request by ID"""
    payload = {
        "PickupRequestId": pickup_request_id,
        "Reason": reason
    }
    result = _make_request("PickupRequest", method="DELETE", data=payload)
    return {
        "status": "cancelled",
        "messages": result.get("Messages", [])
    }


def cancel_pickup_by_pro(pro_number: str, reason: str = "Order cancelled") -> Dict:
    """Cancel a pickup request by PRO number (convenience function)"""
    # First get the pickup request ID
    pickup_info = get_pickup_by_pro(pro_number)
    pickup_id = pickup_info.get("pickup_request_id")
    
    if not pickup_id:
        return {"status": "error", "message": f"No pickup found for PRO {pro_number}"}
    
    # Then cancel it
    return cancel_pickup_request(pickup_id, reason)


# =============================================================================
# NOTIFICATIONS
# =============================================================================

def setup_shipment_notification(
    pro_number: str,
    email_addresses: List[str],
    events: List[str] = None
) -> Dict:
    """
    Set up email notifications for a specific shipment.
    
    Args:
        pro_number: R+L PRO number
        email_addresses: List of email addresses to notify
        events: List of events to notify on. Valid options:
                - "PickedUp"
                - "Departed"
                - "ArrivedAt"
                - "OutForDelivery"
                - "Delivered"
                - "BillChange"
                - "BOLAvailable"
                - "DRAvail"
                Default: ["PickedUp", "OutForDelivery", "Delivered"]
    
    Returns:
        Dict with status
    """
    if not events:
        events = ["PickedUp", "OutForDelivery", "Delivered"]
    
    payload = {
        "ProNumber": pro_number,
        "NotificationType": "Shipment",
        "Events": events,
        "Emails": email_addresses
    }
    
    result = _make_request("ProNotification", method="PUT", data=payload)
    return {
        "status": "ok",
        "messages": result.get("Messages", [])
    }


def get_shipment_notification(pro_number: str) -> Dict:
    """Get notification settings for a shipment"""
    result = _make_request(
        f"ProNotification?request.notificationType=Shipment&request.proNumber={pro_number}",
        method="GET"
    )
    return {
        "events": result.get("Events", []),
        "emails": result.get("Emails", [])
    }


# =============================================================================
# UTILITIES
# =============================================================================

def test_connection() -> Dict:
    """Test API connection by fetching pallet types"""
    try:
        pallet_types = get_pallet_types()
        return {
            "status": "ok",
            "message": "R+L Carriers API connection successful",
            "pallet_types_count": len(pallet_types)
        }
    except RLCarriersError as e:
        return {
            "status": "error",
            "message": str(e),
            "errors": e.errors
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }
