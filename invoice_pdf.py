"""
invoice_pdf.py
Generate a QB-style PDF invoice using reportlab.

Entry point: generate_invoice_pdf(order_data, shipping_result) -> bytes
Returns raw PDF bytes ready to attach to an email.
"""

from io import BytesIO
from typing import Dict, Optional


def generate_invoice_pdf(order_data: dict, shipping_result: dict) -> Optional[bytes]:
    """
    Generate a PDF invoice for an order.

    Args:
        order_data:      dict from fetch_b2bwave_order()
        shipping_result: dict from calculate_order_shipping()

    Returns:
        PDF as bytes, or None if reportlab is not available.
    """
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Table, TableStyle, Paragraph,
            Spacer, HRFlowable
        )
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
    except ImportError:
        print("[PDF] reportlab not installed — cannot generate PDF invoice")
        return None

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()

    # Custom styles
    header_style = ParagraphStyle(
        'Header',
        parent=styles['Normal'],
        fontSize=22,
        fontName='Helvetica-Bold',
        textColor=colors.HexColor('#1a365d'),
        spaceAfter=2,
    )
    subheader_style = ParagraphStyle(
        'SubHeader',
        parent=styles['Normal'],
        fontSize=10,
        fontName='Helvetica',
        textColor=colors.HexColor('#4a5568'),
        spaceAfter=2,
    )
    label_style = ParagraphStyle(
        'Label',
        parent=styles['Normal'],
        fontSize=8,
        fontName='Helvetica-Bold',
        textColor=colors.HexColor('#718096'),
        spaceBefore=8,
        spaceAfter=2,
    )
    value_style = ParagraphStyle(
        'Value',
        parent=styles['Normal'],
        fontSize=10,
        fontName='Helvetica',
        textColor=colors.HexColor('#1a202c'),
    )
    right_style = ParagraphStyle(
        'Right',
        parent=styles['Normal'],
        fontSize=10,
        fontName='Helvetica',
        alignment=TA_RIGHT,
        textColor=colors.HexColor('#1a202c'),
    )
    bold_right_style = ParagraphStyle(
        'BoldRight',
        parent=styles['Normal'],
        fontSize=11,
        fontName='Helvetica-Bold',
        alignment=TA_RIGHT,
        textColor=colors.HexColor('#1a365d'),
    )

    # Pull data
    order_id = str(order_data.get('id') or order_data.get('order_id', ''))
    customer_name = order_data.get('customer_name', '')
    company_name = order_data.get('company_name', '')
    customer_email = order_data.get('customer_email', '') or order_data.get('email', '')
    customer_phone = order_data.get('customer_phone', '')
    order_date = order_data.get('order_date', '')
    shipping_addr = order_data.get('shipping_address', {})
    line_items = order_data.get('line_items', [])

    total_items = shipping_result.get('total_items', 0)
    tariff_amount = shipping_result.get('tariff_amount', 0)
    tariff_rate = shipping_result.get('tariff_rate', 0.08)
    total_shipping = shipping_result.get('total_shipping', 0)
    grand_total = shipping_result.get('grand_total', 0)

    elements = []

    # ==========================================================================
    # HEADER
    # ==========================================================================
    header_data = [
        [
            Paragraph("Cabinets For Contractors", header_style),
            Paragraph(f"INVOICE", ParagraphStyle(
                'InvTitle', parent=styles['Normal'], fontSize=26,
                fontName='Helvetica-Bold', textColor=colors.HexColor('#2563eb'),
                alignment=TA_RIGHT
            )),
        ]
    ]
    header_table = Table(header_data, colWidths=[4 * inch, 3 * inch])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(header_table)

    elements.append(Paragraph("Wholesale RTA Cabinets · (770) 990-4885 · william@cabinetsforcontractors.net", subheader_style))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#e2e8f0'), spaceAfter=10))

    # ==========================================================================
    # BILL TO + INVOICE INFO
    # ==========================================================================
    addr_line1 = shipping_addr.get('address', '')
    addr_line2 = shipping_addr.get('address2', '')
    city = shipping_addr.get('city', '')
    state = shipping_addr.get('state', '')
    zip_code = shipping_addr.get('zip', '')
    addr_str = f"{addr_line1}"
    if addr_line2:
        addr_str += f", {addr_line2}"
    addr_str += f"<br/>{city}, {state} {zip_code}"

    bill_to_content = f"""<b>{company_name or customer_name}</b><br/>
{customer_name if company_name else ''}<br/>
{addr_str}<br/>
{customer_email}<br/>
{customer_phone}"""

    invoice_info = [
        ['Invoice #:', f"CFC-{order_id}"],
        ['Invoice Date:', order_date or '—'],
        ['Due:', 'Upon Receipt'],
    ]

    info_table = Table(invoice_info, colWidths=[1.1 * inch, 1.9 * inch])
    info_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#718096')),
        ('TEXTCOLOR', (1, 0), (1, -1), colors.HexColor('#1a202c')),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))

    meta_data = [
        [
            Table(
                [[Paragraph('<b>BILL TO</b>', ParagraphStyle(
                    'BillToLabel', parent=styles['Normal'], fontSize=8,
                    fontName='Helvetica-Bold', textColor=colors.HexColor('#718096')
                ))],
                 [Paragraph(bill_to_content, ParagraphStyle(
                     'BillToVal', parent=styles['Normal'], fontSize=9,
                     fontName='Helvetica', textColor=colors.HexColor('#1a202c'),
                     leading=14
                 ))]],
                colWidths=[3.5 * inch]
            ),
            info_table,
        ]
    ]
    meta_table = Table(meta_data, colWidths=[3.5 * inch, 3.5 * inch])
    meta_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(meta_table)
    elements.append(Spacer(1, 16))

    # ==========================================================================
    # LINE ITEMS TABLE
    # ==========================================================================
    col_headers = ['SKU', 'Description', 'Qty', 'Unit Price', 'Total']
    table_data = [col_headers]

    for item in line_items:
        sku = item.get('sku', '')
        name = item.get('name', '')
        qty = item.get('quantity', 1)
        price = float(item.get('price', 0))
        line_total = float(item.get('line_total', price * qty))
        table_data.append([
            sku,
            name,
            str(qty),
            f"${price:,.2f}",
            f"${line_total:,.2f}",
        ])

    col_widths = [1.1 * inch, 3.2 * inch, 0.5 * inch, 0.9 * inch, 1.0 * inch]
    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        # Header row
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a365d')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, 0), 6),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        # Data rows
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
        ('TEXTCOLOR', (0, 1), (-1, -1), colors.HexColor('#1a202c')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7fafc')]),
        ('TOPPADDING', (0, 1), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
        # Alignment
        ('ALIGN', (2, 0), (2, -1), 'CENTER'),  # Qty
        ('ALIGN', (3, 0), (4, -1), 'RIGHT'),   # Price, Total
        # Grid
        ('LINEBELOW', (0, 0), (-1, 0), 0.5, colors.HexColor('#1a365d')),
        ('LINEBELOW', (0, 1), (-1, -1), 0.25, colors.HexColor('#e2e8f0')),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 12))

    # ==========================================================================
    # TOTALS TABLE
    # ==========================================================================
    totals_data = [
        ['', 'Subtotal:', f"${total_items:,.2f}"],
        ['', f"Tariff ({int(tariff_rate * 100)}%):", f"${tariff_amount:,.2f}"],
        ['', 'Shipping:', f"${total_shipping:,.2f}"],
        ['', 'TOTAL DUE:', f"${grand_total:,.2f}"],
    ]

    totals_table = Table(totals_data, colWidths=[4.2 * inch, 1.3 * inch, 1.2 * inch])
    totals_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, 2), 'Helvetica'),
        ('FONTNAME', (1, 3), (2, 3), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 2), 9),
        ('FONTSIZE', (1, 3), (2, 3), 11),
        ('TEXTCOLOR', (1, 0), (1, 2), colors.HexColor('#4a5568')),
        ('TEXTCOLOR', (2, 0), (2, 2), colors.HexColor('#1a202c')),
        ('TEXTCOLOR', (1, 3), (2, 3), colors.HexColor('#1a365d')),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LINEABOVE', (1, 3), (2, 3), 1, colors.HexColor('#1a365d')),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(totals_table)
    elements.append(Spacer(1, 24))

    # ==========================================================================
    # POLICY NOTICE
    # ==========================================================================
    policy_style = ParagraphStyle(
        'Policy',
        parent=styles['Normal'],
        fontSize=7,
        fontName='Helvetica',
        textColor=colors.HexColor('#718096'),
        leading=11,
    )
    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#e2e8f0'), spaceAfter=6))
    elements.append(Paragraph(
        "<b>Policy Notice:</b> Payment constitutes acceptance of all terms. "
        "No returns on assembled or installed cabinets. "
        "Damaged items must be noted on delivery receipt and reported within 48 hours. "
        "20% restocking fee on returned, undamaged items in original packaging. "
        "Buyer is responsible for verifying measurements before ordering. "
        "Color variation between door samples and production run is normal. "
        "Questions? Call (770) 990-4885 or reply to your invoice email.",
        policy_style
    ))

    doc.build(elements)
    return buffer.getvalue()
