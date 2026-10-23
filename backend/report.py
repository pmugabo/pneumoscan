"""PDF Report Generation for PneumoScan.

Generates downloadable PDF reports for triage decisions.
"""
from io import BytesIO
from datetime import datetime
from reportlab.lib.units import inch
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors


def generate_report(prediction_data: dict) -> BytesIO:
    """Generate a PDF report for a prediction.
    
    Args:
        prediction_data: Dictionary containing prediction details
        
    Returns:
        BytesIO buffer containing the PDF
    """
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []
    
    # Title
    title = Paragraph("PneumoScan Triage Report", styles["Title"])
    story.append(title)
    story.append(Spacer(1, 12))
    
    # Disclaimer
    disclaimer = Paragraph(
        "<b>DISCLAIMER:</b> This is a research prototype, not a certified medical device. "
        "This output is a triage decision about an image, not a diagnosis or treatment plan.",
        styles["Normal"]
    )
    story.append(disclaimer)
    story.append(Spacer(1, 12))
    
    # Patient info
    story.append(Paragraph("<b>Patient Information:</b>", styles["Heading2"]))
    data = [
        ["Patient:", prediction_data.get("patient_name") or prediction_data.get("patient_id") or "N/A"],
        ["File Name:", prediction_data.get("filename", "N/A")],
        ["Date:", prediction_data.get("created_at", "N/A")[:10] if prediction_data.get("created_at") else "N/A"],
        ["Time:", prediction_data.get("created_at", "N/A")[11:19] if prediction_data.get("created_at") else "N/A"]
    ]
    table = Table(data, colWidths=[1.5 * inch, 4 * inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.grey),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
    ]))
    story.append(table)
    story.append(Spacer(1, 12))
    
    # Triage decision
    story.append(Paragraph("<b>Triage Decision:</b>", styles["Heading2"]))
    story.append(Paragraph(f"<b>State:</b> {prediction_data.get('triage_state', 'N/A')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Predicted Class:</b> {prediction_data.get('confirmed_outcome', 'N/A')}", styles["Normal"]))
    story.append(Spacer(1, 12))
    
    # Metrics
    story.append(Paragraph("<b>Analysis Metrics:</b>", styles["Heading2"]))
    metrics = [
        ["Calibrated Probability:", f"{prediction_data.get('calibrated_prob', 0):.3f}"],
        ["OOD Score:", f"{prediction_data.get('ood_score', 0):.1f}"],
        ["Model Version:", prediction_data.get('model_version', 'N/A')]
    ]
    metrics_table = Table(metrics, colWidths=[2 * inch, 3.5 * inch])
    metrics_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
    ]))
    story.append(metrics_table)
    story.append(Spacer(1, 12))
    
    # Build PDF
    doc.build(story)
    
    buffer.seek(0)
    return buffer
