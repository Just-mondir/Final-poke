from flask import Flask, render_template, request, jsonify, send_from_directory
import os
import json
import tempfile
import asyncio
import re
from playwright.async_api import async_playwright
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import threading
import time

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Global variable to track task status
task_status = {
    'running': False,
    'progress': 0,
    'total': 0,
    'current_row': 0,
    'message': 'Ready to start',
    'error': None
}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/guide')
def guide():
    return render_template('guide.html')

@app.route('/status')
def status():
    return jsonify(task_status)

@app.route('/stop', methods=['POST'])
def stop_automation():
    global task_status
    
    if not task_status['running']:
        return jsonify({'error': 'No automation is currently running'}), 400
    
    task_status['running'] = False
    task_status['message'] = 'Automation stopped by user'
    
    return jsonify({'message': 'Automation stop signal sent'})

@app.route('/run', methods=['POST'])
def run_script():
    global task_status
    
    if task_status['running']:
        return jsonify({'error': 'Script is already running'}), 400
    
    # Get uploaded file and sheet name
    if 'json_file' not in request.files:
        return jsonify({'error': 'No JSON file uploaded'}), 400
    
    json_file = request.files['json_file']
    sheet_name = request.form.get('sheet_name', '').strip()
    
    if not sheet_name:
        return jsonify({'error': 'Sheet name is required'}), 400
    
    if json_file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    # Save uploaded file temporarily
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as temp_file:
            json_content = json.loads(json_file.read().decode('utf-8'))
            json.dump(json_content, temp_file)
            temp_json_path = temp_file.name
    except Exception as e:
        return jsonify({'error': f'Invalid JSON file: {str(e)}'}), 400
    
    # Start the automation in a separate thread
    thread = threading.Thread(target=run_automation, args=(temp_json_path, sheet_name))
    thread.daemon = True
    thread.start()
    
    return jsonify({'message': 'Script started successfully'})

async def click_grader_grade(page, grader: str, grade: str) -> bool:
    """
    Click the "<grader> population" button matching `grade` exactly.
    Returns True if the button was clicked successfully, False otherwise.
    """
    try:
        print(f"🎯 Selecting: {grader} {grade}")

        # 1. Scroll the population section into view
        popup = page.locator("div[data-testid='card-pops']").first
        await popup.scroll_into_view_if_needed()
        await page.wait_for_timeout(300)

        # 2. Locate the grader header, e.g., "PSA population"
        header = page.get_by_text(f"{grader} population", exact=True)
        if not await header.count():
            print(f"❌ Header '{grader} population' not found")
            return False

        # 3. Go up to its parent, then look for buttons in the section
        wrapper = header.locator("xpath=..")
        buttons = wrapper.locator("button")

        # 4. Loop through buttons and check the FIRST span text only
        button_count = await buttons.count()
        for i in range(button_count):
            btn = buttons.nth(i)
            grade_span = btn.locator("span").first
            text = await grade_span.text_content()
            text = text.strip() if text else ""

            if text == grade:
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=2000)
                print(f"✅ Clicked: {grader} {grade}")
                await page.wait_for_timeout(500)
                return True

        print(f"❌ Exact grade '{grade}' not found under '{grader}'.")

    except Exception as e:
        print(f"❌ Error selecting {grader} {grade}: {e}")

    return False

async def fetch_prices(page, num_sales=4):
    print("💵 Waiting for recent sales to load...")
    prices = []
    blocks = page.locator("div.MuiTypography-body1.css-vxna0y")
    
    block_count = await blocks.count()
    for i in range(block_count):
        try:
            price_span = blocks.nth(i).locator("span[class*='css-16tlq5a']")
            price_text = await price_span.inner_text()
            match = re.search(r"\$([0-9\s,\.]+)", price_text)
            if match:
                price_str = match.group(1).replace(" ", "").replace("\u202f", "").replace(",", "")
                price = float(price_str)
                prices.append(price)
            if len(prices) >= num_sales:
                break
        except Exception as e:
            print(f"⚠️ Skipping sale {i+1}: {e}")
    return prices

async def fetch_avg_price(url, grader, grade, num_sales=4):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        print(f"🌐 Navigating to {url}")
        await page.goto(url)
        await page.wait_for_timeout(3000)

        try:
            first_image = page.locator("img[data-testid^='gallery-image']").first
            await first_image.click()
            await page.wait_for_timeout(4000)
            
        except Exception as e:
            print(f"❌ Failed to click first image: {e}")
            await browser.close()
            return None, []
        
        success = await click_grader_grade(page, grader, grade)
        await page.wait_for_timeout(1000)
        
        if success:
            prices = await fetch_prices(page, num_sales)
            await browser.close()

            if not prices:
                return None, []

            avg = sum(prices) / len(prices)
            return avg, prices
        else:
            await browser.close()
            return None, []

def run_automation(json_path, sheet_name):
    global task_status
    
    try:
        task_status['running'] = True
        task_status['error'] = None
        task_status['message'] = 'Connecting to Google Sheets...'
        
        # Setup Google Sheet
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name(json_path, scope)
        client = gspread.authorize(creds)
        sheet = client.open(sheet_name).sheet1

        start_row = 9
        all_values = sheet.get_all_values()
        num_rows = len(all_values)
        
        task_status['total'] = num_rows - start_row + 1
        task_status['message'] = f'Processing {task_status["total"]} rows...'

        for row in range(start_row, num_rows + 1):
            if not task_status['running']:  # Check stop signal
                task_status['message'] = 'Automation stopped by user'
                print("🛑 Automation stopped by user")
                break
                
            task_status['current_row'] = row
            task_status['progress'] = row - start_row + 1
            
            try:
                url = sheet.cell(row, 6).value
                grader = sheet.cell(row, 7).value
                fake_grade = sheet.cell(row, 8).value
                
                if not url or not grader or not fake_grade:
                    print(f"⚠️ Skipping row {row}: Missing required data")
                    continue
                
                if len(fake_grade) > 3:
                    grade = fake_grade[:2]
                else:
                    grade = fake_grade

                print(f"\n🔁 Processing row {row}: {grader} {grade}")
                task_status['message'] = f'Processing row {row}: {grader} {grade}'
                
                # Run the async function
                avg, prices = asyncio.run(fetch_avg_price(url, grader, grade))

                if prices:
                    for i, price in enumerate(prices[:4]):
                        sheet.update_cell(row, 12 + i, price)
                    sheet.update_cell(row, 16, avg)
                    print(f"✅ Updated row {row} with prices and average.")
                else:
                    print(f"❌ No prices found for row {row}. Skipping update.")
                    
            except Exception as e:
                print(f"❌ Error processing row {row}: {e}")
                continue
        
        task_status['message'] = 'Automation completed successfully!'
        
    except Exception as e:
        task_status['error'] = str(e)
        task_status['message'] = f'Error: {str(e)}'
        print(f"❌ Automation error: {e}")
    
    finally:
        task_status['running'] = False
        # Clean up temp file
        try:
            os.unlink(json_path)
        except:
            pass

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))