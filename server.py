#!/usr/bin/env python3
"""
Minimal test to verify SHA-256 header is drawn correctly in PDF
"""

from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch

def test_footer_positioning():
    """Test that footer sections are positioned correctly"""
    
    page_w, page_h = letter
    margin = 0.35 * inch
    
    # Simulate your footer sections
    hashes_h = 1.2 * inch  # Approximate SHA section height
    rfc_h = 0.5 * inch     # Approximate RFC section height
    
    # CORRECT positioning (bottom-up)
    rfc_section_top = margin + rfc_h
    sha_section_top = rfc_section_top + hashes_h
    
    print("=== PDF Footer Position Test ===")
    print(f"Page height: {page_h:.2f} points ({page_h/inch:.2f} inches)")
    print(f"Margin: {margin:.2f} points ({margin/inch:.2f} inches)")
    print(f"\nSHA section:")
    print(f"  Top Y: {sha_section_top:.2f} points ({sha_section_top/inch:.2f} inches from bottom)")
    print(f"  Height: {hashes_h:.2f} points ({hashes_h/inch:.2f} inches)")
    print(f"\nRFC section:")
    print(f"  Top Y: {rfc_section_top:.2f} points ({rfc_section_top/inch:.2f} inches from bottom)")
    print(f"  Height: {rfc_h:.2f} points ({rfc_h/inch:.2f} inches)")
    
    # Create test PDF
    pdf_path = "/tmp/footer_test.pdf"
    c = pdfcanvas.Canvas(pdf_path, pagesize=letter)
    
    # Draw page border for reference
    c.setStrokeGray(0.8)
    c.rect(margin, margin, page_w - 2*margin, page_h - 2*margin)
    
    # Draw SHA section
    y = sha_section_top
    c.setFont("Helvetica-Bold", 10)
    c.setFillColorRGB(1, 0, 0)  # RED so it's obvious
    c.drawString(margin, y, "SHA-256 Verification (should be visible!)")
    y -= 0.16 * inch
    c.setFont("Courier", 7)
    c.setFillColorRGB(0, 0, 0)  # Back to black
    c.drawString(margin, y, "Price Disclosure: abc123...")
    y -= 0.12 * inch
    c.drawString(margin, y, "Payment Disclosure: def456...")
    
    # Draw RFC section
    y = rfc_section_top
    c.setFont("Helvetica-Bold", 10)
    c.setFillColorRGB(0, 0, 1)  # BLUE so it's obvious
    c.drawString(margin, y, "RFC-3161 Timestamps (should be visible!)")
    y -= 0.16 * inch
    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(margin, y, "Price: N/A")
    y -= 0.12 * inch
    c.drawString(margin, y, "Payment: N/A")
    
    # Add label at top
    c.setFont("Helvetica-Bold", 14)
    c.drawString(margin, page_h - margin - 0.3*inch, "Footer Position Test")
    c.setFont("Helvetica", 10)
    c.drawString(margin, page_h - margin - 0.5*inch, "SHA header should be RED at bottom")
    c.drawString(margin, page_h - margin - 0.65*inch, "RFC header should be BLUE below SHA")
    
    c.showPage()
    c.save()
    
    print(f"\n✓ Test PDF created: {pdf_path}")
    print(f"✓ Open it and verify:")
    print(f"  1. RED 'SHA-256 Verification' header is visible at bottom")
    print(f"  2. BLUE 'RFC-3161 Timestamps' header is visible below SHA")
    print(f"  3. Both sections are inside the page border")
    
    return pdf_path

if __name__ == "__main__":
    test_footer_positioning()
