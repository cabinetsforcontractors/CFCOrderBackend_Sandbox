"""
invoice_pdf.py
PDF invoice — a print copy of the v4 invoice EMAIL (William ruling 2026-07-28:
"all that has to be done for the PDF invoice is copy the invoice in the email
from Your Order #N to grand total"). Same order-row fields, same line items,
same labels; Invoice # is the bare order number.

Entry point: generate_invoice_pdf(order_data, shipping_result) -> bytes
Returns raw PDF bytes ready to attach to an email.
"""

from io import BytesIO
from typing import Optional


def _fmt_date(raw) -> str:
    try:
        if hasattr(raw, "strftime"):
            return raw.strftime("%b %d, %Y")
        s = str(raw or "")[:10]
        if s:
            from datetime import datetime
            return datetime.strptime(s, "%Y-%m-%d").strftime("%b %d, %Y")
    except Exception:
        pass
    return str(raw or "")


def generate_invoice_pdf(order_data: dict, shipping_result: dict) -> Optional[bytes]:
    """Render the invoice PDF from the same data the email template shows."""
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Table, TableStyle, Paragraph,
            Spacer, HRFlowable
        )
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_RIGHT
    except ImportError:
        print("[PDF] reportlab not installed — cannot generate PDF invoice")
        return None

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.4 * inch,
        bottomMargin=0.6 * inch,
    )

    styles = getSampleStyleSheet()
    header_style = ParagraphStyle(
        'Header', parent=styles['Normal'], fontSize=22, leading=26,
        fontName='Helvetica-Bold', textColor=colors.HexColor('#1a365d'),
        spaceAfter=2,
    )
    subheader_style = ParagraphStyle(
        'SubHeader', parent=styles['Normal'], fontSize=10, leading=13,
        fontName='Helvetica', textColor=colors.HexColor('#4a5568'),
        spaceAfter=2,
    )
    order_h_style = ParagraphStyle(
        'OrderH', parent=styles['Normal'], fontSize=15, leading=19,
        fontName='Helvetica', textColor=colors.HexColor('#333333'),
    )
    placed_style = ParagraphStyle(
        'Placed', parent=styles['Normal'], fontSize=9, leading=12,
        fontName='Helvetica', textColor=colors.HexColor('#888888'),
    )
    label_style = ParagraphStyle(
        'Label', parent=styles['Normal'], fontSize=8,
        fontName='Helvetica-Bold', textColor=colors.HexColor('#718096'),
        spaceAfter=2,
    )
    addr_val_style = ParagraphStyle(
        'AddrVal', parent=styles['Normal'], fontSize=9,
        fontName='Helvetica', textColor=colors.HexColor('#1a202c'),
        leading=13,
    )
    note_style = ParagraphStyle(
        'Note', parent=styles['Normal'], fontSize=9, leading=13,
        fontName='Helvetica', textColor=colors.HexColor('#333333'),
    )

    # Same fields the email template reads (the order ROW, not B2BWave dicts)
    order_id = str(order_data.get('order_id') or order_data.get('id') or '')
    from email_templates import proper_name
    customer_name = proper_name(order_data.get('customer_name') or '')
    company_name = order_data.get('company_name') or ''
    email = order_data.get('email') or order_data.get('customer_email') or ''
    phone = order_data.get('phone') or order_data.get('customer_phone') or ''
    street = order_data.get('street') or ''
    street2 = order_data.get('street2') or ''
    city = order_data.get('city') or ''
    state = order_data.get('state') or ''
    zip_code = order_data.get('zip_code') or ''
    placed = _fmt_date(order_data.get('order_date'))
    csz = f"{city}, {state} {zip_code}".replace(" ,", ",").strip(", ")
    addr_lines = street + (f", {street2}" if street2 else "") + f"<br/>{csz}"

    line_items = order_data.get('line_items') or []
    if not line_items:
        try:
            from db_helpers import get_order_line_items
            line_items = get_order_line_items(order_id) or []
        except Exception:
            line_items = []

    total_items = float(shipping_result.get('total_items', 0) or 0)
    tariff_amount = float(shipping_result.get('tariff_amount', 0) or 0)
    tariff_rate = shipping_result.get('tariff_rate', 0.08)
    total_shipping = float(shipping_result.get('total_shipping', 0) or 0)
    grand_total = float(shipping_result.get('grand_total', 0) or 0)

    elements = []

    # HEADER — company name at the very top of the page
    header_table = Table(
        [[Paragraph("Cabinets For Contractors", header_style),
          Paragraph("INVOICE", ParagraphStyle(
              'InvTitle', parent=styles['Normal'], fontSize=26, leading=30,
              fontName='Helvetica-Bold', textColor=colors.HexColor('#2563eb'),
              alignment=TA_RIGHT))]],
        colWidths=[4 * inch, 3 * inch])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(header_table)
    elements.append(Paragraph(
        "Wholesale RTA Cabinets · (770) 990-4885 · orders@cabinetsforcontractors.com",
        subheader_style))
    elements.append(HRFlowable(width="100%", thickness=1,
                               color=colors.HexColor('#e2e8f0'), spaceAfter=8))

    # "Your Order #N" — the email's invoice header
    elements.append(Paragraph(f"Your Order #{order_id}", order_h_style))
    elements.append(Paragraph(f"Placed {placed}", placed_style))
    elements.append(Spacer(1, 10))

    # BILLING / SHIPPING blocks (same content as the email) + invoice info
    bill_to_content = (f"<b>{company_name}</b><br/>{customer_name}<br/>"
                       f"{addr_lines}<br/>{phone}<br/>{email}")
    ship_to_content = f"<b>{company_name}</b><br/>{addr_lines}"

    info_table = Table(
        [['Invoice #:', order_id],
         ['Invoice Date:', placed or '—'],
         ['Due:', 'Upon Receipt']],
        colWidths=[1.1 * inch, 1.9 * inch])
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

    def _addr_block(title, content, bg_color='#f7fafc'):
        return Table(
            [[Paragraph(f'<b>{title}</b>', label_style)],
             [Paragraph(content, addr_val_style)]],
            colWidths=[2.2 * inch],
            style=TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor(bg_color)),
                ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
                ('LEFTPADDING', (0, 0), (-1, -1), 6),
                ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))

    meta_table = Table(
        [[_addr_block('BILLING INFO', bill_to_content),
          _addr_block('SHIPPING INFO', ship_to_content, bg_color='#FFFBEB'),
          info_table]],
        colWidths=[2.3 * inch, 2.3 * inch, 3.1 * inch])
    meta_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (1, 0), 8),
    ]))
    elements.append(meta_table)
    elements.append(Spacer(1, 12))

    # Optional per-invoice note (same one the email shows)
    note = (order_data.get("invoice_note") or "").strip()
    if note:
        note_table = Table(
            [[Paragraph(f"<b>Note about your order:</b> {note}", note_style)]],
            colWidths=[6.7 * inch],
            style=TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#FEF3C7')),
                ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#F59E0B')),
                ('LEFTPADDING', (0, 0), (-1, -1), 8),
                ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ]))
        elements.append(note_table)
        elements.append(Spacer(1, 12))

    # LINE ITEMS — every SKU on the order, email column labels
    desc_style = ParagraphStyle(
        'Desc', parent=styles['Normal'], fontSize=8, leading=10,
        fontName='Helvetica', textColor=colors.HexColor('#666666'))
    sku_style = ParagraphStyle(
        'Sku', parent=styles['Normal'], fontSize=9, leading=11,
        fontName='Helvetica-Bold', textColor=colors.HexColor('#1a202c'))

    table_data = [["SKU's", 'Qty', 'Each', 'Total']]
    for item in line_items:
        sku = item.get('sku') or ''
        name = item.get('product_name') or item.get('name') or ''
        qty = int(float(item.get('quantity') or 0))
        price = float(item.get('price') or 0)
        line_total = float(item.get('line_total') or (price * qty))
        table_data.append([
            [Paragraph(sku, sku_style), Paragraph(name, desc_style)],
            str(qty),
            f"${price:,.2f}",
            f"${line_total:,.2f}",
        ])

    items_table = Table(table_data,
                        colWidths=[4.3 * inch, 0.6 * inch, 0.9 * inch, 0.9 * inch],
                        repeatRows=1)
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a365d')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ('ALIGN', (1, 0), (1, -1), 'CENTER'),
        ('ALIGN', (2, 0), (3, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, 0), 6),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
        ('TEXTCOLOR', (0, 1), (-1, -1), colors.HexColor('#1a202c')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1),
         [colors.white, colors.HexColor('#f7fafc')]),
        ('VALIGN', (0, 1), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 1), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
        ('LINEBELOW', (0, 0), (-1, 0), 0.5, colors.HexColor('#1a365d')),
        ('LINEBELOW', (0, 1), (-1, -1), 0.25, colors.HexColor('#e2e8f0')),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 12))

    # TOTALS — email labels, ending at Grand Total. Store credit (William
    # 2026-08-02) rides after tariff when present; style rows are computed
    # so the insert can't shift the bold Grand Total.
    totals_data = [
        ['', 'Subtotal (in-stock):', f"${total_items:,.2f}"],
        ['', f"Tariff Surcharge ({int(tariff_rate * 100)}%):", f"${tariff_amount:,.2f}"],
    ]
    store_credit = float(shipping_result.get('store_credit_amount', 0) or 0)
    if store_credit > 0:
        totals_data.append(['', 'Store Credit Applied:', f"-${store_credit:,.2f}"])
    totals_data.append(['', 'Shipping:', f"${total_shipping:,.2f}"])
    totals_data.append(['', 'Grand Total:', f"${grand_total:,.2f}"])
    last = len(totals_data) - 1
    totals_table = Table(totals_data, colWidths=[3.7 * inch, 1.8 * inch, 1.2 * inch])
    totals_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, last - 1), 'Helvetica'),
        ('FONTNAME', (1, last), (2, last), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, last - 1), 9),
        ('FONTSIZE', (1, last), (2, last), 11),
        ('TEXTCOLOR', (1, 0), (1, last - 1), colors.HexColor('#4a5568')),
        ('TEXTCOLOR', (2, 0), (2, last - 1), colors.HexColor('#1a202c')),
        ('TEXTCOLOR', (1, last), (2, last), colors.HexColor('#1a365d')),
        ('ALIGN', (1, 0), (2, -1), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LINEABOVE', (1, last), (2, last), 1, colors.HexColor('#1a365d')),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(totals_table)

    doc.build(elements)
    return buffer.getvalue()
