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
    predicted = {"FLAGGED_URGENT": "Pneumonia", "CLEARED": "Normal",
                 "ESCALATED": "Uncertain (referred to specialist)",
                 "REJECTED": "Not assessed (input rejected)"}.get(prediction_data.get("triage_state"), "N/A")
    story.append(Paragraph(f"<b>Predicted Class:</b> {predicted}", styles["Normal"]))
    story.append(Paragraph(f"<b>Confirmed Outcome:</b> {prediction_data.get('confirmed_outcome') or 'Not yet confirmed'}", styles["Normal"]))
    rec = prediction_data.get("outcome_record")
    if rec and rec.get("outcome") != "unknown":
        names = {"specialist_review": "specialist review", "clinical_exam": "clinical examination",
                 "laboratory": "laboratory results", "treatment_response": "response to treatment",
                 "repeat_imaging": "repeat imaging", "clinical_course": "clinical course"}
        basis = ", ".join(names.get(b, b) for b in rec.get("basis") or []) or "not stated"
        labs = ", ".join(x for x in (f"CRP {rec['crp_mg_l']} mg/L" if rec.get("crp_mg_l") is not None else "",
                                     f"WBC {rec['wbc_10e9_l']} x10^9/L" if rec.get("wbc_10e9_l") is not None else "") if x)
        lines = [f"<b>Diagnosis basis:</b> {basis} ({rec.get('certainty') or 'certainty not stated'})"]
        if rec.get("exam_findings"):
            lines.append("<b>Examination:</b> " + ", ".join(f.replace("_", " ") for f in rec["exam_findings"]))
        if labs:
            lines.append(f"<b>Laboratory:</b> {labs}")
        if rec.get("treatment"):
            lines.append(f"<b>Treatment:</b> {rec['treatment']}")
        if rec.get("rationale"):
            lines.append(f"<b>Rationale:</b> {rec['rationale']}")
        lines.append(f"<b>Recorded by:</b> {rec.get('recorded_by_name') or 'clinician'}, "
                     f"{(rec.get('recorded_at') or '')[:16].replace('T', ' ')}")
        for line in lines:
            story.append(Paragraph(line, styles["Normal"]))
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
    
    review = prediction_data.get("review")
    if review:
        story.append(Paragraph("<b>Specialist Review:</b>", styles["Heading2"]))
        rows = [["Specialist:", review.get("specialist_name") or "N/A"],
                ["Reviewed:", (review.get("resolved_at") or "N/A")[:16].replace("T", " ")],
                ["Diagnosis:", (review.get("confirmed_outcome") or "Not stated").title()],
                ["Recommendation:", review.get("recommendation") or "N/A"]]
        rt = Table(rows, colWidths=[1.5 * inch, 4 * inch])
        rt.setStyle(TableStyle([('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
                                ('ALIGN', (0, 0), (-1, -1), 'LEFT')]))
        story.append(rt)
        for label, key in (("Findings", "findings"), ("Comment", "specialist_comment")):
            if review.get(key):
                story.append(Spacer(1, 6))
                story.append(Paragraph(f"<b>{label}:</b> {review[key]}", styles["Normal"]))
        story.append(Spacer(1, 12))

    # Build PDF
    doc.build(story)
    
    buffer.seek(0)
    return buffer
