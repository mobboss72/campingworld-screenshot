# Payment hover section - key change is here:

# Hover and wait for tooltip to appear
visible_payment.hover(timeout=10000, force=True)
print("Hovering over payment element, waiting for tooltip...")

# Wait for tooltip with text containing "APR" or "down" (payment breakdown indicators)
tooltip_appeared = False
try:
    # Check if tooltip with APR text appears
    page.wait_for_function('''() => {
        const tooltips = document.querySelectorAll('[role="tooltip"]');
        for (let tooltip of tooltips) {
            if (tooltip.textContent.includes('APR') || 
                tooltip.textContent.includes('down') || 
                tooltip.textContent.includes('Months')) {
                return true;
            }
        }
        return false;
    }''', timeout=5000)
    tooltip_appeared = True
    print("✅ Payment tooltip with breakdown detected!")
except Exception as e:
    print(f"⚠️  Tooltip detection timed out: {e}")
    print("  Taking screenshot anyway...")

# Additional wait for tooltip animation
page.wait_for_timeout(1500)

# Take screenshot
page.screenshot(path=payment_png_path, full_page=True)
