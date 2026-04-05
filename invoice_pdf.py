"""
invoice_pdf.py
Generate a QB-style PDF invoice using reportlab.

Entry point: generate_invoice_pdf(order_data, shipping_result) -> bytes
Returns raw PDF bytes ready to attach to an email.

order_data may include:
  billing_address: dict with street, city, state, zip, company_name (from B2BWave customer)
  shipping_address: dict with address, city, state, zip (delivery address)
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
        spaceAfter=2,
    )
    addr_val_style = ParagraphStyle(
        'AddrVal',
        parent=styles['Normal'],
        fontSize=9,
        fontName='Helvetica',
        textColor=colors.HexColor('#1a202c'),
        leading=13,
    )

    # Pull data
    order_id = str(order_data.get('id') or order_data.get('order_id', ''))
    customer_name = order_data.get('customer_name', '')
    company_name = order_data.get('company_name', '')
    customer_email = order_data.get('customer_email', '') or order_data.get('email', '')
    customer_phone = order_data.get('customer_phone', '')
    order_date = order_data.get('order_date', '')

    # Delivery / shipping address (from order)
    shipping_addr = order_data.get('shipping_address', {})
    ship_line1 = shipping_addr.get('address', '')
    ship_line2 = shipping_addr.get('address2', '')
    ship_city = shipping_addr.get('city', '')
    ship_state = shipping_addr.get('state', '')
    ship_zip = shipping_addr.get('zip', '')
    ship_addr_str = ship_line1
    if ship_line2:
        ship_addr_str += f", {ship_line2}"
    ship_addr_str += f"<br/>{ship_city}, {ship_state} {ship_zip}"

    # Billing address (from B2BWave customer record)
    billing_addr = order_data.get('billing_address', {})
    if billing_addr:
        bill_company = billing_addr.get('company_name', '') or company_name or customer_name
        bill_street = billing_addr.get('street', '')
        bill_street2 = billing_addr.get('street2', '')
        bill_city = billing_addr.get('city', '')
        bill_state = billing_addr.get('state', '')
        bill_zip = billing_addr.get('zip', '')
        bill_addr_str = bill_street
        if bill_street2:
            bill_addr_str += f", {bill_street2}"
        bill_addr_str += f"<br/>{bill_city}, {bill_state} {bill_zip}"
    else:
        # Fall back to shipping address if no billing address available
        bill_company = company_name or customer_name
        bill_addr_str = ship_addr_str

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
            Paragraph("INVOICE", ParagraphStyle(
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
    # ADDRESSES (BILL TO + SHIP TO) + INVOICE INFO
    # ==========================================================================

    # Bill To block
    bill_to_content = f"""<b>{bill_company}</b><br/>
{customer_name if bill_company != customer_name else ''}<br/>
{bill_addr_str}<br/>
{customer_email}<br/>
{customer_phone}"""

    # Ship To block
    ship_display_name = company_name or customer_name
    ship_to_content = f"""<b>{ship_display_name}</b><br/>
{customer_name if ship_display_name != customer_name else ''}<br/>
{ship_addr_str}"""

    # Invoice info
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

    def _addr_block(title: str, content: str, bg_color: str = '#f7fafc') -> Table:
        return Table(
            [
                [Paragraph(f'<b>{title}</b>', label_style)],
                [Paragraph(content, addr_val_style)],
            ],
            colWidths=[2.2 * inch],
            style=TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor(bg_color)),
                ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
                ('LEFTPADDING', (0, 0), (-1, -1), 6),
                ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ])
        )

    bill_block = _addr_block('BILL TO', bill_to_content)
    # Ship To gets a subtle yellow tint to draw attention
    ship_block = _addr_block('SHIP TO — Delivery Address', ship_to_content, bg_color='#FFFBEB')

    meta_data = [[bill_block, ship_block, info_table]]
    meta_table = Table(meta_data, colWidths=[2.3 * inch, 2.3 * inch, 3.1 * inch])
    meta_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 1), (0, 0), 8),
        ('RIGHTPADDING', (1, 0), (1, 0), 8),
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
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a365d')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, 0), 6),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
        ('TEXTCOLOR', (0, 1), (-1, -1), colors.HexColor('#1a202c')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7fafc')]),
        ('TOPPADDING', (0, 1), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
        ('ALIGN', (2, 0), (2, -1), 'CENTER'),
        ('ALIGN', (3, 0), (4, -1), 'RIGHT'),
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
