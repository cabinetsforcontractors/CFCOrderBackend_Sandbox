"""
picklist_pdf.py
Pick list PDF for an order — modeled on the .net (B2BWave) picklist printout
(William 2026-07-28: "I also want to include a pick list in the email as a pdf
file"). Layout: company header + Picklist title, order/customer block, the
order comments, then # | Description ([SKU] NAME) | Quantity | Notes with an
empty check box per line.

Entry point: generate_picklist_pdf(order_data, line_items=None) -> bytes
"""

from io import BytesIO
from typing import Optional


def generate_picklist_pdf(order_data: dict, line_items=None) -> Optional[bytes]:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
        )
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    except ImportError:
        print("[PDF] reportlab not installed — cannot generate picklist PDF")
        return None

    order_id = str(order_data.get('order_id') or order_data.get('id') or '')
    if line_items is None:
        line_items = order_data.get('line_items') or []
    if not line_items:
        try:
            from db_helpers import get_order_line_items
            line_items = get_order_line_items(order_id) or []
        except Exception:
            line_items = []

    company = order_data.get('company_name') or ''
    customer = order_data.get('customer_name') or ''
    street = order_data.get('street') or ''
    street2 = order_data.get('street2') or ''
    city = order_data.get('city') or ''
    state = order_data.get('state') or ''
    zip_code = order_data.get('zip_code') or ''
    phone = order_data.get('phone') or ''
    email = order_data.get('email') or ''
    comments = (order_data.get('comments') or '').strip()
    try:
        od = order_data.get('order_date')
        date_str = od.strftime("%m/%d/%Y") if hasattr(od, "strftime") else str(od or "")[:10]
    except Exception:
        date_str = str(order_data.get('order_date') or "")

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter,
                            rightMargin=0.6 * inch, leftMargin=0.6 * inch,
                            topMargin=0.5 * inch, bottomMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    small = ParagraphStyle('Small', parent=styles['Normal'], fontSize=9,
                           leading=12, textColor=colors.HexColor('#333333'))
    small_right = ParagraphStyle('SmallR', parent=small, alignment=TA_RIGHT)
    label = ParagraphStyle('PLabel', parent=small, fontName='Helvetica-Bold',
                           textColor=colors.HexColor('#666666'))
    title_style = ParagraphStyle('PTitle', parent=styles['Normal'], fontSize=20,
                                 leading=24, alignment=TA_CENTER,
                                 textColor=colors.HexColor('#444444'))
    top_style = ParagraphStyle('PTop', parent=styles['Normal'], fontSize=9,
                               leading=12, alignment=TA_CENTER,
                               textColor=colors.HexColor('#333333'))

    elements = [
        Paragraph("Cabinets For Contractors", top_style),
        HRFlowable(width="100%", thickness=0.5, color=colors.HexColor('#cccccc'),
                   spaceBefore=4, spaceAfter=8),
        Paragraph("Picklist", title_style),
        Spacer(1, 12),
    ]

    addr = street + (f", {street2}" if street2 else "")
    left_rows = [
        ('Order', order_id),
        ('Customer', company or customer),
        ('Address', f"{addr}<br/>{city}, {state} {zip_code}"),
        ('Phone', phone),
        ('Email', email),
        ('Date', date_str),
    ]
    left_tbl = Table(
        [[Paragraph(k, label), Paragraph(v or '', small)] for k, v in left_rows],
        colWidths=[0.8 * inch, 3.0 * inch])
    left_tbl.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    right_block = Paragraph(
        "Cabinets For Contractors<br/>770-990-4885<br/>"
        "orders@cabinetsforcontractors.com", small_right)
    info = Table([[left_tbl, right_block]], colWidths=[4.2 * inch, 3.1 * inch])
    info.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(info)
    elements.append(Spacer(1, 12))

    if comments:
        elements.append(Paragraph(f"<b>Comments</b> {comments}", small))
        elements.append(Spacer(1, 10))

    desc_style = ParagraphStyle('PDesc', parent=small, fontSize=8, leading=10)
    head_style = ParagraphStyle('PHead', parent=small, fontName='Helvetica-Bold',
                                textColor=colors.HexColor('#555555'))
    # little per-line check box (the .net picklist look) — its own narrow
    # column right after Quantity, sized like a real tick box
    rows = [[Paragraph('#', head_style), Paragraph('Description', head_style),
             Paragraph('Quantity', head_style), '',
             Paragraph('Notes', head_style)]]
    for i, item in enumerate(line_items, 1):
        sku = item.get('sku') or ''
        name = item.get('product_name') or item.get('name') or ''
        qty = int(float(item.get('quantity') or 0))
        rows.append([
            Paragraph(str(i), small),
            Paragraph(f"[{sku}] {name}", desc_style),
            Paragraph(str(qty), small),
            Table([['']], colWidths=[0.28 * inch], rowHeights=[0.22 * inch],
                  style=TableStyle([('BOX', (0, 0), (0, 0), 0.75,
                                     colors.HexColor('#999999'))])),
            '',
        ])
    tbl = Table(rows,
                colWidths=[0.5 * inch, 4.0 * inch, 0.75 * inch, 0.45 * inch,
                           1.6 * inch],
                repeatRows=1)
    style = [
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LINEBELOW', (0, 0), (-1, 0), 0.75, colors.HexColor('#999999')),
        ('LINEBELOW', (0, 1), (-1, -1), 0.25, colors.HexColor('#dddddd')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1),
         [colors.HexColor('#f5f5f5'), colors.white]),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('ALIGN', (2, 0), (2, -1), 'CENTER'),
        ('ALIGN', (3, 1), (3, -1), 'CENTER'),
    ]
    tbl.setStyle(TableStyle(style))
    elements.append(tbl)

    doc.build(elements)
    return buffer.getvalue()
