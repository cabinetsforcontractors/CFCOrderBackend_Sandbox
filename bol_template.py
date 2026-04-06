"""
bol_template.py
WS6 — R+L Carriers BOL fallback PDF generator.

Matches the layout of the real R+L Straight Bill of Lading:
  - Header: R+L branding + "STRAIGHT BILL OF LADING ORIGINAL - NOT NEGOTIABLE"
  - WEB PRO prominently displayed
  - Shipper / Consignee / Bill-To grid
  - Reference numbers (BOL#, PO#, Quote#)
  - Services checkboxes
  - Freight commodity table
  - Legal boilerplate
  - Signature lines

Called by supplier_polling_engine._generate_fallback_bol_pdf() when
R+L DocumentRetrieval is unavailable or returns no PDF.
"""

import io
from typing import Optional
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph,
    Spacer, HRFlowable
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT


# =============================================================================
# COLORS
# =============================================================================
RL_RED   = colors.HexColor("#CC0000")
RL_NAVY  = colors.HexColor("#003366")
DARK     = colors.HexColor("#111111")
MID      = colors.HexColor("#444444")
LIGHT    = colors.HexColor("#888888")
RULE     = colors.HexColor("#999999")
BG_GRAY  = colors.HexColor("#F5F5F5")
BG_BLUE  = colors.HexColor("#E8EEF4")
WHITE    = colors.white
BLACK    = colors.black


# =============================================================================
# STYLES
# =============================================================================
def _s(name, **kw):
    defaults = dict(fontName="Helvetica", fontSize=7, leading=9,
                    textColor=DARK, spaceAfter=0, spaceBefore=0)
    defaults.update(kw)
    return ParagraphStyle(name, **defaults)

TITLE_S  = _s("title",  fontName="Helvetica-Bold", fontSize=11, textColor=DARK)
PRO_S    = _s("pro",    fontName="Helvetica-Bold", fontSize=20, textColor=RL_NAVY, leading=22)
LABEL_S  = _s("label",  fontName="Helvetica-Bold", fontSize=6,  textColor=LIGHT,  leading=7)
VAL_S    = _s("val",    fontName="Helvetica",      fontSize=8,  textColor=DARK,   leading=10)
VAL_B_S  = _s("valb",   fontName="Helvetica-Bold", fontSize=8,  textColor=DARK,   leading=10)
SMALL_S  = _s("small",  fontName="Helvetica",      fontSize=6,  textColor=MID,    leading=7)
TINY_S   = _s("tiny",   fontName="Helvetica",      fontSize=5,  textColor=MID,    leading=6)
LEGAL_S  = _s("legal",  fontName="Helvetica",      fontSize=5.5,textColor=MID,    leading=7)
HDR_S    = _s("hdr",    fontName="Helvetica-Bold", fontSize=7,  textColor=WHITE,  leading=9)
COL_HDR_S= _s("colhdr", fontName="Helvetica-Bold", fontSize=6,  textColor=DARK,   leading=8)
RED_S    = _s("red",    fontName="Helvetica-Bold", fontSize=9,  textColor=RL_RED, leading=11)


def _p(text, style=VAL_S):
    return Paragraph(str(text) if text else "", style)


def _lv(label, value, label_style=LABEL_S, val_style=VAL_S):
    """Label above value — used in grid cells."""
    return [_p(label, label_style), _p(value, val_style)]


def _cell(label, value, bold=False):
    vs = VAL_B_S if bold else VAL_S
    return [_p(label, LABEL_S), _p(value, vs)]


def _checkbox(checked=False, label=""):
    box = "☑" if checked else "☐"
    return _p(f"{box} {label}", SMALL_S)


def generate_bol_pdf(
    pro_number: str,
    order_id: str,
    pickup_date: str,
    pickup_time: str,
    # Shipper (warehouse)
    shipper_name: str       = "",
    shipper_address: str    = "",
    shipper_city: str       = "",
    shipper_state: str      = "",
    shipper_zip: str        = "",
    shipper_phone: str      = "",
    # Consignee (customer delivery)
    consignee_name: str     = "",
    consignee_address: str  = "",
    consignee_city: str     = "",
    consignee_state: str    = "",
    consignee_zip: str      = "",
    consignee_phone: str    = "",
    # Freight
    weight_lbs: int         = 0,
    pieces: int             = 1,
    is_residential: bool    = True,
    quote_number: str       = "",
    description: str        = "RTA Cabinetry",
) -> Optional[bytes]:
    """
    Generate a BOL PDF that matches the R+L Straight Bill of Lading layout.
    Returns PDF bytes, or None on failure.
    """
    try:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=letter,
            leftMargin=0.4*inch, rightMargin=0.4*inch,
            topMargin=0.3*inch, bottomMargin=0.3*inch,
        )
        W = letter[0] - 0.8*inch  # usable width

        elems = []

        # ==============================================================
        # HEADER ROW: Branding + Title + PRO label placeholder
        # ==============================================================
        hdr_data = [[
            # Left: R+L branding
            [
                _p("R+L CARRIERS", _s("rlbrand", fontName="Helvetica-Bold",
                                       fontSize=14, textColor=RL_RED)),
                _p("P.O. Box 271, Wilmington, OH 45177-0271", TINY_S),
                _p("800.543.5589  |  rlc.com", TINY_S),
            ],
            # Center: Document title
            [
                _p("STRAIGHT BILL OF LADING", _s("bt", fontName="Helvetica-Bold",
                                                   fontSize=11, textColor=DARK)),
                _p("ORIGINAL - NOT NEGOTIABLE", _s("bt2", fontName="Helvetica-Bold",
                                                    fontSize=8, textColor=DARK)),
            ],
            # Right: PRO label area
            [
                _p("PLEASE PLACE", SMALL_S),
                _p("PRO LABEL HERE", _s("prolabel", fontName="Helvetica-Bold",
                                         fontSize=7, textColor=DARK)),
            ],
        ]]
        hdr_tbl = Table(hdr_data, colWidths=[W*0.30, W*0.45, W*0.25])
        hdr_tbl.setStyle(TableStyle([
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("ALIGN",  (1,0), (1,0),  "CENTER"),
            ("ALIGN",  (2,0), (2,0),  "RIGHT"),
            ("BOX",    (2,0), (2,0),  0.5, RULE),
            ("TOPPADDING",    (0,0),(-1,-1), 4),
            ("BOTTOMPADDING", (0,0),(-1,-1), 4),
        ]))
        elems.append(hdr_tbl)
        elems.append(HRFlowable(width="100%", thickness=1.5, color=DARK, spaceAfter=3))

        # ==============================================================
        # WEB PRO ROW
        # ==============================================================
        pro_data = [[
            _p(f"WEB PRO:  {pro_number}", PRO_S),
            "",
            [
                _p("DATE", LABEL_S),
                _p(pickup_date, VAL_B_S),
            ],
            [
                _p("CONSIGNEE PHONE", LABEL_S),
                _p(consignee_phone or "—", VAL_S),
            ],
            [
                _p("SHIPPER PHONE", LABEL_S),
                _p(shipper_phone or "—", VAL_S),
            ],
        ]]
        pro_tbl = Table(pro_data, colWidths=[W*0.32, W*0.03, W*0.18, W*0.22, W*0.25])
        pro_tbl.setStyle(TableStyle([
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
            ("BOX",           (0,0),(-1,-1), 0.5, RULE),
            ("INNERGRID",     (2,0),(-1,-1), 0.3, RULE),
            ("TOPPADDING",    (0,0),(-1,-1), 3),
            ("BOTTOMPADDING", (0,0),(-1,-1), 3),
            ("LEFTPADDING",   (0,0),(-1,-1), 4),
        ]))
        elems.append(pro_tbl)

        # ==============================================================
        # TO / FROM / BILL TO SECTION
        # ==============================================================
        to_block = [
            _p("TO:  CONSIGNEE", LABEL_S),
            _p(consignee_name, VAL_B_S),
            _p("ADDRESS", LABEL_S),
            _p(consignee_address, VAL_S),
            _p("CITY", LABEL_S),
            _p(f"{consignee_city}   STATE: {consignee_state}   ZIP: {consignee_zip}", VAL_S),
        ]
        from_block = [
            _p("FROM:  SHIPPER", LABEL_S),
            _p(shipper_name, VAL_B_S),
            _p("ADDRESS", LABEL_S),
            _p(shipper_address, VAL_S),
            _p("CITY", LABEL_S),
            _p(f"{shipper_city}   STATE: {shipper_state}   ZIP: {shipper_zip}", VAL_S),
        ]

        addr_data = [[to_block, from_block]]
        addr_tbl = Table(addr_data, colWidths=[W*0.55, W*0.45])
        addr_tbl.setStyle(TableStyle([
            ("VALIGN",        (0,0),(-1,-1), "TOP"),
            ("BOX",           (0,0),(-1,-1), 0.5, RULE),
            ("INNERGRID",     (0,0),(-1,-1), 0.3, RULE),
            ("TOPPADDING",    (0,0),(-1,-1), 3),
            ("BOTTOMPADDING", (0,0),(-1,-1), 3),
            ("LEFTPADDING",   (0,0),(-1,-1), 4),
        ]))
        elems.append(addr_tbl)

        # Bill To + Reference Numbers side by side
        bill_block = [
            _p("BILL TO:  THIRD PARTY", LABEL_S),
            _p("Cabinets For Contractors", VAL_B_S),
            _p("ADDRESS", LABEL_S),
            _p("1472 Ocean Shore Blvd", VAL_S),
            _p("CITY", LABEL_S),
            _p("ORMOND BEACH   FL   32176", VAL_S),
            _p("PHONE: (770) 990-4885   EMAIL: cabinetsforcontractors@gmail.com", SMALL_S),
        ]
        ref_block = [
            [_p("SHIPPER / BOL #", LABEL_S), _p(f"Order # {order_id}", VAL_B_S)],
            [_p("PURCHASE ORDER #", LABEL_S), _p(order_id, VAL_S)],
            [_p("QUOTE #", LABEL_S), _p(quote_number or "—", VAL_B_S)],
        ]
        ref_tbl_inner = Table(ref_block, colWidths=[W*0.20, W*0.25])
        ref_tbl_inner.setStyle(TableStyle([
            ("VALIGN",    (0,0),(-1,-1), "TOP"),
            ("INNERGRID", (0,0),(-1,-1), 0.3, RULE),
            ("BOX",       (0,0),(-1,-1), 0.5, RULE),
            ("TOPPADDING",(0,0),(-1,-1), 2),
            ("BOTTOMPADDING",(0,0),(-1,-1), 2),
            ("LEFTPADDING",(0,0),(-1,-1), 3),
        ]))

        bill_ref_data = [[bill_block, ref_tbl_inner]]
        bill_ref_tbl = Table(bill_ref_data, colWidths=[W*0.55, W*0.45])
        bill_ref_tbl.setStyle(TableStyle([
            ("VALIGN",        (0,0),(-1,-1), "TOP"),
            ("BOX",           (0,0),(-1,-1), 0.5, RULE),
            ("INNERGRID",     (0,0),(-1,-1), 0.3, RULE),
            ("TOPPADDING",    (0,0),(-1,-1), 3),
            ("BOTTOMPADDING", (0,0),(-1,-1), 3),
            ("LEFTPADDING",   (0,0),(-1,-1), 4),
        ]))
        elems.append(bill_ref_tbl)

        # ==============================================================
        # SERVICE LEVEL CHECKBOXES
        # ==============================================================
        svc_data = [[
            _checkbox(True,  "PREPAID (Shipper is responsible)"),
            _checkbox(False, "COLLECT (Consignee is responsible)"),
            _checkbox(False, "R+L GUARANTEED — by 5 PM on service date"),
            _checkbox(False, "R+L GUARANTEED AM — by Noon on service date"),
        ]]
        svc_tbl = Table(svc_data, colWidths=[W*0.25]*4)
        svc_tbl.setStyle(TableStyle([
            ("BOX",           (0,0),(-1,-1), 0.5, RULE),
            ("INNERGRID",     (0,0),(-1,-1), 0.3, RULE),
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
            ("TOPPADDING",    (0,0),(-1,-1), 3),
            ("BOTTOMPADDING", (0,0),(-1,-1), 3),
            ("LEFTPADDING",   (0,0),(-1,-1), 4),
        ]))
        elems.append(svc_tbl)

        # ==============================================================
        # ADDED SERVICES
        # ==============================================================
        added_data = [[
            [_p("ADDED SERVICES", _s("as", fontName="Helvetica-Bold", fontSize=6, textColor=DARK)),
             _p("(May require additional charges)", TINY_S)],
            [_p("LIFTGATE AT:", LABEL_S),
             _checkbox(is_residential, "PICKUP"),
             _checkbox(is_residential, "DELIVERY")],
            [_p("RESIDENTIAL:", LABEL_S),
             _checkbox(is_residential, "PICKUP"),
             _checkbox(is_residential, "DELIVERY")],
            [_p("LIMITED ACCESS:", LABEL_S),
             _checkbox(False, "PICKUP"),
             _checkbox(False, "DELIVERY")],
            _checkbox(False, "APPT REQUIRED"),
            _checkbox(False, "INSIDE DELIVERY"),
        ]]
        added_tbl = Table(added_data, colWidths=[W*0.22, W*0.18, W*0.18, W*0.18, W*0.12, W*0.12])
        added_tbl.setStyle(TableStyle([
            ("BOX",           (0,0),(-1,-1), 0.5, RULE),
            ("INNERGRID",     (0,0),(-1,-1), 0.3, RULE),
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
            ("TOPPADDING",    (0,0),(-1,-1), 3),
            ("BOTTOMPADDING", (0,0),(-1,-1), 3),
            ("LEFTPADDING",   (0,0),(-1,-1), 3),
        ]))
        elems.append(added_tbl)

        # ==============================================================
        # FREIGHT CHARGES BANNER
        # ==============================================================
        banner_data = [[
            _p("FREIGHT CHARGES ARE PREPAID unless marked collect . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . CHECK BOX IF COLLECT  ☐",
               _s("banner", fontName="Helvetica-Bold", fontSize=7, textColor=DARK))
        ]]
        banner_tbl = Table(banner_data, colWidths=[W])
        banner_tbl.setStyle(TableStyle([
            ("BOX",        (0,0),(-1,-1), 0.5, RULE),
            ("BACKGROUND", (0,0),(-1,-1), BG_GRAY),
            ("TOPPADDING", (0,0),(-1,-1), 3),
            ("BOTTOMPADDING",(0,0),(-1,-1), 3),
            ("LEFTPADDING",(0,0),(-1,-1), 4),
        ]))
        elems.append(banner_tbl)

        # ==============================================================
        # COMMODITY TABLE
        # ==============================================================
        col_widths = [W*0.06, W*0.12, W*0.10, W*0.04, W*0.32, W*0.12, W*0.05, W*0.08, W*0.11]
        comm_headers = [
            _p("BULK",          COL_HDR_S),
            _p("HANDLING UNITS\n# / TYPE", COL_HDR_S),
            _p("PIECES\n# / TYPE",         COL_HDR_S),
            _p("HM*",           COL_HDR_S),
            _p("DESCRIPTION OF ARTICLES\nSPECIAL MARKS, EXCEPTIONS", COL_HDR_S),
            _p("NMFC ITEM #",   COL_HDR_S),
            _p("SUB",           COL_HDR_S),
            _p("CLASS",         COL_HDR_S),
            _p("WEIGHT (LB)\nSUBJ. TO CORR", COL_HDR_S),
        ]
        comm_row = [
            _p("",     VAL_S),
            _p(f"1 / PLT", VAL_S),
            _p(f"{pieces} / PLT", VAL_S),
            _p("",     VAL_S),
            _p(description, VAL_S),
            _p("039495", VAL_S),
            _p("08",   VAL_S),
            _p("85.0", VAL_S),
            _p(str(weight_lbs), VAL_B_S),
        ]
        totals_row = [
            _p(f"Total Handling Units: 1", _s("tot", fontName="Helvetica-Bold", fontSize=7)),
            "", "", "",
            _p(f"Total Pieces: {pieces}", _s("tot", fontName="Helvetica-Bold", fontSize=7)),
            "", "",
            _p(f"Total Weight: {weight_lbs}", _s("tot", fontName="Helvetica-Bold", fontSize=7)),
            "",
        ]
        special_row = [
            _p("SPECIAL INSTRUCTIONS:", LABEL_S),
            "", "", "", "", "", "", "", "",
        ]

        comm_tbl = Table(
            [comm_headers, comm_row, totals_row, special_row],
            colWidths=col_widths,
            rowHeights=[14, 18, 14, 14],
        )
        comm_tbl.setStyle(TableStyle([
            ("BOX",           (0,0),(-1,-1), 0.5, RULE),
            ("INNERGRID",     (0,0),(-1,-1), 0.3, RULE),
            ("BACKGROUND",    (0,0),(-1,0),  BG_GRAY),
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
            ("ALIGN",         (8,0),(8,-1),  "RIGHT"),
            ("TOPPADDING",    (0,0),(-1,-1), 2),
            ("BOTTOMPADDING", (0,0),(-1,-1), 2),
            ("LEFTPADDING",   (0,0),(-1,-1), 3),
            ("SPAN",          (0,2),(3,2)),
            ("SPAN",          (4,2),(6,2)),
            ("SPAN",          (7,2),(8,2)),
            ("SPAN",          (0,3),(8,3)),
        ]))
        elems.append(comm_tbl)

        elems.append(Spacer(1, 3))
        elems.append(HRFlowable(width="100%", thickness=0.3, color=RULE))
        elems.append(Spacer(1, 2))

        # ==============================================================
        # LEGAL NOTES (3 columns)
        # ==============================================================
        note1 = ("Note 1 - Where the rate is dependent on value, shippers are required to state specifically "
                 "in writing the agreed or declared value of the property. (Additional Charges may apply) "
                 "The agreed or declared value of the property is hereby specifically stated by the shipper "
                 "to be not exceeding:  $________ per ________")
        note2 = ("Note 2 - Liability limitation for loss or damage on this shipment may be applicable. "
                 "See 49 U.S.C. 14706(c)(1)(A) and (B).\n"
                 "Note 3 - Commodities requiring special or additional care or attention in handling or "
                 "stowing must be so marked and packaged as to ensure safe transportation with ordinary care. "
                 "See Sec. 2(e) of NMFC Item 360.")

        notes_data = [[_p(note1, LEGAL_S), _p(note2, LEGAL_S)]]
        notes_tbl = Table(notes_data, colWidths=[W*0.50, W*0.50])
        notes_tbl.setStyle(TableStyle([
            ("BOX",           (0,0),(-1,-1), 0.5, RULE),
            ("INNERGRID",     (0,0),(-1,-1), 0.3, RULE),
            ("VALIGN",        (0,0),(-1,-1), "TOP"),
            ("TOPPADDING",    (0,0),(-1,-1), 3),
            ("BOTTOMPADDING", (0,0),(-1,-1), 3),
            ("LEFTPADDING",   (0,0),(-1,-1), 4),
        ]))
        elems.append(notes_tbl)

        # ==============================================================
        # RECEIVED / LEGAL BLOCK
        # ==============================================================
        received_text = (
            "RECEIVED, subject to individually determined rates or contracts that have been agreed upon in writing "
            "between the carrier and shipper, if applicable, otherwise to the rates, classifications and rules that "
            "have been established by the carrier and are available to the shipper, on request; the property described "
            "above in apparent good order, except as noted (contents and condition of contents of packages unknown), "
            "marked consigned, and destined as indicated above which said carrier (the word carrier being understood "
            "throughout this contract as meaning any person or corporation in possession of the property under the "
            "contract) agrees to carry to its usual place of delivery at said destination, and as to each party at any "
            "time interested in all or any of said property, that every service to be performed hereunder shall be "
            "subject to all the terms and conditions of the Uniform Bill of Lading set forth in the National Motor "
            "Freight Classification 100-X and successive issues. Further, carrier shall not be liable for damage to "
            "unprotected or uncrated freight or shipments."
        )
        elems.append(Table([[_p(received_text, LEGAL_S)]], colWidths=[W]))
        elems[-1].setStyle(TableStyle([
            ("BOX",(0,0),(-1,-1),0.5,RULE),
            ("TOPPADDING",(0,0),(-1,-1),3),
            ("BOTTOMPADDING",(0,0),(-1,-1),3),
            ("LEFTPADDING",(0,0),(-1,-1),4),
        ]))

        shipper_cert = (
            "Shipper hereby certifies that he is familiar with all of the terms and conditions in the said bill of lading "
            "including those on the back thereof and the said terms and conditions are hereby agreed to by shipper and "
            "accepted for himself and his assigns. Unless otherwise specified by the carrier, notice of loss or damage "
            "should be provided to the carrier within five business days from the date of delivery in accordance with "
            "NMFC Item 300135."
        )
        elems.append(Table([[_p(shipper_cert, LEGAL_S)]], colWidths=[W]))
        elems[-1].setStyle(TableStyle([
            ("BOX",(0,0),(-1,-1),0.5,RULE),
            ("TOPPADDING",(0,0),(-1,-1),3),
            ("BOTTOMPADDING",(0,0),(-1,-1),3),
            ("LEFTPADDING",(0,0),(-1,-1),4),
        ]))

        # ==============================================================
        # FOOTER: SHIPPED AT SHIPPER'S RISK / NO CANADA CUSTOMS
        # ==============================================================
        footer_data = [[
            _checkbox(False, "SHIPPED AT SHIPPER'S RISK (Unprotected Freight)"),
            _checkbox(False, "NO CANADA CUSTOMS DOCUMENTS PROVIDED\nNOTE: All customs documents must be handed to driver at time of pickup"),
        ]]
        footer_tbl = Table(footer_data, colWidths=[W*0.50, W*0.50])
        footer_tbl.setStyle(TableStyle([
            ("BOX",       (0,0),(-1,-1), 0.5, RULE),
            ("INNERGRID", (0,0),(-1,-1), 0.3, RULE),
            ("TOPPADDING",(0,0),(-1,-1), 3),
            ("BOTTOMPADDING",(0,0),(-1,-1), 3),
            ("LEFTPADDING",(0,0),(-1,-1), 4),
        ]))
        elems.append(footer_tbl)

        # ==============================================================
        # SIGNATURE ROW
        # ==============================================================
        sig_data = [[
            [_p("SHIPPER", LABEL_S), _p(shipper_name, VAL_B_S), _p("PER", LABEL_S), _p("", VAL_S)],
            [_p("CARRIER", LABEL_S), _p("R+L Carriers", VAL_B_S), _p("PER", LABEL_S), _p("", VAL_S)],
        ]]
        sig_tbl = Table(sig_data, colWidths=[W*0.50, W*0.50])
        sig_tbl.setStyle(TableStyle([
            ("BOX",       (0,0),(-1,-1), 0.5, RULE),
            ("INNERGRID", (0,0),(-1,-1), 0.3, RULE),
            ("VALIGN",    (0,0),(-1,-1), "TOP"),
            ("TOPPADDING",(0,0),(-1,-1), 4),
            ("BOTTOMPADDING",(0,0),(-1,-1), 4),
            ("LEFTPADDING",(0,0),(-1,-1), 4),
        ]))
        elems.append(sig_tbl)

        # ==============================================================
        # PAGE FOOTER
        # ==============================================================
        elems.append(Spacer(1, 3))
        elems.append(Table(
            [[_p("Generated by Cabinets For Contractors  •  cabinetsforcontractors.net", TINY_S),
              _p("Page 1 of 1", _s("pgn", fontSize=6, alignment=TA_RIGHT))]],
            colWidths=[W*0.75, W*0.25]
        ))

        doc.build(elems)
        pdf_bytes = buf.getvalue()
        print(f"[BOL_TEMPLATE] Generated BOL PDF {len(pdf_bytes)} bytes — PRO {pro_number}")
        return pdf_bytes

    except Exception as e:
        print(f"[BOL_TEMPLATE] PDF generation failed: {e}")
        import traceback
        traceback.print_exc()
        return None
